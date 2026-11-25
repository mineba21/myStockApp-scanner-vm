"""거래일 공통 입력 확정·일봉 수집·결측 복구 재계산 (P1-1·P1-3·P2-4.3).

거래일 D 의 입력은 46 계좌가 함께 쓰고 한 번 정하면 바꾸지 않는다.
- 결정 스캔: ``ab_day_inputs`` (정규 스캔으로 확정, 신호 마감 ``SIGNAL_CUTOFF_ET`` 까지 없으면 NO_DECISION_SCAN)
- 계좌 스냅샷(OPS): 장 마감 후 그날 처리 때 1회 조회해 같은 행에 저장 (OK / UNAVAILABLE / LATE)
- 일봉: ``ab_bars`` 에 공급자·조회 시각·입력 리비전과 함께 저장. 재실행·재계산은 저장된 일봉을 다시 읽는다.

일봉 확정 시각 (거래소 캘린더 기준, 조기폐장은 마감 시각에 맞춰 당겨진다)
- 마감 + ``BAR_MIN_DELAY_MIN`` 전: 잠정치로 보고 조회하지 않는다.
- 데이터 마감(정상일 23:20 ET = 마감 + 7시간 20분) 전: 일부라도 없으면 기다리며 다시 받는다.
- 데이터 마감 후에도 없거나 무효(날짜·OHLC 불일치)면 MISSING/INVALID 로 저장하고 그날을 DATA_INCOMPLETE 로 처리한다.
  결측을 '손절 없음'·정상으로 보지 않는다 (엔진: 주문 FILL_UNKNOWN, 포지션 UNRESOLVED).

결측 복구
- 최근 ``RECOVERY_WINDOW_DAYS`` 거래일(마지막 마감 세션 포함)의 결측 일봉을 job 마다 다시 받아 본다. 대상이 많으면
  job 마다 ``RECOVERY_MAX_DAYS_PER_RUN`` 일씩 순환한다. 하나라도 확인되면 새 리비전과 함께 ``replay_needed`` 를 같은
  트랜잭션에 저장하고, 새 run 을 만들어 시작일부터 46 계좌 전체를 시간순으로 다시 계산한다 (현금·Heat·포지션·수량·신규 판단 모두).
  replay 가 실패하면 현재 run 을 유지하고 ``replay_needed`` 도 남겨 다음 job 이 다시 시도한다.
  단일 손절 이벤트만 끼워 넣지 않는다. 이전 run 은 SUPERSEDED 로 남기고 결과 차이를 이벤트로 기록한다.
- 재계산 중(BUILDING)에는 현재 run 이 그대로 보이며 결과 화면에 재계산 중으로 표시된다.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from trading.auto_buy import settings as S
from trading.auto_buy.shadow import calendar as CAL
from trading.auto_buy.shadow import engine as EN
from trading.auto_buy.shadow.store import event, get_runtime, set_runtime, tx
from trading.auto_buy.shadow.vfills import Bar

logger = logging.getLogger(__name__)
DEFAULT_PROVIDER = "yfinance_daily_auto_adjust"


def _offset(hhmm: str) -> timedelta:
    hh, mm = (int(x) for x in hhmm.split(":"))
    return timedelta(hours=hh - 16, minutes=mm)        # 정규 마감 16:00 ET 기준 상대 시간


def signal_cutoff(day: date, settings=None) -> Optional[datetime]:
    c = CAL.close_at(day, settings)
    return c + _offset(S.SIGNAL_CUTOFF_ET) if c else None


def bars_cutoff(day: date, settings=None) -> Optional[datetime]:
    c = CAL.close_at(day, settings)
    return c + _offset(S.BARS_CUTOFF_ET) if c else None


def bars_earliest(day: date, settings=None) -> Optional[datetime]:
    c = CAL.close_at(day, settings)
    return c + timedelta(minutes=S.BAR_MIN_DELAY_MIN) if c else None


# ── 결정 스캔 ────────────────────────────────────────────────────────

def day_input(db, day: str):
    return db.execute("SELECT * FROM ab_day_inputs WHERE day=?", (day,)).fetchone()


def freeze_no_decision(db, day: str, reason: str, settings=None) -> None:
    from trading.auto_buy.version import logic_hash
    with tx(db):
        if day_input(db, day):
            return
        db.execute("INSERT INTO ab_day_inputs(day, decision_status, decision_scan_log_id, frozen_at, frozen_by, "
                   "signal_set_hash, strategy_version, config_hash, logic_hash) VALUES (?,?,NULL,?,?,NULL,?,?,?)",
                   (day, "NO_DECISION_SCAN", time.time(), "JOB_CUTOFF", S.STRATEGY_VERSION, S.config_hash(settings),
                    logic_hash()))
        event(db, "WARN", "no_decision_scan", f"{day} 정규 스캔이 신호 마감까지 완전 수집되지 않아 신규 매수 판단 없음",
              day=day, reason=reason)


def store_account(db, day: str, status: str, snap: Optional[dict], reason: Optional[str]) -> None:
    """그날 계좌 스냅샷을 1회 저장한다 (이미 있으면 그대로). 계좌번호·원 응답은 저장하지 않는다."""
    with tx(db):
        row = day_input(db, day)
        if row is None or row["account_status"] is not None:
            return
        keep = None
        if status == "OK" and snap:
            keep = json.dumps({k: snap.get(k) for k in ("equity", "cash", "holdings", "holdings_total",
                                                        "ledger_evaluation", "as_of", "source")},
                              ensure_ascii=False, default=str)
        db.execute("UPDATE ab_day_inputs SET account_status=?, account_reason=?, account_snapshot=?, "
                   "account_fetched_at=? WHERE day=?", (status, reason, keep, time.time(), day))


def account_from_input(row) -> Optional[dict]:
    if row is None or row["account_status"] != "OK" or not row["account_snapshot"]:
        return None
    return json.loads(row["account_snapshot"])


# ── 일봉 ────────────────────────────────────────────────────────────

def latest_bar_rows(db, day: str, tickers) -> dict:
    out = {}
    for t in tickers:
        r = db.execute("SELECT * FROM ab_bars WHERE day=? AND ticker=? ORDER BY input_rev DESC LIMIT 1",
                       (day, t)).fetchone()
        if r is not None:
            out[t] = r
    return out


def _rec(row) -> Optional[EN.BarRec]:
    if row is None or row["status"] != "OK":
        return None
    return EN.BarRec(Bar(row["open"], row["high"], row["low"], row["close"]), row["provider"], row["fetched_at"],
                     row["input_rev"])


def _store_bar(db, day: str, ticker: str, bar, provider: str, purpose: str, final_missing: bool) -> Optional[str]:
    """반환: 저장한 상태 (OK/INVALID/MISSING) 또는 None (저장 안 함: 아직 기다림)."""
    rev = db.execute("SELECT COALESCE(MAX(input_rev),0) FROM ab_bars WHERE day=? AND ticker=?",
                     (day, ticker)).fetchone()[0] + 1
    if bar is not None and isinstance(bar, Bar) and bar.valid():
        status, vals, detail = "OK", (bar.open, bar.high, bar.low, bar.close), None
    elif bar is not None:
        if not final_missing:
            return None
        status, vals, detail = "INVALID", (None, None, None, None), "OHLC 불일치 또는 비정상 값"
    else:
        if not final_missing:
            return None
        status, vals, detail = "MISSING", (None, None, None, None), "데이터 마감 후에도 일봉 없음"
    db.execute("INSERT INTO ab_bars(day, ticker, input_rev, status, open, high, low, close, provider, fetched_at, "
               "purpose, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
               (day, ticker, rev, status, *vals, provider, time.time(), purpose, detail))
    return status


def collect_bars(db, day: str, tickers: list, provider: Callable, *, final: bool, purpose: str = "DAILY",
                 provider_name: str = DEFAULT_PROVIDER) -> dict:
    """저장된 OK 일봉은 재사용하고, 없거나 결측인 종목만 다시 받는다.

    final=False(데이터 마감 전): 받은 OK 봉만 저장하고 결측은 저장하지 않는다(기다림).
    final=True: 결측·무효도 기록한다. 반환 {ticker: BarRec | None}, 'pending' 키에 기다리는 종목.
    """
    rows = latest_bar_rows(db, day, tickers)
    # 저장된 행(OK 또는 마감 후 확정된 결측)은 재사용한다. 결측 재조회는 recover_missing 이 맡는다.
    need = [t for t in tickers if t not in rows]
    fetched = provider(need, day) if need else {}
    pending = []
    with tx(db):
        for t in need:
            st = _store_bar(db, day, t, fetched.get(t), provider_name, purpose, final)
            if st is None:
                pending.append(t)
    rows = latest_bar_rows(db, day, tickers)
    out = {t: _rec(rows.get(t)) for t in tickers}
    return {"bars": out, "pending": pending}


# ── 하루 입력 구성 ─────────────────────────────────────────────────────

def required_tickers(db, run_id: int, day: str) -> list:
    return sorted({r[0] for r in db.execute(
        "SELECT ticker FROM ab_orders WHERE run_id=? AND state IN ('PENDING','PARTIAL') AND target_day=? "
        "UNION SELECT ticker FROM ab_positions WHERE run_id=? AND state='OPEN' AND price_status='OK'",
        (run_id, day, run_id))})


def build_inputs(db, run_id: int, day: str, bars: dict, settings=None) -> EN.DayInputs:
    row = day_input(db, day)
    scan_id = row["decision_scan_log_id"] if row else None
    pending = EN.eligibility_pending(db, scan_id)
    if pending:          # 적격 기록 전 후보를 읽으면 후보 0건으로 그날이 확정된다 — 처리하지 않는다
        raise RuntimeError(f"{day} 결정 스캔 #{scan_id} 적격 판정 미기록 {pending}건")
    cands = EN.candidates(db, scan_id)
    nxt = CAL.next_trading_day(date.fromisoformat(day), settings)
    notes = []
    if row is None or row["decision_status"] != "FROZEN":
        notes.append("NO_DECISION_SCAN")
    missing = [t for t, b in bars.items() if b is None]
    if missing:
        notes.append(f"BARS_MISSING:{len(missing)}")
    if row is not None and row["account_status"] and row["account_status"] != "OK":
        notes.append(f"ACCOUNT_{row['account_status']}")
    return EN.DayInputs(day=day, decision_status=row["decision_status"] if row else "NO_DECISION_SCAN",
                        decision_scan_log_id=row["decision_scan_log_id"] if row else None,
                        required=sorted(bars), bars=bars, cands=cands, acct=account_from_input(row),
                        account_status=row["account_status"] if row else None,
                        next_day=nxt.isoformat() if nxt else None, notes=notes)


def process_day(db, run_id: int, inp: EN.DayInputs, settings=None) -> dict:
    for acc_id, *_ in S.account_ids():
        EN.process_account_day(db, run_id, acc_id, inp, settings)
    status = "DATA_INCOMPLETE" if inp.missing else "COMPLETE"
    with tx(db):
        db.execute("INSERT OR IGNORE INTO ab_days(run_id, day, status, decision_status, candidates, bars_requested, "
                   "bars_missing, account_status, notes, processed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (run_id, inp.day, status, inp.decision_status, len(inp.cands.get("B00", [])), len(inp.required),
                    len(inp.missing), inp.account_status, EN._j(inp.notes), time.time()))
    return {"day": inp.day, "status": status, "missing": inp.missing}


# ── 결측 복구·재계산 ───────────────────────────────────────────────────

def recover_missing(db, provider: Callable, now: datetime, settings=None) -> list:
    """복구 기간 안의 결측 일봉을 다시 받는다. 반환: 새로 확인된 (day, ticker) 목록."""
    last = CAL.last_closed_session(now, settings)
    if last is None:
        return []
    floor = recovery_floor(last, settings)
    _flag_manual_review(db, expired_missing_bars(db, floor))
    rows = db.execute(
        "SELECT b.day, b.ticker FROM ab_bars b WHERE b.status IN ('MISSING','INVALID') AND b.day>=? "
        "AND b.input_rev = (SELECT MAX(input_rev) FROM ab_bars x WHERE x.day=b.day AND x.ticker=b.ticker) "
        "ORDER BY b.day", (floor.isoformat(),)).fetchall()
    by_day: dict = {}
    for d, t in rows:
        by_day.setdefault(d, []).append(t)
    recovered = []
    for d in recovery_targets(db, sorted(by_day)):
        got = provider(by_day[d], d)
        with tx(db):
            found = []
            for t in by_day[d]:
                b = got.get(t)
                if isinstance(b, Bar) and b.valid():
                    _store_bar(db, d, t, b, DEFAULT_PROVIDER, "RECOVERY", True)
                    found.append((d, t))
            set_runtime(db, "recovery_cursor", d)
            if found:
                # 새 OK 리비전과 같은 트랜잭션에 '재계산 필요'를 남긴다. replay 가 run 교체를 끝낼 때만 지운다.
                _mark_replay_needed(db, found)
            recovered += found
    if recovered:
        with tx(db):
            event(db, "INFO", "bars_recovered", f"결측 일봉 {len(recovered)}건 확인 — 시작일부터 재계산",
                  recovered=recovered[:50])
    return recovered


def recovery_targets(db, days: list) -> list:
    """이번 job 에 재조회할 결측 거래일 (최대 ``RECOVERY_MAX_DAYS_PER_RUN``). 지난 job 이 마지막으로 본 날 다음부터
    순환하므로, 앞쪽 결측이 계속 남아도 뒤쪽 날짜가 밀려나지 않는다."""
    cur = get_runtime(db, "recovery_cursor")
    order = days if not cur else [d for d in days if d > cur] + [d for d in days if d <= cur]
    return order[:S.RECOVERY_MAX_DAYS_PER_RUN]


def _mark_replay_needed(db, found: list) -> None:
    prev = get_runtime(db, "replay_needed") or {}
    bars = sorted(set(map(tuple, prev.get("bars") or [])) | set(found))
    set_runtime(db, "replay_needed", {"reason": "BARS_RECOVERED", "since": prev.get("since") or time.time(),
                                      "bars": bars[-200:]})


def replay_needed(db) -> bool:
    """복구된 일봉이 아직 현재 run 에 반영되지 않았는지 (이전 replay 실패 포함)."""
    return bool(get_runtime(db, "replay_needed"))


def recovery_floor(last, settings):
    """자동 복구 대상 기간의 첫 거래일. 이보다 이른 결측은 더 이상 재조회하지 않는다 (수동 검토 대상)."""
    if last is None:
        return None
    floor = last
    for _ in range(S.RECOVERY_WINDOW_DAYS - 1):          # last 포함 정확히 RECOVERY_WINDOW_DAYS 개 세션
        p = CAL.prev_trading_day(floor, settings)
        if p is None:
            break
        floor = p
    return floor


def expired_missing_bars(db, floor) -> list:
    """복구 기간이 지난 미복구 일봉 (최신 리비전이 MISSING·INVALID). 삭제·정상 처리 없이 수동 검토 대상으로 보인다."""
    if floor is None:
        return []
    return [{"day": r[0], "ticker": r[1], "status": r[2]} for r in db.execute(
        "SELECT b.day, b.ticker, b.status FROM ab_bars b WHERE b.status IN ('MISSING','INVALID') AND b.day<? "
        "AND b.input_rev = (SELECT MAX(input_rev) FROM ab_bars x WHERE x.day=b.day AND x.ticker=b.ticker) "
        "ORDER BY b.day, b.ticker", (floor.isoformat(),))]


def _flag_manual_review(db, expired: list) -> None:
    """복구 기간이 지난 결측을 한 번씩 이벤트로 남긴다. 상태(불완전·위험·차단)는 바꾸지 않는다."""
    seen = set(map(tuple, get_runtime(db, "manual_review_flagged") or []))
    new = [(b["day"], b["ticker"]) for b in expired if (b["day"], b["ticker"]) not in seen]
    if not new:
        return
    with tx(db):
        event(db, "WARN", "MANUAL_REVIEW_REQUIRED",
              f"자동 복구 기간({S.RECOVERY_WINDOW_DAYS}거래일)이 지난 결측 일봉 {len(new)}건 — 불완전·위험 유지, 수동 검토 필요",
              bars=new[:50])
        set_runtime(db, "manual_review_flagged", sorted(seen | set(new)))


def replay(db, provider: Callable, reason: str, settings=None) -> dict:
    """현재 run 이 처리한 거래일 전체를 저장된 입력으로 새 run 에서 다시 계산하고 원자적으로 교체한다."""
    parent = EN.current_run(db)
    days = [r[0] for r in db.execute("SELECT day FROM ab_days WHERE run_id=? ORDER BY day", (parent,))]
    with tx(db):
        run = EN.create_run(db, reason, parent, "BUILDING", settings)
    set_runtime_safe(db, "replay_in_progress", {"run_id": run, "reason": reason, "at": time.time()})
    try:
        for d in days:
            req = required_tickers(db, run, d)
            have = latest_bar_rows(db, d, req)
            absent = [t for t in req if t not in have]
            if absent:                           # 원래 run 에 없던 종목(복구로 생긴 새 포지션 등)은 이때 받는다
                collect_bars(db, d, absent, provider, final=True, purpose="REPLAY")
            rows = latest_bar_rows(db, d, req)
            bars = {t: _rec(rows.get(t)) for t in req}
            process_day(db, run, build_inputs(db, run, d, bars, settings), settings)
        diff = _diff(db, parent, run)
        with tx(db):
            db.execute("UPDATE ab_runs SET status='SUPERSEDED' WHERE run_id=?", (parent,))
            db.execute("UPDATE ab_runs SET status='CURRENT', completed_at=?, through_day=?, summary=? WHERE run_id=?",
                       (time.time(), days[-1] if days else None, EN._j(diff), run))
            set_runtime(db, "current_run", run)
            set_runtime(db, "replay_in_progress", None)
            set_runtime(db, "replay_needed", None)          # 교체가 끝났을 때만 재계산 필요 상태를 지운다
            event(db, "WARN", "run_replaced", f"재계산 run #{run} 이 #{parent} 를 대체 ({reason}). "
                  f"결과가 달라진 계좌 {diff['changed_accounts']}개", parent=parent, run=run, **diff)
        return {"run": run, "parent": parent, **diff}
    except Exception as exc:
        with tx(db):
            db.execute("UPDATE ab_runs SET status='FAILED', completed_at=? WHERE run_id=?", (time.time(), run))
            set_runtime(db, "replay_in_progress", None)
            event(db, "ERROR", "replay_failed", f"재계산 run #{run} 실패: {type(exc).__name__}", run=run)
        raise


def set_runtime_safe(db, key, value):
    with tx(db):
        set_runtime(db, key, value)


def _diff(db, old: int, new: int) -> dict:
    q = ("SELECT account_id, day, equity, cash, n_positions FROM ab_daily WHERE run_id=? "
         "AND day=(SELECT MAX(day) FROM ab_daily WHERE run_id=?)")
    a = {r[0]: tuple(r[2:]) for r in db.execute(q, (old, old))}
    b = {r[0]: tuple(r[2:]) for r in db.execute(q, (new, new))}
    changed = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    first = db.execute(
        "SELECT MIN(n.day) FROM ab_daily n LEFT JOIN ab_daily o ON o.run_id=? AND o.account_id=n.account_id "
        "AND o.day=n.day WHERE n.run_id=? AND (o.day IS NULL OR o.equity IS NOT n.equity OR o.cash IS NOT n.cash)",
        (old, new)).fetchone()[0]
    stops = lambda r: db.execute("SELECT COUNT(*) FROM ab_stop_events WHERE run_id=?", (r,)).fetchone()[0]
    return {"changed_accounts": len(changed), "changed_sample": changed[:10], "first_divergent_day": first,
            "stops_before": stops(old), "stops_after": stops(new)}
