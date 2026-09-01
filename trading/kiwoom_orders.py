"""키움 주문 전용 클라이언트. 조회 전용 모듈과 의도적으로 분리한다."""

from __future__ import annotations

from typing import Any

import requests

from trading.kiwoom_readonly import KiwoomConfig, KiwoomError


US_ORDER_PATH = "/api/us/ordr"
US_BUY_API_ID = "ust20000"
US_SELL_API_ID = "ust20001"


class KiwoomOrderClient:
    def __init__(self, config: KiwoomConfig, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()

    def sell_us_limit(
        self, token: str, *, exchange: str, ticker: str, quantity: int, price: float
    ) -> dict[str, Any]:
        exchange = exchange.strip().upper()
        ticker = ticker.strip().upper()
        if exchange not in {"NA", "ND", "NY"}:
            raise KiwoomError("미국 거래소 코드는 NA, ND, NY 중 하나여야 합니다.")
        if not ticker or quantity < 1 or price <= 0:
            raise KiwoomError("종목, 1주 이상의 수량, 0보다 큰 지정가가 필요합니다.")
        response = self.session.post(
            self.config.base_url + US_ORDER_PATH,
            headers={
                "Content-Type": "application/json;charset=UTF-8",
                "authorization": f"Bearer {token}",
                "api-id": US_SELL_API_ID,
            },
            json={
                "stex_tp": exchange,
                "stk_cd": ticker,
                "ord_qty": str(quantity),
                # Kiwoom US orders expect cent-denominated prices with two
                # fractional digits, including a trailing zero (e.g. 91.40).
                "ord_uv": format(price, ".2f"),
                "trde_tp": "00",
            },
            timeout=self.config.timeout_seconds,
        )
        try:
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise KiwoomError("미국주식 매도 주문 응답을 확인하지 못했습니다.") from exc
        if not isinstance(data, dict) or data.get("return_code") not in (None, 0, "0"):
            message = data.get("return_msg") if isinstance(data, dict) else None
            raise KiwoomError(f"미국주식 매도 주문 실패: {message or '응답 오류'}")
        return {"order_number": str(data.get("ord_no") or ""), "ticker": ticker}

    def buy_us_limit(
        self, token: str, *, exchange: str, ticker: str, quantity: int, price: float
    ) -> dict[str, Any]:
        exchange = exchange.strip().upper()
        ticker = ticker.strip().upper()
        if exchange not in {"NA", "ND", "NY"}:
            raise KiwoomError("미국 거래소 코드는 NA, ND, NY 중 하나여야 합니다.")
        if not ticker or quantity < 1 or price <= 0:
            raise KiwoomError("종목, 1주 이상의 수량, 0보다 큰 지정가가 필요합니다.")
        response = self.session.post(
            self.config.base_url + US_ORDER_PATH,
            headers={
                "Content-Type": "application/json;charset=UTF-8",
                "authorization": f"Bearer {token}",
                "api-id": US_BUY_API_ID,
            },
            json={
                "stex_tp": exchange,
                "stk_cd": ticker,
                "ord_qty": str(quantity),
                "ord_uv": format(price, ".2f"),
                "trde_tp": "00",
            },
            timeout=self.config.timeout_seconds,
        )
        try:
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise KiwoomError("미국주식 매수 주문 응답을 확인하지 못했습니다.") from exc
        if not isinstance(data, dict) or data.get("return_code") not in (None, 0, "0"):
            message = data.get("return_msg") if isinstance(data, dict) else None
            raise KiwoomError(f"미국주식 매수 주문 실패: {message or '응답 오류'}")
        return {"order_number": str(data.get("ord_no") or ""), "ticker": ticker}
