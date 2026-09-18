"""Point-in-time 13F annotations for result presentation, outside scan gates.

Input is an offline, source-attributed snapshot file. Updates are picked up on the
next request. A missing/broken file cannot block results or imply no holdings.
"""
from datetime import date, datetime, timezone
from functools import lru_cache
import json
import logging
import math
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from config import (FORM13F_DATA_PATH, FORM13F_MAX_AGE_DAYS,
                    FORM13F_NEW_PRIORITY, FORM13F_INCREASED_PRIORITY,
                    FORM13F_DECREASED_PRIORITY)

logger = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
CHANGES = {"NEW", "INCREASED", "HELD", "DECREASED"}


def validate_snapshots(data):
    """Validate complete file before exposing any annotations."""
    if data.get("schema_version") != 1 or not isinstance(data.get("snapshots"), list):
        raise ValueError("Expected schema_version=1 and snapshots array")
    seen = set()
    for snapshot in data["snapshots"]:
        if snapshot["manager_cik"] != "0001536411":
            raise ValueError("Only Duquesne is supported by this feed")
        if not snapshot["manager"] or snapshot["weight_basis"] != "non_option_long":
            raise ValueError("Manager and non_option_long weight basis required")
        if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", snapshot["accession"]):
            raise ValueError("Invalid accession")
        if snapshot["accession"] in seen:
            raise ValueError("Duplicate accession")
        seen.add(snapshot["accession"])
        expected = ("https://www.sec.gov/Archives/edgar/data/1536411/"
                    + snapshot["accession"].replace("-", "") + "/")
        if not snapshot["source_url"].startswith(expected):
            raise ValueError("SEC source must match accession")
        accepted = datetime.fromisoformat(snapshot["filed_at"].replace("Z", "+00:00"))
        if accepted.tzinfo is None:
            raise ValueError("filed_at must include timezone")
        reported = date.fromisoformat(snapshot["report_date"])
        tradable = date.fromisoformat(snapshot["first_tradable"])
        if not reported <= accepted.astimezone(NY).date() < tradable:
            raise ValueError("Invalid disclosure chronology")
        holdings = snapshot["holdings"]
        if not isinstance(holdings, dict):
            raise ValueError("holdings must be keyed by ticker")
        for ticker, h in holdings.items():
            if not re.fullmatch(r"[A-Z0-9.^-]{1,20}", ticker):
                raise ValueError("Invalid ticker")
            if not re.fullmatch(r"[A-Z0-9]{9}", h["cusip"]):
                raise ValueError("Invalid CUSIP")
            if h["change_type"] not in CHANGES:
                raise ValueError("Invalid change type")
            for field in ("weight_pct", "shares", "previous_shares"):
                value = h[field]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    raise ValueError("Invalid numeric holding value")
            if h["weight_pct"] > 100 or h["shares"] <= 0:
                raise ValueError("Invalid weight or current shares")
            current, previous = h["shares"], h["previous_shares"]
            expected_change = ("NEW" if previous == 0 else "INCREASED" if current > previous
                               else "DECREASED" if current < previous else "HELD")
            if h["change_type"] != expected_change:
                raise ValueError("Change type conflicts with reported shares")
    return data["snapshots"]


@lru_cache(maxsize=4)
def _load_version(path, mtime_ns, size):
    try:
        return validate_snapshots(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        logger.warning("13F annotations unavailable: invalid snapshot file")
        return []


def load_snapshots():
    try:
        path = Path(FORM13F_DATA_PATH)
        stat = path.stat()
        return _load_version(str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return []


def annotate(row, snapshots, now=None):
    """Return a copy; never change membership, grade, quality or strict fields."""
    result = dict(row)
    result.update({"13F_NEW": None, "13F_INCREASED": None, "13F_WEIGHT": None,
                   "13F_FILED_AT": None, "13F_CHANGE_TYPE": None,
                   "13F_MANAGER": None, "13F_REPORT_DATE": None,
                   "13F_FIRST_TRADABLE": None, "13F_ACCESSION": None,
                   "13F_SOURCE_URL": None, "13F_WEIGHT_BASIS": None,
                   "13F_SHARES": None, "13F_PREV_SHARES": None,
                   "13F_SHARE_CHANGE_PCT": None, "13F_CUSIP": None,
                   "13F_STATUS": "UNAVAILABLE", "13F_PRIORITY": 0})
    if row.get("market") != "US":
        result["13F_STATUS"] = "NOT_APPLICABLE"
        return result
    now = now or datetime.now(timezone.utc)
    try:
        asof = min(date.fromisoformat(row["signal_date"]), now.astimezone(NY).date())
    except (KeyError, TypeError, ValueError):
        return result
    eligible = [s for s in snapshots if date.fromisoformat(s["first_tradable"]) <= asof
                and datetime.fromisoformat(s["filed_at"].replace("Z", "+00:00")) <= now]
    if not eligible:
        return result
    # Pick the newest report first, never resurrect an old holding if absent now.
    s = max(eligible, key=lambda s: (s["report_date"], s["filed_at"]))
    for field, source in (("MANAGER", "manager"), ("REPORT_DATE", "report_date"),
                          ("FILED_AT", "filed_at"), ("FIRST_TRADABLE", "first_tradable"),
                          ("ACCESSION", "accession"), ("SOURCE_URL", "source_url"),
                          ("WEIGHT_BASIS", "weight_basis")):
        result["13F_" + field] = s[source]
    h = s["holdings"].get(row.get("ticker", "").upper())
    stale = (asof - date.fromisoformat(s["first_tradable"])).days > FORM13F_MAX_AGE_DAYS
    result["13F_STATUS"] = "STALE" if stale else "NO_MATCH"
    if h is None:
        return result
    change = h["change_type"]
    result.update({"13F_NEW": change == "NEW", "13F_INCREASED": change == "INCREASED",
                   "13F_WEIGHT": h["weight_pct"], "13F_CHANGE_TYPE": change,
                   "13F_SHARES": h["shares"], "13F_PREV_SHARES": h["previous_shares"],
                   "13F_CUSIP": h["cusip"],
                   "13F_SHARE_CHANGE_PCT": (100 * (h["shares"] / h["previous_shares"] - 1)
                                            if h["previous_shares"] else None)})
    if not stale:
        result["13F_STATUS"] = "AVAILABLE"
        # Presentation-only choices, not empirically calibrated performance scores.
        result["13F_PRIORITY"] = {"NEW": FORM13F_NEW_PRIORITY,
                                   "INCREASED": FORM13F_INCREASED_PRIORITY,
                                   "DECREASED": FORM13F_DECREASED_PRIORITY}.get(change, 0)
    return result


def enrich_results(rows, prioritize=True, now=None):
    snapshots = load_snapshots()
    now = now or datetime.now(timezone.utc)
    result = [annotate(r, snapshots, now) for r in rows]
    if prioritize:
        # Stable within date and pass/legacy group. Limit/membership chosen upstream.
        result.sort(key=lambda r: (r.get("signal_date") or "",
                    r.get("strict_filter_passed") is not False, r["13F_PRIORITY"]), reverse=True)
    return result
