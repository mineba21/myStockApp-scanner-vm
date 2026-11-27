"""SHADOW 스케줄러 진입점. ``AUTO_BUY_US_MODE=SHADOW`` 이고 저널 스키마가 준비됐을 때만 동작한다.

OFF·설정 오류·스키마 미준비면 파일·테이블을 만들지 않고 즉시 끝난다. 실제 주문 모듈을 import 하지 않는다.
재실행해도 같은 결과다 (거래일 입력을 저장해 재사용, 계좌·거래일 단위 멱등). 미국 정규장 마감 후 일정 간격으로
여러 번 실행돼 스캔 완료·일봉 공개가 늦어도 다음 실행에서 이어서 처리한다.

한 번의 실행 순서
1. 스캔 보충 수집·중단 스캔 기록·스캔 건수 대조 (스캔 진행 중이면 건너뜀)
2. 판정 로직·설정 해시 확인 — 기록과 다르면 처리 중단 (수집은 계속)
2a. 비정상 종료된 재계산(BUILDING) 정리 → FAILED(ABANDONED_BUILD). 살아 있는 다른 작업자의 재계산이면 이번 job 은 대기
3. 결측 일봉 재조회(거래량 미확인 포함) → 확인되면 새 run 으로 전체 재계산
4. 아직 처리하지 않은 거래일을 시간순으로: 결정 스캔 확정 대기/NO_DECISION_SCAN → 적격 판정 → 필요 일봉 수집
   (데이터 마감까지 대기) → 계좌 스냅샷 1회 → 46 계좌 처리
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from trading.auto_buy import settings as S
from trading.auto_buy.settings import AutoBuySettings
from trading.auto_buy.shadow import calendar as CAL
from trading.auto_buy.shadow import engine as EN
from trading.auto_buy.shadow import inputs as IN
from trading.auto_buy.shadow import snapshot as SN
from trading.auto_buy.shadow.schema import schema_status
from trading.auto_buy.shadow.snapshot import catch_up
from trading.auto_buy.shadow.store import connect_rw, event, get_runtime, set_runtime, tx

logger = logging.getLogger(__name__)


def _scan_running() -> bool:
    try:
        from scanner.scan_engine import scan_status
        return bool(scan_status.get("is_running"))
    except Exception:
        return True          # 확인할 수 없으면 보충 수집·결정 마감을 미룬다


def _sector_lookup(db):
    def f(ticker):
        r = db.execute("SELECT sector_name FROM ab_signal_snapshots WHERE ticker=? AND sector_name IS NOT NULL "
                       "ORDER BY id DESC LIMIT 1", (ticker,)).fetchone()
        return r[0] if r else None
    return f


def _rt(db, key, value):
    with tx(db):
        set_runtime(db, key, value)


def run_shadow_job(*, now: Optional[datetime] = None, settings: Optional[AutoBuySettings] = None,
                   bars_provider: Optional[Callable] = None, account_provider: Optional[Callable] = None,
                   session_factory=None, scan_running: Optional[Callable[[], bool]] = None) -> dict:
    settings = settings or AutoBuySettings.from_env()
    if not settings.shadow_enabled:
        return {"skipped": settings.config_error or f"mode={settings.mode}"}
    st = schema_status(settings.journal_path)
    if not st["schema_ready"]:
        logger.warning("자동매수 SHADOW 저널 Migration 필요: %s", st["schema_error"])
        return {"skipped": f"schema_not_ready: {st['schema_error']}"}
    now = CAL._utc(now or datetime.now(timezone.utc))
    running = scan_running or _scan_running
    fetcher = None
    if bars_provider is None:          # job 단위 공급자: 중복 제거·요청 예산·제한 재시도·연속 장애 중단 (market_data.ChartFetcher)
        from trading.auto_buy.shadow.market_data import ChartFetcher
        bars_provider = fetcher = ChartFetcher()
    if account_provider is None:
        from trading.auto_buy.shadow.market_data import kiwoom_account_snapshot as account_provider
    db = connect_rw(settings.journal_path)
    out = {"captures": [], "days": [], "waiting": None, "replay": None}
    try:
        _record_heartbeat_gap(db, now, settings)
        out["captures"] = catch_up(settings, session_factory=session_factory, scan_running=running,
                                   start_day=settings.start_day, now=now)
        start = get_runtime(db, "shadow_start_day")
        if start is None:
            row = db.execute("SELECT MIN(trading_day) FROM ab_scan_captures WHERE intraday=0 "
                             "AND trading_day IS NOT NULL").fetchone()
            if not row or not row[0]:
                out["waiting"] = "첫 스캔 스냅샷 대기"
                _rt(db, "last_job", {"at": time.time(), "logical_at": now.timestamp(), "result": out["waiting"]})
                return out
            start = max(row[0], settings.start_day) if settings.start_day else row[0]
            _rt(db, "shadow_start_day", start)
        start = version_start(db, start, settings)
        vkey = f"s5_validation_start:{S.STRATEGY_VERSION}"
        if get_runtime(db, vkey) is None:          # S5 연속 정상 운영 평가의 시작일을 명시적으로 고정 (이후 바꾸지 않음)
            _rt(db, vkey, {"day": start, "fixed_at": time.time()})
        try:
            EN.ensure_accounts(db, start, settings)
        except EN.VersionMismatch as exc:
            with tx(db):
                event(db, "ERROR", "version_mismatch", str(exc))
                set_runtime(db, "version_block", {"at": time.time(), "logical_at": now.timestamp(), "message": str(exc)})
            out["error"] = "VersionMismatch"
            _rt(db, "last_job", {"at": time.time(), "logical_at": now.timestamp(), "blocked": "VersionMismatch"})
            return out
        _rt(db, "version_block", None)
        out["abandoned_builds"] = IN.reap_abandoned_builds(db)
        rs = IN.replay_state(db)
        if rs and rs["status"] in ("RUNNING", "STALLED"):   # 다른 작업자의 재계산 — 끼어들지 않는다 (응답 없어도 탈취 안 함)
            rid = rs["lease"].get("run_id")
            out["waiting"] = (f"재계산 run #{rid} 진행 중 (다른 작업자)" if rs["status"] == "RUNNING" else
                              f"재계산 run #{rid} 작업자 응답 없음 ({rs['lease_state']}) — 자동 탈취하지 않음, 수동 확인 필요")
            _rt(db, "last_job", {"at": time.time(), "logical_at": now.timestamp(), "waiting": out["waiting"]})
            return out
        run = EN.ensure_run(db, settings)
        EN.record_eligibility(db)
        # Scanner 수집과 Yahoo 동시 호출을 피한다: 실행 중이면 원본 조회(결측 복구·재계산·일봉 수집)를 보류만 한다
        # (결측으로 확정하지 않음 — 다음 job 에 재시도).
        scan_busy = running()
        # 결측 복구 → 재계산
        recovered = [] if scan_busy else IN.recover_missing(db, bars_provider, now, settings)
        if not scan_busy and (recovered or IN.replay_needed(db)):          # 이전 replay 가 실패했으면 교체될 때까지 다시 시도
            req = IN.replay_request(db) or {}
            try:
                res = IN.replay(db, bars_provider, "BARS_RECOVERED", settings, expected_parent=run,
                                expected_generation=req.get("generation"))
            except IN.ReplayBusy as exc:
                out["waiting"] = f"재계산 대기: {exc}"
                _rt(db, "last_job", {"at": time.time(), "logical_at": now.timestamp(), "waiting": out["waiting"]})
                return out
            if res.get("skipped"):         # 이미 처리된 요청·다른 작업자가 먼저 교체 → 새 run 없음
                out["replay_skipped"] = res["skipped"]
                run = EN.current_run(db)
            else:
                out["replay"] = res
                run = res["run"]
        last = CAL.last_closed_session(now, settings)
        done = {r[0] for r in db.execute("SELECT day FROM ab_days WHERE run_id=?", (run,))}
        now_u = now.astimezone(timezone.utc)
        for d in CAL.trading_days_between(date.fromisoformat(start), last, settings) if last else []:
            day = d.isoformat()
            if day in done:
                continue
            # (a) 결정 스캔 확정
            if IN.day_input(db, day) is None:
                if now_u < IN.signal_cutoff(d, settings) or SN.decision_scan_pending(
                        db, d, settings, session_factory=session_factory, scan_running=running()):
                    out["waiting"] = f"{day} 정규 스캔 후보 확정 대기 (신호 마감 {settings_cut(d, settings)})"
                    break
                IN.freeze_no_decision(db, day, "signal_cutoff_passed", settings)
                EN.record_eligibility(db)
            # (b) 일봉
            if now_u < IN.bars_earliest(d, settings):
                out["waiting"] = f"{day} 일봉 확정 대기 (마감 후 잠정치 미사용)"
                break
            final = now_u >= IN.bars_cutoff(d, settings)
            req = IN.required_tickers(db, run, day)
            to_fetch = [t for t in req if t not in IN.latest_bar_rows(db, day, req)]
            if to_fetch and (scan_busy or running()):
                out["waiting"] = f"{day} Scanner 실행 중 — Yahoo 일봉 {len(to_fetch)}건 조회 보류 (다음 job 에 재시도)"
                break
            got = IN.collect_bars(db, day, req, bars_provider, final=final)
            if got["pending"] and not final:
                out["waiting"] = f"{day} 일봉 {len(got['pending'])}/{len(req)}건 대기 (데이터 마감까지 재조회)"
                with tx(db):                       # S5: 마감 전 재시도 기록 (위반 아님 — 최종 판정과 구분)
                    waits = get_runtime(db, "pre_cutoff_waits") or {}
                    waits[day] = int(waits.get(day, 0)) + 1
                    set_runtime(db, "pre_cutoff_waits", dict(sorted(waits.items())[-400:]))
                break
            # (c) 계좌 스냅샷 (그날 1회. 다음 정규장이 이미 열렸거나 지난 날이면 LATE)
            row = IN.day_input(db, day)
            if row["account_status"] is None:
                nxt = CAL.next_trading_day(d, settings)
                late = d != last or CAL.market_open(now, settings) or (nxt is not None and now_u >= CAL.at_et(nxt, "09:30"))
                if late:
                    IN.store_account(db, day, "LATE", None, "그날 장 마감 후 조회 시점을 놓침 (다음 정규장 이후 처리)")
                else:
                    snap = account_provider(_sector_lookup(db))
                    status = (snap or {}).get("status", "UNAVAILABLE")
                    IN.store_account(db, day, "OK" if status == "OK" else "UNAVAILABLE",
                                     snap if status == "OK" else None, (snap or {}).get("reason"))
            # 결정 스캔은 job 의 앞선 적격 기록 이후 Scanner hook 이 확정했을 수 있다. 일 처리 직전에 다시 기록하고
            # 결정 스캔의 적격 기록이 끝났을 때만 그날을 확정한다.
            EN.record_eligibility(db)
            if EN.eligibility_pending(db, row["decision_scan_log_id"]):
                out["waiting"] = f"{day} 결정 스캔 적격 판정 기록 대기"
                break
            if now_u > IN.bars_cutoff(d, settings) + timedelta(minutes=30):   # S5: 처리 지연 (heartbeat LAGGING 기준)
                with tx(db):
                    late = get_runtime(db, "late_processing") or {}
                    late.setdefault(day, now_u.isoformat())
                    set_runtime(db, "late_processing", dict(sorted(late.items())[-400:]))
            res = IN.process_day(db, run, IN.build_inputs(db, run, day, got["bars"], settings), settings)
            if res["missing"]:
                with tx(db):
                    event(db, "WARN", "DATA_INCOMPLETE", f"{day} 데이터 마감 후 일봉 결측 {len(res['missing'])}건 "
                          f"— 해당 주문 체결 불명·포지션 가격 미확정, 신규 매수 차단", day=day,
                          tickers=res["missing"][:50])
            out["days"].append(day)
        if fetcher is not None:
            out["fetch"] = fetcher.stats()
            _record_rate_limit(db, out["fetch"])
        _rt(db, "last_job", {"at": time.time(), "logical_at": now.timestamp(), "days": out["days"], "waiting": out["waiting"],
                             "captures": len(out["captures"]), "run": run, "fetch": out.get("fetch")})
        return out
    except Exception as exc:
        logger.error("자동매수 SHADOW job 실패: %s", exc, exc_info=True)
        try:
            with tx(db):
                event(db, "ERROR", "job_failed", f"SHADOW job 실패: {type(exc).__name__}: {exc}")
                set_runtime(db, "last_error", {"at": time.time(), "logical_at": now.timestamp(), "type": type(exc).__name__,
                                               "message": str(exc)[:300]})
        except Exception:
            pass
        out["error"] = type(exc).__name__
        return out
    finally:
        db.close()


RATE_LIMIT_REPEAT_WARN = 3            # 연속 몇 job 이 호출 제한으로 멈추면 경고 이벤트를 한 번 남기나


def _record_rate_limit(db, fetch: dict) -> None:
    """후속 job 보류 정책: 호출 제한(RATE_LIMITED)으로 멈춘 job 은 다음 job 에서 다시 시도하되, 첫 제한 응답에서 바로
    멈추므로 제한이 계속되는 동안 job 당 Yahoo 시도는 1회 남짓이다. 연속 횟수를 runtime ``yahoo_rate_limit`` 에 남기고,
    ``RATE_LIMIT_REPEAT_WARN`` 회 연속이면 WARN ``yahoo_rate_limited_repeated`` 를 한 번 남긴다 (미수집 일봉은 결측·복구 대상)."""
    st = get_runtime(db, "yahoo_rate_limit") or {}
    limited = fetch.get("halted") == "RATE_LIMITED"
    n = int(st.get("consecutive_jobs") or 0) + 1 if limited else 0
    new = {"consecutive_jobs": n, "last_limited_at": time.time() if limited else st.get("last_limited_at"),
           "warned": bool(st.get("warned")) and n > 0}
    with tx(db):
        if n >= RATE_LIMIT_REPEAT_WARN and not new["warned"]:
            event(db, "WARN", "yahoo_rate_limited_repeated", f"Yahoo 호출 제한이 연속 {n}개 job 에서 반복 — 다음 job 도 "
                  f"첫 제한 응답에서 멈춘다. 미수집 일봉은 결측·복구 대상", consecutive_jobs=n)
            new["warned"] = True
        set_runtime(db, "yahoo_rate_limit", new)


def _record_heartbeat_gap(db, now: datetime, settings) -> None:
    """지난 job 이후 예정 실행 시각을 놓쳤으면 WARN ``heartbeat_gap`` 을 한 번 남긴다 (S5 관찰 기간 이력 — 화면의 현재
    heartbeat 상태는 다음 job 이 돌면 정상으로 돌아오므로 이력으로 따로 남긴다)."""
    from trading.auto_buy.shadow.report import expected_slot
    now = CAL._utc(now)
    lj = get_runtime(db, "last_job") or {}
    at = lj.get("logical_at") or lj.get("at")          # job 기준 시각 (운영에서는 실제 시각과 같음)
    if not at:
        return
    cur = expected_slot(now, settings.job_minutes)
    if cur is None:
        return
    target = cur if (now - cur).total_seconds() > settings.job_minutes * 60 + 120 else \
        expected_slot(cur - timedelta(seconds=1), settings.job_minutes)
    if target is None or at >= target.timestamp() - 120 or get_runtime(db, "heartbeat_gap_last") == target.isoformat():
        return
    with tx(db):
        set_runtime(db, "heartbeat_gap_last", target.isoformat())
        event(db, "WARN", "heartbeat_gap", f"예정 실행 {target.isoformat()} 이후 job 이 돌지 않음 (마지막 job "
              f"{datetime.fromtimestamp(at, timezone.utc).isoformat()})", missed_slot=target.isoformat(), last_job_at=at)


def version_start(db, base_start: str, settings) -> str:
    """현재 전략 버전의 첫 처리 거래일. 첫 버전은 SHADOW 시작일, 버전을 올린 경우 이전 버전 run 이 마지막으로
    처리한 거래일의 다음 거래일부터 새 계좌로 시작한다 (과거 구간을 새 버전으로 다시 채우지 않음)."""
    key = f"version_start:{S.STRATEGY_VERSION}"
    v = get_runtime(db, key)
    if v:
        return v
    v = base_start
    if EN.current_run_version(db) not in (None, S.STRATEGY_VERSION):
        last = db.execute("SELECT MAX(day) FROM ab_days WHERE run_id=?", (EN.current_run(db),)).fetchone()[0]
        nxt = CAL.next_trading_day(date.fromisoformat(last), settings) if last else None
        if nxt is not None:
            v = max(nxt.isoformat(), base_start)
    _rt(db, key, v)
    return v


def settings_cut(d, settings) -> str:
    from trading.auto_stop.us_calendar import NEW_YORK
    c = IN.signal_cutoff(d, settings)
    return c.astimezone(NEW_YORK).strftime("%H:%M ET") if c else "-"
