"""운영 Scanner 신호의 불변 스냅샷 (S1, 별도 SHADOW SQLite).

운영 DB 는 **읽기만** 한다 (스키마·행 변경 없음). 운영 ``scan_engine._save`` 는 같은 신호
(ticker·signal_date·signal_type) 행을 재스캔 때 덮어쓰므로, 스캔 직후의 값을 이 저널에 복사해 둔다.
**운영 DB outbox 없이 별도 DB 만으로는 완전 수집을 보장하지 못한다.** 누락 가능성은 기록·표시하고,
확정할 수 없는 신호는 매수 판단에서 뺀다.

수집 경로
1. HOOK: 스캔 DONE 커밋 직후, 같은 스캔 실행권 안에서 ``scan_time ∈ [started_at, finished_at]`` 인 US 행을 복사한다.
2. CATCHUP: HOOK 이 실패·누락한 DONE·ERROR 스캔, 그리고 비정상 종료로 RUNNING 에 남은 스캔(ABANDONED)을 job 이 보충한다.
   그 사이 다음 스캔이 덮어쓴 행은 복구할 수 없으므로 ``complete=0`` · ``recoverability`` 로 남긴다.

당일 후보 확정 (P1-1)
- 정규 Scanner(``triggered_by='scheduler'``) 의 DONE·완전 수집·장 마감 후 스캔 1회로 그 거래일 후보를 확정한다
  (``ab_day_inputs`` FROZEN). 확정 이후 같은 거래일의 다른 스캔(수동 재스캔 등)은 ``late_after_decision=1`` 로 저장하고
  ``LATE_SCAN_AFTER_DECISION`` 이벤트를 남긴다. 당일 후보에 추가하거나 확정된 후보를 바꾸지 않는다.

최초 Strict 통과 판정 (거래일 구간 기준, P7 의 일 단위 '최초 등장'과 같다)
- 이전 거래일 구간의 장 마감 후 스냅샷에 같은 signal_key 의 Strict 통과가 있으면 이미 통과(0).
  그 통과가 그날의 결정 스캔이 아니었으면 ``PRIOR_STRICT_NOT_DECIDED`` 로 구분한다 (조용히 사라지지 않게).
- 최초 탐지가 이 거래일 구간이면 확정(1). 그 밖에는 수집 이력이 최초 탐지 이후 완전할 때만 확정, 아니면 NULL(불명).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

from trading.auto_buy import settings as S
from trading.auto_buy.settings import AutoBuySettings
from trading.auto_buy.shadow import calendar as CAL
from trading.auto_buy.shadow.store import SchemaNotReady, connect_rw, event, get_runtime, set_runtime, tx

logger = logging.getLogger(__name__)
US_SCAN_MARKETS = ("US", "ALL")
REPO_ROOT = Path(__file__).resolve().parents[3]


def signal_key(market, ticker, signal_type, signal_date) -> str:
    return f"{market}:{ticker}:{signal_type}:{signal_date}"


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if isinstance(dt, datetime) else (str(dt) if dt is not None else None)


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


@lru_cache(maxsize=1)
def scanner_commit() -> str:
    """현재 체크아웃 커밋 (subprocess 없이 .git 을 읽는다). 알 수 없으면 'unknown'."""
    try:
        head = (REPO_ROOT / ".git" / "HEAD").read_text().strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1]
            p = REPO_ROOT / ".git" / ref
            if p.exists():
                return p.read_text().strip()[:12]
            packed = REPO_ROOT / ".git" / "packed-refs"
            for line in packed.read_text().splitlines() if packed.exists() else []:
                if line.endswith(ref):
                    return line.split()[0][:12]
            return "unknown"
        return head[:12]
    except OSError:
        return "unknown"


def threshold_hash() -> str:
    """Scanner 임계값(config 모듈의 대문자 단순 값) 해시. 자격 증명·URL·경로로 보이는 이름은 제외한다."""
    try:
        import config
    except Exception:
        return "unknown"
    skip = ("TOKEN", "KEY", "SECRET", "PASSWORD", "URL", "PATH", "CHAT_ID", "WEBHOOK", "ACCOUNT")
    vals = {k: getattr(config, k) for k in dir(config)
            if k.isupper() and not any(s in k for s in skip)
            and isinstance(getattr(config, k), (int, float, str, bool, list, tuple, dict))}
    return hashlib.sha256(json.dumps(vals, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _logic_hash() -> str:
    try:
        from trading.auto_buy.version import logic_hash
        return logic_hash()
    except Exception:
        return "unknown"


def process_started_at() -> Optional[datetime]:
    """현재 프로세스 시작 시각 (UTC naive). psutil 또는 Linux /proc, 알 수 없으면 None."""
    try:
        import psutil  # type: ignore
        return datetime.utcfromtimestamp(psutil.Process(os.getpid()).create_time())
    except Exception:
        pass
    try:
        ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        start = int(Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()[19])
        btime = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines()
                     if line.startswith("btime"))
        return datetime.utcfromtimestamp(btime + start / ticks)
    except Exception:
        return None


def _row_dict(row) -> dict:
    cols = row.__table__.columns.keys()
    return {c: _iso(getattr(row, c)) if isinstance(getattr(row, c), datetime) else getattr(row, c) for c in cols}


def _parse_list(raw):
    if raw is None:
        return None
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return [str(raw)]
    return v if isinstance(v, list) else [str(v)]


def _complete_since(db, session, first_detected_at, scan_log_id) -> bool:
    """최초 탐지 이후 이 스캔 직전까지 행을 썼을 수 있는 모든 US 스캔이 완전 수집됐는지.

    종료 시각이 최초 탐지 이후인 스캔과, 종료 기록이 없는(RUNNING·중단) 스캔 중 최초 탐지 근처 이후에 시작한 스캔을 포함한다.
    """
    from database.models import ScanLog
    from sqlalchemy import and_, or_
    horizon = first_detected_at - timedelta(hours=S.MAX_SCAN_HOURS)
    logs = session.query(ScanLog.id).filter(
        ScanLog.market.in_(US_SCAN_MARKETS), ScanLog.id < scan_log_id,
        or_(ScanLog.finished_at >= first_detected_at,
            and_(ScanLog.finished_at.is_(None), ScanLog.started_at >= horizon))).all()
    ids = [r[0] for r in logs]
    if not ids:
        return True
    q = "SELECT scan_log_id, complete FROM ab_scan_captures WHERE scan_log_id IN (%s)" % ",".join("?" * len(ids))
    got = {r[0]: r[1] for r in db.execute(q, ids)}
    return all(got.get(i) == 1 for i in ids)


def _first_pass(db, session, row, key, day, is_first, scan_log_id, collection_start) -> tuple:
    if row.strict_filter_passed is not True:
        return None, "NOT_STRICT" if row.strict_filter_passed is False else "STRICT_UNASSESSED"
    if day is not None:
        prior = db.execute(
            "SELECT s.scan_log_id, d.decision_scan_log_id FROM ab_signal_snapshots s "
            "LEFT JOIN ab_day_inputs d ON d.day = s.trading_day "
            "WHERE s.signal_key=? AND s.strict_passed=1 AND s.intraday=0 AND s.trading_day < ?",
            (key, day.isoformat())).fetchall()
        if prior:
            decided = any(p[0] == p[1] for p in prior)
            return 0, "PRIOR_STRICT_SNAPSHOT" if decided else "PRIOR_STRICT_NOT_DECIDED"
    if is_first:
        return 1, "FIRST_DETECTION"
    fd = row.first_detected_at
    if fd is None or collection_start is None or fd < datetime.fromisoformat(collection_start):
        return None, "BEFORE_COLLECTION"
    if not _complete_since(db, session, fd, scan_log_id):
        return None, "CAPTURE_GAP"
    return 1, "NO_PRIOR_STRICT_SNAPSHOT"


def capture_scan_log(scan_log_id: int, *, source: str = "HOOK", settings: Optional[AutoBuySettings] = None,
                     session_factory=None, later_scan_exists: Optional[bool] = None,
                     abandoned: bool = False, window_end: Optional[datetime] = None) -> dict:
    """스캔 1회분 US 행을 SHADOW 저널에 복사한다. 실패해도 예외를 밖으로 던지지 않는다 (Scanner 보호)."""
    settings = settings or AutoBuySettings.from_env()
    if not settings.shadow_enabled:
        return {"skipped": "mode_off_or_config_error"}
    try:
        db = connect_rw(settings.journal_path)
    except SchemaNotReady as exc:
        return {"skipped": f"schema_not_ready: {exc}"}
    try:
        return _capture(db, scan_log_id, source, settings, session_factory, later_scan_exists, abandoned, window_end)
    except Exception as exc:
        logger.warning("자동매수 SHADOW 스냅샷 실패 scan_log_id=%s: %s", scan_log_id, type(exc).__name__)
        try:
            event(db, "ERROR", "snapshot_failed", f"스냅샷 실패 scan_log_id={scan_log_id}",
                  error=type(exc).__name__, source=source)
        except Exception:
            pass
        return {"error": type(exc).__name__}
    finally:
        db.close()


def _capture(db, scan_log_id, source, settings, session_factory, later_scan_exists, abandoned, window_end) -> dict:
    from database.models import ScanLog, ScanResult, SessionLocal
    session = (session_factory or SessionLocal)()
    try:
        if db.execute("SELECT 1 FROM ab_scan_captures WHERE scan_log_id=?", (scan_log_id,)).fetchone():
            return {"duplicate": True, "scan_log_id": scan_log_id}
        log = session.get(ScanLog, scan_log_id)
        if log is None or log.market not in US_SCAN_MARKETS:
            return {"skipped": "not_us_scan"}
        if abandoned:
            status, end = "ABANDONED", window_end
        elif log.status in ("DONE", "ERROR") and log.finished_at is not None:
            status, end = log.status, log.finished_at
        else:
            return {"skipped": f"scan_status_{log.status}"}
        q = session.query(ScanResult).filter(ScanResult.market == "US", ScanResult.scan_time >= log.started_at)
        q = q.filter(ScanResult.scan_time <= end) if end is not None else q
        rows = q.order_by(ScanResult.id).all()
        if later_scan_exists is None:
            later_scan_exists = session.query(ScanLog.id).filter(
                ScanLog.id > scan_log_id, ScanLog.market.in_(US_SCAN_MARKETS)).first() is not None
        if status == "DONE" and (source == "HOOK" or not later_scan_exists):
            complete, recover = 1, "FULL"
        elif status == "DONE":
            complete, recover = 0, "UNRECOVERABLE"
        else:          # ERROR·ABANDONED: 스캔 자체가 일부만 썼다. 이후 스캔이 있으면 그 행 일부도 덮어써졌다
            complete, recover = 0, ("UNRECOVERABLE" if later_scan_exists else "PARTIAL")
        day, intraday = CAL.scan_trading_day(log.started_at, settings)
        scan_bucket = CAL.bucket_day(log.started_at, settings)
        trig = getattr(log, "triggered_by", None)
        commit, thash, lhash, now = scanner_commit(), threshold_hash(), _logic_hash(), time.time()
        with tx(db):
            if abandoned:
                # 중단 후보 선정 뒤 기록 직전에 원본이 끝났으면 ABANDONED 로 저장하지 않는다 (다음 보충 수집이 정상 처리)
                session.expire_all()
                fresh = session.get(ScanLog, scan_log_id)
                if fresh is None or fresh.finished_at is not None or fresh.status != "RUNNING":
                    return {"skipped": "abandoned_candidate_finished", "scan_log_id": scan_log_id,
                            "status": getattr(fresh, "status", None)}
            if db.execute("SELECT 1 FROM ab_scan_captures WHERE scan_log_id=?", (scan_log_id,)).fetchone():
                return {"duplicate": True, "scan_log_id": scan_log_id}
            collection_start = get_runtime(db, "collection_started_at")
            if collection_start is None:
                collection_start = log.started_at.isoformat()
                set_runtime(db, "collection_started_at", collection_start)
            dayk = day.isoformat() if day else None
            frozen = db.execute("SELECT decision_status, decision_scan_log_id, frozen_at FROM ab_day_inputs "
                                "WHERE day=?", (dayk,)).fetchone() if (dayk and not intraday) else None
            # 확정 이후에 시작한 스캔만 '확정 후'다 (결정 스캔보다 먼저 시작해 늦게 수집된 스캔은 NON_DECISION)
            late = int(frozen is not None and (frozen["decision_scan_log_id"] is None
                                               or scan_log_id > frozen["decision_scan_log_id"]))
            notes = []
            if status == "DONE" and not complete:
                notes.append("이후 스캔이 행을 덮어썼을 수 있어 일부 신호를 복구하지 못했을 수 있습니다.")
            if status == "ERROR":
                notes.append("ERROR 스캔 (매수 판단 미사용, 덮어쓴 행 영향 범위 기록)")
            if status == "ABANDONED":
                notes.append("비정상 종료로 RUNNING 에 남은 스캔 (매수 판단 미사용, 일부 행만 저장됐을 수 있음)")
            if late:
                notes.append("당일 후보 확정 이후 스캔 — 당일 후보에 추가하지 않음 (LATE_SCAN_AFTER_DECISION)")
            # 신호 마감 이후에 '새로 시작한' 스캔은 결정 스캔이 될 수 없다 (마감 전에 시작해 늦게 끝난 스캔은 될 수 있음)
            after_cutoff = bool(dayk and not intraday and not late and started_after_cutoff(log.started_at, day, settings))
            if after_cutoff:
                notes.append("신호 마감 이후 시작한 스캔 — 결정 스캔으로 쓰지 않음 (SCAN_AFTER_SIGNAL_CUTOFF)")
            db.execute("INSERT INTO ab_scan_captures(scan_log_id, scan_market, scan_status, triggered_by, "
                       "scan_started_at, scan_finished_at, trading_day, intraday, source, complete, recoverability, "
                       "late_after_decision, rows_captured, strict_rows, signals_found, affected_rows, note, captured_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (scan_log_id, log.market, status, trig, _iso(log.started_at), _iso(log.finished_at), dayk,
                        int(intraday), source, complete, recover, late, 0, 0, log.signals_found,
                        len(rows) if status != "DONE" else None, " ".join(notes) or None, now))
            strict_rows, strict_keys = 0, []
            for r in rows:
                key = signal_key(r.market, r.ticker, r.signal_type, r.signal_date)
                fd = r.first_detected_at
                is_first = int(fd is not None and scan_bucket is not None and CAL.bucket_day(fd, settings) == scan_bucket)
                fp, basis = _first_pass(db, session, r, key, None if intraday else day, is_first, scan_log_id,
                                        collection_start)
                price, pivot, stop = _num(r.price), _num(r.pivot_price), _num(r.stop_loss)
                ext = (price / pivot - 1) * 100 if price and pivot and pivot > 0 else None
                stop_pct = (1 - stop / price) * 100 if price and price > 0 and stop and stop > 0 else None
                raw = _row_dict(r)
                rj = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)
                strict = None if r.strict_filter_passed is None else int(bool(r.strict_filter_passed))
                if strict == 1:
                    strict_rows += 1
                    strict_keys.append((key, r.ticker))
                db.execute(
                    "INSERT INTO ab_signal_snapshots(scan_log_id, scan_result_id, signal_key, market, ticker, name, "
                    "signal_type, signal_date, trading_day, intraday, scan_status, scan_completed_at, "
                    "late_after_decision, first_detected_at, is_first_detection, strict_passed, strict_reasons, "
                    "first_strict_pass, first_pass_basis, grade, signal_quality, market_condition, decision_price, "
                    "pivot_price, stop_loss, ext_pct, stop_pct, entry_warnings, rs_value, volume_ratio, "
                    "weekly_volume_ratio, sector_name, sector_source, scanner_commit, threshold_hash, "
                    "capture_logic_hash, row_sha256, row_json, capture_source, created_at) VALUES ("
                    + ",".join("?" * 40) + ")",
                    (scan_log_id, r.id, key, r.market, r.ticker, r.name, r.signal_type, r.signal_date, dayk,
                     int(intraday), status, _iso(log.finished_at), late, _iso(fd), is_first, strict, r.filter_reasons,
                     fp, basis, r.grade, r.signal_quality, r.market_condition, price, pivot, stop, ext, stop_pct,
                     json.dumps(_parse_list(r.entry_warnings), ensure_ascii=False), _num(r.rs_value),
                     _num(r.volume_ratio), _num(r.weekly_volume_ratio), r.sector_name,
                     getattr(r, "sector_source", None), commit, thash, lhash,
                     hashlib.sha256(rj.encode()).hexdigest(), rj, source, now))
            db.execute("UPDATE ab_scan_captures SET rows_captured=?, strict_rows=? WHERE scan_log_id=?",
                       (len(rows), strict_rows, scan_log_id))
            # 당일 후보 확정: 정규 스캔·DONE·완전 수집·장 마감 후, 아직 확정 전일 때만
            frozen_now = False
            if (frozen is None and status == "DONE" and complete and not intraday and dayk
                    and trig == S.REGULAR_SCAN_TRIGGER and not after_cutoff):
                sig_hash = hashlib.sha256(json.dumps(sorted(
                    r_[0] for r_ in db.execute("SELECT row_sha256 FROM ab_signal_snapshots WHERE scan_log_id=?",
                                                (scan_log_id,)))).encode()).hexdigest()[:16]
                db.execute("INSERT INTO ab_day_inputs(day, decision_status, decision_scan_log_id, frozen_at, frozen_by, "
                           "signal_set_hash, strategy_version, config_hash, logic_hash) VALUES (?,?,?,?,?,?,?,?,?)",
                           (dayk, "FROZEN", scan_log_id, now, source, sig_hash, S.STRATEGY_VERSION,
                            S.config_hash(settings), lhash))
                frozen_now = True
            if late:
                decided = set()
                if frozen["decision_scan_log_id"] is not None:
                    decided = {r_[0] for r_ in db.execute(
                        "SELECT signal_key FROM ab_signal_snapshots WHERE scan_log_id=? AND strict_passed=1",
                        (frozen["decision_scan_log_id"],))}
                new = sorted({t for k, t in strict_keys if k not in decided})
                event(db, "WARN", "LATE_SCAN_AFTER_DECISION",
                      f"{dayk} 후보 확정 이후 스캔 #{scan_log_id}: Strict {strict_rows}건 중 확정 후보에 없던 "
                      f"{len(new)}건은 당일 후보에 추가하지 않음", day=dayk, scan_log_id=scan_log_id,
                      scan_started_at=_iso(log.started_at), triggered_by=trig, new_strict_tickers=new[:50],
                      decision_status=frozen["decision_status"])
            if after_cutoff:
                event(db, "WARN", "SCAN_AFTER_SIGNAL_CUTOFF",
                      f"{dayk} 신호 마감 이후 시작한 스캔 #{scan_log_id} — 결정 스캔으로 쓰지 않음 (Strict {strict_rows}건)",
                      day=dayk, scan_log_id=scan_log_id, scan_started_at=_iso(log.started_at), triggered_by=trig)
            set_runtime(db, "last_capture", {"scan_log_id": scan_log_id, "source": source, "rows": len(rows),
                                             "complete": complete, "status": status, "at": now})
            if not complete:
                event(db, "WARN", "capture_incomplete", f"scan_log_id={scan_log_id} 스냅샷 불완전 ({status}, {recover})",
                      source=source, status=status, recoverability=recover, affected_rows=len(rows))
        return {"scan_log_id": scan_log_id, "rows": len(rows), "strict_rows": strict_rows, "complete": complete,
                "status": status, "trading_day": dayk, "intraday": intraday, "late": bool(late),
                "frozen": frozen_now}
    finally:
        session.close()


def started_after_cutoff(started_at, day, settings) -> bool:
    from trading.auto_buy.shadow.inputs import signal_cutoff
    cut = signal_cutoff(day, settings)
    return cut is not None and CAL._utc(started_at) >= cut


def decision_scan_pending(db, day, settings, *, session_factory=None, scan_running: bool = False) -> bool:
    """신호 마감이 지났어도 그 거래일 결정 스캔을 기다려야 하는지.

    마감 전에 시작한 그날 정규 스캔이 아직 끝나지 않았거나(실행 중), 끝났지만 아직 수집되지 않았으면 기다린다.
    마감 이후 새로 시작한 스캔은 기다리지 않는다 (결정 스캔이 될 수 없음). 운영 DB 를 읽지 못하면 기다린다.
    """
    from database.models import ScanLog, SessionLocal
    from trading.auto_buy.shadow.inputs import signal_cutoff
    close, cut = CAL.close_at(day, settings), signal_cutoff(day, settings)
    if close is None or cut is None:
        return False
    naive = lambda dt: dt.astimezone(timezone.utc).replace(tzinfo=None)  # noqa: E731
    try:
        session = (session_factory or SessionLocal)()
        try:
            logs = session.query(ScanLog.id, ScanLog.status).filter(
                ScanLog.market.in_(US_SCAN_MARKETS), ScanLog.triggered_by == S.REGULAR_SCAN_TRIGGER,
                ScanLog.started_at >= naive(close), ScanLog.started_at < naive(cut)).all()
        finally:
            session.close()
    except Exception:
        return True
    captured = {r[0] for r in db.execute("SELECT scan_log_id FROM ab_scan_captures")}
    for lid, status in logs:
        if lid in captured:
            continue
        if status == "DONE" or (status == "RUNNING" and scan_running):
            return True
    return False


def catch_up(settings: AutoBuySettings, *, session_factory=None, scan_running: Callable[[], bool] = lambda: False,
             start_day: Optional[str] = None, now: Optional[datetime] = None) -> list[dict]:
    """HOOK 이 놓친 스캔과 중단(RUNNING 잔존) 스캔을 보충하고, 운영 스캔 실행 건수와 수집 이력을 대조한다.

    스캔 진행 중이면 아무것도 하지 않는다 (진행 중 스캔을 ABANDONED 로 오인하지 않음).
    ABANDONED 판정: 실행권이 비어 있고, (이후 다른 스캔 기록이 있음 | 현재 프로세스 시작 이전에 시작 | MAX_SCAN_HOURS 경과).
    기록 직전에 원본 상태를 다시 읽어 그 사이 끝난 스캔은 ABANDONED 로 저장하지 않는다.

    전제: Scanner 는 이 앱 프로세스 하나에서만 실행된다 (실행권·``scan_status`` 가 프로세스 안에서만 유효하고,
    ScanLog ID 순서를 실행 순서로 해석한다). 별도 프로세스·서버에서 같은 운영 DB 로 스캔하면 이 판정은 성립하지 않는다.
    """
    if not settings.shadow_enabled or scan_running():
        return []
    from database.models import ScanLog, SessionLocal
    now_n = CAL._utc(now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)
    boot = process_started_at()
    db = connect_rw(settings.journal_path)
    session = (session_factory or SessionLocal)()
    targets: list[tuple[int, bool, Optional[datetime]]] = []
    try:
        captured = {r[0] for r in db.execute("SELECT scan_log_id FROM ab_scan_captures")}
        base = session.query(ScanLog).filter(ScanLog.market.in_(US_SCAN_MARKETS))
        if captured:
            q = base.filter(ScanLog.id > min(captured))
        elif start_day:
            q = base.filter(ScanLog.started_at >= datetime.fromisoformat(start_day))
        else:
            latest = base.filter(ScanLog.status.in_(("DONE", "ERROR"))).order_by(ScanLog.id.desc()).first()
            q = base.filter(ScanLog.id >= latest.id) if latest else base.filter(False)
        logs = [l for l in q.order_by(ScanLog.id).all() if l.id not in captured]
        max_any = session.query(ScanLog.id).order_by(ScanLog.id.desc()).first()
        max_any = max_any[0] if max_any else 0
        for l in logs:
            if l.status in ("DONE", "ERROR") and l.finished_at is not None:
                targets.append((l.id, False, None))
            elif l.finished_at is None:
                later = l.id < max_any
                old = l.started_at < now_n - timedelta(hours=S.MAX_SCAN_HOURS)
                pre_boot = boot is not None and l.started_at < boot
                if later or old or pre_boot:
                    nxt = session.query(ScanLog.started_at).filter(ScanLog.id > l.id).order_by(ScanLog.id).first()
                    targets.append((l.id, True, nxt[0] if nxt else now_n))
    finally:
        session.close()
        db.close()
    out = []
    for sid, abandoned, end in targets:
        out.append(capture_scan_log(sid, source="CATCHUP", settings=settings, session_factory=session_factory,
                                    abandoned=abandoned, window_end=end))
    try:
        reconcile(settings, session_factory=session_factory)
    except Exception as exc:
        logger.warning("자동매수 SHADOW 스캔 대조 실패: %s", type(exc).__name__)
    return out


def reconcile(settings: AutoBuySettings, *, session_factory=None) -> dict:
    """수집 시작 이후 운영 US 스캔 실행 건수(완료·실패·진행·중단)와 수집 이력을 대조해 runtime 에 저장한다 (표시용)."""
    from database.models import ScanLog, SessionLocal
    db = connect_rw(settings.journal_path)
    session = (session_factory or SessionLocal)()
    try:
        start = get_runtime(db, "collection_started_at")
        if start is None:
            return {}
        caps = {r["scan_log_id"]: dict(r) for r in db.execute(
            "SELECT scan_log_id, scan_status, complete, recoverability, late_after_decision FROM ab_scan_captures")}
        logs = session.query(ScanLog.id, ScanLog.status, ScanLog.finished_at).filter(
            ScanLog.market.in_(US_SCAN_MARKETS), ScanLog.started_at >= datetime.fromisoformat(start)).all()
        st = {"scanner_total": len(logs), "scanner_done": 0, "scanner_error": 0, "scanner_running": 0,
              "captured": 0, "captured_complete": 0, "captured_incomplete": 0, "abandoned": 0, "unrecoverable": 0,
              "late_after_decision": 0, "uncaptured": 0, "uncaptured_ids": []}
        for lid, status, fin in logs:
            if status == "DONE":
                st["scanner_done"] += 1
            elif status == "ERROR":
                st["scanner_error"] += 1
            c = caps.get(lid)
            if c is None:
                if fin is None:
                    st["scanner_running"] += 1        # 진행 중이거나 아직 중단 판정 전
                else:
                    st["uncaptured"] += 1
                    st["uncaptured_ids"].append(lid)
                continue
            st["captured"] += 1
            st["captured_complete" if c["complete"] else "captured_incomplete"] += 1
            st["abandoned"] += int(c["scan_status"] == "ABANDONED")
            st["unrecoverable"] += int(c["recoverability"] == "UNRECOVERABLE")
            st["late_after_decision"] += int(bool(c["late_after_decision"]))
        st["uncaptured_ids"] = st["uncaptured_ids"][:20]
        st["at"] = time.time()
        with tx(db):
            set_runtime(db, "scan_reconciliation", st)
        return st
    finally:
        session.close()
        db.close()
