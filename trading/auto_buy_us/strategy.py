"""B00/B01 적격 판정·등급 순위 (순수 함수). PR #14 eligibility 정의를 결정 스캔 전용으로 옮김.

입력은 ``abu_signals`` 행 dict. 판단가는 ``price`` 컬럼. strict 류는 1/0/None(미확정).
"""
from __future__ import annotations

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


def warnings_of(sig: dict) -> list:
    raw = sig.get("entry_warnings")
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return [raw]
    if not isinstance(raw, (list, tuple)):
        return [str(raw)]
    return [str(w) for w in raw]


def common_reasons(sig: dict) -> list[str]:
    r: list[str] = []
    strict = sig.get("strict")
    if strict is None:
        r.append("STRICT_UNASSESSED")
    elif not strict:
        r.append("NOT_STRICT")
    else:
        fp = sig.get("first_strict_pass")
        if fp is None:
            r.append("FIRST_PASS_UNKNOWN")
        elif not fp:
            r.append("ALREADY_PASSED")
    price, stop = _f(sig.get("price")), _f(sig.get("stop_loss"))
    if price is None or price <= 0:
        r.append("INVALID_PRICE")
    if stop is None or stop <= 0 or (price is not None and stop >= price):
        r.append("INVALID_STOP")
    if price is not None and 0 < price < 1:
        r.append("PRICE_BELOW_1")
    return r


def b01_reasons(sig: dict) -> list[str]:
    r: list[str] = []
    if not sig.get("is_first_detection"):
        r.append("LATE_STRICT_PASS")
    if sig.get("market_condition") != "BULL":
        r.append("MARKET_NOT_BULL")
    ext = _f(sig.get("ext_pct"))
    if sig.get("signal_type") in BO and ext is not None and ext < 0:
        r.append("BELOW_PIVOT")
    if any("추격" in w for w in warnings_of(sig)):
        r.append("CHASE_WARNING")
    stop_pct = _f(sig.get("stop_pct"))
    if stop_pct is None or stop_pct > 15:
        r.append("STOP_GT_15")
    return r


def evaluate(sig: dict) -> dict[str, tuple[bool, list[str]]]:
    common = common_reasons(sig)
    b01 = common + b01_reasons(sig)
    return {"B00": (not common, common), "B01": (not b01, b01)}


def grade_rank(sig: dict) -> int:
    """Grade → Signal Quality 순위 (모르는 값은 3 = 최하)."""
    return GRADE_ORDER.get(sig.get("grade"), 3) * 10 + QUALITY_ORDER.get(sig.get("signal_quality"), 3)


def ordered(sigs: Iterable[dict]) -> list[dict]:
    return sorted(sigs, key=lambda s: (grade_rank(s), s["ticker"], s["signal_key"]))
