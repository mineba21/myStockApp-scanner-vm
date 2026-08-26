"""Market-session-aware scan context and finalized OHLCV helpers.

Daily provider rows are labelled by their exchange session date.  A scan may
only consume rows whose session has closed plus the configured publication
delay.  Weekly rows are retained only after the final exchange session of that
calendar week has completed, including holiday-shortened weeks.
"""

import warnings
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

from config import FINAL_BAR_DELAY_MINUTES, STRATEGY_VERSION


_CALENDAR_NAMES = {"KR": "XKRX", "US": "XNYS"}
_TIMEZONE_NAMES = {"KR": "Asia/Seoul", "US": "America/New_York"}


def _market_code(market: str) -> str:
    code = (market or "").strip().upper()
    if code not in _CALENDAR_NAMES:
        raise ValueError("market must be KR or US")
    return code


def _as_utc(value: Optional[Union[datetime, pd.Timestamp]]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        raise ValueError(
            "as_of must include an explicit timezone (for example, +09:00 or Z)"
        )
    ts = ts.tz_convert("UTC")
    return ts.to_pydatetime()


def _calendar(market: str):
    # pandas<2 / NumPy>=1.25 조합에서 exchange_calendars 초기화가 내는
    # 제3자 내부 deprecation만 국소적으로 숨긴다.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="np.find_common_type is deprecated.*",
            category=DeprecationWarning,
        )
        return xcals.get_calendar(_CALENDAR_NAMES[_market_code(market)])


def _freshness_metadata(index: pd.DatetimeIndex, context: "ScanContext") -> dict:
    """Describe whether the latest available row reaches the scan session."""
    if len(index) == 0:
        return {
            "bar_status": "INSUFFICIENT_DATA",
            "data_status": "INSUFFICIENT_DATA",
            "last_bar_date": None,
            "staleness_sessions": None,
        }

    last_bar_date = pd.Timestamp(index[-1]).date()
    cal = _calendar(context.market)
    sessions = cal.sessions_in_range(
        pd.Timestamp(last_bar_date), pd.Timestamp(context.session_date)
    )
    staleness = max(len(sessions) - 1, 0)
    status = "FINAL" if staleness == 0 else "STALE"
    return {
        "bar_status": status,
        "data_status": status,
        "last_bar_date": last_bar_date.isoformat(),
        "staleness_sessions": staleness,
    }


def last_completed_session(
    market: str,
    as_of: Optional[Union[datetime, pd.Timestamp]] = None,
    delay_minutes: int = FINAL_BAR_DELAY_MINUTES,
) -> date:
    """Return the latest exchange session whose close is final at ``as_of``."""
    if delay_minutes < 0:
        raise ValueError("delay_minutes must be non-negative")

    cal = _calendar(market)
    effective = pd.Timestamp(_as_utc(as_of)) - pd.Timedelta(minutes=delay_minutes)
    session = cal.minute_to_session(effective, direction="previous")
    if cal.session_close(session) > effective:
        session = cal.previous_session(session)
    return pd.Timestamp(session).date()


def last_started_session(
    market: str,
    as_of: Optional[Union[datetime, pd.Timestamp]] = None,
) -> date:
    """Return the latest session that has opened by ``as_of``."""
    code = _market_code(market)
    utc_as_of = pd.Timestamp(_as_utc(as_of))
    local_date = utc_as_of.tz_convert(_TIMEZONE_NAMES[code]).date()
    cal = _calendar(code)
    session = cal.date_to_session(pd.Timestamp(local_date), direction="previous")
    if cal.session_open(session) > utc_as_of:
        session = cal.previous_session(session)
    return pd.Timestamp(session).date()


@dataclass(frozen=True)
class ScanContext:
    """Immutable point-in-time contract shared by fetch, scan and replay paths."""

    market: str
    as_of: datetime
    session_date: date
    strategy_version: str = STRATEGY_VERSION
    final_bar_delay_minutes: int = FINAL_BAR_DELAY_MINUTES

    @classmethod
    def create(
        cls,
        market: str,
        as_of: Optional[Union[datetime, pd.Timestamp]] = None,
        strategy_version: str = STRATEGY_VERSION,
        final_bar_delay_minutes: int = FINAL_BAR_DELAY_MINUTES,
    ) -> "ScanContext":
        code = _market_code(market)
        utc_as_of = _as_utc(as_of)
        return cls(
            market=code,
            as_of=utc_as_of,
            session_date=last_completed_session(
                code, utc_as_of, delay_minutes=final_bar_delay_minutes
            ),
            strategy_version=strategy_version,
            final_bar_delay_minutes=final_bar_delay_minutes,
        )

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(_TIMEZONE_NAMES[self.market])

    def for_session(self, session_date: Union[date, str, pd.Timestamp]) -> "ScanContext":
        """Create an as-of context immediately after a specific session closes."""
        label = pd.Timestamp(session_date).normalize()
        cal = _calendar(self.market)
        if not cal.is_session(label):
            label = cal.date_to_session(label, direction="previous")
        as_of = cal.session_close(label) + pd.Timedelta(
            minutes=self.final_bar_delay_minutes
        )
        return ScanContext.create(
            self.market,
            as_of=as_of,
            strategy_version=self.strategy_version,
            final_bar_delay_minutes=self.final_bar_delay_minutes,
        )


def normalize_ohlcv(df: Optional[pd.DataFrame], context: ScanContext) -> pd.DataFrame:
    """Normalize daily index to local session dates and remove non-final rows."""
    if df is None or len(df) == 0:
        normalized = pd.DataFrame()
        normalized.attrs.update(_freshness_metadata(normalized.index, context))
        normalized.attrs.update({
            "market": context.market,
            "as_of": context.as_of.isoformat(),
            "session_date": context.session_date.isoformat(),
        })
        return normalized

    required = ["Open", "High", "Low", "Close", "Volume"]
    missing = [column for column in required if column not in df.columns]
    if missing:
        raise ValueError(f"OHLCV columns missing: {', '.join(missing)}")

    normalized = df.loc[:, required].copy()
    idx = pd.DatetimeIndex(pd.to_datetime(normalized.index))
    if idx.tz is not None:
        idx = idx.tz_convert(context.timezone).tz_localize(None)
    normalized.index = idx.normalize()
    normalized = normalized[~normalized.index.duplicated(keep="last")].sort_index()
    normalized = normalized.loc[
        normalized.index <= pd.Timestamp(context.session_date)
    ]
    if len(normalized):
        cal = _calendar(context.market)
        sessions = cal.sessions_in_range(
            normalized.index.min(), normalized.index.max()
        )
        session_dates = pd.DatetimeIndex(sessions).tz_localize(None).normalize()
        session_ns = set(session_dates.asi8.tolist())
        normalized = normalized.loc[
            [value in session_ns for value in normalized.index.asi8]
        ]
    normalized = normalized.dropna(how="any")
    normalized.attrs.update(_freshness_metadata(normalized.index, context))
    normalized.attrs.update({
        "market": context.market,
        "as_of": context.as_of.isoformat(),
        "session_date": context.session_date.isoformat(),
    })
    return normalized


def normalize_close_series(
    series: Optional[pd.Series], context: ScanContext
) -> Optional[pd.Series]:
    """Apply the same finalized-session cutoff to a benchmark close series."""
    if series is None:
        return None
    if len(series) == 0:
        normalized = series.copy()
        normalized.attrs.update(_freshness_metadata(normalized.index, context))
        normalized.attrs.update({
            "market": context.market,
            "as_of": context.as_of.isoformat(),
            "session_date": context.session_date.isoformat(),
        })
        return normalized

    normalized = series.copy()
    idx = pd.DatetimeIndex(pd.to_datetime(normalized.index))
    if idx.tz is not None:
        idx = idx.tz_convert(context.timezone).tz_localize(None)
    normalized.index = idx.normalize()
    normalized = normalized[~normalized.index.duplicated(keep="last")].sort_index()
    normalized = normalized.loc[
        normalized.index <= pd.Timestamp(context.session_date)
    ]
    if len(normalized):
        cal = _calendar(context.market)
        sessions = cal.sessions_in_range(
            normalized.index.min(), normalized.index.max()
        )
        session_dates = pd.DatetimeIndex(sessions).tz_localize(None).normalize()
        session_ns = set(session_dates.asi8.tolist())
        normalized = normalized.loc[
            [value in session_ns for value in normalized.index.asi8]
        ]
    normalized.attrs.update(_freshness_metadata(normalized.index, context))
    normalized.attrs.update({
        "market": context.market,
        "as_of": context.as_of.isoformat(),
        "session_date": context.session_date.isoformat(),
    })
    return normalized


def completed_week_label(context: ScanContext) -> date:
    """Return the last completed W-FRI label, respecting exchange holidays."""
    session = context.session_date
    monday = session - timedelta(days=session.weekday())
    friday = monday + timedelta(days=4)
    cal = _calendar(context.market)
    sessions = cal.sessions_in_range(pd.Timestamp(monday), pd.Timestamp(friday))
    if len(sessions) and session >= pd.Timestamp(sessions[-1]).date():
        return friday
    return friday - timedelta(days=7)


def finalize_weekly_ohlcv(
    weekly_df: Optional[pd.DataFrame], context: ScanContext
) -> pd.DataFrame:
    """Remove a resampled row until that exchange week's final session closes."""
    if weekly_df is None or len(weekly_df) == 0:
        return pd.DataFrame()
    finalized = weekly_df.copy()
    idx = pd.DatetimeIndex(pd.to_datetime(finalized.index))
    if idx.tz is not None:
        idx = idx.tz_convert(context.timezone).tz_localize(None)
    finalized.index = idx.normalize()
    finalized = finalized.loc[
        finalized.index <= pd.Timestamp(completed_week_label(context))
    ]
    finalized.attrs.update(
        {
            "bar_status": "FINAL",
            "data_status": "FINAL",
            "market": context.market,
            "as_of": context.as_of.isoformat(),
            "session_date": context.session_date.isoformat(),
            "last_bar_date": (
                finalized.index[-1].date().isoformat() if len(finalized) else None
            ),
        }
    )
    return finalized
