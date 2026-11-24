"""NYSE 정규장 캘린더 (규칙 기반, America/New_York).

외부 패키지 없이 정규 휴장일·조기폐장(13:00 ET)을 계산한다. 임시 휴장(국가 애도일 등)은
예측할 수 없으므로 AUTO_STOP_US_EXTRA_CLOSED_DATES / AUTO_STOP_US_EXTRA_EARLY_CLOSE_DATES
(YYYY-MM-DD, 쉼표 구분)로 추가한다. 지원 범위(2020~2040) 밖은 장 닫힘으로 본다 (fail-closed).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
SUPPORTED_YEARS = range(2020, 2041)


def _nth_weekday(year, month, weekday, n):
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year, month, weekday):
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year):
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d):
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=64)
def holidays(year: int) -> frozenset:
    days = set()
    new_year = date(year, 1, 1)
    if new_year.weekday() == 6:
        days.add(new_year + timedelta(days=1))
    elif new_year.weekday() != 5:      # 토요일 신정은 전날(12/31)로 당기지 않는다 (NYSE 규칙)
        days.add(new_year)
    days.add(_nth_weekday(year, 1, 0, 3))          # MLK
    days.add(_nth_weekday(year, 2, 0, 3))          # Washington's Birthday
    days.add(_easter(year) - timedelta(days=2))    # Good Friday
    days.add(_last_weekday(year, 5, 0))            # Memorial Day
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))     # Juneteenth
    days.add(_observed(date(year, 7, 4)))
    days.add(_nth_weekday(year, 9, 0, 1))          # Labor Day
    days.add(_nth_weekday(year, 11, 3, 4))         # Thanksgiving
    days.add(_observed(date(year, 12, 25)))
    return frozenset(days)


@lru_cache(maxsize=64)
def early_closes(year: int) -> frozenset:
    days = set()
    july3 = date(year, 7, 3)
    if july3.weekday() <= 3:                       # 월~목 (금요일이면 7/4 대체휴일)
        days.add(july3)
    days.add(_nth_weekday(year, 11, 3, 4) + timedelta(days=1))   # 추수감사절 다음 날
    dec24 = date(year, 12, 24)
    if dec24.weekday() <= 3:
        days.add(dec24)
    return frozenset(days - holidays(year))


@dataclass(frozen=True)
class Session:
    day: date
    open_at: datetime
    close_at: datetime
    early_close: bool

    def ts(self, dt: datetime) -> float:
        return dt.timestamp()

    @property
    def open_ts(self) -> float:
        return self.open_at.timestamp()

    @property
    def close_ts(self) -> float:
        return self.close_at.timestamp()


def session_for(day: date, *, extra_closed=frozenset(), extra_early=frozenset()) -> Session | None:
    if day.year not in SUPPORTED_YEARS or day.weekday() >= 5:
        return None
    iso = day.isoformat()
    if day in holidays(day.year) or iso in extra_closed:
        return None
    early = day in early_closes(day.year) or iso in extra_early
    open_at = datetime.combine(day, REGULAR_OPEN, NEW_YORK)
    close_at = datetime.combine(day, EARLY_CLOSE if early else REGULAR_CLOSE, NEW_YORK)
    return Session(day, open_at, close_at, early)


def ny_date(ts: float) -> date:
    return datetime.fromtimestamp(ts, NEW_YORK).date()


def next_session(after_day: date, *, extra_closed=frozenset(), extra_early=frozenset(),
                 limit_days: int = 15) -> Session | None:
    d = after_day
    for _ in range(limit_days):
        d += timedelta(days=1)
        s = session_for(d, extra_closed=extra_closed, extra_early=extra_early)
        if s:
            return s
    return None


@dataclass(frozen=True)
class Clock:
    """특정 시각의 정규장 상태."""
    now: float
    session: Session | None
    is_open: bool
    before_cutoff: bool
    after_close: bool


def market_clock(now: float, settings) -> Clock:
    session = session_for(ny_date(now), extra_closed=settings.extra_closed_dates,
                          extra_early=settings.extra_early_close_dates)
    if session is None:
        return Clock(now, None, False, False, False)
    start = session.open_ts + settings.session_start_offset_min * 60
    cutoff = session.close_ts - settings.session_cutoff_min * 60
    is_open = start <= now < session.close_ts
    return Clock(now, session, is_open, is_open and now < cutoff, now >= session.close_ts)
