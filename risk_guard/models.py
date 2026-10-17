"""주문 의도, 계좌 스냅샷, 판정 결과 데이터 모델."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class Verdict(str, Enum):
    APPROVE = "APPROVE"  # 요청 수량 그대로 허용
    RESIZE = "RESIZE"    # 한도에 맞춰 수량을 줄여서 허용
    REJECT = "REJECT"    # 거부


@dataclass(frozen=True)
class OrderIntent:
    """에이전트(또는 스캐너)가 내고 싶은 주문. 가드를 통과해야만 브로커로 간다."""

    symbol: str
    side: Side
    qty: int
    order_type: OrderType
    reference_price: float          # 판단 시점에 알고 있는 최신 가격
    limit_price: float | None = None
    stop_price: float | None = None  # 매수 시 보호용 손절가 (R 계산 기준)
    market: str = "US"
    source: str = "unknown"          # 예: "scanner", "llm-agent", "manual"
    signal_id: str | None = None     # 스캐너 신호 ID 등 추적용
    note: str = ""

    def __post_init__(self) -> None:
        # LLM 출력 등 문자열로 들어와도 Enum으로 정규화. 잘못된 값이면 ValueError.
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(self, "order_type", OrderType(self.order_type))
        object.__setattr__(self, "symbol", str(self.symbol).strip().upper())
        object.__setattr__(self, "market", str(self.market).strip().upper())

    @property
    def entry_price(self) -> float:
        return self.limit_price if self.limit_price is not None else self.reference_price

    def with_qty(self, qty: int) -> "OrderIntent":
        return replace(self, qty=qty)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["side"] = self.side.value
        d["order_type"] = self.order_type.value
        return d


@dataclass(frozen=True)
class AccountSnapshot:
    """에이전트 전용 계좌(또는 서브계좌)의 스냅샷.

    positions에는 에이전트가 관리하는 종목만 넣는다. 개인 보유 종목까지 섞으면
    보유 종목 수 한도와 매도 수량 검사가 의도와 다르게 동작한다.
    """

    equity: float
    cash: float
    positions: dict[str, int] = field(default_factory=dict)
    currency: str = "USD"

    def held_qty(self, symbol: str) -> int:
        return int(self.positions.get(symbol.upper(), 0))

    @property
    def open_symbols(self) -> set[str]:
        return {s.upper() for s, q in self.positions.items() if q > 0}


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    message: str
    max_qty: int | None = None  # 수량 상한을 내는 검사만 채운다


@dataclass(frozen=True)
class Decision:
    decision_id: str
    verdict: Verdict
    intent: OrderIntent
    approved_qty: int
    checks: tuple[CheckResult, ...]
    created_at: datetime
    requires_human_approval: bool

    @property
    def failed_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]

    @property
    def executable(self) -> bool:
        return self.verdict is not Verdict.REJECT and self.approved_qty > 0

    def summary(self) -> str:
        """텔레그램 등 알림용 한 줄 요약."""
        i = self.intent
        head = f"[{self.verdict.value}] {i.side.value} {i.symbol}"
        if self.verdict is Verdict.REJECT:
            reasons = "; ".join(f"{c.name}: {c.message}" for c in self.failed_checks)
            return f"{head} 요청 {i.qty}주 거부 — {reasons}"
        qty = f"{self.approved_qty}주" if self.verdict is Verdict.APPROVE else f"{i.qty}→{self.approved_qty}주"
        tail = " (승인 필요)" if self.requires_human_approval else ""
        return f"{head} {qty} @ {i.entry_price:g}{tail} id={self.decision_id}"

    def to_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "verdict": self.verdict.value,
            "approved_qty": self.approved_qty,
            "requires_human_approval": self.requires_human_approval,
            "created_at": self.created_at.isoformat(),
            "intent": self.intent.to_dict(),
            "checks": [asdict(c) for c in self.checks],
        }


@dataclass(frozen=True)
class ExecutionResult:
    decision_id: str
    status: str            # SUBMITTED | DRY_RUN | REFUSED | ERROR
    mode: str              # live | dry-run | none
    qty: int
    message: str
    broker_order_id: str | None = None

    @property
    def placed(self) -> bool:
        return self.status in ("SUBMITTED", "DRY_RUN")
