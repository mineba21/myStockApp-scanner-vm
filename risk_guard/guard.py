"""RiskGuard: 모든 주문이 거치는 단일 관문.

흐름
  1) evaluate(intent, account)  → Decision (APPROVE / RESIZE / REJECT). 주문은 나가지 않는다.
  2) execute(decision, account, approved_by=...) → 승인·유효시간·재검사를 통과하면 브로커로 전송.

원칙
  - 기본은 dry-run. 실주문은 설정 live_trading=true 와 환경변수 RISK_GUARD_LIVE=1 이
    둘 다 있어야 켜진다 (키 두 개).
  - 검사 중 예외가 나면 실패로 처리한다 (fail closed).
  - 모든 판정과 실행은 감사 로그에 남는다.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from .broker import BrokerAdapter, DryRunBroker
from .checks import BUY_CHECKS, COMMON_CHECKS, SELL_CHECKS, Check, CheckContext
from .config import GuardConfig
from .infra import AuditLog, KillSwitch, Ledger, require_aware, trading_day
from .models import (AccountSnapshot, CheckResult, Decision, ExecutionResult, OrderIntent,
                     Side, Verdict)

LIVE_ENV_VAR = "RISK_GUARD_LIVE"


class RiskGuard:
    def __init__(self, config: GuardConfig, broker: BrokerAdapter | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.config = config
        self.broker = broker
        self._dry = DryRunBroker()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.kill_switch = KillSwitch(config.kill_switch_path)
        self.ledger = Ledger(config.ledger_path)
        self.audit = AuditLog(config.audit_log_path)

    # ------------------------------------------------------------------ 상태
    @property
    def live_enabled(self) -> bool:
        return (self.config.live_trading
                and os.environ.get(LIVE_ENV_VAR) == "1"
                and self.broker is not None)

    def _now(self, now: datetime | None) -> datetime:
        return require_aware(now or self._clock())

    def _checks_for(self, intent: OrderIntent) -> tuple[Check, ...]:
        side_checks = BUY_CHECKS if intent.side is Side.BUY else SELL_CHECKS
        return COMMON_CHECKS + side_checks

    # ------------------------------------------------------------------ 판정
    def evaluate(self, intent: OrderIntent, account: AccountSnapshot,
                 now: datetime | None = None, *, _audit_event: str = "evaluate") -> Decision:
        decision = self.preview(intent, account, now)
        self.audit.write(_audit_event, decision.to_dict(), decision.created_at)
        return decision

    def preview(self, intent: OrderIntent, account: AccountSnapshot,
                now: datetime | None = None) -> Decision:
        """evaluate와 같은 검사를 하되 감사 로그·원장에 아무것도 쓰지 않는다 (표시 전용)."""
        now = self._now(now)
        day = trading_day(now, self.config.market_timezone)
        ctx = CheckContext(
            intent=intent, account=account, ledger=self.ledger.view(day), config=self.config,
            now=now, kill_switch_active=self.kill_switch.is_active(),
            kill_reason=self.kill_switch.reason(),
        )
        results: list[CheckResult] = []
        for check in self._checks_for(intent):
            try:
                results.append(check(ctx))
            except Exception as e:  # fail closed
                results.append(CheckResult(check.__name__.removeprefix("check_"), False,
                                           f"검사 중 오류: {type(e).__name__}: {e}"))

        failed = [r for r in results if not r.passed]
        caps = [r.max_qty for r in results if r.max_qty is not None]
        max_qty = min(caps) if caps else intent.qty

        if failed:
            verdict, approved = Verdict.REJECT, 0
        elif intent.qty <= max_qty:
            verdict, approved = Verdict.APPROVE, intent.qty
        elif self.config.allow_resize:
            verdict, approved = Verdict.RESIZE, max_qty
        else:
            results.append(CheckResult("resize", False,
                                       f"요청 {intent.qty}주가 한도 {max_qty}주 초과 (축소 허용 안 함)"))
            verdict, approved = Verdict.REJECT, 0

        decision = Decision(
            decision_id=f"rg-{uuid.uuid4().hex[:12]}",
            verdict=verdict,
            intent=intent,
            approved_qty=approved,
            checks=tuple(results),
            created_at=now,
            requires_human_approval=self.config.require_human_approval,
        )
        return decision

    # ------------------------------------------------------------------ 실행
    def execute(self, decision: Decision, account: AccountSnapshot,
                approved_by: str | None = None, now: datetime | None = None) -> ExecutionResult:
        now = self._now(now)
        mode = "live" if self.live_enabled else "dry-run"

        def refuse(msg: str) -> ExecutionResult:
            res = ExecutionResult(decision.decision_id, "REFUSED", "none", 0, msg)
            self.audit.write("execute_refused", {"decision_id": decision.decision_id,
                                                 "message": msg}, now)
            return res

        if not decision.executable:
            return refuse("거부된 판정은 실행할 수 없음")
        if now - decision.created_at > timedelta(minutes=self.config.decision_ttl_minutes):
            return refuse(f"판정 유효시간 {self.config.decision_ttl_minutes}분 초과 — 다시 evaluate 필요")
        if self.config.require_human_approval and not approved_by:
            return refuse("사람 승인이 필요함 (approved_by 없음)")
        day = trading_day(now, self.config.market_timezone)
        if decision.decision_id in self.ledger.view(day).executed_ids():
            return refuse("이미 실행된 판정")

        # 판정 이후 상황(킬 스위치, 다른 주문, 시간)이 바뀌었을 수 있으니 승인 수량으로 재검사
        intent = decision.intent.with_qty(decision.approved_qty)
        recheck = self.evaluate(intent, account, now, _audit_event="recheck")
        if recheck.verdict is not Verdict.APPROVE:
            reasons = "; ".join(f"{c.name}: {c.message}" for c in recheck.failed_checks) \
                or f"수량 변경 필요 ({recheck.approved_qty}주)"
            return refuse(f"재검사 실패 — {reasons}")

        broker = self.broker if mode == "live" else self._dry
        new_position = intent.side is Side.BUY and intent.symbol not in account.open_symbols
        try:
            br = broker.place_order(intent, intent.qty, decision.decision_id)
        except Exception as e:
            # 전송 여부를 알 수 없으므로 원장에는 기록해 중복 재전송을 막고, 사람이 확인하게 한다
            self.ledger.record(symbol=intent.symbol, side=intent.side.value, qty=intent.qty,
                               price=intent.entry_price, new_position=new_position,
                               decision_id=decision.decision_id, now=now, day=day)
            msg = f"브로커 오류 — 체결 여부 수동 확인 필요: {type(e).__name__}: {e}"
            self.audit.write("execute_error", {"decision_id": decision.decision_id, "mode": mode,
                                               "message": msg}, now)
            return ExecutionResult(decision.decision_id, "ERROR", mode, intent.qty, msg)

        if not br.accepted:
            msg = f"브로커 거부: {br.message}"
            self.audit.write("execute_rejected_by_broker", {"decision_id": decision.decision_id,
                                                            "mode": mode, "message": msg}, now)
            return ExecutionResult(decision.decision_id, "REFUSED", mode, 0, msg, br.broker_order_id)

        self.ledger.record(symbol=intent.symbol, side=intent.side.value, qty=intent.qty,
                           price=intent.entry_price, new_position=new_position,
                           decision_id=decision.decision_id, now=now, day=day)
        status = "SUBMITTED" if mode == "live" else "DRY_RUN"
        result = ExecutionResult(decision.decision_id, status, mode, intent.qty, br.message,
                                 br.broker_order_id)
        self.audit.write("execute", {"decision_id": decision.decision_id, "status": status,
                                     "mode": mode, "qty": intent.qty, "approved_by": approved_by,
                                     "broker": broker.name, "broker_order_id": br.broker_order_id,
                                     "intent": intent.to_dict()}, now)
        return result
