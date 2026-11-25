"""스캔 시각 → 판단 거래일. NYSE 규칙 캘린더(``trading.auto_stop.us_calendar``, 순수 함수)를 읽기만 한다."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from trading.auto_stop.us_calendar import NEW_YORK, next_session, session_for


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def session(day: date, settings=None):
    kw = {}
    if settings is not None:
        kw = {"extra_closed": settings.extra_closed_dates, "extra_early": settings.extra_early_close_dates}
    return session_for(day, **kw)


def next_trading_day(day: date, settings=None) -> Optional[date]:
    kw = {}
    if settings is not None:
        kw = {"extra_closed": settings.extra_closed_dates, "extra_early": settings.extra_early_close_dates}
    s = next_session(day, **kw)
    return s.day if s else None


def scan_trading_day(started_at: datetime, settings=None) -> tuple[Optional[date], bool]:
    """(판단 거래일, 장중 여부). 판단 거래일 = 스캔 시작 시각 이전에 마감한 가장 최근 정규장.

    장중(개장 ≤ 시작 < 마감)에 시작한 스캔은 당일 미완성 봉을 볼 수 있으므로 intraday=True 로 표시하고
    매수 판단에 쓰지 않는다.
    """
    ts = _utc(started_at).astimezone(NEW_YORK)
    today = ts.date()
    s = session(today, settings)
    if s and s.open_at <= ts < s.close_at:
        intraday = True
    else:
        intraday = False
    if s and ts >= s.close_at:
        return today, intraday
    d = today
    for _ in range(15):
        d -= timedelta(days=1)
        if session(d, settings):
            return d, intraday
    return None, intraday


def last_closed_session(now: datetime, settings=None) -> Optional[date]:
    ts = _utc(now).astimezone(NEW_YORK)
    s = session(ts.date(), settings)
    if s and ts >= s.close_at:
        return ts.date()
    d = ts.date()
    for _ in range(15):
        d -= timedelta(days=1)
        if session(d, settings):
            return d
    return None


def market_open(now: datetime, settings=None) -> bool:
    ts = _utc(now).astimezone(NEW_YORK)
    s = session(ts.date(), settings)
    return bool(s and s.open_at <= ts < s.close_at)


def trading_days_between(start: date, end: date, settings=None) -> list[date]:
    out, d = [], start
    while d <= end:
        if session(d, settings):
            out.append(d)
        d += timedelta(days=1)
    return out


def bucket_day(ts: datetime, settings=None) -> Optional[date]:
    """시각 → 그 시각이 속한 거래일 구간 (개장 시각이 ts 이하인 가장 최근 정규장).

    장 마감 후 저녁 스캔 = 그날, 다음 날 장중 = 다음 날, 개장 전 = 직전 정규장. P7 의 '최초 등장일'(일 단위)과 맞춘다.
    """
    t = _utc(ts).astimezone(NEW_YORK)
    s = session(t.date(), settings)
    if s and t >= s.open_at:
        return t.date()
    d = t.date()
    for _ in range(15):
        d -= timedelta(days=1)
        if session(d, settings):
            return d
    return None


def at_et(day: date, hhmm: str) -> datetime:
    """거래일 ``day`` 의 동부시간 HH:MM (UTC aware)."""
    hh, mm = (int(x) for x in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=NEW_YORK).astimezone(timezone.utc)


def close_at(day: date, settings=None) -> Optional[datetime]:
    s = session(day, settings)
    return s.close_at.astimezone(timezone.utc) if s else None


def prev_trading_day(day: date, settings=None) -> Optional[date]:
    d = day
    for _ in range(15):
        d -= timedelta(days=1)
        if session(d, settings):
            return d
    return None
