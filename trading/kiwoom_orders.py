"""키움 주문 전용 클라이언트. 조회 전용 모듈과 의도적으로 분리한다."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
import requests
from trading.kiwoom_readonly import KiwoomConfig, KiwoomError

US_ORDER_PATH = "/api/us/ordr"
US_BUY_API_ID = "ust20000"
US_SELL_API_ID = "ust20001"


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

    def _limit(self, token, api_id, exchange, ticker, quantity, price):
        exchange, ticker = exchange.strip().upper(), ticker.strip().upper()
        if exchange not in {"NA", "ND", "NY"}:
            raise KiwoomError("미국 거래소 코드는 NA, ND, NY 중 하나여야 합니다.")
        try:
            value = Decimal(str(price))
            valid_price = value.is_finite() and value > 0 and value == value.quantize(Decimal(".01"))
        except InvalidOperation:
            valid_price = False
        if not ticker or type(quantity) is not int or quantity < 1 or not valid_price:
            raise KiwoomError("종목, 정수 수량, 센트 단위의 유효한 양수 지정가가 필요합니다.")
        try:
            response = self.session.post(
                self.config.base_url + US_ORDER_PATH,
                headers={"Content-Type": "application/json;charset=UTF-8",
                         "authorization": f"Bearer {token}", "api-id": api_id},
                json={"stex_tp": exchange, "stk_cd": ticker, "ord_qty": str(quantity),
                      "ord_uv": format(value, ".2f"), "trde_tp": "00"},
                timeout=self.config.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise OrderUnknown("접수 여부 불명: 주문내역 대조 전 재전송 금지") from exc
        if not isinstance(data, dict) or type(data.get("return_code")) not in (int, str):
            raise OrderUnknown("접수 여부 불명: 성공 코드 누락/응답 오류")
        code = str(data["return_code"]).strip()
        if not code or not code.lstrip("-").isascii() or not code.lstrip("-").isdigit():
            raise OrderUnknown("접수 여부 불명: 성공 코드 형식 오류")
        if int(code) != 0:
            raise OrderRejected(f"미국주식 주문 거절: {data.get('return_msg') or '증권사 거절'}")
        raw = data.get("ord_no")
        order_no = str(raw).strip() if type(raw) in (str, int) else ""
        if not order_no.isascii() or not order_no.isdigit() or int(order_no) == 0:
            raise OrderUnknown("접수 여부 불명: 유효한 주문번호 누락")
        return {"order_number": order_no, "ticker": ticker, "status": "submitted"}
