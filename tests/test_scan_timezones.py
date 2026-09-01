from datetime import datetime, timezone

from web.app import _utc_iso


def test_naive_database_time_is_serialized_as_utc():
    assert _utc_iso(datetime(2026, 9, 1, 7, 10)) == "2026-09-01T07:10:00Z"


def test_aware_time_is_normalized_to_utc():
    assert _utc_iso(datetime(2026, 9, 1, 7, 10, tzinfo=timezone.utc)) == "2026-09-01T07:10:00Z"
