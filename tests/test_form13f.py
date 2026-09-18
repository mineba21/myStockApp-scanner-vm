from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from web import form13f as f


@pytest.fixture
def snapshot():
    return {"manager": "Duquesne Family Office", "manager_cik": "0001536411",
            "report_date": "2026-06-30", "filed_at": "2026-08-14T19:55:57Z",
            "first_tradable": "2026-08-17", "accession": "0001536411-26-000006",
            "weight_basis": "non_option_long",
            "source_url": "https://www.sec.gov/Archives/edgar/data/1536411/000153641126000006/xslForm13F_X02/form13f_20260630.xml",
            "holdings": {"NEW": {"cusip": "02079K305", "change_type": "NEW",
                                 "weight_pct": 2.76, "shares": 300, "previous_shares": 0},
                         "INC": {"cusip": "023135106", "change_type": "INCREASED",
                                 "weight_pct": 2.96, "shares": 200, "previous_shares": 100}}}


def row(ticker="NEW", day="2026-08-17", **kwargs):
    return dict(ticker=ticker, signal_date=day, market="US",
                strict_filter_passed=True, grade="A", filter_reasons=[], **kwargs)


NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)


def test_publication_and_signal_date_boundaries(snapshot):
    for day in ("2026-06-30", "2026-08-14", "2026-08-16"):
        assert f.annotate(row(day=day), [snapshot], NOW)["13F_NEW"] is None
    result = f.annotate(row(), [snapshot], NOW)
    assert result["13F_NEW"] is True and result["13F_INCREASED"] is False
    assert result["13F_SHARE_CHANGE_PCT"] is None
    # A future-dated result must not bypass today's clock.
    before_publication = datetime(2026, 8, 14, 18, tzinfo=timezone.utc)
    assert f.annotate(row(), [snapshot], before_publication)["13F_NEW"] is None


def test_preserves_every_technical_field_and_does_not_mutate(snapshot):
    original = row("INC")
    original.update(strict_filter_passed=False, filter_reasons=["rs_below_zero"])
    before = deepcopy(original)
    result = f.annotate(original, [snapshot], NOW)
    assert original == before
    assert all(result[k] == v for k, v in original.items())
    assert result["13F_INCREASED"] is True and result["13F_NEW"] is False
    assert result["13F_SHARE_CHANGE_PCT"] == 100


def test_no_match_unavailable_kr_and_stale(snapshot):
    assert f.annotate(row("MISSING"), [snapshot], NOW)["13F_STATUS"] == "NO_MATCH"
    assert f.annotate(row(), [], NOW)["13F_STATUS"] == "UNAVAILABLE"
    kr = row(); kr["market"] = "KR"
    assert f.annotate(kr, [snapshot], NOW)["13F_STATUS"] == "NOT_APPLICABLE"
    stale = f.annotate(row(day="2027-02-01"), [snapshot], datetime(2027, 2, 2, tzinfo=timezone.utc))
    assert stale["13F_STATUS"] == "STALE" and stale["13F_PRIORITY"] == 0
    assert stale["13F_WEIGHT"] == 2.76


def test_new_report_does_not_resurrect_old_holding(snapshot):
    latest = deepcopy(snapshot)
    latest.update(report_date="2026-09-30", filed_at="2026-11-13T20:00:00Z", first_tradable="2026-11-16")
    latest["holdings"] = {}
    result = f.annotate(row(day="2026-11-17"), [snapshot, latest], datetime(2026, 11, 18, tzinfo=timezone.utc))
    assert result["13F_STATUS"] == "NO_MATCH"
    assert result["13F_NEW"] is None and result["13F_PRIORITY"] == 0


@pytest.mark.parametrize("field,value", [("weight_pct", float("nan")), ("weight_pct", 101),
                                        ("previous_shares", 200), ("shares", -1)])
def test_rejects_invalid_or_inconsistent_input(snapshot, field, value):
    snapshot["holdings"]["NEW"][field] = value
    with pytest.raises(ValueError):
        f.validate_snapshots({"schema_version": 1, "snapshots": [snapshot]})


def test_sort_changes_only_display_order(monkeypatch, snapshot):
    monkeypatch.setattr(f, "load_snapshots", lambda: [snapshot])
    rows = [row("MISSING", id=1), row("INC", id=2), row("NEW", id=3), row("NEW", day="2026-08-16", id=4)]
    rejected = row("NEW", id=5); rejected["strict_filter_passed"] = False
    rows.append(rejected)
    enriched = f.enrich_results(rows, now=NOW)
    assert [r["id"] for r in enriched] == [3, 2, 1, 5, 4]
    assert {r["id"] for r in enriched} == {r["id"] for r in rows}
    assert [r["id"] for r in f.enrich_results(rows, False, now=NOW)] == [1, 2, 3, 4, 5]


def test_file_failure_and_recovery(monkeypatch, tmp_path, snapshot):
    path = tmp_path / "feed.json"
    monkeypatch.setattr(f, "FORM13F_DATA_PATH", str(path))
    assert f.load_snapshots() == []
    path.write_text("broken")
    assert f.load_snapshots() == []
    path.write_text(json.dumps({"schema_version": 1, "snapshots": [snapshot]}))
    assert len(f.load_snapshots()) == 1
