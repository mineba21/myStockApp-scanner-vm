"""등록·일시중지·해제·LIVE 전환·증거 기반 UNKNOWN 해제·헬스. 웹 API 와 테스트가 공유한다.

여기서는 주문을 전송하지 않는다. 증권사 조회는 reader (LiveStopSellBroker 조회 전용 또는 대역)로만 한다.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from trading.auto_stop import settings as S
from trading.auto_stop.quotes import MIN_AUTO_PRICE, QuoteUnavailable, validate
from trading.auto_stop.store import UNRESOLVED, ClaimRefused
from trading.auto_stop.us_calendar import NEW_YORK, market_clock, ny_date, session_for


class Refused(Exception):
    """사용자 요청을 안전 조건 때문에 거부함 (메시지는 화면에 그대로 보여도 됨)."""


def _cents(value) -> Decimal:
    if value is None:
        raise Refused("손절가가 없습니다.")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise Refused("가격을 해석할 수 없습니다.") from exc
    if not d.is_finite() or d <= 0:
        raise Refused("손절가가 없거나 0 이하입니다.")
    return d.quantize(Decimal("0.01"))


def parse_holding_id(holding_id: str) -> str:
    parts = str(holding_id or "").split(":")
    if len(parts) != 4 or parts[0] != "kiwoom" or parts[1] != S.ACCOUNT_PROFILE or parts[2] != "US":
        raise Refused("자동 손절은 자유투자(account1) 미국 보유종목만 등록할 수 있습니다.")
    ticker = parts[3].strip().upper()
    if not ticker or not ticker.replace(".", "").replace("-", "").isalnum():
        raise Refused("보유종목 티커가 올바르지 않습니다.")
    return ticker


def _snapshot(scan: dict) -> dict:
    keys = ("id", "market", "ticker", "name", "signal_type", "signal_date", "scan_time",
            "first_detected_at", "price", "pivot_price", "stop_loss", "grade", "strict_filter_passed")
    return {k: (scan.get(k).isoformat() if hasattr(scan.get(k), "isoformat") else scan.get(k)) for k in keys}


def _stale_days(scan: dict, now: float) -> int | None:
    try:
        d = date.fromisoformat(str(scan.get("signal_date")))
    except ValueError:
        return None
    return (datetime.fromtimestamp(now, NEW_YORK).date() - d).days


def _quote_for_registration(reader, ticker, settings, now, exchange=None):
    try:
        q = reader.quote(ticker, exchange) if exchange else reader.probe_exchange(ticker)
    except QuoteUnavailable as exc:
        raise Refused(f"최신 시세를 확인하지 못해 등록하지 않습니다: {exc}") from exc
    mc = market_clock(now, settings)
    if mc.is_open:
        reason = validate(q, now, mc, settings)
        if reason:
            raise Refused(f"시세 검증 실패: {reason}")
    elif q.as_of is None or now - q.as_of > 4 * 86400:
        raise Refused("장외 시간이며 최근 시세 기준시각을 확인할 수 없습니다.")
    return q, mc


def register(store, reader, *, scan: dict, holding_id: str, managed_qty: int, mode: str,
             confirmation_ticker: str, acknowledge_stale: bool = False, now: float | None = None,
             settings=None, actor="web") -> dict:
    settings = settings or S.Settings.from_env()
    now = time.time() if now is None else now
    mode = str(mode).upper()
    if mode not in ("SHADOW", "LIVE"):
        raise Refused("등록 모드는 SHADOW 또는 LIVE 여야 합니다.")
    if settings.mode != mode:
        raise Refused(f"현재 운영 모드({settings.mode})와 다른 {mode} 등록은 받지 않습니다.")
    ticker = parse_holding_id(holding_id)
    if str(confirmation_ticker or "").strip().upper() != ticker:
        raise Refused("확인 티커가 보유종목과 다릅니다.")
    if str(scan.get("market") or "").upper() != "US" or str(scan.get("ticker") or "").upper() != ticker:
        raise Refused("스캔 결과의 시장·티커가 보유종목과 다릅니다.")
    if scan.get("strict_filter_passed") is not True:
        raise Refused("Strict 필터 통과가 확인되지 않은 신호(거부 또는 미평가)는 등록할 수 없습니다.")
    stop = _cents(scan.get("stop_loss"))
    days = _stale_days(scan, now)
    if days is None:
        raise Refused("신호 날짜를 확인할 수 없습니다.")
    if days > settings.stale_signal_days and not acknowledge_stale:
        raise Refused(f"신호가 {days}일 전입니다 (기준 {settings.stale_signal_days}일). "
                      "오래된 신호임을 확인하는 체크가 필요합니다.")
    if not isinstance(managed_qty, int) or managed_qty < 1:
        raise Refused("관리 수량은 1주 이상 정수여야 합니다.")
    if mode == "LIVE":
        block = S.live_order_block_reason(settings)
        if block:
            raise Refused(f"LIVE 등록 불가: {block}")
    try:
        holdings = reader.holdings()
    except Exception as exc:
        raise Refused("잔고를 완전하게 확인하지 못해 등록하지 않습니다.") from exc
    held = holdings.get(ticker)
    if not held or held["qty"] < 1:
        raise Refused("현재 보유하지 않은 종목입니다.")
    if managed_qty > held["qty"]:
        raise Refused(f"관리 수량({managed_qty})이 보유 수량({held['qty']})보다 많습니다.")
    q, _ = _quote_for_registration(reader, ticker, settings, now)
    if q.price <= stop:
        raise Refused(f"현재가 {q.price}가 이미 손절가 {stop} 이하입니다. 자동 등록하지 않습니다 (수동 판단 필요).")
    if q.price < MIN_AUTO_PRICE:
        raise Refused("$1 미만 종목은 호가 단위가 검증되지 않아 등록할 수 없습니다.")
    if managed_qty > settings.max_order_qty or q.price * managed_qty > settings.max_order_notional_usd:
        raise Refused("관리 수량이 1회 주문 상한(수량·금액)을 넘습니다.")
    if mode == "LIVE" and managed_qty > settings.live_qty_cap():
        raise Refused("UST20001_SELL 1주 실매도 검증 전에는 LIVE 관리 수량을 1주로 제한합니다.")
    if mode == "LIVE":
        _single_live_position_guard(store, settings)
    try:
        pid = store.insert_position(
            now, mode=mode, ticker=ticker, exchange=q.exchange, source="MANUAL_LINK", source_ref=holding_id,
            scan_result_id=int(scan["id"]), signal_snapshot=_snapshot(scan), stop_price=stop, qty=managed_qty,
            broker_qty=held["qty"], avg_cost=str(held["avg_cost"]) if held.get("avg_cost") else None,
            quote_price=str(q.price), quote_at=q.as_of, max_active=settings.max_active_positions,
            details={"stale_days": days, "acknowledged_stale": bool(acknowledge_stale)})
    except ClaimRefused as exc:
        raise Refused(str(exc)) from exc
    store.audit(now, actor, "register", pid, {"ticker": ticker, "mode": mode, "qty": managed_qty,
                                              "stop": str(stop), "scan_result_id": scan["id"]})
    store.enqueue_alert(now, f"position:{pid}:registered", "INFO",
                        f"[자동손절 등록] {ticker} {managed_qty}주 · 손절가 {stop} · 현재가 {q.price} ({mode})")
    return store.position(pid)


def _single_live_position_guard(store, settings) -> None:
    """UST20001_SELL 실매도 검증 전에는 LIVE 검증 대상 포지션을 1개로 제한한다 (주문당 1주와 별개)."""
    if settings.verified("UST20001_SELL"):
        return
    if store.positions(mode="LIVE", states=("ACTIVE", "EXITING", "NEEDS_REVIEW")):
        raise Refused("UST20001_SELL 1주 실매도 검증 전에는 LIVE 포지션을 하나만 둘 수 있습니다. "
                      "기존 LIVE 포지션을 정리한 뒤 진행하세요.")


def _has_unresolved(store, pid) -> bool:
    return any(store.orders(event_id=e["id"], states=UNRESOLVED)
               for e in store.events() if e["position_id"] == pid)


def set_paused(store, pid, paused: bool, *, now=None, actor="web") -> dict:
    now = time.time() if now is None else now
    pos = store.position(pid)
    if not pos:
        raise Refused("등록된 자동 손절이 없습니다.")
    if pos["state"] not in ("ACTIVE", "EXITING", "NEEDS_REVIEW"):
        raise Refused("종료된 자동 손절입니다.")
    store.update_position(pid, now, auto_paused=1 if paused else 0)
    store.audit(now, actor, "pause" if paused else "resume", pid, {})
    store.enqueue_alert(now, f"position:{pid}:{'pause' if paused else 'resume'}:{int(now)}", "WARN",
                        f"[자동손절] {pos['ticker']} 자동 매도 {'일시중지' if paused else '재개'} ({pos['mode']}). "
                        + ("이미 접수된 주문은 취소하지 않습니다 — 필요하면 키움 앱에서 취소하세요." if paused else ""))
    return store.position(pid)


def disable(store, pid, *, now=None, actor="web") -> dict:
    now = time.time() if now is None else now
    pos = store.position(pid)
    if not pos:
        raise Refused("등록된 자동 손절이 없습니다.")
    if pos["state"] not in ("ACTIVE", "EXITING", "NEEDS_REVIEW"):
        raise Refused("이미 종료된 자동 손절입니다.")
    if _has_unresolved(store, pid):
        raise Refused("미확정 주문이 있어 해제할 수 없습니다. 일시중지 후 주문이 종료되면 해제하세요.")
    if not store.update_position(pid, now, expect_states=("ACTIVE", "EXITING", "NEEDS_REVIEW"),
                                 state="DISABLED", auto_paused=1):
        raise Refused("상태가 바뀌어 해제하지 못했습니다. 새로고침 후 다시 시도하세요.")
    for e in store.events():
        if e["position_id"] == pid and e["state"] in ("OPEN", "CARRY_OVER", "NEEDS_REVIEW"):
            store.update_event(e["id"], now, state="DONE", review_reason="사용자 해제")
    store.audit(now, actor, "disable", pid, {})
    return store.position(pid)


def promote(store, reader, pid, *, confirmation_ticker, now=None, settings=None, actor="web") -> dict:
    """SHADOW 포지션 → 새 LIVE 포지션. Shadow 주문·체결·이벤트는 복사하지 않는다."""
    settings = settings or S.Settings.from_env()
    now = time.time() if now is None else now
    src = store.position(pid)
    if not src or src["mode"] != "SHADOW":
        raise Refused("SHADOW 포지션만 LIVE로 전환할 수 있습니다.")
    if src["state"] != "ACTIVE":
        raise Refused("손절이 진행 중이거나 종료된 SHADOW 포지션은 전환할 수 없습니다.")
    if str(confirmation_ticker or "").strip().upper() != src["ticker"]:
        raise Refused("확인 티커가 다릅니다.")
    block = S.live_order_block_reason(settings)
    if block:
        raise Refused(f"LIVE 전환 불가: {block}")
    try:
        held = reader.holdings().get(src["ticker"])
    except Exception as exc:
        raise Refused("잔고를 완전하게 확인하지 못했습니다.") from exc
    qty = src["initial_managed_qty"]
    if qty > settings.live_qty_cap():
        raise Refused("UST20001_SELL 1주 실매도 검증 전에는 1주 포지션만 LIVE로 전환할 수 있습니다.")
    _single_live_position_guard(store, settings)
    if not held or held["qty"] < qty:
        raise Refused(f"실제 보유 수량({held['qty'] if held else 0})이 관리 수량({qty})보다 적습니다.")
    q, _ = _quote_for_registration(reader, src["ticker"], settings, now, exchange=src["exchange"])
    stop = Decimal(src["stop_price"])
    below = q.price <= stop
    try:
        new_id = store.insert_position(
            now, mode="LIVE", ticker=src["ticker"], exchange=src["exchange"], source="PROMOTED",
            source_ref=f"shadow:{pid}", scan_result_id=src["scan_result_id"],
            signal_snapshot=json.loads(src["signal_snapshot"]), stop_price=stop, qty=qty,
            broker_qty=held["qty"], avg_cost=str(held["avg_cost"]) if held.get("avg_cost") else None,
            quote_price=str(q.price), quote_at=q.as_of, max_active=settings.max_active_positions,
            state="NEEDS_REVIEW" if below else "ACTIVE",
            review_reason=f"LIVE 전환 시점 현재가 {q.price} ≤ 손절가 {stop}" if below else None)
    except ClaimRefused as exc:
        raise Refused(str(exc)) from exc
    store.update_position(pid, now, expect_states=("ACTIVE",), state="DISABLED",
                          review_reason=f"LIVE 포지션 {new_id}로 전환")
    store.audit(now, actor, "promote", pid, {"live_position_id": new_id, "below_stop": below})
    store.enqueue_alert(now, f"position:{new_id}:promoted", "URGENT" if below else "INFO",
                        f"[자동손절] {src['ticker']} SHADOW→LIVE 전환 ({qty}주, 손절가 {stop}, 현재가 {q.price})"
                        + (" — 이미 손절가 이하라 자동 매도하지 않고 확인 필요 상태로 등록" if below else ""))
    return store.position(new_id)


def resolve_unknown(store, broker, order_id, *, order_number: str | None, confirmation_ticker: str,
                    now=None, actor="web") -> dict:
    """UNKNOWN/CANCEL_UNKNOWN 을 증권사 주문내역 증거로만 해제한다. 해제 후 이벤트는 NEEDS_REVIEW."""
    now = time.time() if now is None else now
    o = store.order(order_id)
    if not o or o["state"] not in ("UNKNOWN", "CANCEL_UNKNOWN"):
        raise Refused("UNKNOWN 상태 주문만 해제할 수 있습니다.")
    if str(confirmation_ticker or "").strip().upper() != o["ticker"]:
        raise Refused("확인 티커가 주문과 다릅니다.")
    number = str(order_number or o["order_number"] or "").strip()
    if not number.isdigit():
        raise Refused("증권사 주문번호가 필요합니다.")
    if o["order_number"] and number != o["order_number"]:
        raise Refused("기록된 주문번호와 다릅니다.")
    try:
        row = broker.status({**o, "order_number": number})
    except Exception as exc:
        raise Refused("증권사 주문내역을 조회하지 못했습니다. 상태는 바뀌지 않았습니다.") from exc
    if row is None:
        raise Refused("증권사 주문내역에서 종목·방향·수량·가격·시각이 모두 일치하는 주문을 찾지 못했습니다.")
    if store.apply_fill(order_id, now, int(row["filled_quantity"]), {"source": "manual_resolve", "row": row}) \
            == "ANOMALY":
        raise Refused("체결 수량이 기록과 맞지 않습니다. 해제하지 않습니다.")
    target = {"FILLED": "FILLED", "CANCELLED": "CANCELLED", "REJECTED": "REJECTED",
              "OPEN": "SUBMITTED", "PARTIAL": "PARTIAL_FILLED"}.get(row["state"])
    if target is None:
        raise Refused(f"증권사 주문 상태({row['state']})를 해석할 수 없어 해제하지 않습니다.")
    fields = {"broker_state": row["state"]}
    if not o["order_number"]:
        fields.update(order_number=number,
                      order_date=datetime.fromtimestamp(o["sent_at"] or o["created_at"], NEW_YORK).date().isoformat())
    try:
        ok = store.transition(order_id, now, frm=("UNKNOWN", "CANCEL_UNKNOWN"), to=target, **fields)
    except Exception as exc:
        raise Refused("주문번호가 다른 주문 기록과 겹칩니다.") from exc
    if not ok:
        raise Refused("상태가 바뀌어 해제하지 못했습니다.")
    store.review(now, event_id=o["exit_event_id"], reason=f"UNKNOWN 수동 해제 ({row['state']}) — 사람 확인 후 해제/재등록")
    store.audit(now, actor, "resolve_unknown", order_id, {"order_number": number, "broker_state": row["state"]})
    return store.order(order_id)


def health(store, settings=None, now=None) -> dict:
    settings = settings or S.Settings.from_env()
    now = time.time() if now is None else now
    unresolved = store.orders(states=UNRESOLVED)
    live_positions = store.positions(states=("ACTIVE", "EXITING", "NEEDS_REVIEW"))
    events = store.events(states=("OPEN", "CARRY_OVER", "NEEDS_REVIEW"))
    mc = market_clock(now, settings)
    heartbeat = store.runtime_get("heartbeat_at")
    kill = store.kill_switch()
    return {
        "mode": settings.mode,
        "kill_switch": kill,
        "shadow_fill_model": settings.shadow_fill_model,
        "verified_contracts": sorted(settings.verified_contracts),
        "missing_live_contracts": [c for c in S.LIVE_REQUIRED if not settings.verified(c)],
        "missing_replace_contracts": [c for c in S.REPLACE_REQUIRED if not settings.verified(c)],
        "live_qty_cap": settings.live_qty_cap(),
        "live_cancel_block_reason": S.live_cancel_block_reason(settings),
        "replace_enabled": settings.replace_enabled,
        "shadow_replace_enabled": settings.shadow_replace_enabled,
        "live_order_block_reason": S.live_order_block_reason(settings),
        "market_open": mc.is_open,
        "before_cutoff": mc.before_cutoff,
        "heartbeat_at": heartbeat,
        "heartbeat_age_sec": None if heartbeat is None else round(now - heartbeat, 1),
        "last_tick_ok_at": store.runtime_get("last_tick_ok_at"),
        "last_quote_ok_at": store.runtime_get("last_quote_ok_at"),
        "last_quote_issue": store.runtime_get("last_quote_issue"),
        "consecutive_errors": store.runtime_get("consecutive_api_errors", 0),
        "last_api_error": store.runtime_get("last_api_error"),
        "api_errors": store.runtime_get("api_errors", {}) or {},
        "global_hold": store.runtime_get("global_hold_state") or {"reasons": [], "since": None},
        "quote_global_hold": store.runtime_get("quote_global_hold"),
        "quote_incident_unresolved": [
            {"position_id": a.get("position_id"), "ticker": a.get("ticker"), "causes": a.get("causes"),
             "category": a.get("category"), "evidence": len(a.get("evidence_ticks") or []),
             "last_status": a.get("last_status")}
            for a in ((store.runtime_get("quote_global_hold") or {}).get("affected") or {}).values()
            if not a.get("resolved")],
        "quote_health": _quote_health_view(store, settings.mode),
        "recent_quote_errors": (store.runtime_get("quote_errors_recent", []) or [])[-10:],
        "unresolved_orders": len(unresolved),
        "unknown_orders": sum(o["state"] in ("UNKNOWN", "CANCEL_UNKNOWN") for o in unresolved),
        "needs_review": sum(p["state"] == "NEEDS_REVIEW" for p in live_positions)
                        + sum(e["state"] == "NEEDS_REVIEW" for e in events),
        "open_managed_qty": {m: sum(p["remaining_managed_qty"] for p in live_positions if p["mode"] == m)
                             for m in ("SHADOW", "LIVE")},
        "events": [{"id": e["id"], "position_id": e["position_id"], "state": e["state"], "mode": e["mode"],
                    "session_replace_count": e["session_replace_count"],
                    "event_replace_count": e["event_replace_count"],
                    "carry_over_count": e["carry_over_count"]} for e in events],
    }


def _quote_health_view(store, mode) -> list:
    if mode not in ("SHADOW", "LIVE"):
        return []
    health = store.runtime_get(f"quote_health:{mode}", {}) or {}
    keys = ("position_id", "ticker", "state", "consecutive_errors", "last_ok_at", "last_ok_as_of", "next_due_at",
            "fault_started_at", "fault_reason", "recovered_at", "confirm_as_of", "last_error", "first_error",
            "last_observation", "rate_limit_streak")
    return [{k: rec.get(k) for k in keys} for _, rec in sorted(health.items(), key=lambda kv: int(kv[0]))]


KILL_ON_PHRASE, KILL_OFF_PHRASE = "STOP", "RESUME"


def set_kill_switch(store, on: bool, *, confirmation: str, reason: str, actor: str, now=None) -> dict:
    """전체 긴급 중지. 신규 매도·자동 취소·재주문만 막고 기존 주문 대조·잔고 동기화는 계속한다."""
    now = time.time() if now is None else now
    expected = KILL_ON_PHRASE if on else KILL_OFF_PHRASE
    if str(confirmation or "").strip().upper() != expected:
        raise Refused(f"확인 문구 '{expected}'를 입력해야 합니다.")
    reason = str(reason or "").strip()[:200]
    if on and not reason:
        raise Refused("중지 사유를 입력하세요.")
    value = store.set_kill_switch(now, on, actor=actor, reason=reason)
    store.enqueue_alert(now, f"kill:{'on' if on else 'off'}:{int(now)}", "URGENT",
                        f"[자동손절] 전체 긴급 중지 {'설정' if on else '해제'} — {reason or '사유 없음'} "
                        + ("(이미 접수된 주문은 취소하지 않음. 필요하면 키움 앱에서 취소)" if on else ""))
    return value


SIM_NOTE = "가상(SHADOW) 체결은 실제 체결률·실현손익이 아닙니다."


def daily_report(store, day: str, mode: str, settings=None, now=None) -> dict:
    """미국 정규장 하루의 감시·주문·알림 운영 보고. 손절이 없어도 감시 정상 여부를 보여 준다."""
    settings = settings or S.Settings.from_env()
    now = time.time() if now is None else now
    d = date.fromisoformat(day)
    session = session_for(d, extra_closed=settings.extra_closed_dates, extra_early=settings.extra_early_close_dates)
    base = {"day": day, "mode": mode, "trading_day": session is not None, "note": SIM_NOTE if mode == "SHADOW" else ""}
    if session is None:
        return {**base, "watch": {"missed": 0}, "api": {"errors": 0}, "unresolved_events": 0, "unresolved_orders": 0}
    start, end = session.open_ts, min(session.close_ts, max(now, session.open_ts))
    stats = store.stats(day, mode)

    def stat(metric, key=None):
        return sum(r["value"] for r in stats if r["metric"] == metric and (key is None or r["key"] == key))

    def maxstat(metric, key=None):
        vals = [r["value"] for r in stats if r["metric"] == metric and (key is None or r["key"] == key)]
        return round(max(vals), 1) if vals else None

    events = [e for e in store.events(mode=mode) if ny_date(e["triggered_at"]) == d]
    positions = []
    total_expected = total_ok = 0
    health = store.runtime_get(f"quote_health:{mode}", {}) or {}
    day_start = datetime.combine(d, datetime.min.time(), tzinfo=NEW_YORK).timestamp()
    fault_audits = store.audits("quote_fault", day_start, day_start + 86400) + \
        store.audits("quote_recovered", day_start, day_start + 86400)
    for p in store.positions(mode=mode):
        if p["created_at"] >= end:
            continue
        p_events = [e for e in store.events(mode=mode) if e["position_id"] == p["id"]]
        stop_at = min([e["triggered_at"] for e in p_events] + [end])
        if p["state"] not in ("ACTIVE", "EXITING", "NEEDS_REVIEW") and not p_events:
            stop_at = min(stop_at, p["updated_at"])
        window = max(0.0, stop_at - max(start, p["created_at"]))
        if window <= 0 and not p_events:
            continue
        expected = int(window // max(1, settings.price_interval_sec))
        t = p["ticker"]
        ok = int(stat("quote_ok", t))
        held = int(stat("slot_held", t))
        paused = int(stat("slot_paused", t))
        failed_slots = int(stat("slot_failed", t))
        # 기대 슬롯은 기존 기준(장중 등록~트리거 구간 ÷ 감시 주기, 일시중지 구간 포함)을 유지한다.
        # 누락 = 기대 - 정상. 누락을 보류 → 일시중지 → 감시 실패 → 스케줄 지연 순으로 나눠 합이 항상 누락과 같게 한다
        # (보고 시점이 슬롯 중간이면 원시 슬롯 수가 기대보다 1 클 수 있어 각 항목을 남은 누락으로 자른다).
        missed = max(0, expected - ok)
        held_m = min(held, missed)
        paused_m = min(paused, missed - held_m)
        failed_m = min(failed_slots, missed - held_m - paused_m)
        schedule_m = missed - held_m - paused_m - failed_m
        total_expected += expected
        total_ok += ok
        rec = health.get(str(p["id"])) or {}
        target = f"{mode}:{t}"
        faults = [{"at": a["at"], "kind": "fault" if a["action"] == "quote_fault" else "recovered",
                   "category": a["details"].get("category")}
                  for a in sorted(fault_audits, key=lambda a: a["at"])
                  if a["target"] == target and a["details"].get("position_id") in (None, p["id"])]
        positions.append({
            "id": p["id"], "ticker": t, "state": p["state"], "expected_checks": expected,
            # 슬롯 기준 (감시 슬롯 = 감시 주기 1칸, 성공은 슬롯당 1회만 센다)
            "ok_checks": ok, "missed_checks": missed, "held_slots": held_m, "paused_slots": paused_m,
            "failed_slots": failed_m, "schedule_missed": schedule_m,
            "slot_counts_raw": {"ok": ok, "held": held, "paused": paused, "failed": failed_slots},
            "deferred": int(stat("slot_deferred", t)),
            "normal_rate_pct": round(ok / expected * 100, 1) if expected else None,
            # 시도 기준 (일반 감시 1회 = 증권사 시세 조회 1회 + 검증. HTTP 요청 수와 다를 수 있다)
            "watch_attempts": int(stat("watch_attempt", t)),
            "quote_fail": int(stat("quote_fail", t)), "quote_invalid": int(stat("quote_invalid", t)),
            "recovery": {"attempts": int(stat("recovery_attempt", t)), "ok": int(stat("recovery_ok", t)),
                         "fail": int(stat("recovery_fail", t)), "invalid": int(stat("recovery_invalid", t)),
                         "stale_repeat": int(stat("recovery_stale", t))},
            "order_quote": {"ok": int(stat("order_quote_ok", t)), "fail": int(stat("order_quote_fail", t)),
                            "invalid": int(stat("order_quote_invalid", t)),
                            "held": int(stat("order_held_symbol", t))},
            "held_triggers": int(stat("held_trigger", t)),
            "max_quote_age_sec": maxstat("max_quote_age_sec", t),
            "last_quote_at": p["last_quote_at"], "last_check_at": p["last_price_check_at"],
            "health_state": rec.get("state"), "fault_started_at": rec.get("fault_started_at"),
            "recovered_at": rec.get("recovered_at"), "faults": faults,
            "first_error": rec.get("first_error") if (rec.get("first_error") or {}).get("day") == day else None})
    orders = [o for o in store.orders(mode=mode) if ny_date(o["created_at"]) == d]
    fills = []
    for o in orders:
        if o["filled_qty"] <= 0:
            continue
        ev = store.event(o["exit_event_id"])
        stop = Decimal(ev["stop_price"])
        price = Decimal(o["avg_fill_price"] or o["limit_price"])
        fills.append({"order_id": o["id"], "ticker": o["ticker"], "filled_qty": o["filled_qty"],
                      "fill_price_basis": "avg_fill_price" if o["avg_fill_price"] else "limit_price",
                      "fill_price": str(price), "stop_price": str(stop),
                      "vs_stop_pct": float(round((price - stop) / stop * 100, 2))})
    shadow = _shadow_breakdown(store, orders, fills, d) if mode == "SHADOW" else {}
    if shadow:
        shadow["current"] = {"fill_model": settings.shadow_fill_model,
                             "shadow_replace_enabled": settings.shadow_replace_enabled}
    alerts = [a for a in store.recent_alerts(2000) if ny_date(a["created_at"]) == d]
    expected_ticks = int((end - start) // max(1, settings.tick_sec))
    max_gap = maxstat("max_tick_gap_sec")
    holds = _hold_periods(store, start, end)
    sum_pos = lambda key: sum(int(p[key]) for p in positions)       # noqa: E731
    recovery_tot = {k: sum(int(p["recovery"][k]) for p in positions)
                    for k in ("attempts", "ok", "fail", "invalid", "stale_repeat")}
    heartbeat = store.runtime_get("heartbeat_at")
    unresolved_orders = store.orders(mode=mode, states=UNRESOLVED)
    return {
        **base,
        "session": {"open_at": start, "close_at": session.close_ts, "early_close": session.early_close},
        "watched_positions": len(positions),
        "watch": {"expected": total_expected, "ok": total_ok, "missed": sum_pos("missed_checks"),
                  "held_slots": sum_pos("held_slots"), "paused_slots": sum_pos("paused_slots"),
                  "failed_slots": sum_pos("failed_slots"),
                  "schedule_missed": sum_pos("schedule_missed"), "attempts": sum_pos("watch_attempts"),
                  "api_errors": sum_pos("quote_fail"), "invalid": sum_pos("quote_invalid"),
                  "normal_rate_pct": round(total_ok / total_expected * 100, 1) if total_expected else None,
                  "positions": positions},
        # 일반 감시와 따로 센다. 회복 확정에 쓴 시세는 정상 감시 성공(watch.ok)에 넣지 않는다.
        "recovery": recovery_tot,
        "holds": {**holds, "held_triggers": int(stat("held_trigger"))},
        # Scheduler heartbeat (tick) 은 종목별 시세 감시와 별개로 평가한다.
        "ticks": {"expected": expected_ticks, "actual": int(stat("ticks")), "max_gap_sec": max_gap},
        "api": {"errors": int(stat("api_error")), "by_category": {r["key"]: int(r["value"]) for r in stats
                                                                   if r["metric"] == "api_error"},
                "quote_errors": {r["key"]: int(r["value"]) for r in stats if r["metric"] == "quote_error"},
                "http_429": int(stat("http_429")), "http_requests": int(stat("http_requests")),
                "http_requests_by_api": {r["key"]: int(r["value"]) for r in stats if r["metric"] == "http_requests"},
                "other_recovery": {k: {"attempts": int(stat("recovery_attempt", k)),
                                       "ok": int(stat("recovery_ok", k)), "fail": int(stat("recovery_fail", k))}
                                   for k in ("balance", "orders")}},
        "triggers": len(events),
        "orders": {"total": len(orders), "by_kind": {k: sum(o["kind"] == k for o in orders)
                                                     for k in ("ORIGINAL", "REPLACE", "CARRY_OVER")},
                   "filled": sum(o["state"] == "FILLED" for o in orders),
                   "partial_or_open": sum(o["state"] in UNRESOLVED for o in orders),
                   "cancelled": sum(o["state"] == "CANCELLED" for o in orders)},
        "fills_vs_stop": fills,
        "shadow": shadow,
        "unresolved_events": len(store.events(mode=mode, states=("OPEN", "CARRY_OVER", "NEEDS_REVIEW"))),
        "unresolved_orders": len(unresolved_orders),
        "alerts": {"sent": sum(a["state"] == "SENT" for a in alerts),
                   "failed": sum(a["state"] == "FAILED" for a in alerts),
                   "pending": sum(a["state"] == "PENDING" for a in alerts)},
        "heartbeat": {"last_at": heartbeat, "max_tick_gap_sec": max_gap,
                      "ok": bool(heartbeat) and (max_gap is None or max_gap <= 3 * settings.tick_sec)},
    }


def _hold_periods(store, start, end) -> dict:
    """전체 보류 구간 (global_hold 감사 기록). 정규장 [start, end] 로 자른다."""
    rows = sorted(store.audits("global_hold", 0, end + 1), key=lambda a: a["at"])
    periods, cur, reasons = [], None, []
    for a in rows:
        to = a["details"].get("to") or []
        if to and cur is None:
            cur, reasons = a["at"], to
        elif to and cur is not None:
            reasons = sorted(set(reasons) | set(to))
        elif not to and cur is not None:
            periods.append((cur, a["at"], reasons))
            cur, reasons = None, []
    if cur is not None:
        periods.append((cur, None, reasons))
    out, total = [], 0.0
    for a, b, why in periods:
        lo, hi = max(a, start), min(b if b is not None else end, end)
        if hi <= lo:
            continue
        total += hi - lo
        out.append({"from": lo, "to": b, "reasons": why})
    return {"global_seconds": round(total, 1), "periods": out}


def _order_shadow_config(o) -> dict:
    try:
        tags = json.loads(o.get("details") or "{}").get("shadow") or []
        return tags[0]
    except (ValueError, TypeError, AttributeError, IndexError):
        return {}


def _shadow_breakdown(store, orders, fills, d) -> dict:
    """SHADOW 결과를 가상 체결 모델별로 분리한다. 서로 다른 모델 결과를 합쳐 평가하지 않는다."""
    fill_by_order = {f["order_id"]: f for f in fills}
    by_model = {}
    for o in orders:
        cfg = _order_shadow_config(o)
        model = cfg.get("fill_model") or "UNTAGGED"
        m = by_model.setdefault(model, {"orders": 0, "by_kind": {"ORIGINAL": 0, "REPLACE": 0, "CARRY_OVER": 0},
                                        "filled": 0, "cancelled": 0, "partial_or_open": 0,
                                        "shadow_replace_enabled": set(), "fills_vs_stop": []})
        m["orders"] += 1
        m["by_kind"][o["kind"]] += 1
        m["filled"] += o["state"] == "FILLED"
        m["cancelled"] += o["state"] == "CANCELLED"
        m["partial_or_open"] += o["state"] in UNRESOLVED
        m["shadow_replace_enabled"].add(bool(cfg.get("shadow_replace_enabled")))
        if o["id"] in fill_by_order:
            m["fills_vs_stop"].append(fill_by_order[o["id"]])
    for m in by_model.values():
        m["shadow_replace_enabled"] = sorted(m["shadow_replace_enabled"])
    start = datetime.combine(d, datetime.min.time(), tzinfo=NEW_YORK).timestamp()
    changes = [{"at": a["at"], "from": a["details"].get("from"), "to": a["details"].get("to")}
               for a in store.audits("shadow_config", start, start + 86400)]
    return {"by_model": by_model, "config_changes": changes, "mixed_models": len(by_model) > 1,
            "evaluation_note": "가상 체결 모델별로 따로 평가한다. NEXT_QUOTE와 STRESS 결과를 합쳐 성과로 보지 않는다."}


def report_text(rep: dict) -> str:
    if not rep["trading_day"]:
        return f"[자동손절 일일 보고 {rep['day']} {rep['mode']}] 휴장일"
    w, o = rep["watch"], rep["orders"]
    r, hd = rep.get("recovery") or {}, rep.get("holds") or {}
    per = " · ".join(f"{p['ticker']} {p['ok_checks']}/{p['expected_checks']}"
                     + (f" 장애 {p['health_state']}" if p.get("health_state") not in (None, "OK") else "")
                     for p in w.get("positions", []))
    return (f"[자동손절 일일 보고 {rep['day']} {rep['mode']}] 감시 {rep['watched_positions']}종목 · "
            f"정상 감시 {w['ok']}/{w['expected']} (누락 {w['missed']} = 보류 미실행 {w.get('held_slots', 0)} + 일시중지 "
            f"{w.get('paused_slots', 0)} + 감시 실패 {w.get('failed_slots', 0)} + 스케줄 지연 "
            f"{w.get('schedule_missed', 0)}) · 일반 감시 시도 {w.get('attempts', 0)} "
            f"(API 오류 {w.get('api_errors', 0)}, 무효 {w.get('invalid', 0)}) · 회복 조회 {r.get('ok', 0)}/"
            f"{r.get('attempts', 0)} (실패 {r.get('fail', 0)}, 무효 {r.get('invalid', 0)}, 동일시세 "
            f"{r.get('stale_repeat', 0)}) · 전체 보류 {int(hd.get('global_seconds', 0))}초"
            + (f" · 종목별 {per}" if per else "")
            + f" · tick {rep['ticks']['actual']}/"
            f"{rep['ticks']['expected']} (최대 간격 {rep['ticks']['max_gap_sec']}초) · API 오류 합계 {rep['api']['errors']} "
            f"(HTTP 요청 {rep['api'].get('http_requests', 0)}, 429 {rep['api']['http_429']}) · 손절 {rep['triggers']} · "
            f"주문 {o['total']} (재주문 {o['by_kind']['REPLACE']}, "
            f"체결 {o['filled']}, 미체결 {o['partial_or_open']}) · 미해결 이벤트 {rep['unresolved_events']} · "
            f"알림 실패 {rep['alerts']['failed']}" + _shadow_text(rep.get("shadow"))
            + (f" · {rep['note']}" if rep["note"] else ""))


def _shadow_text(sh) -> str:
    if not sh:
        return ""
    cur = sh.get("current") or {}
    parts = [f" · 모델 {cur.get('fill_model')} (SHADOW 재주문 {'켜짐' if cur.get('shadow_replace_enabled') else '꺼짐'})"]
    for model, m in sh.get("by_model", {}).items():
        parts.append(f" · [{model}] 주문 {m['orders']} 재주문 {m['by_kind']['REPLACE']} 체결 {m['filled']} "
                     f"취소 {m['cancelled']}")
    if sh.get("config_changes"):
        parts.append(f" · 설정 변경 {len(sh['config_changes'])}회")
    if sh.get("mixed_models"):
        parts.append(" · 모델 혼재: 모델별로 따로 평가")
    return "".join(parts)
