"""Import source-attributed research events as display-only 13F snapshots.

Usage: .venv/bin/python scripts/import_13f_research.py ../research/audited_results.json
This does not fetch filings, modify scan results, or execute orders.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from web.form13f import validate_snapshots


def build_snapshot(research, period=None):
    filing = next(f for f in reversed(research["filings"])
                  if f["period"] == (period or research["filings"][-1]["period"]))
    if not filing["prior_consecutive"]:
        raise ValueError("Missing prior quarter: cannot classify changes")
    events = [e for e in research["events"] if e["period"] == filing["period"]]
    holdings = {}
    for e in events:
        if not e["ticker"]:
            continue
        if e["ticker"] in holdings:
            raise ValueError("Duplicate ticker; resolve CUSIP mapping before import")
        holdings[e["ticker"]] = {
            "cusip": e["cusip"], "change_type": {"INC": "INCREASED", "DEC": "DECREASED"}.get(e["change"], e["change"]),
            "weight_pct": e["weight_equity_pct"], "shares": e["shares"],
            "previous_shares": e["prior_shares"],
        }
    accession_digits = filing["url"].split("/1536411/")[1].split("/")[0]
    accession = f'{accession_digits[:10]}-{accession_digits[10:12]}-{accession_digits[12:]}'
    return {"manager": "Duquesne Family Office", "manager_cik": "0001536411",
            "report_date": filing["period"], "filed_at": filing["accepted"],
            "first_tradable": filing["entry"], "accession": accession,
            "source_url": filing["url"], "weight_basis": "non_option_long",
            "mapping_source": "research CUSIP mapping; unmapped securities omitted",
            "corporate_actions_adjusted": False,
            "unmapped_count": sum(not e["ticker"] for e in events), "holdings": holdings}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("research", type=Path)
    parser.add_argument("--period")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "duquesne_13f.json")
    args = parser.parse_args()
    snapshot = build_snapshot(json.loads(args.research.read_text()), args.period)
    data = json.loads(args.output.read_text()) if args.output.exists() else {"schema_version": 1, "snapshots": []}
    validate_snapshots(data)
    data["snapshots"] = [s for s in data["snapshots"] if s["accession"] != snapshot["accession"]] + [snapshot]
    data["snapshots"].sort(key=lambda s: (s["report_date"], s["filed_at"]))
    validate_snapshots(data)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(args.output)
    print(f'{snapshot["report_date"]}: {len(snapshot["holdings"])} linked holdings; '
          f'{snapshot["unmapped_count"]} unmapped; saved {args.output}')


if __name__ == "__main__":
    main()
