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

거래량 (스키마 ab0002, 출처·원본 근거 ab0003)
- 정상 OHLC + 양수 거래량: OK/POSITIVE — 기존 가상 체결 규칙.
- 0 은 출처가 원본 확인 출처(Yahoo chart 원본 JSON, ``vfills.CONFIRMED_ZERO_SOURCES``)일 때만 확정이다. 변환 DataFrame 의
  0·출처 기록 없는 0(ab0002 시절 행)은 미확인으로 본다. 출처와 원본 근거는 ``volume_source``·``source_evidence`` 에 남긴다.
- 정상 OHLC + 원본 거래량 0: 데이터 마감에 OK/ZERO 로 확정 (마감 전에는 잠정으로 보고 기다린다). 엔진은 그날 가상 매수·매도
  체결을 만들지 않는다 (매수 주문은 미체결 만료, 보유는 손절 체결 없이 위험 유지).
- 거래량 NULL·음수·NaN(미확인): 마감 전에는 기다리고, 마감 후에는 INVALID/UNKNOWN 으로 저장해 결측과 같이 데이터 불완전
  (FILL_UNKNOWN·UNRESOLVED)으로 처리하고 결측 복구 대상에 넣는다. 미확인을 0 으로 보지 않는다.
- ab0002 이전에 저장된 OK 행(거래량 열 NULL)도 미확인으로 보고 복구 대상에 넣는다.

하루 완료 판정: 필요한 종목 전체의 일봉·거래량 확인, FILL_UNKNOWN 주문, 가격 미확정 포지션, 계좌별 처리 결과(46 계좌 모두
기록·모두 COMPLETE)를 함께 본다. 하나라도 미확정이면 그날은 DATA_INCOMPLETE 이며 미확정 건수를 notes 에 남긴다.
일 처리 중간에 중단돼 다시 실행해도 그날 목표였던 주문(FILL_UNKNOWN 포함)·그날 청산·미확정된 포지션 종목을
필요 종목에 다시 넣어 결측 0건·COMPLETE 로 잘못 기록하지 않는다.

결측 복구
- 최근 ``RECOVERY_WINDOW_DAYS`` 거래일(마지막 마감 세션 포함)의 결측 일봉을 job 마다 다시 받아 본다. 대상이 많으면
  job 마다 ``RECOVERY_MAX_DAYS_PER_RUN`` 일씩 순환한다. 하나라도 확인되면 새 리비전과 함께 ``replay_needed`` 를 같은
  트랜잭션에 저장하고, 새 run 을 만들어 시작일부터 46 계좌 전체를 시간순으로 다시 계산한다 (현금·Heat·포지션·수량·신규 판단 모두).
  replay 가 실패하면 현재 run 을 유지하고 ``replay_needed`` 도 남겨 다음 job 이 다시 시도한다.
  단일 손절 이벤트만 끼워 넣지 않는다. 이전 run 은 SUPERSEDED 로 남기고 결과 차이를 이벤트로 기록한다.
- 재계산 중(BUILDING)에는 현재 run 이 그대로 보이며 결과 화면에 재계산 중으로 표시된다.

재계산 실행권 (``replay_in_progress`` lease)
- BUILDING run 을 만들 때 같은 트랜잭션(BEGIN IMMEDIATE)에 실행권(run·pid·호스트·프로세스 토큰·heartbeat)을 기록한다.
  실행권이 살아 있으면 다른 작업자는 재계산을 시작하지 않는다 (ReplayBusy).
- 재계산은 거래일마다 heartbeat 를 갱신한다. 실행권을 잃었으면(다른 작업자가 비정상 종료로 판정) 즉시 멈추고 CURRENT 로
  바꾸지 않는다. CURRENT 전환은 실행권 확인과 같은 트랜잭션에서 원자적으로 한다.
- 실행권에 pid·호스트·boot 식별자·프로세스 시작 식별자·토큰을 기록한다. 판정은 ``classify_lease`` (상태 표는 그 위 주석).
- 사망이 확인된 경우(같은 호스트에서 pid 없음·재부팅·pid 재사용)만 BUILDING run 을 FAILED(ABANDONED_BUILD)로 남기고
  (삭제 없음) ERROR 이벤트를 남긴다. heartbeat 지연만으로는 정리하지 않는다: 같은 호스트에서 원래 프로세스가 살아 있으면
  STALLED, 다른 호스트면 REMOTE_STALE 로 새 재계산을 막고 경고만 한다 (자동 탈취 없음, 수동 확인 ``release_stalled_lease``).
- fencing: ab0003 트리거가 BUILDING·CURRENT 가 아닌 run 의 행 쓰기와 FAILED·SUPERSEDED run 의 상태 복귀, CURRENT 중복을
  DB 에서 거부한다. CURRENT 전환 직전에 같은 트랜잭션에서 실행권·토큰을 다시 확인한다.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from trading.auto_buy import settings as S
from trading.auto_buy.shadow import calendar as CAL
from trading.auto_buy.shadow import engine as EN
from trading.auto_buy.shadow.store import event, get_runtime, set_runtime, tx
from trading.auto_buy.shadow.vfills import (CONFIRMED_ZERO_SOURCES, VOLUME_POSITIVE, VOLUME_UNKNOWN, VOLUME_ZERO, Bar,
                                            BarUnavailable, volume_state, zero_confirmable)

logger = logging.getLogger(__name__)
DEFAULT_PROVIDER = "yahoo_chart_raw_v8_auto_adjust"
KNOWN_VOLUME = (VOLUME_POSITIVE, VOLUME_ZERO)
# 최신 리비전이 이 조건이면 그 일봉은 미확정 (결측·무효·거래량 미확인·ab0002 이전 행·원본 확인 출처가 아닌 0)
_CONFIRMED_SQL = ",".join("'%s'" % x for x in sorted(CONFIRMED_ZERO_SOURCES))
INCOMPLETE_BAR_SQL = ("(b.status != 'OK' OR b.volume_status IS NULL "
                      "OR b.volume_status NOT IN ('POSITIVE','ZERO') "
                      f"OR (b.volume_status = 'ZERO' AND (b.volume_source IS NULL OR b.volume_source NOT IN ({_CONFIRMED_SQL}))))")
PROCESS_TOKEN = uuid.uuid4().hex          # 이 프로세스의 실행권 식별자 (pid 재사용 구분)
REPLAY_LEASE_STALE_SEC = 15 * 60          # heartbeat 가 이보다 오래되면 응답 없음(STALLED). 사망 판정에는 쓰지 않는다


class ReplayBusy(RuntimeError):
    """다른 작업자의 재계산이 살아 있다 (시작하지 않음)."""


class ReplayLeaseLost(RuntimeError):
    """재계산 중 실행권을 잃었다 (다른 작업자가 비정상 종료로 판정). CURRENT 로 바꾸지 않는다."""


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
            keep = json.dumps({k: snap.get(k) for k in ("equity", "cash", "orderable_cash", "total_cash", "holdings",
                                                        "holdings_total", "ledger_evaluation", "as_of", "source")},
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


def bar_complete(row) -> bool:
    """저장된 일봉 행이 가상 체결·평가에 쓸 수 있는지 (OHLC 정상 + 거래량 양수 또는 원본 확인 출처의 0 확정).
    출처 기록이 없는 0(ab0002 시절 yfinance 변환값)은 확정으로 보지 않는다."""
    if row is None or row["status"] != "OK":
        return False
    if row["volume_status"] == VOLUME_POSITIVE:
        return True
    return row["volume_status"] == VOLUME_ZERO and zero_confirmable(_col(row, "volume_source"))


def _col(row, key):
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _rec(row) -> Optional[EN.BarRec]:
    if not bar_complete(row):
        return None
    return EN.BarRec(Bar(row["open"], row["high"], row["low"], row["close"], volume=row["volume"],
                         volume_source=_col(row, "volume_source")), row["provider"], row["fetched_at"],
                     row["input_rev"])


def usable_bar(bar) -> bool:
    """공급자 응답이 확정 저장 가능한 봉인지 (OHLC 정상 + 거래량 확인)."""
    return isinstance(bar, Bar) and bar.valid() and bar.volume_state in KNOWN_VOLUME


def _store_bar(db, day: str, ticker: str, bar, provider: str, purpose: str, final_missing: bool) -> Optional[str]:
    """반환: 저장한 상태 (OK/INVALID/MISSING) 또는 None (저장 안 함: 아직 기다림).

    거래량 0·미확인은 데이터 마감 전에는 잠정으로 보고 저장하지 않는다 (다음 job 에 다시 받음)."""
    rev = db.execute("SELECT COALESCE(MAX(input_rev),0) FROM ab_bars WHERE day=? AND ticker=?",
                     (day, ticker)).fetchone()[0] + 1
    if isinstance(bar, BarUnavailable):          # 수집 실패 (HTTP 오류·예산·연속 장애 등): 결측과 같다, 근거만 남긴다
        if not final_missing:
            return None
        db.execute("INSERT INTO ab_bars(day, ticker, input_rev, status, open, high, low, close, provider, fetched_at, "
                   "purpose, detail, volume, volume_status, volume_source, source_evidence) "
                   "VALUES (?,?,?,'MISSING',NULL,NULL,NULL,NULL,?,?,?,?,NULL,?,?,?)",
                   (day, ticker, rev, provider, time.time(), purpose,
                    f"데이터 마감 후 수집 실패 {bar.code} — 결측(복구 대상), 정상 미체결로 보지 않음", VOLUME_UNKNOWN,
                    (bar.evidence or {}).get("source"),
                    json.dumps(bar.evidence or {"error": bar.code}, ensure_ascii=False, default=str)))
        return "MISSING"
    is_bar = isinstance(bar, Bar)
    vol = bar.volume if is_bar else None
    src = bar.volume_source if is_bar else None
    vstate = volume_state(vol, src)
    vol_v = vol if vstate in KNOWN_VOLUME else None
    evidence = dict(bar.evidence) if is_bar and bar.evidence else ({"source": src} if is_bar else None)
    if is_bar and vol is not None and vstate == VOLUME_UNKNOWN:
        evidence["reported_volume"] = vol          # 확정할 수 없는 0 등 (예: 변환 DataFrame) — 근거로만 남김
    if is_bar and bar.valid():
        vals = (bar.open, bar.high, bar.low, bar.close)
        if vstate == VOLUME_POSITIVE:
            status, detail = "OK", None
        elif not final_missing:
            return None
        elif vstate == VOLUME_ZERO:
            status, detail = "OK", "원본 거래량 0 확정 — 그날 가상 체결·손절 체결 없음"
        elif vol == 0:
            status, detail = "INVALID", f"거래량 0 이지만 원본 확인 출처가 아님({src}) — 미확인, 데이터 불완전"
        else:
            status, detail = "INVALID", "거래량 확인 불가 (NULL·필드 없음·음수·NaN) — 데이터 불완전"
    elif bar is not None:
        if not final_missing:
            return None
        status, vals, detail = "INVALID", (None, None, None, None), "OHLC 불일치 또는 비정상 값"
    else:
        if not final_missing:
            return None
        status, vals, detail, vstate = "MISSING", (None, None, None, None), "데이터 마감 후에도 일봉 없음", VOLUME_UNKNOWN
    db.execute("INSERT INTO ab_bars(day, ticker, input_rev, status, open, high, low, close, provider, fetched_at, "
               "purpose, detail, volume, volume_status, volume_source, source_evidence) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (day, ticker, rev, status, *vals, provider, time.time(), purpose, detail, vol_v, vstate, src,
                json.dumps(evidence, ensure_ascii=False, default=str) if evidence else None))
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
    """거래일 D 처리에 D 일봉이 필요한 종목. 일 처리 중간에 중단된 뒤 다시 실행해도 같은 집합이 되도록, 이미 처리된
    계좌의 결과(그날 목표 주문의 FILL_UNKNOWN·체결·미체결, 그날 청산, 그날 가격 미확정이 된 포지션)도 포함한다.
    (예전 날부터 가격 미확정인 포지션은 엔진이 판정을 보류하므로 넣지 않는다 — 계좌 INCOMPLETE 로 따로 집계.)"""
    return sorted({r[0] for r in db.execute(
        "SELECT ticker FROM ab_orders WHERE run_id=? AND target_day=? "
        "UNION SELECT ticker FROM ab_positions WHERE run_id=? AND entry_day<=? AND "
        "((state='OPEN' AND price_status='OK') OR exit_day=? OR unresolved_since=?)",
        (run_id, day, run_id, day, day, day))})


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


def day_completeness(db, run_id: int, inp: EN.DayInputs) -> dict:
    """하루 완료 판정 근거. 필요한 종목의 일봉·거래량, 미확정 주문·포지션, 계좌별 처리 결과를 함께 본다."""
    day = inp.day
    accounts = [a[0] for a in S.account_ids()]
    daily = {r[0]: r[1] for r in db.execute("SELECT account_id, data_status FROM ab_daily WHERE run_id=? AND day=?",
                                            (run_id, day))}
    rows = latest_bar_rows(db, day, inp.required)
    out = {
        "bars_missing": sorted(inp.missing),
        "bars_unconfirmed": sorted(t for t in inp.required if not bar_complete(rows.get(t))),
        "volume_unknown": sorted(t for t, r in rows.items() if r["status"] == "INVALID"
                                 and r["volume_status"] == VOLUME_UNKNOWN and r["open"] is not None),
        "zero_volume": sorted(t for t, b in inp.bars.items() if b is not None and b.bar.no_volume),
        "fill_unknown": db.execute("SELECT COUNT(*) FROM ab_orders WHERE run_id=? AND state='FILL_UNKNOWN' "
                                   "AND target_day<=?", (run_id, day)).fetchone()[0],
        "unresolved_positions": db.execute(
            "SELECT COUNT(*) FROM ab_positions WHERE run_id=? AND state='OPEN' AND price_status='UNRESOLVED' "
            "AND entry_day<=?", (run_id, day)).fetchone()[0],
        "accounts_missing": [a for a in accounts if a not in daily],
        "accounts_incomplete": sorted(a for a, st in daily.items() if st != "COMPLETE"),
    }
    out["complete"] = not (out["bars_missing"] or out["bars_unconfirmed"] or out["fill_unknown"]
                           or out["unresolved_positions"] or out["accounts_missing"] or out["accounts_incomplete"])
    return out


def process_day(db, run_id: int, inp: EN.DayInputs, settings=None, on_account: Optional[Callable] = None) -> dict:
    """46 계좌 처리 → 일 요약. ``on_account``: 계좌마다 처리 전에 부른다 (재계산 실행권 heartbeat·소유 확인)."""
    for acc_id, *_ in S.account_ids():
        if on_account is not None:
            on_account()
        EN.process_account_day(db, run_id, acc_id, inp, settings)
    with tx(db):
        row = db.execute("SELECT status, notes FROM ab_days WHERE run_id=? AND day=?", (run_id, inp.day)).fetchone()
        if row is not None:                     # 이미 기록된 날은 그대로 (재실행 불변)
            return {"day": inp.day, "status": row[0], "missing": inp.missing, "already": True}
        c = day_completeness(db, run_id, inp)
        status = "COMPLETE" if c["complete"] else "DATA_INCOMPLETE"
        notes = list(inp.notes)
        for k in ("fill_unknown", "unresolved_positions"):
            if c[k]:
                notes.append(f"{k.upper()}:{c[k]}")
        for k in ("bars_unconfirmed", "volume_unknown", "zero_volume", "accounts_missing", "accounts_incomplete"):
            if c[k]:
                notes.append(f"{k.upper()}:{len(c[k])}")
        db.execute("INSERT INTO ab_days(run_id, day, status, decision_status, candidates, bars_requested, "
                   "bars_missing, account_status, notes, processed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (run_id, inp.day, status, inp.decision_status, len(inp.cands.get("B00", [])), len(inp.required),
                    len(c["bars_unconfirmed"]), inp.account_status, EN._j(notes), time.time()))
        if c["zero_volume"]:
            event(db, "WARN", "ZERO_VOLUME", f"{inp.day} 거래량 0 확정 {len(c['zero_volume'])}종목 — 가상 매수 미체결 만료, "
                  "보유 종목은 손절 체결 없이 위험 유지 (거래 재개 첫 봉에서 손절 판단, 갭 손실 가능)",
                  day=inp.day, run_id=run_id, tickers=c["zero_volume"][:50])
    return {"day": inp.day, "status": status, "missing": inp.missing, "completeness": c}


# ── 결측 복구·재계산 ───────────────────────────────────────────────────

def recover_missing(db, provider: Callable, now: datetime, settings=None) -> list:
    """복구 기간 안의 결측 일봉을 다시 받는다. 반환: 새로 확인된 (day, ticker) 목록."""
    last = CAL.last_closed_session(now, settings)
    if last is None:
        return []
    floor = recovery_floor(last, settings)
    _flag_manual_review(db, expired_missing_bars(db, floor))
    rows = db.execute(
        f"SELECT b.day, b.ticker FROM ab_bars b WHERE {INCOMPLETE_BAR_SQL} AND b.day>=? "
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
                if usable_bar(b):
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
    """재계산 요청 기록 (호출 측 트랜잭션 안). 요청마다 세대(generation)를 1씩 올린다. 재계산은 시작할 때 본 세대까지만
    처리한 것으로 기록하므로, 재계산 도중 들어온 새 요청(더 높은 세대)은 지워지지 않는다."""
    prev = get_runtime(db, "replay_needed") or {}
    bars = sorted(set(map(tuple, prev.get("bars") or [])) | set(map(tuple, found)))
    gen = int(get_runtime(db, "replay_generation") or 0) + 1
    set_runtime(db, "replay_generation", gen)
    set_runtime(db, "replay_needed", {"reason": "BARS_RECOVERED", "since": prev.get("since") or time.time(),
                                      "bars": [list(b) for b in bars[-200:]], "generation": gen,
                                      "request_id": prev.get("request_id") or uuid.uuid4().hex})


def replay_request(db) -> Optional[dict]:
    """대기 중인 재계산 요청 (없으면 None). generation 이 없는 이전 형식은 0 세대로 본다."""
    need = get_runtime(db, "replay_needed")
    return need if need else None


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
    """복구 기간이 지난 미복구 일봉 (최신 리비전이 MISSING·INVALID·거래량 미확인). 삭제·정상 처리 없이 수동 검토
    대상으로 보인다."""
    if floor is None:
        return []
    return [{"day": r[0], "ticker": r[1], "status": r[2]} for r in db.execute(
        "SELECT b.day, b.ticker, CASE WHEN b.status='OK' THEN 'VOLUME_UNKNOWN' ELSE b.status END "
        f"FROM ab_bars b WHERE {INCOMPLETE_BAR_SQL} AND b.day<? "
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


# ── 재계산 실행권 (lease) ───────────────────────────────────────────────
#
# 상태 (classify_lease):
#   OWN                — 이 프로세스(토큰 일치)
#   ALIVE              — 같은 호스트·같은 boot, pid 살아 있고 프로세스 시작 식별자 일치, heartbeat 최신
#   ALIVE_UNVERIFIED   — 위와 같지만 시작 식별자를 확인할 수 없음 (pid 재사용 판정 불가 → 살아 있다고 본다)
#   STALLED            — 같은 호스트에서 원래 프로세스가 살아 있으나 heartbeat 지연 (응답 없음). 자동 탈취하지 않고 차단·경고
#   STALLED_UNVERIFIED — 위와 같지만 시작 식별자 확인 불가
#   REMOTE_ACTIVE      — 다른 호스트, heartbeat 최신 (건드리지 않음)
#   REMOTE_STALE       — 다른 호스트, heartbeat 지연. 사망을 확인할 수 없으므로 자동 탈취하지 않고 차단·경고
#   DEAD               — 같은 호스트에서 프로세스 사망 확인: pid 없음, boot 변경(재부팅), pid 재사용(시작 식별자 불일치
#                        또는 이 프로세스가 같은 pid 를 다른 토큰으로 가짐). 이것만 자동 정리(FAILED)한다.
# 실행권 만료(heartbeat 지연)와 프로세스 사망을 구분한다. heartbeat 지연만으로는 FAILED 로 바꾸지 않는다.
LEASE_BLOCKING = ("OWN", "ALIVE", "ALIVE_UNVERIFIED", "STALLED", "STALLED_UNVERIFIED", "REMOTE_ACTIVE", "REMOTE_STALE")
LEASE_RUNNING = ("OWN", "ALIVE", "ALIVE_UNVERIFIED", "REMOTE_ACTIVE")
LEASE_STALLED = ("STALLED", "STALLED_UNVERIFIED", "REMOTE_STALE")


def _now() -> float:
    return time.time()


_BOOT_ID: dict = {}


def boot_id() -> Optional[str]:
    """이 호스트의 부팅 식별자 (Linux boot_id, macOS kern.boottime). 알 수 없으면 None."""
    if "v" in _BOOT_ID:
        return _BOOT_ID["v"]
    v = None
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            v = "linux:" + f.read().strip()
    except OSError:
        try:
            import subprocess
            out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5).stdout
            sec = out.split("sec =", 1)[1].split(",", 1)[0].strip() if "sec =" in out else ""
            v = f"boottime:{sec}" if sec.isdigit() else None
        except Exception:
            v = None
    _BOOT_ID["v"] = v
    return v


def proc_start_id(pid) -> Optional[str]:
    """프로세스 시작 식별자 (pid 재사용 구분). psutil → Linux /proc → ps. 알 수 없으면 None."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        import psutil  # type: ignore
        return f"psutil:{psutil.Process(pid).create_time():.3f}"
    except Exception:
        pass
    try:
        with open(f"/proc/{pid}/stat") as f:
            return "proc:" + f.read().rsplit(")", 1)[1].split()[19]
    except Exception:
        pass
    try:
        import subprocess
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5).stdout.strip()
        return f"ps:{' '.join(out.split())}" if out else None
    except Exception:
        return None


_OWNER: dict = {}


def _owner() -> dict:
    if not _OWNER or _OWNER.get("pid") != os.getpid():
        _OWNER.clear()
        _OWNER.update({"pid": os.getpid(), "host": socket.gethostname(), "boot_id": boot_id(),
                       "proc_start": proc_start_id(os.getpid()), "token": PROCESS_TOKEN})
    return dict(_OWNER)


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def classify_lease(lease, now: Optional[float] = None) -> dict:
    """실행권 상태 판정 → {"state", "reason", "stale", "age_sec"}. 위 상태 표 참조."""
    if not isinstance(lease, dict) or lease.get("run_id") is None:
        return {"state": "NONE", "reason": None, "stale": None, "age_sec": None}
    now = _now() if now is None else now
    try:
        hb = float(lease.get("heartbeat_at") or lease.get("at") or 0)
    except (TypeError, ValueError):
        hb = 0.0
    age = now - hb
    stale = age > REPLAY_LEASE_STALE_SEC
    out = {"stale": stale, "age_sec": round(age, 1)}
    if lease.get("token") == PROCESS_TOKEN:
        return {**out, "state": "OWN", "reason": None}
    if lease.get("host") != socket.gethostname():
        return {**out, "state": "REMOTE_STALE" if stale else "REMOTE_ACTIVE",
                "reason": "다른 호스트 — 사망 확인 불가, 자동 탈취하지 않음" if stale else None}
    mine = _owner()
    if lease.get("boot_id") and mine.get("boot_id") and lease["boot_id"] != mine["boot_id"]:
        return {**out, "state": "DEAD", "reason": "REBOOTED"}
    pid = lease.get("pid")
    if pid == os.getpid():
        return {**out, "state": "DEAD", "reason": "PID_REUSED"}       # 같은 pid, 다른 토큰 = 이전 프로세스의 실행권
    if not _pid_alive(pid):
        return {**out, "state": "DEAD", "reason": "PROCESS_GONE"}
    rec, cur = lease.get("proc_start"), proc_start_id(pid)
    if rec and cur and rec != cur:
        return {**out, "state": "DEAD", "reason": "PID_REUSED"}
    unverified = not (rec and cur)
    if stale:
        return {**out, "state": "STALLED_UNVERIFIED" if unverified else "STALLED",
                "reason": "원래 작업자가 살아 있으나 heartbeat 지연 — 자동 탈취하지 않음, 수동 확인 필요"}
    return {**out, "state": "ALIVE_UNVERIFIED" if unverified else "ALIVE",
            "reason": "프로세스 시작 식별자 확인 불가 — pid 재사용 판정 없이 살아 있다고 봄" if unverified else None}


def lease_alive(lease, now: Optional[float] = None) -> bool:
    """실행권이 다른 재계산을 막는지 (사망이 확인된 경우만 False). heartbeat 지연만으로는 False 가 아니다."""
    return classify_lease(lease, now)["state"] in LEASE_BLOCKING


def replay_state(db, now: Optional[float] = None) -> Optional[dict]:
    """모니터용 재계산 상태. None 이면 재계산 없음.
    RUNNING / STALLED (응답 없음·자동 탈취 안 함, 경고) / ABANDONED_SUSPECTED (사망 확인 또는 실행권 없는 BUILDING)."""
    lease = get_runtime(db, "replay_in_progress")
    building = [r[0] for r in db.execute("SELECT run_id FROM ab_runs WHERE status='BUILDING' ORDER BY run_id")]
    if not lease and not building:
        return None
    c = classify_lease(lease, now)
    on_building = isinstance(lease, dict) and lease.get("run_id") in building
    if c["state"] in LEASE_RUNNING and on_building:
        status = "RUNNING"
    elif c["state"] in LEASE_STALLED and on_building:
        status = "STALLED"
    else:
        status = "ABANDONED_SUSPECTED"
    return {"status": status, "lease_state": c["state"], "lease_reason": c["reason"], "heartbeat_age_sec": c["age_sec"],
            "lease": lease, "building_runs": building}


def reap_abandoned_builds(db, now: Optional[float] = None) -> list:
    """사망이 확인된 작업자(DEAD)의 BUILDING run 과 실행권 없는 BUILDING run 만 FAILED(ABANDONED_BUILD)로 기록한다
    (삭제 없음). 살아 있거나 확인할 수 없는 작업자(STALLED·REMOTE_*)는 건드리지 않고 경고만 남긴다.
    CURRENT run·replay_needed 는 바꾸지 않는다. 반환: FAILED 로 바꾼 run 목록."""
    now = _now() if now is None else now
    reaped = []
    with tx(db):
        lease = get_runtime(db, "replay_in_progress")
        c = classify_lease(lease, now)
        lease_run = lease.get("run_id") if isinstance(lease, dict) else None
        for (run,) in db.execute("SELECT run_id FROM ab_runs WHERE status='BUILDING' ORDER BY run_id").fetchall():
            if run == lease_run and c["state"] in LEASE_BLOCKING:
                active = get_runtime(db, "replay_stall_active")
                if c["state"] in LEASE_STALLED and active != run:       # 정체 에피소드 시작 (같은 장애는 한 번만 기록)
                    event(db, "WARN", "replay_stalled", f"재계산 run #{run} 작업자 응답 없음 ({c['state']}, heartbeat "
                          f"{c['age_sec']}초 전) — 자동 탈취하지 않음. 새 재계산 차단, 수동 확인 필요",
                          run=run, lease=lease, lease_state=c["state"])
                    set_runtime(db, "replay_stall_active", run)
                elif c["state"] in LEASE_RUNNING and active == run:
                    _close_stall(db, run, "heartbeat 재개")
                continue
            mine = lease if run == lease_run else None
            reason = c["reason"] if mine else "NO_LEASE"
            summary = {"reason": "ABANDONED_BUILD", "abandoned": True, "detected_at": now, "lease": mine,
                       "death_evidence": reason,
                       "note": "재계산 작업자 사망 확인 (프로세스 없음·재부팅·pid 재사용) 또는 실행권 없음. 현재 run 유지, "
                               "다음 job 이 재계산"}
            db.execute("UPDATE ab_runs SET status='FAILED', completed_at=?, summary=? WHERE run_id=? "
                       "AND status='BUILDING'", (now, EN._j(summary), run))
            event(db, "ERROR", "replay_abandoned", f"재계산 run #{run} 비정상 종료 확인({reason}) — FAILED(ABANDONED_BUILD)로 "
                  f"기록, 현재 run 유지", run=run, lease=mine, death_evidence=reason)
            _close_stall(db, run, "작업자 사망 확인")
            reaped.append(run)
        if lease and c["state"] == "DEAD":
            set_runtime(db, "replay_in_progress", None)
    return reaped


def _close_stall(db, run: int, how: str) -> None:
    """정체 에피소드 종료 기록 (호출 측 트랜잭션). 관찰 기간 위반 수는 줄지 않는다 (replay_stalled 이벤트는 그대로)."""
    if get_runtime(db, "replay_stall_active") == run:
        set_runtime(db, "replay_stall_active", None)
        event(db, "INFO", "replay_stall_recovered", f"재계산 run #{run} 응답 없음 상태 종료 ({how})", run=run, how=how)


class ManualReleaseRefused(RuntimeError):
    """수동 실행권 해제 거부 (아무것도 바꾸지 않음, 기존 실행권 유지)."""


RELEASE_EVIDENCE_KEYS = ("host", "pid", "proc_start", "confirmed_stopped_at", "method")


def release_stalled_lease(db, *, run: int, token: str, operator: str, reason: str, evidence: dict,
                          now: Optional[float] = None) -> dict:
    """수동 조치 전용 (운영자가 원래 작업자 종료를 직접 확인한 뒤, 별도 승인 하에). 자동 경로에서는 부르지 않는다.

    해제할 수 있는 경우는 **다른 호스트의 응답 없는 작업자(REMOTE_STALE)** 뿐이다 (이 프로세스가 사망을 확인할 수 없는 경우).
    - 같은 호스트에서 원래 프로세스가 살아 있으면(STALLED·STALLED_UNVERIFIED) 거부한다. 먼저 그 프로세스를 종료하면
      다음 job 이 사망을 확인해 자동 정리한다 (``reap_abandoned_builds``).
    - 정상 heartbeat 작업자(OWN·ALIVE·ALIVE_UNVERIFIED·REMOTE_ACTIVE)는 거부한다.
    - 사망이 이미 확인된 작업자(DEAD)는 자동 정리 대상이므로 거부한다.
    검증: 예상 run ID·실행권 토큰 일치, 운영자·사유 필수, 종료 확인 근거 필수(``RELEASE_EVIDENCE_KEYS``, 그중 host·pid·
    proc_start 는 실행권 기록과 일치해야 함 — 해당 작업자를 실제로 확인했다는 표시). BUILDING → FAILED 갱신 행 수가
    정확히 1 이어야 하며, 갱신·실행권 해제·감사 이벤트를 한 트랜잭션에서 한다. 어느 하나라도 어긋나면 ManualReleaseRefused
    (롤백, 기존 실행권 유지). 해제 뒤 원래 작업자가 다시 실행돼도 fencing 트리거·실행권 확인 때문에 쓰거나 CURRENT 로
    바꿀 수 없다."""
    if not (isinstance(operator, str) and operator.strip()):
        raise ManualReleaseRefused("운영자 식별 필수")
    if not (isinstance(reason, str) and reason.strip()):
        raise ManualReleaseRefused("해제 사유 필수")
    if not isinstance(evidence, dict) or any(evidence.get(k) in (None, "") for k in RELEASE_EVIDENCE_KEYS):
        raise ManualReleaseRefused(f"작업자 종료 확인 근거 필수: {', '.join(RELEASE_EVIDENCE_KEYS)}")
    with tx(db):
        lease = get_runtime(db, "replay_in_progress")
        if not isinstance(lease, dict) or lease.get("run_id") != run:
            raise ManualReleaseRefused(f"실행권이 run #{run} 의 것이 아님")
        if not token or lease.get("token") != token:
            raise ManualReleaseRefused("실행권 토큰 불일치")
        for k in ("host", "pid", "proc_start"):
            if str(evidence.get(k)) != str(lease.get(k)):
                raise ManualReleaseRefused(f"종료 확인 근거의 {k} 가 실행권 기록과 다름")
        c = classify_lease(lease, now)
        if c["state"] != "REMOTE_STALE":
            why = {"STALLED": "같은 호스트에서 원래 프로세스가 살아 있음 — 먼저 종료하면 다음 job 이 자동 정리",
                   "STALLED_UNVERIFIED": "같은 호스트에서 원래 프로세스가 살아 있음 — 먼저 종료하면 다음 job 이 자동 정리",
                   "DEAD": "사망 확인됨 — 다음 job 이 자동 정리"}.get(c["state"], "정상 heartbeat 작업자")
            raise ManualReleaseRefused(f"해제 불가 ({c['state']}): {why}")
        summary = {"reason": "ABANDONED_MANUAL", "lease": lease, "operator": operator, "note": reason,
                   "evidence": evidence, "lease_state": c["state"], "heartbeat_age_sec": c["age_sec"]}
        cur = db.execute("UPDATE ab_runs SET status='FAILED', completed_at=?, summary=? WHERE run_id=? "
                         "AND status='BUILDING'", (_now(), EN._j(summary), run))
        if cur.rowcount != 1:
            raise ManualReleaseRefused(f"run #{run} 이 BUILDING 이 아님 (갱신 {cur.rowcount}행) — 해제하지 않음")
        set_runtime(db, "replay_in_progress", None)
        _close_stall(db, run, "수동 해제")
        event(db, "ERROR", "replay_released_manually", f"재계산 run #{run} 실행권 수동 해제 ({operator}): {reason}",
              run=run, lease=lease, operator=operator, evidence=evidence, lease_state=c["state"])
    return {"released": True, "run": run, "lease_state": c["state"]}


def _owns(db, run: int) -> bool:
    lease = get_runtime(db, "replay_in_progress")
    if not isinstance(lease, dict) or lease.get("run_id") != run or lease.get("token") != PROCESS_TOKEN:
        return False
    r = db.execute("SELECT status FROM ab_runs WHERE run_id=?", (run,)).fetchone()
    return r is not None and r[0] == "BUILDING"


HEARTBEAT_FAILURES: dict = {}


def _heartbeat(db, run: int) -> None:
    """실행권 소유 확인 + heartbeat 갱신. 소유를 잃었으면 ReplayLeaseLost. DB 잠김 등으로 갱신만 실패하면 경고하고
    계속한다 (소유 상실과 구분). 실패가 길어지면 다른 작업자에게 STALLED 로 보이지만 자동 탈취되지는 않는다."""
    try:
        with tx(db):
            if not _owns(db, run):
                raise ReplayLeaseLost(f"재계산 run #{run} 실행권을 잃었습니다 (해제·정리됨)")
            lease = get_runtime(db, "replay_in_progress")
            lease["heartbeat_at"] = _now()
            set_runtime(db, "replay_in_progress", lease)
        HEARTBEAT_FAILURES.pop(run, None)
    except ReplayLeaseLost:
        raise
    except sqlite3.OperationalError as exc:
        n = HEARTBEAT_FAILURES[run] = HEARTBEAT_FAILURES.get(run, 0) + 1
        logger.warning("자동매수 SHADOW 재계산 run #%s heartbeat 갱신 실패 %s회: %s", run, n, type(exc).__name__)


def replay(db, provider: Callable, reason: str, settings=None, *, expected_parent: Optional[int] = None,
           expected_generation: Optional[int] = None) -> dict:
    """현재 run 이 처리한 거래일 전체를 저장된 입력으로 새 run 에서 다시 계산하고 원자적으로 교체한다.

    실행권을 막는 다른 재계산(살아 있음·응답 없음·다른 호스트)이 있으면 ReplayBusy. 도중에 실행권을 잃으면
    ReplayLeaseLost (CURRENT 전환 없음). heartbeat 는 거래일마다, 계좌마다, 일봉 조회 전후에 갱신한다.
    run fencing 트리거(ab0003)가 FAILED run 에 대한 쓰기를 DB 에서 거부하므로, 실행권을 잃은 뒤 다시 실행돼도 쓸 수 없다.

    멱등성 (같은 요청은 한 번만 성공): 실행권 획득과 아래 확인을 한 트랜잭션(BEGIN IMMEDIATE)에서 한다.
    - 대기 중인 요청(``replay_needed``)이 없으면 새 run 을 만들지 않는다 (``skipped=NOT_NEEDED``).
    - ``expected_generation`` 이 이미 처리된 세대 이하면 만들지 않는다 (``ALREADY_APPLIED``).
    - ``expected_parent`` 가 현재 CURRENT 와 다르면 만들지 않는다 (``PARENT_CHANGED``, 요청은 남김).
    - 완료 시 시작할 때 본 세대까지만 처리로 기록한다. 도중에 들어온 새 요청(더 높은 세대)은 남겨 다음 job 이 다시 계산한다.
    - 실패·중단이면 요청을 지우지 않으므로 다음 시도가 다시 계산한다 (기존 CURRENT 유지)."""
    with tx(db):
        lease = get_runtime(db, "replay_in_progress")
        c = classify_lease(lease)
        if c["state"] in LEASE_BLOCKING and db.execute("SELECT 1 FROM ab_runs WHERE run_id=? AND status='BUILDING'",
                                                       (lease.get("run_id"),)).fetchone():
            raise ReplayBusy(f"재계산 run #{lease['run_id']} {c['state']} (pid {lease.get('pid')}, "
                             f"host {lease.get('host')})")
        parent = EN.current_run(db)
        need = replay_request(db)
        done = int(get_runtime(db, "replay_done_generation") or 0)
        skip = None
        if need is None:
            skip = "NOT_NEEDED"
        elif expected_generation is not None and expected_generation <= done:
            skip = "ALREADY_APPLIED"
        elif expected_parent is not None and parent != expected_parent:
            skip = "PARENT_CHANGED"
        if skip:
            return {"run": None, "parent": parent, "skipped": skip, "done_generation": done}
        gen = int(need.get("generation") or 0)
        days = [r[0] for r in db.execute("SELECT day FROM ab_days WHERE run_id=? ORDER BY day", (parent,))]
        run = EN.create_run(db, reason, parent, "BUILDING", settings)
        now = _now()
        set_runtime(db, "replay_in_progress", {"run_id": run, "reason": reason, "at": now, "heartbeat_at": now,
                                               "generation": gen, "parent": parent, **_owner()})
    beat = lambda: _heartbeat(db, run)  # noqa: E731
    try:
        for d in days:
            beat()
            req = required_tickers(db, run, d)
            have = latest_bar_rows(db, d, req)
            absent = [t for t in req if t not in have]
            if absent:                           # 원래 run 에 없던 종목(복구로 생긴 새 포지션 등)은 이때 받는다
                collect_bars(db, d, absent, provider, final=True, purpose="REPLAY")
                beat()
            rows = latest_bar_rows(db, d, req)
            bars = {t: _rec(rows.get(t)) for t in req}
            try:
                process_day(db, run, build_inputs(db, run, d, bars, settings), settings, on_account=beat)
            except sqlite3.IntegrityError as exc:
                if "run fenced" in str(exc):     # 실행권 정리 후 이 run 에 쓰려 함 → DB 가 거부
                    raise ReplayLeaseLost(f"재계산 run #{run} 쓰기 차단 (fencing): 실행권을 잃었습니다") from exc
                raise
        diff = _diff(db, parent, run)
        with tx(db):
            if not _owns(db, run):               # 교체 직전 실행권 재확인 (같은 트랜잭션에서 원자적 전환)
                raise ReplayLeaseLost(f"재계산 run #{run} 실행권을 잃어 CURRENT 로 바꾸지 않습니다")
            db.execute("UPDATE ab_runs SET status='SUPERSEDED' WHERE run_id=?", (parent,))
            db.execute("UPDATE ab_runs SET status='CURRENT', completed_at=?, through_day=?, summary=? WHERE run_id=?",
                       (_now(), days[-1] if days else None, EN._j(diff), run))
            set_runtime(db, "current_run", run)
            set_runtime(db, "replay_in_progress", None)
            # 교체가 끝났을 때만, 시작할 때 본 세대까지만 처리로 기록한다 (도중의 새 요청은 남긴다)
            set_runtime(db, "replay_done_generation", max(gen, int(get_runtime(db, "replay_done_generation") or 0)))
            cur = replay_request(db)
            newer = cur is not None and int(cur.get("generation") or 0) > gen
            if not newer:
                set_runtime(db, "replay_needed", None)
            _close_stall(db, run, "재계산 완료")
            event(db, "WARN", "run_replaced", f"재계산 run #{run} 이 #{parent} 를 대체 ({reason}). "
                  f"결과가 달라진 계좌 {diff['changed_accounts']}개", parent=parent, run=run, generation=gen, **diff)
            if newer:
                event(db, "INFO", "replay_request_pending", f"재계산 중 새 요청(세대 {cur.get('generation')}) 도착 — "
                      f"다음 job 이 다시 계산", run=run, generation=gen, pending_generation=cur.get("generation"))
        return {"run": run, "parent": parent, "generation": gen, "pending_newer_request": newer, **diff}
    except ReplayLeaseLost as exc:
        with tx(db):
            event(db, "ERROR", "replay_lease_lost", f"재계산 run #{run} 중단: {exc}", run=run)
        raise
    except Exception as exc:
        with tx(db):
            if _owns(db, run):
                db.execute("UPDATE ab_runs SET status='FAILED', completed_at=? WHERE run_id=? AND status='BUILDING'",
                           (time.time(), run))
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
