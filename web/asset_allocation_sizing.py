"""퀀트투자(account2) 계좌의 자산배분 정수주 매수 제안."""

from __future__ import annotations

import math
import os
from trading.allocation_rebalance import LIVE_BLOCK_REASON, RebalanceBlocked, weights, number
from typing import Any, Callable


def calculate_allocation_sizing(
    report: dict[str, Any],
    holdings: list[dict[str, Any]],
    account_summary: dict[str, Any],
    price_loader: Callable[[str], float | None],
) -> dict[str, Any]:
    overseas = account_summary.get("overseas") or {}
    errors = []
    def checked(value, label):
        try:
            return float(number(value, label))
        except RebalanceBlocked as exc:
            errors.append(str(exc))
            return 0.0
    cash = checked(overseas.get("orderable_cash"), "주문가능 현금")
    targets = report.get("combined_allocations") or {}
    try:
        targets = {t: float(w) for t, w in weights(targets).items()}
    except RebalanceBlocked as exc:
        errors.append(str(exc))
        targets = {}
    # No strategy ownership exists in broker holdings. Only an explicit,
    # exclusively owned ticker scope may include prior-month non-target ETFs.
    scope = report.get("liquidation_scope")
    if not isinstance(scope, list) or not scope:
        errors.append("자산배분 전용 매도 대상 범위 미설정 (계좌 전체 청산 금지)")
        scope = list(targets)
    scope = {str(t).strip().upper() for t in scope}
    if not set(targets) <= scope:
        errors.append("목표 ETF가 자산배분 전용 범위 밖에 있습니다.")
    by_ticker = {
        str(row.get("ticker") or "").upper(): row
        for row in holdings
        if row.get("account_profile") == "account2" and row.get("market") == "US"
        and str(row.get("ticker") or "").upper() in scope
    }

    all_tickers = set(targets) | set(by_ticker)
    prices = {t: checked(price_loader(t), f"{t} 가격") for t in all_tickers}
    for ticker, price in prices.items():
        if not math.isfinite(price) or price <= 0:
            errors.append(f"{ticker}: 유효한 최신 가격 누락")
            prices[ticker] = 0
    evaluation = sum(checked(row.get("quantity", 0), "보유수량") * prices[t]
                     for t, row in by_ticker.items())
    total_assets = cash + evaluation
    if not math.isfinite(total_assets) or evaluation < 0:
        errors.append("계획용 평가액이 유효하지 않습니다.")
        total_assets = 0
    rows: list[dict[str, Any]] = []
    for ticker in all_tickers:
        raw_weight = targets.get(ticker, 0)
        ticker = str(ticker).upper()
        weight = max(0.0, float(raw_weight or 0))
        held = by_ticker.get(ticker, {})
        quantity = checked(held.get("quantity", 0), "보유수량")
        price = prices[ticker]
        target_value = total_assets * weight
        current_value = quantity * price
        target_quantity = math.floor(target_value / price) if price > 0 else None
        # Monthly targets retain existing shares: trade only the difference.
        adjustment = (target_quantity - int(quantity)) if target_quantity is not None else 0
        needed = max(adjustment, 0)
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
            "additional_buy_quantity": max(adjustment, 0),
            "additional_sell_quantity": max(-adjustment, 0),
            "retained_quantity": quantity - max(-adjustment, 0),
            "adjustment_quantity": adjustment,
        })

    # The result is only a pre-sale estimate. No estimated proceeds are
    # executable cash, and any invalid input invalidates the entire plan.
    for row in rows:
        row["buy_quantity"] = row["needed_quantity"] if not errors else 0
    estimated_sales = sum(row["additional_sell_quantity"] * (row["price"] or 0) for row in rows) if not errors else 0
    recommended_cost = sum(row["buy_quantity"] * (row["price"] or 0) for row in rows)
    remaining = cash + estimated_sales - recommended_cost

    for row in rows:
        required_cost = row["required_buy_quantity"] * (row["price"] or 0)
        estimated_cost = row["buy_quantity"] * (row["price"] or 0)
        post_value = (row["current_quantity"] - row["additional_sell_quantity"] + row["buy_quantity"]) * (row["price"] or 0)
        row["estimated_cost"] = round(estimated_cost, 2)
        row["required_cost"] = round(required_cost, 2)
        row["post_weight"] = round(post_value / total_assets, 8) if total_assets > 0 else 0
        row.pop("needed_quantity", None)

    return {
        "estimate_only": True,
        "plan_valid": not errors,
        "validation_errors": errors,
        "execution_enabled": False,
        "execution_block_reason": LIVE_BLOCK_REASON,
        "liquidation_scope": sorted(scope),
        "planning_assets": round(total_assets, 2),
        "account_profile": "account2",
        "currency": "USD",
        "orderable_cash": round(cash, 2),
        "evaluation_amount": round(evaluation, 2),
        "total_assets": round(total_assets, 2),
        "recommended_cost": round(recommended_cost, 2),
        "estimated_sale_proceeds": round(estimated_sales, 2),
        "required_cost": round(sum(row["required_cost"] for row in rows), 2),
        "remaining_cash": round(remaining, 2),
        "liquidate_before_rebalance": False,
        "rebalance_mode": "DELTA_V1",
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

    # Entering the allocation tab must refresh live account state, while the
    # monthly strategy signal remains the cached report.
    summaries = get_kiwoom_account_summaries(force=True)
    summary = next(
        (item for item in summaries if item.get("account_profile") == "account2"),
        None,
    )
    if summary is None:
        raise RuntimeError("퀀트투자 계좌 요약을 확인하지 못했습니다.")

    holdings = get_kiwoom_holdings()
    missing = sorted(set(str(t).upper() for t in report.get("combined_allocations", {})) |
                     set(str(t).strip().upper() for t in os.getenv("ALLOCATION_LIQUIDATION_SCOPE", "").split(",") if t.strip()))

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

    result = calculate_allocation_sizing(
        {**report, "liquidation_scope": [t.strip().upper() for t in
         os.getenv("ALLOCATION_LIQUIDATION_SCOPE", "").split(",") if t.strip()]},
        holdings, summary, lambda ticker: fetched.get(ticker)
    )

    # A successful HTTP quote refresh does not establish exchange quote time.
    # No source timestamp parser is implemented yet; never mark this executable.
    result["quote_time_verified"] = False
    result["plan_valid"] = False
    result["validation_errors"].append("증권사 시세 기준시각 미검증 — 계획용 참고값입니다.")
    return result
