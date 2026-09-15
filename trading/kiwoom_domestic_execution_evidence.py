"""Strict domestic order, open-order and KRW cash evidence for mock tests."""
from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation

from trading.kiwoom_readonly import KiwoomError

DOMESTIC_ACCOUNT_PATH = "/api/dostk/acnt"


def amount(value):
    if isinstance(value, bool) or value is None:
        raise KiwoomError("국내 금액/수량 필드가 없습니다.")
    try:
        result = Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation as exc:
        raise KiwoomError("국내 금액/수량 형식 오류") from exc
    if not result.is_finite() or result < 0:
        raise KiwoomError("국내 금액/수량은 0 이상이어야 합니다.")
    return result


def quantity(value):
    result = amount(value)
    if result != result.to_integral_value():
        raise KiwoomError("국내주식 수량은 정수여야 합니다.")
    return int(result)


def order_number(value):
    result = str(value or "").strip()
    if not result.isascii() or not result.isdigit() or int(result) == 0 or len(result) > 7:
        raise KiwoomError("유효한 국내 주문번호가 없습니다.")
    return result.zfill(7)


def ticker(value):
    result = str(value or "").strip().upper()
    if len(result) == 7 and result[0] in {"A", "J", "Q"}:
        result = result[1:]
    if len(result) != 6 or not result.isascii() or not result.isdigit():
        raise KiwoomError("유효한 국내 종목코드가 없습니다.")
    return result


class DomesticExecutionEvidence:
    def __init__(self, client, token, *, account_profile="account4", max_pages=100,
                 rate_limit_delays=(2, 4, 8, 16), sleeper=time.sleep):
        if not account_profile or not 1 <= max_pages <= 100:
            raise KiwoomError("계좌 프로필과 유효한 페이지 제한이 필요합니다.")
        self.client, self.token = client, token
        self.account_profile, self.max_pages = account_profile, max_pages
        self.rate_limit_delays = tuple(rate_limit_delays)
        self.sleeper = sleeper

    def _post_with_rate_limit_retry(self, headers, payload):
        """조회 요청만 429에서 제한적으로 재시도한다.

        주문 전송에는 이 경로를 사용하지 않으므로 재시도로 중복 주문이 생기지 않는다.
        """
        response = None
        for attempt in range(len(self.rate_limit_delays) + 1):
            response = self.client.session.post(
                self.client.config.base_url + DOMESTIC_ACCOUNT_PATH,
                headers=dict(headers), json=payload,
                timeout=self.client.config.timeout_seconds,
            )
            if getattr(response, "status_code", None) != 429:
                return response
            if attempt < len(self.rate_limit_delays):
                self.sleeper(self.rate_limit_delays[attempt])
        return response

    def _request(self, api_id, payload, *, list_key=None):
        if api_id not in {"ka10075", "ka10076", "kt00001", "kt00007"}:
            raise KiwoomError("허용되지 않은 국내 주문 검증 API")
        headers = {"Content-Type": "application/json;charset=UTF-8",
                   "authorization": "Bearer " + self.token, "api-id": api_id}
        rows, seen = [], set()
        for page in range(self.max_pages):
            response = self._post_with_rate_limit_retry(headers, payload)
            data = self.client._parse_response(response, "국내 주문 검증 조회")
            code = data.get("return_code")
            if type(code) not in (int, str) or str(code).strip() != "0":
                raise KiwoomError("국내 조회에 명시적인 성공 코드가 없습니다.")
            if list_key is None:
                if response.headers.get("cont-yn") not in (None, "", "N"):
                    raise KiwoomError("국내 단일 조회 응답이 완전하지 않습니다.")
                return data
            items = data.get(list_key)
            if not isinstance(items, list) or any(not isinstance(row, dict) for row in items):
                raise KiwoomError("국내 주문 검증 목록을 확인할 수 없습니다.")
            rows.extend(items)
            continuation = response.headers.get("cont-yn", "")
            if continuation in ("", "N", None):
                return rows
            key = response.headers.get("next-key")
            if continuation != "Y" or not key or key in seen:
                raise KiwoomError("국내 연속조회 응답이 불완전하거나 반복됩니다.")
            seen.add(key)
            headers.update({"cont-yn": "Y", "next-key": key})
            if page + 1 < self.max_pages:
                time.sleep(.2)
        raise KiwoomError("국내 주문 검증 전체 페이지 조회가 끝나지 않았습니다.")

    def open_orders(self):
        rows = self._request("ka10075", {
            "all_stk_tp": "0", "trde_tp": "0", "stk_cd": "", "stex_tp": "0",
        }, list_key="oso")
        return [self._normalize_open(row) for row in rows]

    def history(self):
        rows = self._request("kt00007", {
            "ord_dt": "", "qry_tp": "1", "stk_bond_tp": "1", "sell_tp": "0",
            "stk_cd": "", "fr_ord_no": "", "dmst_stex_tp": "%",
        }, list_key="acnt_ord_cntr_prps_dtl")
        return [self._normalize_history(row) for row in rows]

    def snapshot(self):
        started = time.time()
        deposit = self._request("kt00001", {"qry_tp": "2"})
        cash = amount(deposit.get("ord_alow_amt"))
        opens = self.open_orders()
        return {"account": self.account_profile, "currency": "KRW",
                "broker_orderable_cash": str(cash), "cash_source": "kt00001.ord_alow_amt",
                "open_order_count": len(opens), "open_orders": opens,
                "observed_at": started, "validated_for_execution": False}

    def lookup_open(self, intent):
        number = order_number(intent.get("buy_order_number") or intent.get("order_number"))
        matches = [row for row in self.open_orders() if row["order_number"] == number]
        if len(matches) != 1:
            return None
        row = matches[0]
        if (row["ticker"] != intent["ticker"] or row["side"] != intent.get("side", "BUY")
                or row["quantity"] != intent["quantity"] or row["price"] != str(intent["price"])):
            return None
        return row

    def lookup_cancel(self, intent):
        original = order_number(intent.get("buy_order_number") or intent.get("order_number"))
        cancel = order_number(intent.get("cancel_order_number"))
        if any(row["order_number"] == original for row in self.open_orders()):
            return None
        matches = [row for row in self.history() if row["order_number"] == cancel]
        if len(matches) != 1:
            return None
        row = matches[0]
        if row["original_order_number"] != original or "취소" not in row["modify_cancel"]:
            return None
        return {**row, "state": "CANCELLED", "remaining_quantity": 0}

    def lookup_filled(self, intent):
        number = order_number(intent.get("order_number"))
        matches = [row for row in self.history() if row["order_number"] == number]
        if len(matches) != 1:
            return None
        row = matches[0]
        if (row["ticker"] != intent["ticker"] or row["side"] != intent["side"]
                or row["quantity"] != intent["quantity"]
                or row["filled_quantity"] != intent["quantity"]
                or row["remaining_quantity"] != 0):
            return None
        return {**row, "state": "FILLED"}

    @staticmethod
    def _side(value):
        value = str(value or "")
        if "매수" in value:
            return "BUY"
        if "매도" in value:
            return "SELL"
        raise KiwoomError("국내 주문 방향을 확인할 수 없습니다.")

    @classmethod
    def _normalize_open(cls, row):
        qty, filled, remaining = [quantity(row.get(key)) for key in
                                  ("ord_qty", "cntr_qty", "oso_qty")]
        if qty < 1 or filled + remaining != qty or remaining < 1:
            raise KiwoomError("국내 미체결 수량이 일관되지 않습니다.")
        return {"order_number": order_number(row.get("ord_no")),
                "ticker": ticker(row.get("stk_cd")), "side": cls._side(row.get("io_tp_nm")),
                "quantity": qty, "filled_quantity": filled, "remaining_quantity": remaining,
                "price": str(amount(row.get("ord_pric"))), "state": "PARTIAL" if filled else "OPEN"}

    @classmethod
    def _normalize_history(cls, row):
        original = str(row.get("ori_ord") or "").strip()
        original = "0000000" if original in {"", "0", "0000000"} else order_number(original)
        return {"order_number": order_number(row.get("ord_no")),
                "original_order_number": original, "ticker": ticker(row.get("stk_cd")),
                "side": cls._side(row.get("io_tp_nm")),
                "quantity": quantity(row.get("ord_qty")),
                "filled_quantity": quantity(row.get("cntr_qty")),
                "remaining_quantity": quantity(row.get("ord_remnq")),
                "price": str(amount(row.get("ord_uv"))),
                "acceptance": str(row.get("acpt_tp") or "").strip(),
                "modify_cancel": str(row.get("mdfy_cncl") or "").strip()}
