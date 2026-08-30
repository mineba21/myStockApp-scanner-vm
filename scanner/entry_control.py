"""Step 4 entry timing observations.

This module never creates a signal and never recalculates a base or pivot.  It
only annotates an already-created signal with signal-date/current-price entry
risk and the post-signal upthrust outcome.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import pandas as pd


def _append_unique(values: list, message: str) -> None:
    if message not in values:
        values.append(message)


def _pct(price: Optional[float], reference: Optional[float]) -> Optional[float]:
    if price is None or reference is None or reference <= 0:
        return None
    return round((float(price) - float(reference)) / float(reference) * 100.0, 2)


def _stop_pct(price: Optional[float], stop: Optional[float]) -> Optional[float]:
    if price is None or stop is None or price <= 0:
        return None
    return round((float(price) - float(stop)) / float(price) * 100.0, 2)


def evaluate_upthrust(
    daily: pd.DataFrame,
    signal_date: str,
    pivot_price: Optional[float],
    check_days: int,
) -> Tuple[Optional[bool], Optional[str]]:
    """Return ``(failed, first_failed_date)`` for the complete D+1..D+N window.

    A partial window deliberately returns ``(None, None)`` even if one of the
    already-observed bars is below pivot.  This preserves the requested
    three-state contract: True/False only after all N trading bars are closed.
    """
    if (daily is None or len(daily) == 0 or not signal_date
            or pivot_price is None or check_days <= 0):
        return None, None

    try:
        signal_ts = pd.Timestamp(signal_date)
        ordered = daily.copy().sort_index()
        index = pd.DatetimeIndex(ordered.index).tz_localize(None).normalize()
        future = ordered.loc[index > signal_ts.normalize()].head(check_days)
    except Exception:
        return None, None

    if len(future) < check_days:
        return None, None

    closes = pd.to_numeric(future["Close"], errors="coerce")
    failed = closes < float(pivot_price)
    if not bool(failed.any()):
        return False, None
    failed_index = future.index[int(failed.to_numpy().argmax())]
    return True, pd.Timestamp(failed_index).strftime("%Y-%m-%d")


def annotate_signal_entry(signal: Dict[str, Any], daily: pd.DataFrame) -> Dict[str, Any]:
    """Add signal-date pivot extension and upthrust state in place."""
    import config

    warnings = list(signal.get("entry_warnings") or [])
    flags = list(signal.get("warning_flags") or [])

    signal["pivot_ext_pct"] = None
    signal["upthrust_failed"] = None
    signal["_upthrust_failed_date"] = None

    if signal.get("signal_type") == "BREAKOUT":
        pivot_ext = _pct(signal.get("strict_price"), signal.get("pivot_price"))
        signal["pivot_ext_pct"] = pivot_ext
        if pivot_ext is not None and pivot_ext > config.MAX_PIVOT_EXT_PCT:
            message = f"피벗 대비 +{pivot_ext:.1f}% (추격 구간)"
            _append_unique(warnings, message)
            _append_unique(flags, message)

        failed, failed_date = evaluate_upthrust(
            daily,
            signal.get("signal_date"),
            signal.get("pivot_price"),
            config.UPTHRUST_CHECK_DAYS,
        )
        signal["upthrust_failed"] = failed
        signal["_upthrust_failed_date"] = failed_date
        if failed is True:
            message = (f"돌파 실패 — 신호 후 {config.UPTHRUST_CHECK_DAYS}거래일 내 "
                       "피벗 하회")
            _append_unique(warnings, message)
            _append_unique(flags, message)
        elif failed is None:
            message = (f"돌파 실패 판정 보류 "
                       f"(D+{config.UPTHRUST_CHECK_DAYS} 미도래)")
            _append_unique(warnings, message)
            _append_unique(flags, message)

    signal["entry_warnings"] = warnings
    signal["warning_flags"] = flags
    return signal


def annotate_alert_freshness(signal: Dict[str, Any]) -> Dict[str, Any]:
    """Add current-price entry risk immediately before notification enqueue."""
    import config

    warnings = list(signal.get("entry_warnings") or [])
    flags = list(signal.get("warning_flags") or [])
    signal["cur_ext_pct"] = None
    signal["cur_stop_pct"] = None
    signal["_alert_freshness_would_cut"] = False

    if signal.get("signal_type") == "BREAKOUT":
        cur_ext = _pct(signal.get("price"), signal.get("pivot_price"))
        cur_stop = _stop_pct(signal.get("price"), signal.get("stop_loss"))
        signal["cur_ext_pct"] = cur_ext
        signal["cur_stop_pct"] = cur_stop

        if cur_ext is not None and cur_ext > config.ALERT_MAX_CUR_EXT_PCT:
            message = (f"추격 구간 — 피벗 대비 +{cur_ext:.1f}% "
                       f"(기준 {config.ALERT_MAX_CUR_EXT_PCT:g}%)")
            _append_unique(warnings, message)
            _append_unique(flags, message)
            signal["_alert_freshness_would_cut"] = True
        if cur_stop is not None:
            if cur_stop < 0:
                # 현재가가 이미 stop_loss 아래로 내려간 상태 — 손절폭 초과보다
                # 심각한 케이스라 별도 문구로 강하게 표시한다.
                message = f"손절가 이탈 — 현재가가 손절선 아래 {abs(cur_stop):.1f}%"
                _append_unique(warnings, message)
                _append_unique(flags, message)
                signal["_alert_freshness_would_cut"] = True
            elif cur_stop > config.ALERT_MAX_CUR_STOP_PCT:
                message = (f"손절폭 과대 — {cur_stop:.1f}% "
                           f"(기준 {config.ALERT_MAX_CUR_STOP_PCT:g}%)")
                _append_unique(warnings, message)
                _append_unique(flags, message)
                signal["_alert_freshness_would_cut"] = True

    signal["entry_warnings"] = warnings
    signal["warning_flags"] = flags
    return signal
