"""가상 계좌 엔진 (전략별 독립 계좌, 재계산 run 단위, 일 단위 멱등 처리).

하루(D) 처리 순서는 P7 시뮬레이터와 같다: D 일봉으로 (1) D 가 목표일인 미체결 매수 체결 판정 → (2) 가상 S01 →
(3) 종가 평가·위험 기록 → (4) D 결정 스캔 스냅샷으로 다음 정규장 매수 판단·예약.

- 입력은 거래일별로 저장된 공통 입력(``DayInputs``: 결정 스캔 후보·일봉·계좌 스냅샷)만 쓴다. 46 계좌가 같은 입력을 쓰며
  job 재실행·재계산에서도 같은 입력을 다시 읽는다.
- ``ab_daily(run_id, account_id, day)`` 가 있으면 그 계좌의 그날은 다시 처리하지 않는다 (재실행 불변).
- 계좌 하루 처리는 한 트랜잭션. 중간에 실패하면 그 계좌·그날 전체가 롤백되고 다음 실행에서 다시 처리된다.
- 일봉 결측(확정 마감 후에도 없음): 그날이 목표일인 주문은 ``FILL_UNKNOWN`` (예약·위험 유지), 보유 포지션은
  ``UNRESOLVED`` (그날 이후 손절 판정 보류, 손절 미발생으로 보지 않음). 해당 계좌는 신규 매수를 막는다.
  결측 일봉이 나중에 확인되면 새 run 으로 처음부터 시간순 재계산한다 (``inputs.replay``).
- 체결 반영은 누적 체결 수량 ledger 의 증분만 적용한다. 체결마다 사용한 OHLC·공급자·조회 시각·근거를 기록한다.
- 손절 트리거 때 같은 종목 매수 잔량이 남아 있으면 매수 잔량을 먼저 취소(예약 해제)한 뒤 체결 수량만 청산한다.

거래량 0 확정 봉 (``Bar.no_volume``, 저장 단계에서 데이터 마감 후에만 확정)
- 그날이 목표일인 매수 주문: 체결하지 않고 미체결 만료 (UNFILLED, reason=ZERO_VOLUME_EXPIRED). 유효기간은 다음 정규장
  1일 그대로이며 다음 날로 이월하거나 같은 신호로 다시 주문하지 않는다 (signal_key 당 주문 1회).
- 보유 포지션: 손절 체결을 만들지 않는다. 종가로 평가하고(손절가 아래면 이탈 미청산 → 위험 유지·신규 매수 차단),
  ``zero_volume_days``·``last_zero_volume_day`` 를 남긴다. 거래가 재개된 첫 봉에서 같은 S01 규칙으로 판단하고
  (시가 ≤ 손절가면 시가 청산 = 갭 손실), 손절 기록에 거래량 0 이력과 갭 손실 금액을 남긴다.
- 거래량 미확인은 여기까지 오지 않는다 (저장 단계에서 결측과 같이 미확정 → FILL_UNKNOWN·UNRESOLVED).
- 거래량이 양수여도 실제 지정가 체결이 보장되지는 않는다 (일봉 근사).
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional

from trading.auto_buy import settings as S
from trading.auto_buy.shadow import eligibility as EL
from trading.auto_buy.shadow import risk as RK
from trading.auto_buy.shadow import integrity as IG
from trading.auto_buy.shadow.store import event, get_runtime, set_runtime, tx
from trading.auto_buy.shadow.vfills import Bar, check_stop, try_buy, unit_cost, unit_proceeds

OPEN_ORDER_STATES = ("PENDING", "PARTIAL")
DAY0_UNCERTAINTY = ("체결 당일 저가가 손절가 이하: 일봉으로는 매수·손절 순서를 확정할 수 없어 보수적으로 "
                    "매수 후 손절가 청산으로 가정 (대안: 다음 거래일부터 감시)")


class FillAnomaly(RuntimeError):
    pass


class EligibilityConflict(RuntimeError):
    """같은 (스냅샷, 풀, 버전)에 이미 다른 적격 판단이 기록돼 있다 (ab0006 ``ab_eligibility_v6_conflict``)."""


class VersionMismatch(RuntimeError):
    pass


@dataclass(frozen=True)
class BarRec:
    bar: Bar
    provider: str
    fetched_at: float
    input_rev: int


@dataclass
class DayInputs:
    day: str
    decision_status: str
    decision_scan_log_id: Optional[int]
    required: list
    bars: dict                      # ticker -> BarRec | None (None = 확정 마감 후 결측·무효)
    cands: dict                     # pool -> [snapshot dict]
    acct: Optional[dict]
    account_status: Optional[str]
    next_day: Optional[str]
    notes: list = field(default_factory=list)
    set_fingerprint: Optional[tuple] = None   # 공통 무결성 검증 시점의 결정 신호 집합 지문 (integrity.fingerprint)

    @property
    def missing(self) -> list:
        return [t for t in self.required if self.bars.get(t) is None]


def _j(x) -> str:
    return json.dumps(x, ensure_ascii=False, default=str)


# ── 계좌·run ─────────────────────────────────────────────────────────

def ensure_accounts(db, start_day: str, settings=None) -> int:
    """동결 설정·판정 로직 해시로 가상 계좌를 만든다. 같은 버전 계좌의 해시가 다르면 덮어쓰지 않고 중단한다."""
    from trading.auto_buy.version import logic_hash
    h, lh, cfg, now, made = (S.config_hash(settings), logic_hash(), _j(S.frozen_config(settings)), time.time(), 0)
    with tx(db):
        for acc_id, prof, strat, seed in S.account_ids():
            row = db.execute("SELECT config_hash, logic_hash FROM ab_accounts WHERE id=?", (acc_id,)).fetchone()
            if row:
                if row[0] != h or row[1] != lh:
                    raise VersionMismatch(
                        f"{acc_id}: 기록된 설정/판정 로직 해시({row[0]}/{row[1]})와 현재({h}/{lh})가 다릅니다. "
                        f"기존 평가 버전으로 계속 처리하지 않습니다 (STRATEGY_VERSION 을 올려 새 계좌로 시작).")
                continue
            db.execute("INSERT INTO ab_accounts(id, profile, strategy, seed, strategy_version, config_hash, logic_hash, "
                       "config_json, start_day, state, created_at) VALUES (?,?,?,?,?,?,?,?,?,'ACTIVE',?)",
                       (acc_id, prof, strat, seed, S.STRATEGY_VERSION, h, lh, cfg, start_day, now))
            made += 1
    return made


def current_run(db) -> Optional[int]:
    r = db.execute("SELECT run_id FROM ab_runs WHERE status='CURRENT'").fetchone()
    return r[0] if r else None


def current_run_version(db) -> Optional[str]:
    r = db.execute("SELECT strategy_version FROM ab_runs WHERE status='CURRENT'").fetchone()
    return r[0] if r else None


def create_run(db, reason: str, parent: Optional[int], status: str, settings=None) -> int:
    from trading.auto_buy.version import logic_hash
    cur = db.execute("INSERT INTO ab_runs(status, reason, parent_run, strategy_version, config_hash, logic_hash, "
                     "created_at) VALUES (?,?,?,?,?,?,?)",
                     (status, reason, parent, S.STRATEGY_VERSION, S.config_hash(settings), logic_hash(), time.time()))
    run = cur.lastrowid
    now = time.time()
    for acc_id, prof, *_ in S.account_ids():
        p = S.PROFILES[prof]
        db.execute("INSERT INTO ab_account_runs(run_id, account_id, initial_cash, cash, realized_pnl, updated_at) "
                   "VALUES (?,?,?,?,0,?)", (run, acc_id, p["initial_cash"], p["initial_cash"] or 0.0, now))
    return run


def ensure_run(db, settings=None) -> int:
    """현재 버전의 CURRENT run. 이전 버전 run 이 CURRENT 면 SUPERSEDED 로 남기고(삭제 없음) 새 버전 run 을 만든다.
    이전 버전 run 에는 새 버전 계좌가 없으므로 재사용하지 않는다."""
    run = current_run(db)
    if run is not None and current_run_version(db) == S.STRATEGY_VERSION:
        return run
    with tx(db):
        run, ver = current_run(db), current_run_version(db)
        if run is not None and ver == S.STRATEGY_VERSION:
            return run
        if run is not None:
            db.execute("UPDATE ab_runs SET status='SUPERSEDED', completed_at=? WHERE run_id=?", (time.time(), run))
            event(db, "WARN", "version_switch", f"전략 버전 {ver} → {S.STRATEGY_VERSION}: run #{run} 보존(SUPERSEDED), "
                  f"새 버전 계좌로 새 run 시작", old_run=run, old_version=ver, new_version=S.STRATEGY_VERSION)
        run = create_run(db, "INITIAL" if ver is None else "VERSION_START", None, "CURRENT", settings)
        set_runtime(db, "current_run", run)
    return run


# ── 적격 판정 ────────────────────────────────────────────────────────

def decision_role(snap, day_input) -> str:
    if snap["intraday"] or snap["scan_status"] != "DONE":
        return "NON_DECISION"
    if day_input is not None and day_input["decision_scan_log_id"] == snap["scan_log_id"]:
        return "DECISION"
    if snap["late_after_decision"]:
        return "LATE_AFTER_DECISION"
    return "NON_DECISION"


def record_eligibility(db) -> int:
    """적격 판정을 기록하지 않은 스냅샷 중 역할을 정할 수 있는 것(그날 입력 확정됨, 또는 장중·ERROR·ABANDONED)을 기록한다.
    늦게 수집된 스냅샷도 여기서 제외 사유(LATE_SCAN_AFTER_DECISION)와 함께 남는다."""
    from trading.auto_buy.version import logic_hash
    lh = logic_hash()
    n, now = 0, time.time()
    current = None
    try:
        with tx(db):
            rows = db.execute(
                "SELECT s.*, d.decision_scan_log_id AS d_scan, d.day AS d_day FROM ab_signal_snapshots s "
                "LEFT JOIN ab_day_inputs d ON d.day = s.trading_day "
                "WHERE NOT EXISTS (SELECT 1 FROM ab_eligibility e WHERE e.snapshot_id=s.id AND e.strategy_version=?) "
                "AND (d.day IS NOT NULL OR s.intraday=1 OR s.scan_status!='DONE')", (S.STRATEGY_VERSION,)).fetchall()
            # 결정 스캔 구성원의 판정은 동결 집합 검증을 통과한 뒤에만 기록한다 (같은 트랜잭션)
            for day in sorted({r["d_day"] for r in rows if r["d_scan"] is not None and r["d_scan"] == r["scan_log_id"]}):
                IG.verify_decision_set(db, day, "record_eligibility")
            for r in rows:
                snap = dict(r)
                snap["decision_role"] = decision_role(r, {"decision_scan_log_id": r["d_scan"]} if r["d_day"] else None)
                for pool, (ok, reasons) in EL.evaluate(snap).items():
                    current = (r["id"], pool)
                    # 같은 판단의 재기록은 스키마가 멱등 무시 (rowcount 0), 다른 판단은 거부 (INSERT OR IGNORE 여도)
                    cur = db.execute("INSERT OR IGNORE INTO ab_eligibility(snapshot_id, pool, strategy_version, "
                                     "eligible, reasons, decision_role, logic_hash, created_at) VALUES (?,?,?,?,?,?,?,?)",
                                     (r["id"], pool, S.STRATEGY_VERSION, int(ok), _j(reasons), snap["decision_role"],
                                      lh, now))
                    n += cur.rowcount
    except IG.SignalSetIntegrityError as exc:
        IG.record_failure(db, exc)
        raise
    except sqlite3.IntegrityError as exc:
        if "eligibility conflict" not in str(exc):
            raise
        with tx(db):                         # 위 트랜잭션은 롤백됨 (기록 없음) — 안전 위반으로 남기고 처리를 멈춘다
            event(db, "ERROR", "eligibility_conflict", f"스냅샷 #{current[0]} {current[1]} 적격 판단이 기존 기록과 다름 "
                  f"— 기존 기록 유지, 이번 기록 전체 롤백", snapshot_id=current[0], pool=current[1],
                  strategy_version=S.STRATEGY_VERSION, logic_hash=lh)
        raise EligibilityConflict(f"snapshot {current[0]} {current[1]}: 적격 판단 충돌") from exc
    return n


def eligibility_pending(db, decision_scan_log_id: Optional[int]) -> int:
    """결정 스캔 스냅샷 중 현재 버전 적격 판정이 아직 없는 행 수. 0 이 아니면 그날 후보를 확정할 수 없다."""
    if decision_scan_log_id is None:
        return 0
    return db.execute(
        "SELECT COUNT(*) FROM ab_signal_snapshots s WHERE s.scan_log_id=? AND NOT EXISTS ("
        "SELECT 1 FROM ab_eligibility e WHERE e.snapshot_id=s.id AND e.strategy_version=?)",
        (decision_scan_log_id, S.STRATEGY_VERSION)).fetchone()[0]


def candidates(db, decision_scan_log_id: Optional[int]) -> dict:
    """{'B00': [...], 'B01': [...]} — 그날 결정 스캔 1회의 적격 스냅샷."""
    out = {"B00": [], "B01": []}
    if decision_scan_log_id is None:
        return out
    for pool in out:
        out[pool] = [dict(r) for r in db.execute(
            "SELECT s.* FROM ab_signal_snapshots s JOIN ab_eligibility e ON e.snapshot_id=s.id "
            "WHERE s.scan_log_id=? AND e.pool=? AND e.strategy_version=? AND e.eligible=1 ORDER BY s.id",
            (decision_scan_log_id, pool, S.STRATEGY_VERSION)).fetchall()]
    return out


# ── 체결 ledger ─────────────────────────────────────────────────────

def apply_fill(db, order_id: int, cum_qty: int, price: float, day: str, *, basis: str = "unspecified",
               rec: Optional[BarRec] = None) -> int:
    """누적 체결 수량으로 증분만 반영한다. 반환: 적용 증분 (같은 누적값 재전달이면 0). 트랜잭션은 호출 측."""
    o = db.execute("SELECT * FROM ab_orders WHERE id=?", (order_id,)).fetchone()
    last = db.execute("SELECT COALESCE(MAX(cum_filled_qty),0) FROM ab_fill_ledger WHERE order_id=?",
                      (order_id,)).fetchone()[0]
    if db.execute("SELECT 1 FROM ab_fill_ledger WHERE order_id=? AND cum_filled_qty=?", (order_id, cum_qty)).fetchone():
        return 0
    if cum_qty < last or cum_qty > o["qty"]:
        raise FillAnomaly(f"order {order_id} 누적 체결 {cum_qty} (이전 {last}, 주문 {o['qty']})")
    if o["state"] not in OPEN_ORDER_STATES:
        raise FillAnomaly(f"order {order_id} 상태 {o['state']} 에 체결 반영 불가")
    delta = cum_qty - last
    now = time.time()
    b = rec.bar if rec else None
    db.execute("INSERT INTO ab_fill_ledger(order_id, cum_filled_qty, applied_delta, price, day, basis, bar_open, "
               "bar_high, bar_low, bar_close, bar_provider, bar_fetched_at, bar_input_rev, applied_at, bar_volume) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (order_id, cum_qty, delta, price, day, basis, b.open if b else None, b.high if b else None,
                b.low if b else None, b.close if b else None, rec.provider if rec else None,
                rec.fetched_at if rec else None, rec.input_rev if rec else None, now, b.volume if b else None))
    prev_avg = o["avg_fill_price"] or 0.0
    avg = (prev_avg * last + price * delta) / cum_qty
    state = "FILLED" if cum_qty == o["qty"] else "PARTIAL"
    db.execute("UPDATE ab_orders SET filled_qty=?, avg_fill_price=?, state=?, updated_at=? WHERE id=?",
               (cum_qty, avg, state, now, order_id))
    cost = delta * unit_cost(price)
    risk0 = delta * (unit_cost(price) - unit_proceeds(o["stop_price"]))
    pos = db.execute("SELECT * FROM ab_positions WHERE order_id=?", (order_id,)).fetchone()
    if pos is None:
        db.execute("INSERT INTO ab_positions(run_id, account_id, order_id, signal_key, ticker, sector, entry_day, "
                   "entry_price, stop_price, qty, cost, risk0, last_close, last_close_day, state, created_at, updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,'OPEN',?,?)",
                   (o["run_id"], o["account_id"], order_id, o["signal_key"], o["ticker"], o["sector"], day, price,
                    o["stop_price"], delta, cost, risk0, now, now))
    else:
        if pos["state"] != "OPEN":
            raise FillAnomaly(f"order {order_id} 포지션이 이미 청산됨")
        q = pos["qty"] + delta
        db.execute("UPDATE ab_positions SET qty=?, entry_price=?, cost=cost+?, risk0=risk0+?, updated_at=? WHERE id=?",
                   (q, (pos["entry_price"] * pos["qty"] + price * delta) / q, cost, risk0, now, pos["id"]))
    rem = o["qty"] - cum_qty
    db.execute("UPDATE ab_reservations SET reserved_cash=?, planned_risk=?, state=?, closed_day=?, updated_at=? "
               "WHERE order_id=?", (rem * unit_cost(o["limit_price"]), rem * max(0.0, o["limit_price"] - o["stop_price"]),
                                    "CONVERTED" if rem == 0 else "ACTIVE", day if rem == 0 else None, now, order_id))
    db.execute("UPDATE ab_account_runs SET cash=cash-?, updated_at=? WHERE run_id=? AND account_id=?",
               (cost, now, o["run_id"], o["account_id"]))
    return delta


def close_order(db, order_id: int, state: str, day: str, **details) -> None:
    now = time.time()
    db.execute("UPDATE ab_orders SET state=?, updated_at=?, details=? WHERE id=? AND state IN ('PENDING','PARTIAL')",
               (state, now, _j(details), order_id))
    db.execute("UPDATE ab_reservations SET state='RELEASED', reserved_cash=0, planned_risk=0, closed_day=?, "
               "updated_at=? WHERE order_id=? AND state='ACTIVE'", (day, now, order_id))


def close_position(db, pos, day: str, px: float, reason: str, **details) -> None:
    now = time.time()
    proceeds = pos["qty"] * unit_proceeds(px)
    pnl = proceeds - pos["cost"]
    cancelled = 0
    o = db.execute("SELECT * FROM ab_orders WHERE id=?", (pos["order_id"],)).fetchone()
    if o["state"] in OPEN_ORDER_STATES:            # 매수 잔량과 손절 충돌: 매수 잔량 먼저 취소
        cancelled = o["qty"] - o["filled_qty"]
        close_order(db, o["id"], "CANCELLED_BY_STOP", day, reason="stop_triggered_with_buy_remainder")
    db.execute("UPDATE ab_positions SET state='CLOSED', exit_day=?, exit_price=?, exit_reason=?, proceeds=?, pnl=?, "
               "r_multiple=?, updated_at=? WHERE id=? AND state='OPEN'",
               (day, px, reason, proceeds, pnl, pnl / pos["risk0"] if pos["risk0"] > 0 else None, now, pos["id"]))
    db.execute("UPDATE ab_account_runs SET cash=cash+?, realized_pnl=realized_pnl+?, updated_at=? "
               "WHERE run_id=? AND account_id=?", (proceeds, pnl, now, pos["run_id"], pos["account_id"]))
    db.execute("INSERT INTO ab_stop_events(run_id, account_id, position_id, day, reason, stop_price, exit_price, qty, "
               "cancelled_buy_qty, details, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (pos["run_id"], pos["account_id"], pos["id"], day, reason, pos["stop_price"], px, pos["qty"], cancelled,
                _j(details), now))


def _rec_detail(rec: BarRec) -> dict:
    b = rec.bar
    return {"bar": [b.open, b.high, b.low, b.close], "volume": b.volume, "provider": rec.provider,
            "fetched_at": rec.fetched_at, "input_rev": rec.input_rev}


ZERO_VOLUME_EXPIRY_NOTE = "목표일 거래량 0 확정 — 미체결 만료 (다음 정규장 1일 유효, 이월·같은 신호 재주문 없음)"


# ── 하루 처리 ───────────────────────────────────────────────────────

def process_account_day(db, run_id: int, acc_id: str, inp: DayInputs, settings=None) -> dict:
    day = inp.day
    with tx(db):
        if db.execute("SELECT 1 FROM ab_daily WHERE run_id=? AND account_id=? AND day=?",
                      (run_id, acc_id, day)).fetchone():
            return {"skipped": "already_processed"}
        IG.check_inputs_current(db, inp, "process_account_day")   # 후보 사용 트랜잭션 안에서 검증 시점과 같은지 확인
        a = db.execute("SELECT * FROM ab_accounts WHERE id=?", (acc_id,)).fetchone()
        prof, strat = S.PROFILES[a["profile"]], S.STRATEGIES[a["strategy"]]
        counts = {"filled": 0, "unfilled": 0, "skip_open_stop": 0, "fill_unknown": 0, "stops": 0, "orders": 0,
                  "unresolved_new": 0, "unfilled_zero_volume": 0, "zero_volume_hold": 0}
        # (1) 체결 판정
        for o in db.execute("SELECT * FROM ab_orders WHERE run_id=? AND account_id=? AND state IN ('PENDING','PARTIAL') "
                            "AND target_day=?", (run_id, acc_id, day)).fetchall():
            rec = inp.bars.get(o["ticker"])
            if rec is None:
                db.execute("UPDATE ab_orders SET state='FILL_UNKNOWN', updated_at=?, details=? WHERE id=?",
                           (time.time(), _j({"reason": "daily_bar_missing_after_cutoff",
                                             "note": "체결 여부 불명 — 예약·위험 유지, 복구 시 재계산"}), o["id"]))
                counts["fill_unknown"] += 1
                continue
            bar = rec.bar
            if bar.no_volume:
                close_order(db, o["id"], "UNFILLED", day, reason="ZERO_VOLUME_EXPIRED", note=ZERO_VOLUME_EXPIRY_NOTE,
                            **_rec_detail(rec))
                counts["unfilled"] += 1
                counts["unfilled_zero_volume"] += 1
                continue
            st_, px, kind = try_buy(bar, o["limit_price"], o["stop_price"])
            if st_ != "FILLED":
                close_order(db, o["id"], st_, day, **_rec_detail(rec))
                counts["skip_open_stop" if st_ == "SKIP_OPEN_STOP" else "unfilled"] += 1
                continue
            if px <= o["stop_price"]:
                close_order(db, o["id"], "REJECTED_AT_FILL", day, reason="fill_at_or_below_stop", px=px,
                            **_rec_detail(rec))
                continue
            db.execute("UPDATE ab_orders SET fill_kind=? WHERE id=?", (kind, o["id"]))
            apply_fill(db, o["id"], o["qty"], px, day, basis=f"{S.FILL_MODEL}:{kind}", rec=rec)
            counts["filled"] += 1
        # (2) 가상 S01
        for p in db.execute("SELECT * FROM ab_positions WHERE run_id=? AND account_id=? AND state='OPEN'",
                            (run_id, acc_id)).fetchall():
            if p["price_status"] == "UNRESOLVED":
                continue                    # 결측일 이후 손절 판정 보류 (미발생으로 보지 않음)
            rec = inp.bars.get(p["ticker"])
            if rec is None:
                db.execute("UPDATE ab_positions SET price_status='UNRESOLVED', unresolved_since=?, updated_at=? "
                           "WHERE id=?", (day, time.time(), p["id"]))
                counts["unresolved_new"] += 1
                continue
            if rec.bar.no_volume:            # 거래 없음: 손절 체결을 만들지 않고 위험 유지 (평가만)
                db.execute("UPDATE ab_positions SET last_close=?, last_close_day=?, zero_volume_days=zero_volume_days+1, "
                           "last_zero_volume_day=?, updated_at=? WHERE id=?",
                           (rec.bar.close, day, day, time.time(), p["id"]))
                counts["zero_volume_hold"] += 1
                continue
            day0 = p["entry_day"] == day
            reason, px = check_stop(rec.bar, p["stop_price"], day0)
            if reason:
                details = _rec_detail(rec)
                if day0:
                    details["uncertainty"] = DAY0_UNCERTAINTY
                if reason == "STOP_GAP":
                    details["gap_loss_vs_stop_usd"] = (p["stop_price"] - px) * p["qty"]
                if p["zero_volume_days"]:
                    details["zero_volume_history"] = {
                        "days": p["zero_volume_days"], "last_day": p["last_zero_volume_day"],
                        "first_bar_after_resume": p["last_close_day"] == p["last_zero_volume_day"],
                        "policy": "거래량 0 인 날은 손절 체결 없음 — 거래 재개 첫 봉에서 같은 S01 규칙 적용 (갭 손실 가능)"}
                close_position(db, p, day, px, reason, **details)
                counts["stops"] += 1
            else:
                db.execute("UPDATE ab_positions SET last_close=?, last_close_day=?, updated_at=? WHERE id=?",
                           (rec.bar.close, day, time.time(), p["id"]))
        # (3) 평가·위험
        ar = db.execute("SELECT * FROM ab_account_runs WHERE run_id=? AND account_id=?", (run_id, acc_id)).fetchone()
        acct = inp.acct
        initial = ar["initial_cash"]
        if prof["kind"] == "sleeve" and acct is not None and not initial:
            initial = prof["sleeve_pct"] * acct["equity"]
            db.execute("UPDATE ab_account_runs SET initial_cash=? WHERE run_id=? AND account_id=?",
                       (initial, run_id, acc_id))
        positions = []
        for p in db.execute("SELECT * FROM ab_positions WHERE run_id=? AND account_id=? AND state='OPEN'",
                            (run_id, acc_id)).fetchall():
            rec = inp.bars.get(p["ticker"])
            unresolved = p["price_status"] == "UNRESOLVED"
            positions.append({"ticker": p["ticker"], "sector": p["sector"], "qty": p["qty"],
                              "entry_price": p["entry_price"], "stop_price": p["stop_price"], "cost": p["cost"],
                              "last_close": p["last_close"], "unresolved": unresolved,
                              "close": rec.bar.close if (rec is not None and not unresolved) else None})
        res = [dict(r) | {"unknown": r["state"] == "FILL_UNKNOWN"} for r in db.execute(
            "SELECT r.reserved_cash, r.planned_risk, r.sector, o.ticker, o.state FROM ab_reservations r "
            "JOIN ab_orders o ON o.id=r.order_id WHERE r.run_id=? AND r.account_id=? AND r.state='ACTIVE'",
            (run_id, acc_id)).fetchall()]
        st = RK.account_state(prof, positions, res, ar["cash"], ar["realized_pnl"], initial, acct)
        stale = db.execute("SELECT COUNT(*) FROM ab_orders WHERE run_id=? AND account_id=? AND "
                           "state IN ('PENDING','PARTIAL') AND target_day<=?", (run_id, acc_id, day)).fetchone()[0]
        blocks = RK.safety_blocks(prof, st, acct is not None, stale)
        # (4) 매수 판단
        pool = inp.cands.get(strat["pool"], [])
        held = {p["ticker"] for p in positions} | {r["ticker"] for r in res}
        nxt = inp.next_day
        placed = 0
        for s in EL.ordered(pool, strat["rank"], a["seed"], day):
            if db.execute("SELECT 1 FROM ab_decisions WHERE run_id=? AND account_id=? AND snapshot_id=?",
                          (run_id, acc_id, s["id"])).fetchone():
                continue
            rank_v = (EL.random_rank(a["seed"] or 0, day, s["signal_key"]) if strat["rank"] == "RANDOM"
                      else EL.grade_rank(s))
            outcome, reason, qty, info = "BLOCKED", None, 0, {}
            if s["ticker"] in held or db.execute("SELECT 1 FROM ab_orders WHERE run_id=? AND account_id=? AND "
                                                 "signal_key=?", (run_id, acc_id, s["signal_key"])).fetchone():
                reason = "DUPLICATE_HOLDING_OR_ORDER"
            elif strat["topn"] is not None and placed >= strat["topn"]:
                outcome, reason = "SKIPPED", "DAILY_TOP_N"
            elif blocks:
                reason = blocks[0]
            elif nxt is None:
                reason = "NO_NEXT_SESSION"
            else:
                qty, reason, info = RK.size_order(prof, st, s["decision_price"], s["stop_loss"], s["sector_name"])
            if reason is None:
                outcome = "ORDERED"
                now = time.time()
                sector = RK.sector_of(s["sector_name"])
                cur = db.execute(
                    "INSERT INTO ab_orders(run_id, account_id, snapshot_id, signal_key, ticker, sector, decision_day, "
                    "target_day, decision_price, limit_price, stop_price, qty, state, fill_model, created_at, "
                    "updated_at, details) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'PENDING',?,?,?,?)",
                    (run_id, acc_id, s["id"], s["signal_key"], s["ticker"], sector, day, nxt,
                     s["decision_price"], info["limit_price"], s["stop_loss"], qty, S.FILL_MODEL, now, now,
                     _j({"constrained_by": info.get("constrained_by"), "r_pct": info.get("r_pct")})))
                db.execute("INSERT INTO ab_reservations(order_id, run_id, account_id, reserved_cash, planned_risk, sector, "
                           "state, created_day, updated_at) VALUES (?,?,?,?,?,?,'ACTIVE',?,?)",
                           (cur.lastrowid, run_id, acc_id, info["reserved_cash"], info["planned_risk"], sector, day, now))
                st["reserved"] += info["reserved_cash"]
                st["free_cash"] -= info["reserved_cash"]
                st["entry_heat_usd"] += info["planned_risk"]
                st["current_heat_usd"] += info["planned_risk"]
                st["sector_values"][sector] = st["sector_values"].get(sector, 0.0) + info["reserved_cash"]
                if st.get("sleeve_used") is not None:
                    st["sleeve_used"] += info["reserved_cash"]
                held.add(s["ticker"])
                placed += 1
                counts["orders"] += 1
            db.execute("INSERT INTO ab_decisions(run_id, account_id, snapshot_id, day, rank_value, outcome, reason, qty, "
                       "details, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (run_id, acc_id, s["id"], day, rank_v, outcome, reason, qty, _j(info), time.time()))
        # 기록 (판단 후 예약 포함)
        base = st["heat_base"]
        ok = _fin(base) and base > 0
        eh = st["entry_heat_usd"] / base if ok else float("nan")
        ch = st["current_heat_usd"] / base if ok else float("nan")
        incomplete = bool(st["unresolved"] or st["unknown_orders"])
        ar = db.execute("SELECT * FROM ab_account_runs WHERE run_id=? AND account_id=?", (run_id, acc_id)).fetchone()
        db.execute("INSERT INTO ab_daily(run_id, account_id, day, data_status, equity, cash, invested, reserved, "
                   "realized_pnl, unrealized_pnl, entry_heat, current_heat, heat_base, current_heat_level, "
                   "sleeve_budget, sleeve_used, max_pos_w, max_sector_w, n_positions, n_pending, unresolved_positions, "
                   "unresolved_orders, unresolved_value, stress_full_loss, safety_blocks, account_level, counts, "
                   "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (run_id, acc_id, day, "INCOMPLETE" if incomplete else "COMPLETE", _nz(st["equity"]), ar["cash"],
                    st["invested"], st["reserved"], ar["realized_pnl"], st["unrealized"], _nz(eh), _nz(ch), _nz(base),
                    RK.heat_level(ch), st.get("sleeve_budget"), st.get("sleeve_used"), st["max_pos_w"],
                    st["max_sector_w"], len(positions), len(res) + counts["orders"], len(st["unresolved"]),
                    len(st["unknown_orders"]), st["unresolved_value"], st["stress_full_loss"], _j(blocks),
                    _j(st["acct"] or {}), _j(counts), time.time()))
        prev = db.execute("SELECT current_heat_level FROM ab_daily WHERE run_id=? AND account_id=? AND day<? "
                          "ORDER BY day DESC LIMIT 1", (run_id, acc_id, day)).fetchone()
        if RK.heat_level(ch) in ("WARN", "DANGER") and (prev is None or prev[0] != RK.heat_level(ch)):
            event(db, "WARN", "current_heat", f"{acc_id} Current Heat {ch:.1%} ({RK.heat_level(ch)}) — 표시 전용",
                  day=day, run_id=run_id)
        return {"account": acc_id, "counts": counts, "blocks": blocks, "incomplete": incomplete}


def _fin(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def _nz(x):
    return x if _fin(x) else None
