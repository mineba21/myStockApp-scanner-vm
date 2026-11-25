"""매수 적격 판정 (순수 함수). P0~P7 백테스트 정의를 그대로 옮긴다 (새 점수제·새 조건 없음).

공통 (모든 전략의 후보 조건, 백테스트 후보 = signal_key 별 최초 Strict 통과 스냅샷)
- ``SCAN_NOT_DONE``      스캔이 DONE 이 아님
- ``INTRADAY_SCAN``      장중 시작 스캔 (미완성 일봉 가능)
- ``STRICT_UNASSESSED``  Strict 미평가(null) — 통과로 보지 않는다
- ``NOT_STRICT``         Strict 거부
- ``FIRST_PASS_UNKNOWN`` 최초 Strict 통과인지 수집 이력으로 확정할 수 없음 (수집 시작 이전 신호·수집 누락)
- ``ALREADY_PASSED``     같은 signal_key 가 이전 거래일 결정 스캔에서 이미 Strict 통과 (재스캔 반복 행)
- ``ALREADY_PASSED_NOT_DECIDED`` 이전 거래일에 Strict 통과했지만 결정 스캔이 아니었음 (늦은 재스캔 등).
                         다음 날 신규 신호로 바꾸지 않되, 이력은 이 사유로 남긴다
- ``NOT_DECISION_SCAN``  그 거래일의 정규 결정 스캔이 아님 (확정 전 비정규 스캔, ERROR·ABANDONED, 결정 스캔 없는 날)
- ``LATE_SCAN_AFTER_DECISION`` 당일 후보 확정 이후 스캔 (당일 후보에 추가하지 않음)
- ``INVALID_PRICE`` / ``INVALID_STOP`` 판단가·손절가 결측·비정상 (손절가 ≥ 판단가 포함)

B01 추가 조건 (P7 확정 정의, 추격 경고 제외 포함. P6 의 B01HX 와 다르다)
- ``LATE_STRICT_PASS``   최초 Strict 통과가 최초 탐지가 아님
- ``MARKET_NOT_BULL``    스캔 시점 시장 상태가 BULL 이 아님
- ``BELOW_PIVOT``        BREAKOUT/RE_BREAKOUT 이 피벗 아래 (판단가 기준 ext < 0)
- ``CHASE_WARNING``      운영 entry_control 의 '추격' 경고
- ``STOP_GT_15``         손절폭 > 15%
REBOUND 는 피벗이 없어 피벗 조건을 적용하지 않는다. STRONG·Grade A·손절폭 7%·주봉 거래량 경고·판정 보류는 제외하지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Iterable, Optional

BO = ("BREAKOUT", "RE_BREAKOUT")
GRADE_ORDER = {"S": 0, "A": 1, "B": 2}
QUALITY_ORDER = {"STRONG": 0, "MODERATE": 1, "WEAK": 2}


def _f(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def warnings_of(snap) -> list:
    raw = snap.get("entry_warnings")
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return [raw]
    return [str(w) for w in (raw or [])]


ROLE_REASON = {"LATE_AFTER_DECISION": "LATE_SCAN_AFTER_DECISION", "NON_DECISION": "NOT_DECISION_SCAN"}


def common_reasons(snap) -> list:
    r = []
    role = snap.get("decision_role", "DECISION")
    if role != "DECISION":
        r.append(ROLE_REASON.get(role, "NOT_DECISION_SCAN"))
    if snap.get("scan_status") != "DONE":
        r.append("SCAN_NOT_DONE")
    if snap.get("intraday"):
        r.append("INTRADAY_SCAN")
    sp = snap.get("strict_passed")
    if sp is None:
        r.append("STRICT_UNASSESSED")
    elif not sp:
        r.append("NOT_STRICT")
    else:
        fp = snap.get("first_strict_pass")
        if fp is None:
            r.append("FIRST_PASS_UNKNOWN")
        elif not fp:
            r.append("ALREADY_PASSED_NOT_DECIDED" if snap.get("first_pass_basis") == "PRIOR_STRICT_NOT_DECIDED"
                     else "ALREADY_PASSED")
    price, stop = _f(snap.get("decision_price")), _f(snap.get("stop_loss"))
    if price is None or price <= 0:
        r.append("INVALID_PRICE")
    if stop is None or stop <= 0 or (price is not None and stop >= price):
        r.append("INVALID_STOP")
    return r


def b01_reasons(snap) -> list:
    r = []
    if not snap.get("is_first_detection"):
        r.append("LATE_STRICT_PASS")
    if snap.get("market_condition") != "BULL":
        r.append("MARKET_NOT_BULL")
    ext = _f(snap.get("ext_pct"))
    if snap.get("signal_type") in BO and ext is not None and ext < 0:
        r.append("BELOW_PIVOT")
    if any("추격" in w for w in warnings_of(snap)):
        r.append("CHASE_WARNING")
    stop_pct = _f(snap.get("stop_pct"))
    if stop_pct is None or stop_pct > 15:
        r.append("STOP_GT_15")
    return r


def evaluate(snap) -> dict:
    """{'B00': (eligible, reasons), 'B01': (eligible, reasons)}."""
    common = common_reasons(snap)
    b01 = common + b01_reasons(snap)
    return {"B00": (not common, common), "B01": (not b01, b01)}


def grade_rank(snap) -> float:
    """운영 우선순위와 같은 Grade → Signal Quality (동률은 티커 순으로 호출 측에서 정렬)."""
    return GRADE_ORDER.get(snap.get("grade"), 3) * 10 + QUALITY_ORDER.get(snap.get("signal_quality"), 3)


def random_rank(seed: int, day: str, key: str) -> float:
    """재현 가능한 무작위 순위 (seed·거래일·signal_key 해시). 같은 입력이면 항상 같은 순서."""
    h = hashlib.sha256(f"{seed}:{day}:{key}".encode()).hexdigest()
    return int(h[:12], 16) / float(16 ** 12)


def ordered(cands: Iterable[dict], rank: str, seed: Optional[int], day: str) -> list:
    rows = list(cands)
    if rank == "RANDOM":
        return sorted(rows, key=lambda s: (random_rank(seed or 0, day, s["signal_key"]), s["ticker"]))
    return sorted(rows, key=lambda s: (grade_rank(s), s["ticker"], s["signal_key"]))
