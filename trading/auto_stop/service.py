"""등록·일시중지·해제·LIVE 전환·증거 기반 UNKNOWN 해제·헬스. 웹 API 와 테스트가 공유한다.

여기서는 주문을 전송하지 않는다. 증권사 조회는 reader (LiveStopSellBroker 조회 전용 또는 대역)로만 한다.
"""
from __future__ import annotations

import json
import math
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from trading.auto_stop import quote_health as QH
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
    pause_audits = store.audits("pause", day_start, day_start + 86400) + \
        store.audits("resume", day_start, day_start + 86400)
    hold_periods = _hold_periods(store, start, end)["periods"]
    interval = int(settings.price_interval_sec)

    def pstat(metric, pid):
        return int(stat("pos_" + metric, str(pid)))

    # 보고서 데이터 형식: LEGACY(티커별 일계 집계만) / SLOT_V2(position_id 슬롯·성공 시각) / MIXED(같은 날 공존).
    v2_since = next((r["value"] for r in stats if r["metric"] == "slot_v2_since"), None)
    tol = settings.tick_sec + QH.SLOT_TOLERANCE_SEC
    is_today = session.open_ts <= now <= session.close_ts + 6 * 3600
    cands = []
    for p in store.positions(mode=mode):
        if p["created_at"] >= end:
            continue
        p_events = [e for e in store.events(mode=mode) if e["position_id"] == p["id"]]
        stop_at = min([e["triggered_at"] for e in p_events] + [end])
        if p["state"] not in ("ACTIVE", "EXITING", "NEEDS_REVIEW") and not p_events:
            stop_at = min(stop_at, p["updated_at"])
        ws = max(start, p["created_at"])
        if max(0.0, stop_at - ws) <= 0 and not p_events:
            continue
        fmt = ("LEGACY" if v2_since is None or v2_since > stop_at
               else "SLOT_V2" if v2_since <= ws + tol else "MIXED")
        cands.append((p, ws, stop_at, fmt))
    legacy_share = {}
    for p, _, _, fmt in cands:
        if fmt != "SLOT_V2":
            legacy_share[p["ticker"]] = legacy_share.get(p["ticker"], 0) + 1
    legacy_ticker_totals = {}

    for p, ws, stop_at, fmt in cands:
        t, pid = p["ticker"], p["id"]
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
            states, sq = _state_intervals(store, mode, p, measured_from, stop_at, rec, hold_periods,
                                          check_current=is_today)
            quality += sq
            gaps = _gap_view(pid, stats, measured_from, stop_at, per_slot, interval, transitions, states,
                             measurable=not sq)
            gaps["measured_from"], gaps["partial"] = measured_from, fmt == "MIXED"
        else:
            gaps = {"measurable": False, "reason": "LEGACY: 이 거래일은 성공 시각을 기록하지 않았다", "successes": None,
                    "first_success_at": None, "last_success_at": None, "max_sec": None, "max_normal_sec": None,
                    "over_120": None, "normal_over_120": None, "items": [], "measured_from": None, "partial": False}
        if fmt == "SLOT_V2":
            ok, expected = slots["ok"], slots["expected"]
            breakdown = {"held_slots": slots["held"], "paused_slots": slots["paused"],
                         "failed_slots": slots["watch_fail"] + slots["invalid"], "schedule_missed": slots["missed"]}
        else:
            # 기존 일계 집계 기준 (구간 ÷ 감시 주기, 티커별 정상 감시 수). 같은 날 같은 티커가 여럿이면 포지션별로
            # 나눌 수 없으므로 임의 배분하지 않고 티커 합계로만 보인다.
            expected = int(max(0.0, stop_at - ws) // max(1, interval))
            legacy_ok = int(stat("quote_ok", t))
            ok = None if legacy_share.get(t, 0) > 1 else legacy_ok
            if ok is None:
                agg = legacy_ticker_totals.setdefault(t, {"ok": legacy_ok, "expected": 0, "positions": []})
                agg["expected"] += expected
                agg["positions"].append(pid)
                quality.append("legacy_ticker_shared: 같은 티커 포지션이 여럿이라 티커 집계를 포지션별로 나누지 않음")
            breakdown = {"held_slots": None, "paused_slots": None, "failed_slots": None, "schedule_missed": None}
            if fmt == "MIXED":
                quality.append(f"mixed_format: 슬롯·공백은 {measured_from:.0f} 이후만 측정 (정상·기대는 일계 집계 기준)")
        if ok is not None:
            total_expected += expected
            total_ok += ok
        recovery = {"attempts": pstat("attempt_recovery", pid), "ok": pstat("recovery_ok", pid),
                    "fail": pstat("recovery_fail", pid), "invalid": pstat("recovery_invalid", pid),
                    "stale_repeat": pstat("recovery_stale", pid)}
        positions.append({
            "id": pid, "ticker": t, "state": p["state"], "format": fmt, "window": {"start": ws, "end": stop_at},
            # 감시 슬롯 (거래일 + 모드 + position_id + 슬롯 시작). 완료 슬롯마다 분류 하나, 진행 중 슬롯은 따로.
            "slots": slots, "expected_checks": expected, "ok_checks": ok,
            "missed_checks": None if ok is None else expected - ok, **breakdown,
            "normal_rate_pct": round(ok / expected * 100, 1) if (expected and ok is not None) else None,
            "data_quality": quality,
            # 조회 시도 (목적별, 같은 슬롯의 재시도 포함. HTTP 요청 수와 다를 수 있다)
            "attempts": {k: pstat(f"attempt_{k}", pid) for k in ("watch", "recovery", "order", "cancel_check",
                                                                  "incident")},
            "watch_attempts": pstat("attempt_watch", pid),
            "quote_fail": pstat("watch_fail", pid) if fmt == "SLOT_V2" else int(stat("quote_fail", t)),
            "quote_invalid": pstat("watch_invalid", pid) if fmt == "SLOT_V2" else int(stat("quote_invalid", t)),
            "recovery": recovery,
            "order_quote": {"ok": pstat("order_ok", pid), "fail": pstat("order_fail", pid),
                            "invalid": pstat("order_invalid", pid), "held": int(stat("order_held_symbol", t))},
            "held_triggers": pstat("held_trigger", pid), "deferred": pstat("deferred", pid),
            "gaps": gaps,
            "max_quote_age_sec": maxstat("max_quote_age_sec", t),
            "last_quote_at": p["last_quote_at"], "last_check_at": p["last_price_check_at"],
            "health_state": rec.get("state"), "fault_started_at": rec.get("fault_started_at"),
            "recovered_at": rec.get("recovered_at"), "faults": faults,
            "first_error": rec.get("first_error") if (rec.get("first_error") or {}).get("day") == day else None})
    for agg in legacy_ticker_totals.values():
        total_expected += agg["expected"]
        total_ok += agg["ok"]
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
    sum_pos = lambda key: sum(int(p[key] or 0) for p in positions)  # noqa: E731
    recovery_tot = {k: sum(int(p["recovery"][k]) for p in positions)
                    for k in ("attempts", "ok", "fail", "invalid", "stale_repeat")}
    slot_tot = {c: sum(p["slots"][c] for p in positions if p["slots"]) for c in QH.SLOT_CLASSES + ("expected",)}
    measured = [p["gaps"] for p in positions if p["gaps"].get("max_sec") is not None]
    normal_gaps = [g["max_normal_sec"] for g in measured if g["max_normal_sec"] is not None]
    gap_tot = {"max_sec": max([g["max_sec"] for g in measured], default=None),
               "max_normal_sec": max(normal_gaps, default=None),
               "over_120": sum(g["over_120"] or 0 for g in measured),
               "normal_over_120": sum(g["normal_over_120"] or 0 for g in measured),
               # full: 모든 포지션 전 구간 측정 / partial: 일부(혼합 형식·측정 불가 포함) / none: 측정 불가(LEGACY)
               "coverage": ("none" if not measured else
                            "full" if all(p["format"] == "SLOT_V2" and p["gaps"].get("measurable") for p in positions)
                            else "partial")}
    quality = [f"{p['ticker']}#{p['id']}: {q}" for p in positions for q in p["data_quality"]]
    heartbeat = store.runtime_get("heartbeat_at")
    unresolved_orders = store.orders(mode=mode, states=UNRESOLVED)
    return {
        **base,
        "session": {"open_at": start, "close_at": session.close_ts, "early_close": session.early_close},
        "watched_positions": len(positions),
        "data_quality": quality,
        "format": report_format, "slot_v2_since": v2_since,
        "watch": {"expected": total_expected, "ok": total_ok, "missed": total_expected - total_ok,
                  "legacy_ticker_totals": legacy_ticker_totals,
                  "slots": slot_tot, "gaps": gap_tot,
                  "held_slots": sum_pos("held_slots"), "paused_slots": sum_pos("paused_slots"),
                  "failed_slots": sum_pos("failed_slots"),
                  "schedule_missed": sum_pos("schedule_missed"), "attempts": sum_pos("watch_attempts"),
                  "api_errors": sum_pos("quote_fail"), "invalid": sum_pos("quote_invalid"),
                  "normal_rate_pct": round(total_ok / total_expected * 100, 1) if total_expected else None,
                  "positions": positions},
        # 일반 감시와 따로 센다. 회복 확정에 쓴 시세는 정상 감시 성공(watch.ok)에 넣지 않는다.
        "recovery": recovery_tot,
        "holds": {**holds, "held_triggers": sum(p["held_triggers"] for p in positions)},
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
    quality = []
    if check_current and p["state"] in ("ACTIVE", "EXITING"):
        last_paused = sorted(pauses)[-1][1] if pauses else False
        if bool(p["auto_paused"]) != last_paused:
            quality.append("state_unaudited: 일시중지 상태가 감사 기록과 맞지 않아 정상 상태 공백을 측정할 수 없음")
        last_fault = sorted(faults)[-1][1] if faults else False
        if rec and (rec.get("state", QH.OK) != QH.OK) != last_fault:
            quality.append("state_unaudited: 종목 장애 상태가 감사 기록과 맞지 않아 정상 상태 공백을 측정할 수 없음")
    return {"paused": paused_iv, "fault": fault_iv, "hold": hold_iv,
            "at_start": {"paused": paused0, "fault": fault0,
                         "global_hold": any(a <= ws < b for a, b in hold_iv)}}, quality


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


def report_text(rep: dict) -> str:
    if not rep["trading_day"]:
        return f"[자동손절 일일 보고 {rep['day']} {rep['mode']}] 휴장일"
    w, o = rep["watch"], rep["orders"]
    r, hd = rep.get("recovery") or {}, rep.get("holds") or {}
    per = " · ".join(f"{p['ticker']}#{p['id']} {'-' if p['ok_checks'] is None else p['ok_checks']}/{p['expected_checks']}"
                     + (f" 최대공백 {p['gaps']['max_sec']}초" if p.get("gaps", {}).get("max_sec") is not None else "")
                     + (f" 장애 {p['health_state']}" if p.get("health_state") not in (None, "OK") else "")
                     for p in w.get("positions", []))
    g = w.get("gaps") or {}
    gap_text = (f" · 최대 감시 공백 {g.get('max_sec')}초 (정상 상태 {g.get('max_normal_sec')}초, 120초 초과 "
                f"{g.get('over_120', 0)}건 중 정상 상태 {g.get('normal_over_120', 0)}건)" if g else "")
    dq = rep.get("data_quality") or []
    return (f"[자동손절 일일 보고 {rep['day']} {rep['mode']}] 감시 {rep['watched_positions']}종목 · "
            f"정상 감시 {w['ok']}/{w['expected']} (누락 {w['missed']} = 보류 미실행 {w.get('held_slots', 0)} + 일시중지 "
            f"{w.get('paused_slots', 0)} + 감시 실패 {w.get('failed_slots', 0)} + 스케줄 지연 "
            f"{w.get('schedule_missed', 0)}){gap_text} · 일반 감시 시도 {w.get('attempts', 0)} "
            f"(API 오류 {w.get('api_errors', 0)}, 무효 {w.get('invalid', 0)}) · 회복 조회 {r.get('ok', 0)}/"
            f"{r.get('attempts', 0)} (실패 {r.get('fail', 0)}, 무효 {r.get('invalid', 0)}, 동일시세 "
            f"{r.get('stale_repeat', 0)}) · 전체 보류 {int(hd.get('global_seconds', 0))}초"
            + (f" · 종목별 {per}" if per else "")
            + f" · tick {rep['ticks']['actual']}/"
            f"{rep['ticks']['expected']} (최대 간격 {rep['ticks']['max_gap_sec']}초) · API 오류 합계 {rep['api']['errors']} "
            f"(HTTP 요청 {rep['api'].get('http_requests', 0)}, 429 {rep['api']['http_429']}) · 손절 {rep['triggers']} · "
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
