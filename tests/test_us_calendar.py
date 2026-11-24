from datetime import date, datetime, timezone

from trading.auto_stop.settings import Settings
from trading.auto_stop.us_calendar import early_closes, holidays, market_clock, session_for
from tests.auto_stop_fakes import ts


def test_2026_holidays():
    assert sorted(holidays(2026)) == [date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
                                      date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
                                      date(2026, 11, 26), date(2026, 12, 25)]


def test_known_good_friday_and_saturday_new_year():
    assert date(2025, 4, 18) in holidays(2025) and date(2024, 3, 29) in holidays(2024)
    assert date(2021, 12, 31) not in holidays(2021) and date(2022, 1, 1) not in holidays(2022)
    assert date(2021, 6, 18) not in holidays(2021)          # Juneteenth 은 2022년부터
    assert date(2022, 6, 20) in holidays(2022)              # 일요일 → 월요일 대체


def test_early_closes():
    assert sorted(early_closes(2026)) == [date(2026, 11, 27), date(2026, 12, 24)]
    assert date(2025, 7, 3) in early_closes(2025)
    s = session_for(date(2026, 11, 27))
    assert s.early_close and s.close_at.hour == 13


def test_dst_open_in_utc():
    assert datetime.fromtimestamp(session_for(date(2026, 3, 9)).open_ts, timezone.utc).hour == 13
    assert datetime.fromtimestamp(session_for(date(2026, 3, 6)).open_ts, timezone.utc).hour == 14


def test_weekend_out_of_range_and_override_closed():
    assert session_for(date(2026, 11, 28)) is None
    assert session_for(date(2045, 1, 3)) is None
    assert session_for(date(2026, 11, 23), extra_closed=frozenset({"2026-11-23"})) is None
    assert session_for(date(2026, 11, 23), extra_early=frozenset({"2026-11-23"})).early_close


def test_market_clock_cutoff_and_early_close():
    st = Settings()
    c = market_clock(ts(2026, 11, 23, 15, 54), st)
    assert c.is_open and c.before_cutoff
    c = market_clock(ts(2026, 11, 23, 15, 56), st)
    assert c.is_open and not c.before_cutoff
    c = market_clock(ts(2026, 11, 27, 12, 56), st)          # 조기폐장 cutoff 12:55
    assert c.is_open and not c.before_cutoff
    assert not market_clock(ts(2026, 11, 27, 13, 1), st).is_open
    assert not market_clock(ts(2026, 11, 23, 9, 29), st).is_open


# 공식 NYSE 일정 (https://www.nyse.com/markets/hours-calendars, 2026-11-24 확인)과 대조
OFFICIAL_HOLIDAYS = {
    2026: ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
           "2026-09-07", "2026-11-26", "2026-12-25"],
    2027: ["2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
           "2027-09-06", "2027-11-25", "2027-12-24"],
    2028: ["2028-01-17", "2028-02-21", "2028-04-14", "2028-05-29", "2028-06-19", "2028-07-04", "2028-09-04",
           "2028-11-23", "2028-12-25"],          # 2028-01-01 토요일: 대체휴일 없음
}
OFFICIAL_EARLY = {2026: ["2026-11-27", "2026-12-24"], 2027: ["2027-11-26"], 2028: ["2028-07-03", "2028-11-24"]}


def test_matches_official_nyse_calendar_2026_2028():
    for year, days in OFFICIAL_HOLIDAYS.items():
        assert sorted(d.isoformat() for d in holidays(year)) == days, year
    for year, days in OFFICIAL_EARLY.items():
        assert sorted(d.isoformat() for d in early_closes(year)) == days, year


def test_thanksgiving_and_christmas_week_2026_sessions():
    assert session_for(date(2026, 11, 26)) is None
    s = session_for(date(2026, 11, 27))
    assert s.early_close and datetime.fromtimestamp(s.close_ts, timezone.utc).hour == 18   # 13:00 EST
    assert session_for(date(2026, 12, 24)).early_close
    assert session_for(date(2026, 12, 25)) is None
    assert not session_for(date(2026, 12, 28)).early_close
    assert session_for(date(2027, 12, 24)) is None and not session_for(date(2027, 12, 23)).early_close


def test_dst_end_shifts_utc_open():
    assert datetime.fromtimestamp(session_for(date(2026, 10, 30)).open_ts, timezone.utc).hour == 13
    assert datetime.fromtimestamp(session_for(date(2026, 11, 2)).open_ts, timezone.utc).hour == 14


def test_unscheduled_closure_needs_env_override():
    # 2025-01-09 (카터 전 대통령 국가 애도일) 같은 임시 휴장은 규칙으로 알 수 없다 → env 로 추가해야 닫힌다.
    assert session_for(date(2025, 1, 9)) is not None
    assert session_for(date(2025, 1, 9), extra_closed=frozenset({"2025-01-09"})) is None


def test_fail_closed_outside_supported_range():
    assert session_for(date(2019, 12, 31)) is None and session_for(date(2041, 1, 2)) is None
    st = Settings()
    assert not market_clock(ts(2041, 1, 2, 10, 0), st).is_open
