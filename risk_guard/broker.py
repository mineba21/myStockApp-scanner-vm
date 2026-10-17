"""브로커 어댑터 인터페이스.

실제 브로커(Alpaca, 한국투자증권 KIS 등) 호출은 이 인터페이스를 구현한 어댑터에만 둔다.
에이전트 코드가 브로커 SDK를 직접 import하지 않게 하는 것이 가드의 전제다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import OrderIntent


@dataclass(frozen=True)
class BrokerResult:
    accepted: bool
    broker_order_id: str | None
    message: str


class BrokerAdapter(Protocol):
    name: str

    def place_order(self, intent: OrderIntent, qty: int, client_order_id: str) -> BrokerResult:
        """주문을 전송한다. client_order_id는 가드의 decision_id로, 중복 전송 방지에 쓴다."""
        ...


class DryRunBroker:
    """아무것도 전송하지 않는 기본 어댑터."""

    name = "dry-run"

    def place_order(self, intent: OrderIntent, qty: int, client_order_id: str) -> BrokerResult:
        return BrokerResult(True, f"dry-{client_order_id}", "실제 주문 없음 (dry-run)")
