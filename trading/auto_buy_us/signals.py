"""결정 스캔 확인과 신호 수집 (설계 §8.3).

운영 DB 는 SELECT 만 하고(세션을 닫는다) 저널에는 abu_signal_days + abu_signals 를 한 트랜잭션으로 쓴다.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timedelta, timezone

from trading.auto_buy_us.settings import STRATEGY_VERSION
from trading.auto_buy_us.store import Store
from trading.auto_stop.us_calendar import NEW_YORK, session_for

US_MARKETS = ("US", "ALL")
COLLECTION_START = "collection_start"


def signal_key(ticker, signal_type, signal_date) -> str:
    return f"US:{ticker}:{signal_type}:{signal_date}"


def _utc(dt):
    """운영 DB 의 naive UTC → aware UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _cutoff(day: date, settings) -> datetime:
    hh, mm = (int(x) for x in settings.signal_cutoff_et.split(":"))
    return datetime.combine(day, time(hh, mm), NEW_YORK)


def _session(day: date, settings):
    return session_for(day, extra_closed=settings.extra_closed_dates, extra_early=settings.extra_early_close_dates)


def _raw(row) -> dict:
    out = {}
    for c in row.__table__.columns:
        v = getattr(row, c.name)
        out[c.name] = v.isoformat() if isinstance(v, datetime) else v
    return out


def _existing(db, day: date):
    r = db.execute("SELECT status FROM abu_signal_days WHERE session_day=?", (day.isoformat(),)).fetchone()
    return r["status"] if r else None


def _read_operational(session_factory, day, now, settings):
    """운영 DB 조회(읽기 전용). (status, decision_log_id, rows, decision_started_ts) — rows 는 신호 dict 목록."""
    sess = _session(day, settings)
    if sess is None:
        raise ValueError(f"거래일이 아닙니다: {day}")
    cutoff_ts = _cutoff(day, settings).timestamp()
    from database.models import ScanLog, ScanResult

    s = session_factory()
    try:
        logs = (s.query(ScanLog).filter(ScanLog.market.in_(US_MARKETS))
                .order_by(ScanLog.started_at, ScanLog.id).all())
        decision = None
        for lg in logs:
            if (lg.triggered_by == "scheduler" and lg.status == "DONE" and lg.started_at and lg.finished_at
                    and _utc(lg.started_at).timestamp() >= sess.close_ts
                    and _utc(lg.finished_at).timestamp() <= cutoff_ts):
                decision = lg
                break
        if decision is None:
            return ("NO_SIGNAL_SCAN" if now >= cutoff_ts else "PENDING"), None, [], None
        for lg in logs:
            if lg.id != decision.id and lg.started_at and lg.started_at > decision.started_at \
                    and _utc(lg.started_at).timestamp() <= now:
                return "MISSED", decision.id, [], None
        res = (s.query(ScanResult).filter(ScanResult.market == "US",
                                          ScanResult.scan_time >= decision.started_at,
                                          ScanResult.scan_time <= decision.finished_at)
               .order_by(ScanResult.id).all())
        rows = []
        for r in res:
            rows.append({
                "id": r.id, "ticker": r.ticker, "name": r.name, "signal_type": r.signal_type,
                "signal_date": r.signal_date, "grade": r.grade, "signal_quality": r.signal_quality,
                "price": r.price, "pivot": r.pivot_price, "stop_loss": r.stop_loss,
                "market_condition": r.market_condition,
                "strict": None if r.strict_filter_passed is None else int(bool(r.strict_filter_passed)),
                "first_detected_at": _utc(r.first_detected_at),
                "sector_name": r.sector_name, "entry_warnings": r.entry_warnings,
                "raw_json": json.dumps(_raw(r), ensure_ascii=False, sort_keys=True, default=str),
            })
        return "CAPTURED", decision.id, rows, _utc(decision.started_at).timestamp()
    finally:
        s.close()


def _ny_date(dt_utc: datetime) -> date:
    return dt_utc.astimezone(NEW_YORK).date()


def _first_pass(db, row, key, day, is_first, collection_start, settings):
    """(first_strict_pass, basis)."""
    if row["strict"] == 1:
        prior = db.execute("SELECT 1 FROM abu_signals WHERE signal_key=? AND strict=1 AND session_day<? LIMIT 1",
                           (key, day.isoformat())).fetchone()
        if prior:
            return 0, "PRIOR_STRICT"
    if is_first:
        return 1, "FIRST_DETECTION"
    fd = row["first_detected_at"]
    if fd is None or (collection_start is not None and fd.timestamp() < collection_start):
        return None, "BEFORE_COLLECTION"
    d = _ny_date(fd) + timedelta(days=1)
    while d < day:
        if _session(d, settings) is not None and _existing(db, d) != "CAPTURED":
            return None, "CAPTURE_GAP"
        d += timedelta(days=1)
    return 1, "NO_PRIOR_STRICT"


def capture_day(store: Store, session_factory, day: date, now: float, settings) -> str:
    with store.read() as db:
        st = _existing(db, day)
    if st:
        return st
    status, log_id, rows, started_ts = _read_operational(session_factory, day, now, settings)
    if status == "PENDING":
        return status
    with store.tx() as db:
        st = _existing(db, day)
        if st:
            return st
        db.execute("INSERT INTO abu_signal_days(session_day,status,scan_log_id,captured_at) VALUES(?,?,?,?)",
                   (day.isoformat(), status, log_id, now))
        if status != "CAPTURED":
            return status
        start = Store.get_runtime(db, COLLECTION_START)
        if start is None:
            # 수집 시작 = 첫 결정 스캔 시작 시각 (now 를 쓰면 첫날 스캔의 최초 탐지가 수집 이전으로 오판된다)
            start = min(now, started_ts)
            Store.set_runtime(db, COLLECTION_START, start)
        seen = set()
        for r in rows:
            if r["ticker"] in seen:  # UNIQUE(scan_log_id, ticker): 같은 종목의 첫 행만
                continue
            seen.add(r["ticker"])
            key = signal_key(r["ticker"], r["signal_type"], r["signal_date"])
            fd = r["first_detected_at"]
            is_first = 1 if fd is not None and _ny_date(fd) == day else 0
            fsp, basis = _first_pass(db, r, key, day, is_first, start, settings)
            price, pivot, stop = _num(r["price"]), _num(r["pivot"]), _num(r["stop_loss"])
            ext = (price / pivot - 1) * 100 if price is not None and pivot else None
            stop_pct = (1 - stop / price) * 100 if price and stop is not None else None
            db.execute(
                "INSERT INTO abu_signals(session_day,scan_log_id,scan_result_id,ticker,name,signal_type,signal_date,"
                "signal_key,grade,signal_quality,price,pivot,stop_loss,market_condition,strict,first_detected_at,"
                "is_first_detection,first_strict_pass,first_pass_basis,ext_pct,stop_pct,sector_name,entry_warnings,"
                "raw_json,strategy_version,captured_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (day.isoformat(), log_id, r["id"], r["ticker"], r["name"], r["signal_type"], r["signal_date"],
                 key, r["grade"], r["signal_quality"], price, pivot, stop, r["market_condition"], r["strict"],
                 fd.isoformat() if fd else None, is_first, fsp, basis, ext, stop_pct, r["sector_name"],
                 r["entry_warnings"], r["raw_json"], STRATEGY_VERSION, now))
        return status
