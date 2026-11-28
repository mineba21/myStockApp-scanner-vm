"""단일 파일 HTML 보고서 (외부 스크립트 없음, 인라인 SVG 차트, 라이트/다크)."""
from __future__ import annotations

import html
import json
import math
import re
from pathlib import Path

import numpy as np
import pandas as pd

from .data import OUTPUT_DIR, PKG_DIR

OUT = OUTPUT_DIR
_KEEP = ["role", "profile", "pool", "market", "rank", "topn", "timing", "horizon", "scenario", "heat", "segment",
         "sizing", "slip_bp", "total_return", "cagr", "mdd", "sharpe", "calmar", "profit_factor",
         "profit_factor_incl_open", "win_rate", "stop_rate", "trades", "trades_per_year", "avg_R", "pnl_per_risk",
         "capital_utilization", "opportunity_reduction", "max_consec_stops", "heat_current_avg", "heat_current_max",
         "open_at_end", "open_unrealized_pnl", "open_capital_share", "open_avg_age_days", "avg_hold_days",
         "fill_rate", "pool_size"]


def _clean(o):
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        return None if not math.isfinite(float(o)) else round(float(o), 6)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return o


def _records(df: pd.DataFrame, cols=None):
    if cols:
        df = df[[c for c in cols if c in df.columns]]
    return _clean(df.to_dict(orient="records"))


def md_to_html(md: str) -> str:
    out, lines, i = [], md.splitlines(), 0
    inline = lambda s: re.sub(r"`([^`]+)`", r"<code>\1</code>",                       # noqa: E731
                              re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", html.escape(s)))
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("```"):
            j = i + 1
            buf = []
            while j < len(lines) and not lines[j].startswith("```"):
                buf.append(html.escape(lines[j])); j += 1
            out.append("<pre>" + "\n".join(buf) + "</pre>"); i = j + 1; continue
        m = re.match(r"^(#{1,4})\s+(.*)", ln)
        if m:
            lv = min(len(m.group(1)) + 1, 5)
            out.append(f"<h{lv}>{inline(m.group(2))}</h{lv}>"); i += 1; continue
        if ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.match(r"^:?-+:?$", c) for c in cells):
                    rows.append(cells)
                i += 1
            t = "<div class='tw'><table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in rows[0]) + "</tr></thead><tbody>"
            t += "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in rows[1:])
            out.append(t + "</tbody></table></div>"); continue
        if re.match(r"^\s*(-|\d+\.)\s+", ln):
            tag = "ol" if re.match(r"^\s*\d+\.", ln) else "ul"
            buf = []
            while i < len(lines) and re.match(r"^\s*(-|\d+\.)\s+", lines[i]):
                buf.append("<li>" + inline(re.sub(r"^\s*(-|\d+\.)\s+", "", lines[i])) + "</li>"); i += 1
            out.append(f"<{tag}>" + "".join(buf) + f"</{tag}>"); continue
        if ln.strip():
            out.append(f"<p>{inline(ln)}</p>")
        i += 1
    return "\n".join(out)


def _p7cfg():
    from .data import load_config
    c = load_config()["p7"]
    return {"profiles": c["profiles"], "strategies": c["strategies"]}


def build() -> Path:
    audit = json.load(open(OUT / "data_audit.json", encoding="utf-8"))
    verdict = json.load(open(OUT / "verdict.json", encoding="utf-8"))
    rec = json.load(open(OUT / "recommended_entry_rules.json", encoding="utf-8"))
    summ = pd.read_csv(OUT / "strategy_summary.csv")
    fs = pd.read_csv(OUT / "factor_study.csv")
    cg = pd.read_csv(OUT / "grade_quality_controlled.csv")
    et = pd.read_csv(OUT / "entry_timing.csv")
    rk = pd.read_csv(OUT / "rank_vs_random.csv")
    rnd = pd.read_csv(OUT / "random_baseline.csv")
    wf = pd.read_csv(OUT / "walk_forward.csv")
    rp = pd.read_csv(OUT / "regime_performance.csv")
    vs = pd.read_csv(OUT / "validation_selection.csv")
    sf = pd.read_csv(OUT / "score_fit_train.csv")
    ph = pd.read_csv(OUT / "posthoc_exploration.csv") if (OUT / "posthoc_exploration.csv").exists() else pd.DataFrame()
    eq = pd.read_csv(OUT / "daily_equity.csv")
    if (OUT / "posthoc_equity.csv").exists():
        pe = pd.read_csv(OUT / "posthoc_equity.csv")
        eq = pd.concat([eq, pe[~pe["series"].isin(eq["series"].unique())]], ignore_index=True)
    tl = pd.read_csv(OUT / "trade_log.csv")
    design = (PKG_DIR / "docs" / "AUTO_BUY_S01_LINK_DESIGN.md").read_text(encoding="utf-8")
    plan_p = PKG_DIR / "docs" / "AUTO_BUY_SHADOW_PLAN.md"
    plan = plan_p.read_text(encoding="utf-8") if plan_p.exists() else ""
    p7 = {}
    if (OUT / "p7_summary.csv").exists():
        p7s = pd.read_csv(OUT / "p7_summary.csv")
        p7cols = [c for c in p7s.columns if c in _KEEP or c.startswith(("block_", "bind_")) or c in (
            "strategy", "group", "RISK_PCT", "HEAT_PCT", "POS_PCT", "MIN_CASH", "heat_def", "current_block",
            "manual_pct", "manual_risk", "auto_capital_utilization", "heat_entry_avg", "heat_entry_max",
            "heat_current_gain_share", "max_pos_weight_avg", "max_pos_weight_max", "max_sector_weight_avg",
            "max_sector_weight_max", "cash_ratio_min", "cash_ratio_avg", "current_warn_day_share", "worst_day",
            "gap_stops", "gap_avg_R", "gap_worst_R", "gap_excess_loss", "R_p10", "R_p50", "R_p90",
            "top3_pnl_share", "decided", "orders")]
        pd7 = pd.read_csv(OUT / "p7_daily.csv")
        ser7 = {}
        for name, g in pd7.groupby("series"):
            ser7[name] = {"i0": 0, "d0": g["date"].iloc[0], **{k: g[k].tolist() for k in (
                "equity", "drawdown", "heat_entry", "heat_current", "utilization", "max_sector_w", "positions")}}
        uo = pd.read_csv(OUT / "p7_unl_open_positions.csv")
        p7 = {"summary": _records(p7s, p7cols), "b02": _records(pd.read_csv(OUT / "p7_b01_vs_b02.csv")),
              "daily": ser7, "dates": sorted(pd7["date"].unique().tolist()),
              "open": _records(uo), "meta": json.load(open(OUT / "p7_meta.json", encoding="utf-8")),
              "concl": json.load(open(OUT / "p7_conclusions.json", encoding="utf-8")), "cfg": _p7cfg()}

    dates = sorted(eq["date"].unique())
    di = {d: i for i, d in enumerate(dates)}
    series = {}
    for name, g in eq.groupby("series"):
        g = g.sort_values("date")
        series[name] = {"i0": di[g["date"].iloc[0]], "eq": [round(x, 2) for x in g["equity"]],
                        "dd": [round(x, 5) for x in g["drawdown"]],
                        "heat": [round(x, 5) if x == x else None for x in g.get("heat_current", pd.Series([np.nan] * len(g)))],
                        "pos": [int(x) if x == x else None for x in g.get("positions", pd.Series([np.nan] * len(g)))]}

    keep = _KEEP
    rsel = rnd[rnd["topn"] <= 3]
    rdist = {}
    for (pool, mk, n, seg, hz), g in rsel.groupby(["pool", "market", "topn", "segment", "horizon"]):
        rdist[f"{pool}|{mk}|{n}|{seg}|{hz}"] = [round(x, 5) for x in g["total_return"]]
    trades_sel = tl[tl["series"].isin(["SELECTED", "SELECTED_UNL", "BASE_UNL"]) | tl["series"].str.startswith("BASE_E00")]
    payload = _clean({
        "audit": audit, "verdict": verdict, "rec": rec, "dates": dates, "series": series,
        "summary": _records(summ, keep), "factor": _records(fs), "controlled": _records(cg),
        "timing": _records(et), "rank": _records(rk), "rdist": rdist, "wf": _records(wf),
        "regime": _records(rp), "valsel": _records(vs.head(48), ["pool", "market", "rank", "topn", "total_return", "mdd",
                                                                   "profit_factor", "trades", "objective"]),
        "scorefit": _records(sf), "posthoc": _records(ph, keep + ["random_percentile", "random_median"]) if len(ph) else [],
        "trades": _records(trades_sel), "p7": p7,
    })
    tpl = (PKG_DIR / "src" / "report_template.html").read_text(encoding="utf-8")
    page = tpl.replace("/*__DATA__*/null", json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) \
              .replace("<!--__DESIGN__-->", md_to_html(design)) \
              .replace("<!--__PLAN__-->", md_to_html(plan))
    path = OUT / "entry_strategy_backtest_report.html"
    path.write_text(page, encoding="utf-8")
    return path
