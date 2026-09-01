"""퀀트투자(account2) 계좌의 자산배분 정수주 매수 제안."""

from __future__ import annotations

import math
from typing import Any, Callable


def calculate_allocation_sizing(
    report: dict[str, Any],
    holdings: list[dict[str, Any]],
    account_summary: dict[str, Any],
    price_loader: Callable[[str], float | None],
) -> dict[str, Any]:
    overseas = account_summary.get("overseas") or {}
    cash = max(0.0, float(overseas.get("orderable_cash") or 0))
    evaluation = max(0.0, float(overseas.get("evaluation_amount") or 0))
    total_assets = cash + evaluation
    targets = report.get("combined_allocations") or {}
    by_ticker = {
        str(row.get("ticker") or "").upper(): row
        for row in holdings
        if row.get("account_profile") == "account2" and row.get("market") == "US"
    }

    rows: list[dict[str, Any]] = []
    for ticker, raw_weight in targets.items():
        ticker = str(ticker).upper()
        weight = max(0.0, float(raw_weight or 0))
        held = by_ticker.get(ticker, {})
        quantity = max(0.0, float(held.get("quantity") or 0))
        price = float(held.get("current_price") or 0) or float(price_loader(ticker) or 0)
        target_value = total_assets * weight
        current_value = float(held.get("eval_amount") or 0)
        if current_value <= 0 and price > 0:
            current_value = quantity * price
        target_quantity = math.floor(target_value / price) if price > 0 else None
        needed = max(0, target_quantity - math.floor(quantity)) if target_quantity is not None else 0
        rows.append({
            "ticker": ticker,
            "target_weight": weight,
            "current_quantity": quantity,
            "current_value": round(current_value, 2),
            "price": round(price, 4) if price > 0 else None,
            "target_value": round(target_value, 2),
            "target_quantity": target_quantity,
            "needed_quantity": needed,
            "required_buy_quantity": needed,
            "buy_quantity": 0,
        })

    remaining = cash
    while True:
        eligible = [
            row for row in rows
            if row["price"] and row["buy_quantity"] < row["needed_quantity"]
            and row["price"] <= remaining + 1e-9
        ]
        if not eligible:
            break
        selected = max(
            eligible,
            key=lambda row: (
                (row["target_value"] - row["current_value"] - row["buy_quantity"] * row["price"])
                / row["target_value"]
                if row["target_value"] > 0 else 0
            ),
        )
        selected["buy_quantity"] += 1
        remaining -= selected["price"]

    for row in rows:
        required_cost = row["required_buy_quantity"] * (row["price"] or 0)
        estimated_cost = row["buy_quantity"] * (row["price"] or 0)
        post_value = row["current_value"] + estimated_cost
        row["estimated_cost"] = round(estimated_cost, 2)
        row["required_cost"] = round(required_cost, 2)
        row["post_weight"] = round(post_value / total_assets, 8) if total_assets > 0 else 0
        row.pop("needed_quantity", None)

    return {
        "account_profile": "account2",
        "currency": "USD",
        "orderable_cash": round(cash, 2),
        "evaluation_amount": round(evaluation, 2),
        "total_assets": round(total_assets, 2),
        "recommended_cost": round(cash - remaining, 2),
        "required_cost": round(sum(row["required_cost"] for row in rows), 2),
        "remaining_cash": round(remaining, 2),
        "items": sorted(rows, key=lambda row: (-row["target_weight"], row["ticker"])),
        "read_only": True,
    }


def build_live_allocation_sizing(report: dict[str, Any]) -> dict[str, Any]:
    from trading.kiwoom_readonly import KiwoomReadOnlyClient, load_profile_configs
    from web.kiwoom_holdings import (
        _get_token,
        get_kiwoom_account_summaries,
        get_kiwoom_holdings,
    )

    summaries = get_kiwoom_account_summaries()
    summary = next(
        (item for item in summaries if item.get("account_profile") == "account2"),
        None,
    )
    if summary is None:
        raise RuntimeError("퀀트투자 계좌 요약을 확인하지 못했습니다.")

    holdings = get_kiwoom_holdings()
    held_prices = {
        str(row.get("ticker") or "").upper(): float(row.get("current_price") or 0)
        for row in holdings
        if row.get("account_profile") == "account2" and row.get("market") == "US"
    }
    missing = [
        str(ticker).upper()
        for ticker in (report.get("combined_allocations") or {})
        if held_prices.get(str(ticker).upper(), 0) <= 0
    ]

    fetched: dict[str, float | None] = {}
    if missing:
        config = load_profile_configs()["account2"]
        client = KiwoomReadOnlyClient(config)
        token = _get_token("account2", config, client)
        exchanges = {
            "AGG": "NY", "BIL": "NY", "EFA": "NY", "GLD": "NY",
            "IEF": "ND", "IEMG": "NY", "LQD": "ND", "QQQ": "ND",
            "SPY": "NY", "VTV": "NY", "SHY": "ND",
        }
        for ticker in missing:
            quote = client.get_overseas_quote(
                token, exchange=exchanges.get(ticker, "NY"), ticker=ticker
            )["quote"]
            try:
                fetched[ticker] = abs(float(str(quote.get("cur_prc") or "0").replace(",", ""))) or None
            except (TypeError, ValueError):
                fetched[ticker] = None

    return calculate_allocation_sizing(
        report, holdings, summary, lambda ticker: fetched.get(ticker)
    )
