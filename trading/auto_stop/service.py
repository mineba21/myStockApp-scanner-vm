"""등록·일시중지·해제·LIVE 전환·증거 기반 UNKNOWN 해제·헬스. 웹 API 와 테스트가 공유한다.

여기서는 주문을 전송하지 않는다. 증권사 조회는 reader (LiveStopSellBroker 조회 전용 또는 대역)로만 한다.
"""
from __future__ import annotations

import json
import math
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from trading.auto_stop import quote_health as QH
from trading.auto_stop import settings as S
from trading.auto_stop.quotes import MIN_AUTO_PRICE, QuoteUnavailable, validate
from trading.auto_stop.store import UNRESOLVED, watch_end_of, watch_ended_by_of, ClaimRefused
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
    record_watch_end(store, pos["mode"], pos, now)          # 관리 종료: 감시 종료 시점 상태 근거
    if not store.update_position(pid, now, expect_states=("ACTIVE", "EXITING", "NEEDS_REVIEW"),
                                 state="DISABLED", auto_paused=1):
        raise Refused("상태가 바뀌어 해제하지 못했습니다. 새로고침 후 다시 시도하세요.")
    for e in store.events():
        if e["position_id"] == pid and e["state"] in ("OPEN", "CARRY_OVER", "NEEDS_REVIEW"):
            store.update_event(e["id"], now, state="DONE", review_reason="사용자 해제")
    # 이전 상태·마지막 수정 시각을 남긴다: ACTIVE 에서 바로 해제한 경우만 이 시각이 최초 감시 종료다.
    store.audit(now, actor, "disable", pid, {"prev_state": pos["state"], "prev_updated_at": pos["updated_at"]})
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
    record_watch_end(store, src["mode"], store.position(pid), now)
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
    ev = store.event(o["exit_event_id"])
    pos = store.position(ev["position_id"]) if ev else None
    store.audit(now, actor, "resolve_unknown", order_id, {
        "order_number": number, "broker_state": row["state"], "event_state": ev["state"] if ev else None,
        "remaining": pos["remaining_managed_qty"] if pos else None,
        # 취소 확정은 매도 완료가 아니다: 잔여가 남으면 대체 주문이 필요한 상태임을 기록한다 (자동 재주문은 하지 않음).
        "replacement_needed": bool(pos and row["state"] == "CANCELLED" and pos["remaining_managed_qty"] > 0)})
    reconcile_wait_records(store, now, actor=actor)     # 대기 기록을 확정된 실제 상태와 맞춘다
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
    health = store.runtime_get(f"quote_health:{mode}", {}) or {}
    day_start = datetime.combine(d, datetime.min.time(), tzinfo=NEW_YORK).timestamp()
    day_end = datetime.combine(d + timedelta(days=1), datetime.min.time(), tzinfo=NEW_YORK).timestamp()   # 뉴욕 달력일
    fault_audits = store.audits("quote_fault", day_start, day_start + 86400) + \
        store.audits("quote_recovered", day_start, day_start + 86400)
    pause_audits = store.audits("pause", day_start, day_start + 86400) + \
        store.audits("resume", day_start, day_start + 86400)
    hold_periods = _hold_periods(store, start, end)["periods"]
    interval = int(settings.price_interval_sec)

    def pstat(metric, pid):
        return int(stat("pos_" + metric, str(pid)))

    # ── 보고서 데이터 형식과 집계 단위 ─────────────────────────────────
    # 집계 단위 두 가지를 구분한다.
    #   (A) 거래일+모드+티커 일계 quote_ok: 구버전은 일반 감시 성공마다, 신버전은 일반 감시 슬롯의 첫 성공마다 1회 올린다
    #       (engine._watch). 따라서 같은 정의(일반 감시 성공 1회 = 감시 슬롯 1개)로 신·구 성공이 함께 들어 있다.
    #   (B) 거래일+모드+position_id 슬롯 기록: 신버전만 남긴다.
    # 티커 일계 − 그 티커 신버전 포지션의 성공 슬롯 수(진행 중 슬롯 포함) = 구버전 몫. 구버전 몫은 구버전 기간이 있는
    # 포지션에만 한 번 귀속한다. 그런 포지션이 여럿이면 배분하지 않고 그룹 합계로 보인다. 음수면 집계 불일치.
    v2_since = next((r["value"] for r in stats if r["metric"] == "slot_v2_since"), None)
    tol = settings.tick_sec + QH.SLOT_TOLERANCE_SEC
    finalized = any(r["metric"] == "state_audit_done" for r in stats)
    close_evidence = {r["key"]: r["value"] for r in stats if r["metric"] == "state_audit"}
    live_day = ny_date(now) == d and not finalized          # 마감 처리 전 당일: 현재 상태가 그날 상태다
    slot_ok_rows = {}
    for r in stats:
        if r["metric"] == "slot" and int(r["value"]) == QH.SLOT_CODES["ok"]:
            slot_ok_rows.setdefault(r["key"].split(":", 1)[0], set()).add(round(float(r["key"].split(":", 1)[1]), 3))
    cands, followups = [], []
    all_events = store.events(mode=mode)
    hist = {"audits": store.audits("disable", 0, 1e18) + store.audits("promote", 0, 1e18),
            "alerts": store.alerts_with_prefix("position:")}
    fu_ctx = {"order_alerts": store.alerts_with_prefix("order:"), "event_alerts": store.alerts_with_prefix("event:"),
              "resolves": store.audits("resolve_unknown", 0, 1e18)}
    for p in store.positions(mode=mode):
        if p["created_at"] >= end:
            continue
        # 보고일 감시 대상 = 이 정규장 [개장, 마감·현재] 과 포지션의 실제 일반 감시 구간이 겹치는 포지션.
        #   감시 시작 = max(개장, 등록), 감시 끝 = 첫 손절 트리거 / 관리 종료·확인 필요 전환 시각 / 마감.
        #   일반 감시는 ACTIVE 만 하며, 트리거 이후 ACTIVE 로 돌아가는 경로는 없다 → 보고일 감시 시작 전에 트리거된
        #   포지션은 그날 감시 대상이 아니다 (전날 청산 이벤트의 이월 주문 대조는 exit_followups 로 따로 보인다).
        p_events = [e for e in all_events if e["position_id"] == p["id"]]
        triggers = sorted(e["triggered_at"] for e in p_events)
        ws = max(start, p["created_at"])
        if triggers and triggers[0] < ws:
            fu = _followup(store, p, p_events, day_start, day_end, fu_ctx)
            if fu:
                followups.append(fu)
            continue
        in_window = [t for t in triggers if t <= end]
        stop_at = min(in_window + [end])
        # 감시(engine._watch)는 ACTIVE 만 한다. 확인 필요(NEEDS_REVIEW)·종료 상태는 최초 영구 감시 종료 시각에 끝났다.
        # updated_at(마지막 수정 시각)을 종료 시각으로 쓰지 않는다: 후속 조작(NEEDS_REVIEW → 관리 종료 등)이 바꾼다.
        end_src = None
        if p["state"] not in ("ACTIVE", "EXITING"):
            wend, ended_by, end_src = _watch_end(p, triggers, hist)
            if wend is not None:
                stop_at = min(stop_at, wend)
            elif ended_by < ws:
                continue      # 정확한 종료 시각은 몰라도 보고일 감시 시작 전에 이미 끝나 있었음이 입증됨
        if stop_at - ws <= 0 and not (in_window and ny_date(in_window[0]) == d):
            continue          # 구간 길이 0 은 그날 개장 시각 트리거처럼 당일 이벤트 근거가 있을 때만 포함
        # 표식이 감시 시작 이전이면 그 감시 구간 전체를 신버전이 기록했다. 감시 중에 바뀌었으면 전환 후보다.
        fmt = ("LEGACY" if v2_since is None or v2_since > stop_at else "SLOT_V2" if v2_since <= ws else "SWITCH")
        cands.append({"p": p, "ws": ws, "stop": stop_at, "fmt": fmt, "end_src": end_src})

    def v2_ok_all(pid):
        """신버전이 그 포지션에서 quote_ok 를 올린 횟수 = 성공 슬롯 수 (기록된 슬롯 + 확정 전 진행 중 슬롯)."""
        recorded = slot_ok_rows.get(str(pid), set())
        rec = health.get(str(pid)) or {}
        cur = rec.get("slot_start")
        extra = 0
        if (cur is not None and ny_date(cur) == d and round(cur, 3) not in recorded
                and (rec.get("slot_flags") or {}).get("ok")):
            extra = 1
        return len(recorded) + extra

    tickers = {}
    for c in cands:
        tickers.setdefault(c["p"]["ticker"], []).append(c)
    ticker_info = {}
    for t, group in tickers.items():
        total = int(stat("quote_ok", t))
        v2 = sum(v2_ok_all(c["p"]["id"]) for c in group if c["fmt"] != "LEGACY") if interval > 0 else 0
        remainder = total - v2
        for c in group:
            if c["fmt"] == "SWITCH":
                # 허용 오차만으로 SLOT_V2 를 주지 않는다: 오차 이내이고 그 티커에 구버전 기록이 없을 때만.
                c["fmt"] = "SLOT_V2" if (c["ws"] + tol >= v2_since and remainder == 0) else "MIXED"
        claimants = [c for c in group if c["fmt"] in ("LEGACY", "MIXED") and c["end_src"] != "unverified"]
        # 감시 구간 끝을 입증할 수 없는 구버전 기간 포지션(LEGACY·MIXED)도 이 티커 일계에 기여했을 수 있다.
        # 그런 포지션이 있으면 나머지 구버전 포지션이 유일한 귀속 대상이라고 판단하지 않는다 (임의 배분 금지).
        # 신버전 슬롯(SLOT_V2, MIXED 의 전환 후)은 position_id 별로 따로 기록되므로 영향을 받지 않는다.
        uncertain = [c["p"]["id"] for c in group if c["fmt"] in ("LEGACY", "MIXED") and c["end_src"] == "unverified"]
        # 일반 감시 실패·무효: 티커 일계(quote_fail/quote_invalid)와 신버전 포지션 상세(pos_watch_fail/invalid)는 신버전에서
        # 같은 호출(_record_quote_error, purpose=watch)이 함께 올린다 → 같은 범위·같은 의미. 차이 = 구버전 몫.
        v2_fail = sum(int(stat("pos_watch_fail", str(c["p"]["id"]))) for c in group if c["fmt"] != "LEGACY")
        v2_inv = sum(int(stat("pos_watch_invalid", str(c["p"]["id"]))) for c in group if c["fmt"] != "LEGACY")
        ticker_info[t] = {"quote_ok": total, "v2_ok": v2, "remainder": remainder, "claimants": claimants,
                          "uncertain": uncertain,
                          "fail_rem": int(stat("quote_fail", t)) - v2_fail,
                          "inv_rem": int(stat("quote_invalid", t)) - v2_inv}
    legacy_ticker_totals, unattributed, ok_unmeasured = {}, [], 0

    for c in cands:
        p, ws, stop_at, fmt = c["p"], c["ws"], c["stop"], c["fmt"]
        t, pid = p["ticker"], p["id"]
        info = ticker_info[t]
        rec = health.get(str(pid)) or {}
        measured_from = ws if fmt == "SLOT_V2" else (v2_since if fmt == "MIXED" else None)
        quality = []
        if measured_from is not None:
            slots, per_slot, quality = _slot_view(pid, stats, rec, measured_from, stop_at, interval)
        else:
            slots, per_slot = None, {}
        target = f"{mode}:{t}"
        faults = [{"at": a["at"], "kind": "fault" if a["action"] == "quote_fault" else "recovered",
                   "category": a["details"].get("category")}
                  for a in sorted(fault_audits, key=lambda a: a["at"])
                  if a["target"] == target and a["details"].get("position_id") in (None, pid)]
        transitions = sorted(
            [{"at": f["at"], "action": "quote_fault" if f["kind"] == "fault" else "quote_recovered"} for f in faults]
            + [{"at": a["at"], "action": a["action"]} for a in pause_audits if str(a["target"]) == str(pid)]
            + [{"at": hp["from"], "action": "global_hold_on"} for hp in hold_periods]
            + [{"at": hp["to"], "action": "global_hold_off"} for hp in hold_periods if hp["to"]],
            key=lambda x: x["at"])
        if measured_from is not None:
            # 지금 감시 중인 포지션(마감 처리 전 당일 ACTIVE)만 현재 상태로 확인한다. 이미 감시가 끝난 포지션의 상태를
            # 현재 값으로 추정하지 않는다.
            watching_now = live_day and p["state"] == "ACTIVE"
            states, sq = _state_intervals(store, mode, p, measured_from, stop_at, rec, hold_periods,
                                          check_current=watching_now)
            # 상태 이력의 완전성 근거: 감시 종료 시점(관리 종료·트리거·확인 필요·장 마감)에 포지션별로 저장한 상태 대조
            # 결과 + 당시 기록된 슬롯 분류. 일자 완료 표식(state_audit_done)은 포지션별 근거가 아니다.
            ev = close_evidence.get(str(pid))
            if ev is not None and int(ev) != 1:
                sq.append("state_unaudited: 감시 종료 시점 상태가 감사 기록과 맞지 않았음")
            elif ev is None and not watching_now:
                sq.append("state_evidence_missing: 이 포지션의 감시 종료 시점 상태 근거가 없음")
            sq += _slot_state_issues(per_slot, states, interval)
            quality += sq
            gaps = _gap_view(pid, stats, measured_from, stop_at, per_slot, interval, transitions, states,
                             measurable=not sq)
            gaps["measured_from"], gaps["partial"] = measured_from, fmt == "MIXED"
        else:
            gaps = {"measurable": False, "reason": "LEGACY: 이 거래일은 성공 시각을 기록하지 않았다", "successes": None,
                    "first_success_at": None, "last_success_at": None, "max_sec": None, "max_normal_sec": None,
                    "over_120": None, "normal_over_120": None, "items": [], "measured_from": None, "partial": False}
        breakdown = {"held_slots": None, "paused_slots": None, "failed_slots": None, "schedule_missed": None}
        if fmt == "SLOT_V2":
            ok, expected = slots["ok"], slots["expected"]
            breakdown = {"held_slots": slots["held"], "paused_slots": slots["paused"],
                         "failed_slots": slots["watch_fail"] + slots["invalid"], "schedule_missed": slots["missed"]}
            if info["remainder"] != 0 and not info["claimants"]:
                quality.append(f"aggregate_mismatch: 티커 일계 {info['quote_ok']} ≠ 신버전 성공 {info['v2_ok']}")
        else:
            # LEGACY 구간 기대 = 기존 보고 기준(구간 ÷ 주기 내림). MIXED 의 구버전 구간은 그 구간에 도래한 감시 예정
            # 시각 수(올림) + 신버전 완료 슬롯.
            if fmt == "LEGACY":
                expected = int(max(0.0, stop_at - ws) // max(1, interval))
                own_v2_ok = 0
            else:
                expected = math.ceil(max(0.0, v2_since - ws) / max(1, interval)) + slots["expected"]
                own_v2_ok = slots["ok"]
                quality.append(f"mixed_format: 슬롯·공백은 {measured_from:.0f} 이후만 측정 (구버전 구간은 일계 기준)")
            if c["end_src"] == "unverified":
                ok = None                    # 감시 구간 끝을 입증할 수 없음: 구버전 몫을 귀속하지 않는다
            elif info["uncertain"]:
                ok = None                    # 같은 티커에 기여 범위를 모르는 포지션이 있다: 귀속을 입증할 수 없음
                quality.append(f"legacy_attribution_uncertain: 같은 티커의 종료 시각 미확인 포지션 {info['uncertain']}"
                               f" 이 일계 {info['quote_ok']}회에 기여했을 수 있어 포지션별로 귀속하지 않음")
            elif info["remainder"] < 0:
                ok = None
                ok_unmeasured += 1
                quality.append(f"aggregate_mismatch: 티커 일계 {info['quote_ok']} < 신버전 성공 {info['v2_ok']}")
            elif len(info["claimants"]) == 1:
                ok = info["remainder"] + own_v2_ok
            else:
                ok = None                    # 같은 티커 구버전 기간 포지션이 여럿: 배분하지 않는다
                agg = legacy_ticker_totals.setdefault(
                    t, {"ok": info["remainder"], "expected": 0, "positions": [], "quote_ok": info["quote_ok"],
                        "quote_fail": info["fail_rem"] if info["fail_rem"] >= 0 else None,
                        "quote_invalid": info["inv_rem"] if info["inv_rem"] >= 0 else None})
                agg["ok"] += own_v2_ok
                agg["expected"] += expected
                if fmt == "MIXED":
                    for k, m in (("quote_fail", "watch_fail"), ("quote_invalid", "watch_invalid")):
                        if agg[k] is not None:
                            agg[k] += pstat(m, pid)
                agg["positions"].append(pid)
                quality.append("legacy_ticker_shared: 같은 티커 구버전 기간 포지션이 여럿이라 포지션별로 나누지 않음")
        if c["end_src"] == "unverified":
            # 이전 버전 포지션: 최초 감시 종료 시각 기록도, 이를 입증할 이력(트리거·관리 종료·확인 필요 알림)도 없다.
            # 마지막 수정 시각을 종료 시각으로 확정하지 않고 기대·정상·공백을 측정 불가로 둔다.
            ok = expected = None
            breakdown = {k: None for k in breakdown}
            gaps = {**gaps, "measurable": False, "max_sec": None, "max_normal_sec": None, "over_120": None,
                    "normal_over_120": None, "reason": "watch_end_unverified"}
            quality.append("watch_end_unverified: 최초 감시 종료 시각을 기록·이력으로 확인할 수 없음 (측정 불가)")
        elif c["end_src"] == "history":
            quality.append("watch_end_from_history: 최초 감시 종료 시각을 기존 이력(트리거·감사·알림)으로 복원")
        missed = None if ok is None or ok > expected else expected - ok
        if ok is not None and ok > expected:
            quality.append(f"aggregate_exceeds_expected: 정상 {ok} > 기대 {expected} (전환 구간 겹침 등)")
        group = t if (fmt != "SLOT_V2" and t in legacy_ticker_totals and pid in legacy_ticker_totals[t]["positions"]) \
            else None
        # 일반 감시 실패·무효 (같은 범위의 기록만, 임의 차감·합산 없음)
        if fmt == "SLOT_V2":
            q_fail, q_inv = pstat("watch_fail", pid), pstat("watch_invalid", pid)
        elif group or len(info["claimants"]) != 1 or info["uncertain"] or c["end_src"] == "unverified":
            q_fail = q_inv = None                     # 티커 그룹 합계로만 보이거나 귀속을 입증할 수 없다
        else:
            own_f = pstat("watch_fail", pid) if fmt == "MIXED" else 0
            own_i = pstat("watch_invalid", pid) if fmt == "MIXED" else 0
            q_fail = info["fail_rem"] + own_f if info["fail_rem"] >= 0 else None
            q_inv = info["inv_rem"] + own_i if info["inv_rem"] >= 0 else None
            if info["fail_rem"] < 0 or info["inv_rem"] < 0:
                quality.append("aggregate_mismatch: 티커 일계 감시 실패·무효가 신버전 상세보다 작음")
        # 신버전에만 있는 지표: 전 구간을 신버전이 기록한 포지션(SLOT_V2)만 값이 있다. 구버전 회복 조회 실패는
        # 저널에 기록되지 않았다 (로그에만 있음) → 측정 불가.
        full_v2 = fmt == "SLOT_V2"

        def v2(metric):
            return pstat(metric, pid) if full_v2 else None
        recovery = {"attempts": v2("attempt_recovery"), "ok": v2("recovery_ok"), "fail": v2("recovery_fail"),
                    "invalid": v2("recovery_invalid"), "stale_repeat": v2("recovery_stale")}
        positions.append({
            "id": pid, "ticker": t, "state": p["state"], "format": fmt, "window": {"start": ws, "end": stop_at},
            "ticker_group": group,
            # 감시 슬롯 (거래일 + 모드 + position_id + 슬롯 시작). 완료 슬롯마다 분류 하나, 진행 중 슬롯은 따로.
            "slots": slots, "expected_checks": expected, "ok_checks": ok, "missed_checks": missed, **breakdown,
            "normal_rate_pct": round(ok / expected * 100, 1) if (expected and missed is not None) else None,
            # 집계 무결성: 값이 있는 것(측정)과 그 값이 유효한 것(정상 ≤ 기대)을 구분한다. 티커 그룹은 그룹에서 판정.
            "ok_integrity": (None if ok is None else "valid" if ok <= expected else "exceeds_expected"),
            "data_quality": quality,
            # 조회 시도 (목적별, 같은 슬롯의 재시도 포함. HTTP 요청 수와 다를 수 있다)
            "attempts": {k: v2(f"attempt_{k}") for k in ("watch", "recovery", "order", "cancel_check", "incident")},
            "watch_attempts": v2("attempt_watch"),
            "watch_attempts_since_switch": pstat("attempt_watch", pid) if fmt == "MIXED" else None,
            "quote_fail": q_fail, "quote_invalid": q_inv,
            "quote_fail_source": "position_slots" if fmt == "SLOT_V2" else "ticker_daily",
            "recovery": recovery,
            "order_quote": {"ok": v2("order_ok"), "fail": v2("order_fail"), "invalid": v2("order_invalid"),
                            "held": int(stat("order_held_symbol", t)) if full_v2 else None},
            "held_triggers": v2("held_trigger"), "deferred": v2("deferred"),
            "gaps": gaps,
            "max_quote_age_sec": maxstat("max_quote_age_sec", t),
            "last_quote_at": p["last_quote_at"], "last_check_at": p["last_price_check_at"],
            "health_state": rec.get("state"), "fault_started_at": rec.get("fault_started_at"),
            "recovered_at": rec.get("recovered_at"), "faults": faults,
            "first_error": rec.get("first_error") if (rec.get("first_error") or {}).get("day") == day else None})
    # ── 전체 집계: 기대 횟수와 성공 횟수의 측정 가능 여부를 따로 본다 ─────────────────────
    #   기대: 모든 포지션(티커 그룹 포함)의 확인된 기대를 그대로 합한다.
    #   성공·실패 등: 측정 불가(None) 포지션이 하나라도 있으면 전체는 None. 티커 그룹은 그룹 합계로 정확히 한 번 센다.
    #   측정된 범위의 소계는 measured_subtotal 로 따로 보인다 (전체 통계가 아니다).
    solo = [p for p in positions if not p["ticker_group"]]

    def total_of(key, group_key=None):
        if any(p[key] is None for p in solo):
            return None
        groups = list(legacy_ticker_totals.values()) if group_key else []
        if any(a[group_key] is None for a in groups):
            return None
        return sum(p[key] for p in solo) + sum(a[group_key] for a in groups)
    # 집계 순서: (1) position 별 검증 (2) LEGACY 티커 그룹 검증 (3) 유효·불일치 구분 (4) 전체·유효 소계 (5) 품질 표시.
    # 개별 단위의 정상 > 기대는 전체 합이 정상 범위여도 숨기지 않는다: 전체 정상·누락·정상률은 None, 원시 기록은
    # ok_recorded 로 따로 보인다. 유효 소계에는 유효한 포지션·그룹만 넣는다.
    for t, agg in legacy_ticker_totals.items():
        agg["integrity"] = "valid" if agg["ok"] <= agg["expected"] else "exceeds_expected"
        agg["missed"] = agg["expected"] - agg["ok"] if agg["integrity"] == "valid" else None
    total_expected = (None if any(p["expected_checks"] is None for p in solo) else
                      sum(p["expected_checks"] for p in solo) + sum(a["expected"] for a in legacy_ticker_totals.values()))
    measured_solo = [p for p in solo if p["ok_checks"] is not None]
    valid_solo = [p for p in measured_solo if p["ok_integrity"] == "valid"]
    valid_groups = [a for a in legacy_ticker_totals.values() if a["integrity"] == "valid"]
    integrity_errors = (
        [f"{p['ticker']}#{p['id']}: 정상 {p['ok_checks']} > 기대 {p['expected_checks']}"
         for p in measured_solo if p["ok_integrity"] != "valid"]
        + [f"{t} 티커 합계(포지션 {a['positions']}): 정상 {a['ok']} > 기대 {a['expected']}"
           for t, a in legacy_ticker_totals.items() if a["integrity"] != "valid"])
    ok_recorded = (sum(p["ok_checks"] for p in measured_solo) + sum(a["ok"] for a in legacy_ticker_totals.values())
                   if (measured_solo or legacy_ticker_totals) else (0 if not positions else None))
    total_ok = total_of("ok_checks", "ok") if not integrity_errors else None
    grouped_n = sum(len(a["positions"]) for a in legacy_ticker_totals.values())
    measured_n = len(measured_solo) + grouped_n
    sub_exp = sum(p["expected_checks"] for p in valid_solo) + sum(a["expected"] for a in valid_groups)
    sub_ok = sum(p["ok_checks"] for p in valid_solo) + sum(a["ok"] for a in valid_groups)
    measured_subtotal = {
        "positions": len(valid_solo) + sum(len(a["positions"]) for a in valid_groups),
        "expected": sub_exp, "ok": sub_ok, "missed": sub_exp - sub_ok,
        "normal_rate_pct": round(sub_ok / sub_exp * 100, 1) if sub_exp else None}
    for t, info in ticker_info.items():
        if info["remainder"] > 0 and not info["claimants"] and not info["uncertain"]:
            unattributed.append(f"unattributed_legacy_ok: {t} 구버전 몫 {info['remainder']}회를 귀속할 포지션 없음")
    formats = {p["format"] for p in positions}
    report_format = ("LEGACY" if v2_since is None else "SLOT_V2" if formats <= {"SLOT_V2"} else "MIXED")
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
    def pos_total(get):
        vals = [get(p) for p in positions]
        return None if any(v is None for v in vals) else sum(vals)
    recovery_tot = {k: pos_total(lambda p, k=k: p["recovery"][k])
                    for k in ("attempts", "ok", "fail", "invalid", "stale_repeat")}
    recovery_tot["coverage"] = ("none" if not positions or all(p["recovery"]["attempts"] is None for p in positions)
                                else "full" if recovery_tot["attempts"] is not None else "partial")
    slot_tot = {c: sum(p["slots"][c] for p in positions if p["slots"]) for c in QH.SLOT_CLASSES + ("expected",)}
    def coverage(n_ok, n_all):
        return "none" if n_ok == 0 else "full" if n_ok == n_all else "partial"
    # 실제 공백: 전 구간 측정(SLOT_V2)한 포지션만 '측정'으로 센다. MIXED 는 부분 측정으로 따로 센다.
    full_gap = [p["gaps"] for p in positions if p["format"] == "SLOT_V2" and p["gaps"].get("max_sec") is not None]
    part_gap = [p["gaps"] for p in positions if p["format"] == "MIXED" and p["gaps"].get("max_sec") is not None]
    normal_ok = [g for g in full_gap if g.get("measurable")]
    gap_tot = {
        # 측정된 포지션만의 값이다. coverage 가 full 이 아니면 전체 종목의 정확한 최대값이 아니다.
        "max_sec": max([g["max_sec"] for g in full_gap + part_gap], default=None),
        "over_120": sum(g["over_120"] for g in full_gap + part_gap) if (full_gap or part_gap) else None,
        "coverage": coverage(len(full_gap), len(positions)) if not part_gap else "partial",
        "measured_positions": len(full_gap), "partially_measured_positions": len(part_gap),
        "unmeasured_positions": len(positions) - len(full_gap) - len(part_gap),
        # 정상 상태 공백: 상태 이력 완전성이 확인된 포지션만
        "max_normal_sec": max([g["max_normal_sec"] for g in normal_ok if g["max_normal_sec"] is not None],
                              default=None),
        "normal_over_120": sum(g["normal_over_120"] for g in normal_ok) if normal_ok else None,
        "normal_coverage": coverage(len(normal_ok), len(positions)),
        "normal_measured_positions": len(normal_ok), "normal_unmeasured_positions": len(positions) - len(normal_ok)}
    # 누락 세부 원인: 슬롯으로 분해할 수 있는 몫만 나누고, 나머지는 '원인 측정 불가'로 둔다 (0 으로 채우지 않는다).
    measurable_bd = [p for p in positions if p["held_slots"] is not None]
    bd_unmeasured = sum(p["missed_checks"] for p in positions if p["held_slots"] is None
                        and p["missed_checks"] is not None)
    bd_unmeasured += sum(max(0, a["expected"] - a["ok"]) for a in legacy_ticker_totals.values())
    total_missed = (total_expected - total_ok) if (total_ok is not None and total_ok <= total_expected) else None
    if total_missed is None:
        bd_unmeasured = None                         # 전체 누락을 모르면 원인 미측정 몫도 확정할 수 없다

    def bd_sum(key):
        return sum(p[key] for p in measurable_bd) if measurable_bd else None
    ok_cov = coverage(measured_n, len(positions)) if positions else "none"
    ok_status = ("no_targets" if not positions else "unmeasured" if measured_n == 0
                 else "partial" if measured_n < len(positions) else "full")
    quality = [f"{p['ticker']}#{p['id']}: {q}" for p in positions for q in p["data_quality"]] + unattributed
    quality += [f"aggregate_exceeds_expected: {e}" for e in integrity_errors if "티커 합계" in e]
    day_full_v2 = v2_since is not None and v2_since <= start + tol      # 그날 전 구간을 신버전이 기록
    if total_ok is not None and total_ok > total_expected:
        quality.append(f"aggregate_exceeds_expected: 전체 정상 {total_ok} > 기대 {total_expected}")
    heartbeat = store.runtime_get("heartbeat_at")
    unresolved_orders = store.orders(mode=mode, states=UNRESOLVED)
    return {
        **base,
        "session": {"open_at": start, "close_at": session.close_ts, "early_close": session.early_close},
        "watched_positions": len(positions),
        # 일반 감시 대상이 아닌, 전 거래일 청산 이벤트의 당일 주문 대조·이월 (감시 통계에 넣지 않는다)
        "exit_followups": followups,
        # 거래일+모드+티커 원시 일계와 포지션 귀속 상태 (유효 평가용 집계와 별개로 보존, 전체 합계에 더하지 않는다)
        "ticker_daily_raw": {t: {"quote_ok": info["quote_ok"], "quote_fail": int(stat("quote_fail", t)),
                                 "quote_invalid": int(stat("quote_invalid", t)), "v2_position_ok": info["v2_ok"],
                                 "attribution": ("uncertain" if info["uncertain"] else "shared" if len(info["claimants"]) > 1
                                                 else "single" if info["claimants"] else "position_slots_only"),
                                 "uncertain_positions": info["uncertain"]}
                             for t, info in ticker_info.items()},
        # 전 거래일 주문의 당일 신규 반영 체결 수량 (일반 감시·당일 신규 주문 수와 별개, 원장 증분이라 중복 없음)
        "exit_followup_fills_applied_qty": sum(o["fills_applied_qty_on_day"] for f in followups
                                               for e in f["events"] for o in e["orders"]),
        "data_quality": quality,
        "format": report_format, "slot_v2_since": v2_since,
        "watch": {"expected": total_expected, "ok": total_ok, "missed": total_missed,
                  "ok_coverage": ok_cov, "ok_status": ok_status,
                  "ok_unmeasured_positions": len(positions) - measured_n, "ok_measured_positions": measured_n,
                  "ok_ticker_aggregated_positions": grouped_n, "measured_subtotal": measured_subtotal,
                  "ok_recorded": ok_recorded,
                  "ok_integrity": "invalid" if integrity_errors else "valid", "integrity_errors": integrity_errors,
                  "legacy_ticker_totals": legacy_ticker_totals,
                  "slots": slot_tot, "gaps": gap_tot,
                  "held_slots": bd_sum("held_slots"), "paused_slots": bd_sum("paused_slots"),
                  "failed_slots": bd_sum("failed_slots"), "schedule_missed": bd_sum("schedule_missed"),
                  "cause_unmeasured_missed": bd_unmeasured,
                  "attempts": pos_total(lambda p: p["watch_attempts"]),
                  "api_errors": total_of("quote_fail", "quote_fail"),
                  "invalid": total_of("quote_invalid", "quote_invalid"),
                  "normal_rate_pct": (round(total_ok / total_expected * 100, 1)
                                      if total_expected and total_missed is not None else None),
                  "positions": positions},
        # 일반 감시와 따로 센다. 회복 확정에 쓴 시세는 정상 감시 성공(watch.ok)에 넣지 않는다.
        "recovery": recovery_tot,
        "holds": {**holds, "held_triggers": pos_total(lambda p: p["held_triggers"])},
        # Scheduler heartbeat (tick) 은 종목별 시세 감시와 별개로 평가한다.
        "ticks": {"expected": expected_ticks, "actual": int(stat("ticks")), "max_gap_sec": max_gap},
        "api": {"errors": int(stat("api_error")), "by_category": {r["key"]: int(r["value"]) for r in stats
                                                                   if r["metric"] == "api_error"},
                # 아래는 신버전에만 있는 지표: 그날 전 구간을 신버전이 기록했을 때만 값이 있다 (구버전 기간 = 측정 불가).
                "quote_errors": ({r["key"]: int(r["value"]) for r in stats if r["metric"] == "quote_error"}
                                 if day_full_v2 else None),
                "http_429": int(stat("http_429")) if day_full_v2 else None,
                "http_requests": int(stat("http_requests")) if day_full_v2 else None,
                "http_requests_by_api": ({r["key"]: int(r["value"]) for r in stats if r["metric"] == "http_requests"}
                                         if day_full_v2 else None),
                "other_recovery": ({k: {"attempts": int(stat("recovery_attempt", k)),
                                        "ok": int(stat("recovery_ok", k)), "fail": int(stat("recovery_fail", k))}
                                    for k in ("balance", "orders")} if day_full_v2 else None)},
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


def _slot_view(pid, stats, rec, ws, we, interval):
    """position 의 감시 슬롯 분류. 기대 슬롯 = 그날 슬롯 격자(slot_anchor + k×주기) 중 감시 구간 [ws, we) 안에서
    끝난 슬롯 + 트리거·관리 종료로 일찍 확정된 슬롯. 각 슬롯은 기록된 최종 분류를 그대로 쓴다 (임의 절삭 없음).
    끝났지만 다음 tick 이 아직 없어 확정 기록 전인 슬롯은 진행 중 레코드의 플래그로 분류하고, 기록이 없는 슬롯은
    스케줄 누락(missed, missed_unrecorded 로 따로 표시)이다. 구간 밖 기록은 데이터 품질 오류로 표시한다."""
    empty = {c: 0 for c in QH.SLOT_CLASSES}
    if interval <= 0:
        return {"expected": 0, **empty, "missed_unrecorded": 0, "in_progress": None, "interval": interval}, {}, []
    key = f"{pid}:"
    recorded = {}
    for r in stats:
        if r["metric"] == "slot" and r["key"].startswith(key):
            recorded[round(float(r["key"][len(key):]), 3)] = QH.CODE_SLOTS.get(int(r["value"]), "missed")
    anchor = next((r["value"] for r in stats if r["metric"] == "slot_anchor" and r["key"] == str(pid)), None)
    quality = []
    if anchor is None:
        anchor = rec.get("anchor") or (min(recorded) if recorded else None)
    if anchor is None:
        anchor = ws
        quality.append("anchor_missing: 슬롯 기록이 없어 감시 시작 시각을 격자 기준으로 사용")
    tol = QH.SLOT_TOLERANCE_SEC
    k = math.ceil((ws - anchor - tol) / interval - 1e-9)
    expected, s = [], anchor + k * interval
    while s + interval - tol <= we + 1e-6:
        if s + tol >= ws:
            expected.append(round(s, 3))
        s += interval
    exp = set(expected)
    for rs in recorded:                               # 트리거·관리 종료로 끝나기 전에 확정된 마지막 슬롯
        if rs not in exp and ws - tol <= rs < we:
            expected.append(rs)
            exp.add(rs)
    outside = [rs for rs in recorded if rs not in exp]
    if outside:
        quality.append(f"slot_out_of_window: 감시 구간 밖 슬롯 기록 {len(outside)}건")
    cur = rec.get("slot_start")
    cur = round(cur, 3) if cur is not None else None
    classes, unrecorded, per_slot = dict(empty), 0, {}
    for s in sorted(expected):
        cls = recorded.get(s)
        if cls is None and cur is not None and abs(s - cur) < 1e-3:
            cls = QH.slot_final_class(rec.get("slot_flags"))
        if cls is None:
            cls, unrecorded = "missed", unrecorded + 1
        classes[cls] += 1
        per_slot[s] = cls
    in_progress = cur if (cur is not None and cur not in exp and ws - tol <= cur < we) else None
    return ({"expected": len(expected), **classes, "missed_unrecorded": unrecorded, "in_progress": in_progress,
             "interval": interval}, per_slot, quality)


GAP_LIMIT_SEC = 120


def _timeline(events, ws, we):
    """[(시각, 켜짐?)] → ws 이전 마지막 상태부터 이어진 [시작, 끝) 구간 목록 (ws~we 로 자름)."""
    events = sorted(events, key=lambda e: e[0])
    on = False
    for at, flag in events:
        if at <= ws:
            on = flag
    out, cur = [], (ws if on else None)
    for at, flag in events:
        if at <= ws or at > we:
            continue
        if flag and cur is None:
            cur = at
        elif not flag and cur is not None:
            out.append((cur, at))
            cur = None
    if cur is not None:
        out.append((cur, we))
    return out, on


def _state_intervals(store, mode, p, ws, we, rec, hold_periods, *, check_current=False):
    """감사 기록 전체 이력으로 상태 구간을 복원한다 (장 시작 이전부터 이어진 상태 포함).

    - 일시중지: pause/resume (대상 position_id)
    - 종목 장애: quote_fault/quote_recovered (대상 mode:ticker, position_id) + 기존 카운터 이행(quote_health_migrate)
    - 전체 보류: global_hold (모든 종목 공통)
    현재 상태(포지션 auto_paused, 종목 레코드 state)가 복원 결과와 맞지 않으면 측정 불가 사유를 돌려준다."""
    pid, t = p["id"], p["ticker"]
    pauses = [(a["at"], a["action"] == "pause") for a in store.audits("pause", 0, we + 1) + store.audits("resume", 0, we + 1)
              if str(a["target"]) == str(pid)]
    target = f"{mode}:{t}"
    faults = [(a["at"], a["action"] == "quote_fault")
              for a in store.audits("quote_fault", 0, we + 1) + store.audits("quote_recovered", 0, we + 1)
              if a["target"] == target and a["details"].get("position_id") in (None, pid)]
    faults += [(a["at"], True) for a in store.audits("quote_health_migrate", 0, we + 1)
               if target in (a["details"].get("marked_fault") or []) and p["created_at"] <= a["at"]]
    paused_iv, paused0 = _timeline(pauses, ws, we)
    fault_iv, fault0 = _timeline(faults, ws, we)
    hold_iv = [(hp["from"], hp["to"] or we) for hp in hold_periods]
    quality = current_state_issues(store, mode, p, rec, we) if check_current else []
    return {"paused": paused_iv, "fault": fault_iv, "hold": hold_iv,
            "at_start": {"paused": paused0, "fault": fault0,
                         "global_hold": any(a <= ws < b for a, b in hold_iv)}}, quality


def _slot_state_issues(per_slot, states, interval):
    """당시 기록된 슬롯 분류와 감사 기록 구간의 대조 (조회 시각과 무관한 근거).
    일시중지 슬롯이 일시중지 구간 밖이거나, 보류 슬롯이 종목 장애·전체 보류 구간 밖이면 상태 이력이 불완전하다."""
    tol = QH.SLOT_TOLERANCE_SEC
    issues = []

    def covered(s, kinds):
        a, b = s - tol, s + interval - tol
        return any(s0 < b and s1 > a for k in kinds for s0, s1 in states[k])
    if any(c == "paused" and not covered(s, ("paused",)) for s, c in per_slot.items()):
        issues.append("state_unaudited: 일시중지 슬롯이 감사 기록의 일시중지 구간 밖에 있음")
    if any(c == "held" and not covered(s, ("fault", "hold")) for s, c in per_slot.items()):
        issues.append("state_unaudited: 보류 슬롯이 감사 기록의 장애·보류 구간 밖에 있음")
    return issues


WAIT_PREFIXES = ("replace_waiting:", "order_block:")
_ACCEPTED_OPEN = ("SUBMITTED", "PARTIAL_FILLED", "CANCEL_SENDING", "CANCEL_PENDING", "CANCEL_UNKNOWN")


def _wait_decision(w, ev, pos, subject):
    """대기 기록 하나의 정합성 판정 → ("keep", 갱신 기록) | ("close", 사유). 주문을 만들거나 상태를 바꾸지 않는다.

    subject = 같은 청산 이벤트에서 대기 시작(since) 이후 생긴 가장 최근 주문 (없으면 None). 기록의 order_id 가
    다른 이벤트의 주문이거나 더 새 주문으로 대체됐으면 쓰지 않는다 (최신 대기 기록을 잘못 지우지 않는다)."""
    if ev is None or pos is None:
        return "close", "orphan: 청산 이벤트·포지션 없음"
    facts = {"event_state": ev["state"], "remaining": pos["remaining_managed_qty"], "position_state": pos["state"]}
    if subject is not None:
        st = subject["state"]
        facts.update(order_id=subject["id"], order_state=st, order_filled=subject["filled_qty"])
        if st in ("SENDING", "UNKNOWN"):
            # 전송 결과 불명: 기록 유지, 자동 재전송 없음. 긴급 알림은 기존 UNKNOWN 알림이 맡는다.
            return "keep", {**w, **facts, "status": "result_unknown", "outcome": st.lower()}
        if st in _ACCEPTED_OPEN:
            return "close", f"accepted: 증권사 접수 확인 ({st})"
        if st == "FILLED":
            return "close", "filled: 대체 주문 전량 체결"
        if st == "CANCELLED":
            # 접수 후 취소 확정: 이 대기 구간은 끝났다. 잔여 재주문이 필요하면 다음 진행에서 새 대기 구간으로 기록한다.
            return "close", "accepted_then_cancelled: 대체 주문이 접수 후 취소 확정 (매도 완료 아님)"
        if st in ("REJECTED", "NOT_SENT"):
            if ev["state"] in ("OPEN", "CARRY_OVER", "NEEDS_REVIEW"):
                # 확인된 최종 결과로 바꿔 둔다. 자동 재주문 없음 (이벤트는 기존 정책대로 확인 필요).
                return "keep", {**w, **facts, "status": "closed", "outcome": st.lower()}
            return "close", f"event_ended: 이벤트 {ev['state']} (대체 주문 {st})"
        return "keep", {**w, **facts, "status": "result_unknown", "outcome": st.lower()}
    if ev["state"] in ("OPEN", "CARRY_OVER") and pos["state"] == "EXITING" and pos["remaining_managed_qty"] > 0:
        return "keep", {**w, **facts, "status": w.get("status") or "waiting"}
    if ev["state"] == "NEEDS_REVIEW" and pos["remaining_managed_qty"] > 0:
        return "keep", {**w, **facts, "status": "closed", "outcome": "needs_review", "replacement_needed": True}
    return "close", f"event_ended: 이벤트 {ev['state']}, 관리 잔여 {pos['remaining_managed_qty']}주"


def reconcile_wait_records(store, now, *, keys=None, actor="engine") -> dict:
    """재주문 대기(replace_waiting)·주문 차단(order_block) 기록을 실제 주문·이벤트·포지션 상태와 맞춘다.

    청산 이벤트가 OPEN 인지와 무관하게 모든 기록을 본다 (DONE·NEEDS_REVIEW 로 바뀐 이벤트 포함). 키마다 한 쓰기
    트랜잭션에서 읽고 판정하며, 바뀐 것이 있을 때만 쓴다 → 반복 실행·중간 재시작에도 결과가 같다. 기록을 닫을 때는
    같은 트랜잭션에서 감사 기록(wait_record_closed)을 남긴다. 읽기·정합성 처리일 뿐 주문 실행 권한을 주지 않는다."""
    results = {}
    for key in (keys if keys is not None else store.runtime_keys(WAIT_PREFIXES)):
        def fn(vals, db, key=key):
            w = vals.get(key)
            if not w:
                return None
            eid = int(key.split(":", 1)[1])
            ev = db.execute("SELECT * FROM auto_stop_exit_events WHERE id=?", (eid,)).fetchone()
            pos = (db.execute("SELECT * FROM auto_stop_positions WHERE id=?", (ev["position_id"],)).fetchone()
                   if ev is not None else None)
            since = float(w.get("since") or 0)
            later = db.execute("SELECT * FROM auto_stop_orders WHERE exit_event_id=? AND created_at>=? "
                               "ORDER BY created_at, id", (eid, since)).fetchall()
            linked, link_issue = _linked_order(db, key, w, eid, ev, pos, since)
            cands = list(later) + ([linked] if linked is not None and linked["created_at"] < since else [])
            subject = max(cands, key=lambda o: (o["created_at"], o["id"])) if cands else None
            action, detail = _wait_decision(w, ev, pos, subject)
            if action == "keep" and link_issue and detail.get("link_issue") != link_issue:
                detail = {**detail, "link_issue": link_issue}      # 연결 근거 부족: 확정 결과를 만들지 않고 표시만
            if action == "keep":
                if detail == w:
                    results[key] = ("keep", None)
                    return None
                results[key] = ("keep", detail.get("status"))
                return {key: {**detail, "checked_at": now}}, []
            results[key] = ("close", detail)
            return {key: None}, [(actor, "wait_record_closed", key, {
                "reason": detail, "record": w, "order_id": subject["id"] if subject is not None else None,
                "order_state": subject["state"] if subject is not None else None,
                "event_state": ev["state"] if ev is not None else None, "link_issue": link_issue,
                "remaining": pos["remaining_managed_qty"] if pos is not None else None})]
        store.runtime_txn(now, [key], fn)
    return results


_BROKER_TIME_KEYS = ("fill_time", "executed_at", "last_fill_at", "cntr_tm")


def _followup(store, p, p_events, d0, d1, ctx):
    """보고일 [d0, d1) 에 실제로 있었던 전 거래일 청산 이벤트의 후속 이력 (현재 상태만으로 판단하지 않는다).

    근거: 보고일 생성 주문(이월·재주문) / 체결 원장 applied_at 이 보고일인 증분 체결 / 보고일 주문 알림(취소 확정·
    소멸·체결·UNKNOWN 해제 등) / 보고일 resolve_unknown 감사 / 보고일 시작 시점에 미해결이던 주문(보고일 전 생성 +
    지금도 미해결이거나 확정 근거 시각이 보고일 이후) / 보고일의 이벤트 확인 필요 알림 / 지금도 열린 이벤트.
    당일 활동이 없는 오래된 종료 이벤트는 제외한다. 증권사 실제 체결 시각은 원장 증거에 있을 때만 보이고 추정하지 않는다."""
    events_out = []
    for e in p_events:
        if e["triggered_at"] >= d0:
            continue
        orders = store.orders(event_id=e["id"])
        ledger = {}
        for row in store.fill_ledger(o["id"] for o in orders):
            ledger.setdefault(row["order_id"], []).append(row)
        orders_out = []
        for o in orders:
            oid, reasons = o["id"], []
            pre = f"order:{oid}:"
            day_alerts = [a for a in ctx["order_alerts"] if a["dedup_key"].startswith(pre) and d0 <= a["created_at"] < d1]
            resolves = [a for a in ctx["resolves"] if str(a["target"]) == str(oid)]
            fills = [r for r in ledger.get(oid, []) if d0 <= r["applied_at"] < d1]
            if d0 <= o["created_at"] < d1:
                reasons.append("created_on_day")
            if fills:
                reasons.append("fill_applied_on_day")
            if day_alerts:
                reasons.append("order_events_on_day:" + ",".join(sorted({a["dedup_key"][len(pre):].split(":")[0]
                                                                        for a in day_alerts})))
            if any(d0 <= a["at"] < d1 for a in resolves):
                reasons.append("unknown_resolved_on_day")
            if o["created_at"] < d0:
                if o["state"] in UNRESOLVED:
                    reasons.append("unresolved_at_day_start")
                else:
                    fin, issue = _final_evidence_at(o, ledger.get(oid, []), ctx, resolves)
                    if fin is not None and fin >= d0:
                        reasons.append("unresolved_at_day_start")     # 나중에 종료돼도 그날 미해결이었던 사실은 유지
                    elif fin is None and o["updated_at"] >= d0:
                        # 종료 시각은 마지막 수정 이전이다: 마지막 수정이 보고일 이후면 그날 미해결이었을 수 있다
                        reasons.append(issue or "resolution_time_unverified")
            if not reasons:
                continue

            def broker_time(r):
                row = (json.loads(r["evidence"] or "{}") or {}).get("row") or {}
                return next((row[k] for k in _BROKER_TIME_KEYS if row.get(k)), None)
            orders_out.append({
                "order_id": oid, "kind": o["kind"], "state_now": o["state"], "created_at": o["created_at"],
                "cum_filled_now": o["filled_qty"], "requested_qty": o["requested_qty"], "reasons": reasons,
                "fills_applied_qty_on_day": sum(r["applied_delta"] for r in fills),
                "fills_on_day": [{"applied_at": r["applied_at"], "delta": r["applied_delta"], "cum": r["cum_filled_qty"],
                                  "broker_fill_time": broker_time(r)} for r in fills]})
        ev_reasons = []
        if e["state"] in ("OPEN", "CARRY_OVER"):
            ev_reasons.append("event_open")
        if any(a["dedup_key"].startswith(f"event:{e['id']}:") and d0 <= a["created_at"] < d1 for a in ctx["event_alerts"]):
            ev_reasons.append("event_alerts_on_day")
        if orders_out or ev_reasons:
            events_out.append({"event_id": e["id"], "state_now": e["state"], "triggered_at": e["triggered_at"],
                               "reasons": ev_reasons, "orders": orders_out})
    if not events_out:
        return None
    return {"position_id": p["id"], "ticker": p["ticker"], "events": events_out}


# resolve_unknown 감사의 broker_state 중 주문의 최종 종료를 입증하는 것. OPEN(접수)·PARTIAL(부분체결)·누락·알 수 없음은 아니다.
FINAL_BROKER_STATES = {"FILLED": "FILLED", "CANCELLED": "CANCELLED", "REJECTED": "REJECTED"}


def _final_evidence_at(o, ledger_rows, ctx, resolves):
    """확정 상태 주문의 최종 종료 근거 시각 → (시각 | None, 문제 | None). 그 주문 자체의 종료를 입증하는 근거만 쓴다.

    - 증거 해제 감사(resolve_unknown): broker_state 가 FILLED·CANCELLED·REJECTED 이고 현재 최종 상태와 같을 때만.
      UNKNOWN 해제(접수 확인 OPEN, 부분체결 PARTIAL)는 종료 증거가 아니다. broker_state 가 없는 구버전 감사도 쓰지 않는다.
    - 주문 대조의 최종 확인 기록(주문 details.final: 상태·확인 시각), 전량 체결 원장(cum ≥ 요청 수량), 우리 취소 확정·
      마감 소멸 알림(order:{id}:cancelled|expired), 전송 시점 거부·미전송(details.reject|not_sent → 전송 시각).
    - 다른 주문일 수 있는 이벤트 단위 확인 필요 알림은 쓰지 않는다.
    - 현재 상태와 다른 최종 상태를 입증하는 근거가 있으면 가장 이른 시각을 고르지 않고 final_evidence_conflict.
    시각은 시스템이 확인·반영한 시각이다 (증권사 실제 체결 시각과 다를 수 있다)."""
    st, times, conflicts = o["state"], [], []
    try:
        details = json.loads(o["details"] or "{}")
    except (TypeError, ValueError):
        details = {}
    for a in resolves:
        fin = FINAL_BROKER_STATES.get((a.get("details") or {}).get("broker_state"))
        if fin is not None:
            (times if fin == st else conflicts).append(a["at"])
    for f in details.get("final") or []:
        if isinstance(f, dict) and f.get("state"):
            (times if f["state"] == st else conflicts).append(f.get("at"))
    full = [r["applied_at"] for r in ledger_rows if r["cum_filled_qty"] >= o["requested_qty"]]
    (times if st == "FILLED" else conflicts).extend(full)
    if st == "CANCELLED":
        times += [a["created_at"] for a in ctx["order_alerts"]
                  if a["dedup_key"] in (f"order:{o['id']}:cancelled", f"order:{o['id']}:expired")]
    if (st == "REJECTED" and details.get("reject")) or (st == "NOT_SENT" and details.get("not_sent")):
        times.append(o["sent_at"] or o["created_at"])
    if conflicts:
        return None, "final_evidence_conflict"
    times = [t for t in times if t is not None]
    return (min(times), None) if times else (None, None)


def _watch_end(p, triggers, hist):
    """최초 영구 감시 종료 → (정확한 시각 | None, 상한 | None, 근거).

    - 정확한 시각(exact): recorded = 상태 전이와 함께 기록된 watch_ended_at. history = 이전 버전 포지션의 실제 이력 중
      ACTIVE 에서 벗어난 전이를 입증하는 것 — 첫 손절 트리거, LIVE 전환 감사(ACTIVE 만 가능), 확인 필요·외부 매도 알림,
      이전 상태가 ACTIVE 로 기록된 관리 해제 감사.
    - 상한(ended_by): 그 시각에는 이미 감시가 끝나 있었다는 근거일 뿐 종료 시각이 아니다 — watch_ended_by, 이전 상태가
      ACTIVE 가 아닌 관리 해제 감사의 prev_updated_at, 이전 상태가 없는 구버전 관리 해제 감사 시각, 현재 updated_at.
      후속 관리 해제 시각을 최초 종료 시각으로 쓰지 않는다.
    - unverified: 정확한 시각이 없다. 상한이 보고일 감시 시작 전이면 그날 감시 대상이 아님이 확실하다."""
    rec = watch_end_of(p)
    if rec is not None:
        return float(rec), None, "recorded"
    pid = str(p["id"])
    exact = list(triggers[:1])
    bounds = [p["updated_at"]]
    if watch_ended_by_of(p) is not None:
        bounds.append(float(watch_ended_by_of(p)))
    for a in hist["audits"]:
        if str(a["target"]) != pid:
            continue
        det = a.get("details") or {}
        if a["action"] == "promote" or det.get("prev_state") == "ACTIVE":
            exact.append(a["at"])
        elif det.get("prev_state"):
            bounds.append(det.get("prev_updated_at") or a["at"])
        else:
            bounds.append(a["at"])                 # 구버전 관리 해제 감사: 이전 상태 모름 → 상한만
    exact += [a["created_at"] for a in hist["alerts"]
              if a["dedup_key"].startswith(f"position:{pid}:review") or a["dedup_key"] == f"position:{pid}:external_closed"]
    exact = [t for t in exact if t >= p["created_at"]]
    if exact:
        return min(exact), None, "history"
    return None, min(bounds), "unverified"


def _linked_order(db, key, w, eid, ev, pos, since):
    """기록에 남은 order_id 의 호환 판정 → (주문 | None, 문제 | None).

    이전 버전은 첫 재주문 대기 기록의 since 를 주문 생성 뒤 시각으로 저장했다. 그래서 created_at < since 인 주문도
    아래가 모두 맞으면 그 대기 구간의 주문으로 인정한다 (시간 조건을 모든 주문에서 없애지 않는다).
      - 주문이 있고, 같은 청산 이벤트·모드·계좌·티커이며, 기록 종류와 맞는 주문 종류다
        (replace_waiting → REPLACE, order_block → ORIGINAL·CARRY_OVER 이며 기록의 kind 와 같음).
      - 그 주문 이후 대기 시작 전까지 같은 이벤트의 다른 주문이 없다 (과거 주문을 잘못 고르지 않는다).
    그 뒤에 생긴 주문은 created_at >= since 로 따로 조회되므로, 더 새 주문이 있으면 그 주문이 판정 대상이 된다."""
    oid = w.get("order_id")
    if oid is None:
        return None, None
    o = db.execute("SELECT * FROM auto_stop_orders WHERE id=?", (oid,)).fetchone()
    if o is None:
        return None, f"order_missing: order_id {oid} 주문 없음"
    if ev is None or o["exit_event_id"] != eid:
        return None, f"order_other_event: order_id {oid} 는 다른 청산 이벤트의 주문"
    if pos is None or o["mode"] != ev["mode"] or o["account_profile"] != pos["account_profile"] \
            or o["ticker"] != pos["ticker"]:
        return None, f"order_mismatch: order_id {oid} 모드·계좌·티커 불일치"
    kinds = ("REPLACE",) if key.startswith("replace_waiting:") else ("ORIGINAL", "CARRY_OVER")
    if o["kind"] not in kinds or (w.get("kind") and o["kind"] != w["kind"]):
        return None, f"order_kind_mismatch: order_id {oid} 종류 {o['kind']}"
    between = db.execute("SELECT COUNT(*) FROM auto_stop_orders WHERE exit_event_id=? AND id<>? AND created_at<? "
                         "AND (created_at>? OR (created_at=? AND id>?))",
                         (eid, oid, since, o["created_at"], o["created_at"], oid)).fetchone()[0]
    if between:
        return None, f"order_superseded: order_id {oid} 이후 대기 시작 전에 다른 주문 {between}건"
    return o, None


def record_watch_end(store, mode, p, at, rec=None):
    """감시가 끝나는 시점(관리 종료·손절 트리거·확인 필요 전환·외부 매도·장 마감)의 상태 근거를 포지션별로 남긴다.

    p 는 전환 직전 행이다 (ACTIVE/EXITING 일 때만 감시 대상). 현재 일시중지·종목 시세 상태가 at 까지의 감사 기록과
    맞으면 1, 아니면 0 — 같은 거래일에 여러 번 기록되면 작은 값(불일치)을 남긴다. 보고서 조회 경로에서는 부르지 않는다."""
    if p is None or p["state"] not in ("ACTIVE", "EXITING"):
        return None
    if rec is None:
        rec = (store.runtime_get(f"quote_health:{mode}") or {}).get(str(p["id"]))
    value = 0 if current_state_issues(store, mode, p, rec, at) else 1
    store.stat_min(ny_date(at).isoformat(), mode, "state_audit", str(p["id"]), value)
    return value


def current_state_issues(store, mode, p, rec, until):
    """현재 상태(포지션 auto_paused, 종목 시세 상태)가 until 까지의 감사 기록과 맞는지. 마감 처리에서 기록한다."""
    if p["state"] not in ("ACTIVE", "EXITING"):
        return []
    pauses = sorted((a["at"], a["action"] == "pause") for a in store.audits("pause", 0, until + 1)
                    + store.audits("resume", 0, until + 1) if str(a["target"]) == str(p["id"]))
    target = f"{mode}:{p['ticker']}"
    faults = sorted([(a["at"], a["action"] == "quote_fault")
                     for a in store.audits("quote_fault", 0, until + 1) + store.audits("quote_recovered", 0, until + 1)
                     if a["target"] == target and a["details"].get("position_id") in (None, p["id"])]
                    + [(a["at"], True) for a in store.audits("quote_health_migrate", 0, until + 1)
                       if target in (a["details"].get("marked_fault") or []) and p["created_at"] <= a["at"]])
    issues = []
    if bool(p["auto_paused"]) != (pauses[-1][1] if pauses else False):
        issues.append("state_unaudited: 일시중지 상태가 감사 기록과 맞지 않음")
    if rec and (rec.get("state", QH.OK) != QH.OK) != (faults[-1][1] if faults else False):
        issues.append("state_unaudited: 종목 장애 상태가 감사 기록과 맞지 않음")
    return issues


def _gap_view(pid, stats, ws, we, per_slot, interval, transitions, states, *, measurable=True):
    """최대 연속 감시 공백. 기준점 = 감시 시작(ws), 일반 감시 실제 성공 완료 시각들, 감시 종료(we).
    회복 조회는 성공에 넣지 않는다. 공백 안의 슬롯 분류·상태 전이로 원인을 붙이고, 종목 장애·전체 보류·일시중지가
    없던 공백을 '정상 상태 공백'으로 따로 센다. (Scheduler tick 간격·시세 신선도와는 별개 지표)"""
    key = f"{pid}:"
    succ = sorted(r["value"] for r in stats
                  if r["metric"] == "watch_ok_at" and r["key"].startswith(key) and ws <= r["value"] <= we)
    pts = [ws] + succ + [we]
    tol = QH.SLOT_TOLERANCE_SEC
    items, max_sec, max_normal = [], None, None
    over = normal_over = 0
    for a, b in zip(pts, pts[1:]):
        dur = round(b - a, 1)
        classes = {}
        for s, c in per_slot.items():
            if c != "ok" and s - tol < b and s + interval - tol > a:
                classes[c] = classes.get(c, 0) + 1
        trans = [x for x in transitions if a <= x["at"] <= b]
        overlap = {k: any(s0 < b and s1 > a for s0, s1 in states[k]) for k in ("paused", "fault", "hold")}
        # 정상 상태 공백: 공백 전 구간이 일시중지·종목 장애·전체 보류와 겹치지 않고, 보류·일시중지 슬롯도 없음.
        # 상태가 섞인 공백은 정상 상태로 보지 않는다. 상태 복원이 불확실하면 판정하지 않는다(None).
        normal = (not any(overlap.values()) and not classes.get("held") and not classes.get("paused")) \
            if measurable else None
        cause = ("global_hold" if overlap["hold"] else "symbol_fault" if overlap["fault"] or classes.get("held")
                 else "paused" if overlap["paused"] or classes.get("paused")
                 else "watch_fail" if classes.get("watch_fail") else "invalid" if classes.get("invalid")
                 else "schedule_missed" if classes.get("missed") else "interval")
        max_sec = dur if max_sec is None else max(max_sec, dur)
        if normal:
            max_normal = dur if max_normal is None else max(max_normal, dur)
        if dur > GAP_LIMIT_SEC:
            over += 1
            normal_over += 1 if normal else 0
            items.append({"start": a, "end": b, "duration": dur, "normal": normal, "cause": cause,
                          "classes": classes, "states": [k for k, v in overlap.items() if v], "transitions": trans})
    return {"measurable": measurable, "successes": len(succ), "first_success_at": succ[0] if succ else None,
            "last_success_at": succ[-1] if succ else None, "max_sec": max_sec,
            "max_normal_sec": max_normal if measurable else None, "over_120": over,
            "normal_over_120": normal_over if measurable else None, "state_at_start": states["at_start"],
            "items": items}


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


def _fmt(v, unit=""):
    return "측정 불가" if v is None else f"{v}{unit}"


def report_text(rep: dict) -> str:
    if not rep["trading_day"]:
        return f"[자동손절 일일 보고 {rep['day']} {rep['mode']}] 휴장일"
    w, o = rep["watch"], rep["orders"]
    r, hd = rep.get("recovery") or {}, rep.get("holds") or {}
    per = " · ".join(f"{p['ticker']}#{p['id']} {'측정 불가' if p['ok_checks'] is None else p['ok_checks']}/"
                     f"{_fmt(p['expected_checks'])}"
                     + (f" 최대공백 {p['gaps']['max_sec']}초" if p.get("gaps", {}).get("max_sec") is not None else "")
                     + (f" 장애 {p['health_state']}" if p.get("health_state") not in (None, "OK") else "")
                     for p in w.get("positions", []))
    g = w.get("gaps") or {}
    total_pos = rep.get("watched_positions", 0)
    if g.get("max_sec") is None:
        gap_text = " · 최대 감시 공백 측정 불가"
    else:
        gap_text = (f" · 최대 감시 공백 {g['max_sec']}초 (측정 {g.get('measured_positions', 0)}/{total_pos}종목"
                    + (f", 부분 측정 {g['partially_measured_positions']}종목" if g.get("partially_measured_positions")
                       else "") + ")"
                    + f" · 정상 상태 최대 공백 {_fmt(g.get('max_normal_sec'), '초')} (측정 "
                    f"{g.get('normal_measured_positions', 0)}/{total_pos}종목) · 120초 초과 {_fmt(g.get('over_120'), '건')}"
                    f" (정상 상태 {_fmt(g.get('normal_over_120'), '건')})")
    unmeasured = w.get("cause_unmeasured_missed") or 0
    sub = w.get("measured_subtotal") or {}
    if w.get("missed") is None:
        miss_text = ("누락 측정 불가"
                     + (f" (측정 가능 {sub.get('positions')}/{total_pos}종목 소계 정상 {sub.get('ok')}/"
                        f"{sub.get('expected')}, 누락 {sub.get('missed')})" if sub.get("positions") else ""))
    elif w.get("held_slots") is None:
        miss_text = f"누락 {w['missed']} (세부 원인 측정 불가)"
    else:
        miss_text = (f"누락 {w['missed']} = 보류 미실행 {w['held_slots']} + 일시중지 {w['paused_slots']} + 감시 실패 "
                     f"{w['failed_slots']} + 스케줄 지연 {w['schedule_missed']}"
                     + (f" + 원인 측정 불가 {unmeasured}" if unmeasured else ""))
    dq = rep.get("data_quality") or []
    return (f"[자동손절 일일 보고 {rep['day']} {rep['mode']}] 형식 {rep.get('format', '-')} · 감시 "
            f"{rep['watched_positions']}종목 · 정상 감시 {_fmt(w['ok'])}/{_fmt(w['expected'])}"
            + (" (일부 종목 측정 불가)" if w.get("ok_coverage") == "partial" else "")
            + (f" (데이터 무결성 오류 {len(w['integrity_errors'])}건, 원시 성공 기록 {_fmt(w.get('ok_recorded'))})"
               if w.get("ok_integrity") == "invalid" else "")
            + ("".join(f" ({t} 티커 일계 정상 {r['quote_ok']}회 귀속 불가)" for t, r in (rep.get("ticker_daily_raw") or {}).items()
                       if r.get("attribution") == "uncertain"))
            + f" ({miss_text}){gap_text} · 일반 감시 시도 {_fmt(w.get('attempts'))} "
            f"(API 오류 {_fmt(w.get('api_errors'))}, 무효 {_fmt(w.get('invalid'))}) · 회복 조회 {_fmt(r.get('ok'))}/"
            f"{_fmt(r.get('attempts'))} (실패 {_fmt(r.get('fail'))}, 무효 {_fmt(r.get('invalid'))}, 동일시세 "
            f"{_fmt(r.get('stale_repeat'))}) · 전체 보류 {int(hd.get('global_seconds', 0))}초"
            + (f" · 종목별 {per}" if per else "")
            + f" · tick {rep['ticks']['actual']}/"
            f"{rep['ticks']['expected']} (최대 간격 {_fmt(rep['ticks']['max_gap_sec'], '초')}) · API 오류 합계 {rep['api']['errors']} "
            f"(HTTP 요청 {_fmt(rep['api'].get('http_requests'))}, 429 {_fmt(rep['api'].get('http_429'))}) · 손절 {rep['triggers']} · "
            f"주문 {o['total']} (재주문 {o['by_kind']['REPLACE']}, "
            f"체결 {o['filled']}, 미체결 {o['partial_or_open']}) · 미해결 이벤트 {rep['unresolved_events']} · "
            f"알림 실패 {rep['alerts']['failed']}" + (f" · 데이터 품질 오류 {len(dq)}건" if dq else "")
            + _shadow_text(rep.get("shadow"))
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
