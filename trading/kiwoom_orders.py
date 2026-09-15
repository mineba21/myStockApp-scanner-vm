"""키움 주문 전용 클라이언트. 조회 전용 모듈과 의도적으로 분리한다."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
import requests
from trading.kiwoom_readonly import KiwoomConfig, KiwoomError

US_ORDER_PATH = "/api/us/ordr"
US_BUY_API_ID = "ust20000"
US_SELL_API_ID = "ust20001"
US_CANCEL_API_ID = "ust20003"
KR_ORDER_PATH = "/api/dostk/ordr"
KR_BUY_API_ID = "kt10000"
KR_SELL_API_ID = "kt10001"
KR_CANCEL_API_ID = "kt10003"


class OrderUnknown(KiwoomError):
    """Request may have reached the broker. Reconcile before any retry."""


class OrderRejected(KiwoomError):
    """Explicit broker rejection, distinct from transport/response loss."""


class KiwoomOrderClient:
    def __init__(self, config: KiwoomConfig, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()

    def sell_us_limit(self, token: str, *, exchange: str, ticker: str,
                      quantity: int, price: float) -> dict[str, Any]:
        return self._limit(token, US_SELL_API_ID, exchange, ticker, quantity, price)

    def buy_us_limit(self, token: str, *, exchange: str, ticker: str,
                     quantity: int, price: float) -> dict[str, Any]:
        return self._limit(token, US_BUY_API_ID, exchange, ticker, quantity, price)

    def cancel_us_order(self, token: str, *, exchange: str, ticker: str,
                        original_order_number: str) -> dict[str, Any]:
        exchange, ticker = self._symbol(exchange, ticker)
        original = str(original_order_number).strip()
        if (not original.isascii() or not original.isdigit()
                or int(original) == 0 or len(original) > 9):
            raise KiwoomError("유효한 미국주식 원주문번호가 필요합니다.")
        data = self._post(
            token,
            US_CANCEL_API_ID,
            {"orig_ord_no": original, "stex_tp": exchange, "stk_cd": ticker},
            action="취소",
            path=US_ORDER_PATH,
            market="미국주식",
        )
        return {
            "order_number": self._accepted_order_number(data, action="취소"),
            "original_order_number": original,
            "ticker": ticker,
            "status": "submitted",
        }

    def buy_kr_limit(self, token: str, *, exchange: str, ticker: str,
                     quantity: int, price: int, trade_type: str = "0") -> dict[str, Any]:
        return self._kr_limit(
            token, KR_BUY_API_ID, exchange, ticker, quantity, price, trade_type,
        )

    def sell_kr_limit(self, token: str, *, exchange: str, ticker: str,
                      quantity: int, price: int) -> dict[str, Any]:
        return self._kr_limit(token, KR_SELL_API_ID, exchange, ticker, quantity, price)

    def cancel_kr_order(self, token: str, *, exchange: str, ticker: str,
                        original_order_number: str, quantity: int = 0) -> dict[str, Any]:
        exchange, ticker = self._kr_symbol(exchange, ticker)
        original = str(original_order_number).strip()
        if (not original.isascii() or not original.isdigit()
                or int(original) == 0 or len(original) > 7):
            raise KiwoomError("유효한 국내주식 원주문번호가 필요합니다.")
        if type(quantity) is not int or quantity < 0:
            raise KiwoomError("취소수량은 0 이상의 정수여야 합니다. 0은 잔량 전부 취소입니다.")
        data = self._post(
            token, KR_CANCEL_API_ID,
            {"dmst_stex_tp": exchange, "orig_ord_no": original,
             "stk_cd": ticker, "cncl_qty": str(quantity)},
            action="취소", path=KR_ORDER_PATH,
            market="국내주식",
        )
        return {"order_number": self._accepted_order_number(data, action="취소", max_digits=7),
                "original_order_number": original, "ticker": ticker, "status": "submitted"}

    def _limit(self, token, api_id, exchange, ticker, quantity, price):
        exchange, ticker = self._symbol(exchange, ticker)
        try:
            value = Decimal(str(price))
            valid_price = value.is_finite() and value > 0 and value == value.quantize(Decimal(".01"))
        except InvalidOperation:
            valid_price = False
        if type(quantity) is not int or quantity < 1 or not valid_price:
            raise KiwoomError("종목, 정수 수량, 센트 단위의 유효한 양수 지정가가 필요합니다.")
        data = self._post(
            token,
            api_id,
            {"stex_tp": exchange, "stk_cd": ticker, "ord_qty": str(quantity),
             "ord_uv": format(value, ".2f"), "trde_tp": "00"},
            action="주문",
            path=US_ORDER_PATH,
            market="미국주식",
        )
        return {"order_number": self._accepted_order_number(data, action="주문"),
                "ticker": ticker, "status": "submitted"}

    def _kr_limit(self, token, api_id, exchange, ticker, quantity, price,
                  trade_type="0"):
        exchange, ticker = self._kr_symbol(exchange, ticker)
        if (type(quantity) is not int or quantity < 1 or type(price) is not int
                or price < 1):
            raise KiwoomError("국내주식은 1주 이상의 정수 수량과 1원 이상의 정수 지정가가 필요합니다.")
        if trade_type not in {"0", "62"}:
            raise KiwoomError("국내 지정가는 보통(0) 또는 시간외단일가(62)만 허용합니다.")
        data = self._post(
            token, api_id,
            {"dmst_stex_tp": exchange, "stk_cd": ticker, "ord_qty": str(quantity),
             "ord_uv": str(price), "trde_tp": trade_type, "cond_uv": ""},
            action="주문", path=KR_ORDER_PATH,
            market="국내주식",
        )
        return {"order_number": self._accepted_order_number(data, action="주문", max_digits=7),
                "ticker": ticker, "status": "submitted"}

    @staticmethod
    def _symbol(exchange, ticker):
        exchange, ticker = str(exchange).strip().upper(), str(ticker).strip().upper()
        if exchange not in {"NA", "ND", "NY"}:
            raise KiwoomError("미국 거래소 코드는 NA, ND, NY 중 하나여야 합니다.")
        if not ticker or len(ticker) > 12 or not ticker.isascii() or not ticker.replace(".", "").isalnum():
            raise KiwoomError("유효한 미국주식 종목코드가 필요합니다.")
        return exchange, ticker

    @staticmethod
    def _kr_symbol(exchange, ticker):
        exchange, ticker = str(exchange).strip().upper(), str(ticker).strip()
        if exchange not in {"KRX", "NXT", "SOR"}:
            raise KiwoomError("국내 거래소 코드는 KRX, NXT, SOR 중 하나여야 합니다.")
        if len(ticker) != 6 or not ticker.isascii() or not ticker.isdigit():
            raise KiwoomError("국내주식 종목코드는 숫자 6자리여야 합니다.")
        return exchange, ticker

    def _post(self, token, api_id, payload, *, action, path, market):
        try:
            response = self.session.post(
                self.config.base_url + path,
                headers={"Content-Type": "application/json;charset=UTF-8",
                         "authorization": f"Bearer {token}", "api-id": api_id},
                json=payload,
                timeout=self.config.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise OrderUnknown(f"접수 여부 불명: {action}내역 대조 전 재전송 금지") from exc
        if not isinstance(data, dict) or type(data.get("return_code")) not in (int, str):
            raise OrderUnknown("접수 여부 불명: 성공 코드 누락/응답 오류")
        code = str(data["return_code"]).strip()
        if not code or not code.lstrip("-").isascii() or not code.lstrip("-").isdigit():
            raise OrderUnknown("접수 여부 불명: 성공 코드 형식 오류")
        if int(code) != 0:
            raise OrderRejected(f"{market} {action} 거절: {data.get('return_msg') or '증권사 거절'}")
        return data

    @staticmethod
    def _accepted_order_number(data, *, action, max_digits=9):
        raw = data.get("ord_no")
        order_no = str(raw).strip() if type(raw) in (str, int) else ""
        if (not order_no.isascii() or not order_no.isdigit()
                or int(order_no) == 0 or len(order_no) > max_digits):
            raise OrderUnknown(f"접수 여부 불명: 유효한 {action} 주문번호 누락")
        return order_no
