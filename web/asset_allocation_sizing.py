"""퀀트투자(account2) 계좌의 자산배분 정수주 매수 제안."""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable
from zoneinfo import ZoneInfo

from trading.allocation_rebalance import RebalanceBlocked, weights, number


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

    # Invalid account/price inputs invalidate all quantities: zero means no trade,
    # not a failed calculation. Missing held prices also invalidate total assets.
    if errors:
        for row in rows:
            for key in ("target_quantity", "required_buy_quantity", "buy_quantity",
                        "additional_buy_quantity", "additional_sell_quantity",
                        "retained_quantity", "adjustment_quantity", "target_value",
                        "estimated_cost", "required_cost", "post_weight"):
                row[key] = None

    return {
        "estimate_only": True,
        "plan_valid": not errors,
        "validation_errors": errors,
        "execution_enabled": False,
        "execution_block_reason": "참고용 예상 수량입니다. 주문 미리보기에서 최신 시세·잔고·현금을 다시 검증합니다.",
        "liquidation_scope": sorted(scope),
        "planning_assets": round(total_assets, 2) if not errors else None,
        "account_profile": "account2",
        "currency": "USD",
        "orderable_cash": round(cash, 2),
        "evaluation_amount": round(evaluation, 2) if not errors else None,
        "total_assets": round(total_assets, 2) if not errors else None,
        "recommended_cost": round(recommended_cost, 2) if not errors else None,
        "estimated_sale_proceeds": round(estimated_sales, 2) if not errors else None,
        "required_cost": round(sum(row["required_cost"] for row in rows), 2) if not errors else None,
        "remaining_cash": round(remaining, 2) if not errors else None,
        "liquidate_before_rebalance": False,
        "rebalance_mode": "DELTA_V1",
        "items": sorted(rows, key=lambda row: (-row["target_weight"], row["ticker"])),
        "read_only": True,
    }


# Reference prices are display-only. The execution broker retains its 60s limit.
REFERENCE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


class SizingUnavailable(RuntimeError):
    """Only controlled, credential-free messages may cross the HTTP boundary."""


def _reference_quote(client, token, ticker, exchange, now):
    from trading.kiwoom_allocation_live_broker import KiwoomAllocationLiveBroker

    try:
        raw = KiwoomAllocationLiveBroker._read_retry(
            lambda: client.get_overseas_orderbook(token, exchange=exchange, ticker=ticker)
        )["quote"]
    except Exception as exc:
        raise SizingUnavailable(f"{ticker}: 호가 조회 실패 — 잠시 후 다시 조회하세요.") from exc
    if not isinstance(raw, dict):
        raise SizingUnavailable(f"{ticker}: 호가 응답 형식 오류")
    if (str(raw.get("stk_cd") or "").strip().upper() != ticker
            or str(raw.get("stex_tp") or "").strip().upper() != exchange):
        raise SizingUnavailable(f"{ticker}: 호가 응답 종목/거래소 불일치")
    try:
        observed = datetime.strptime(
            f"{raw['dt']} {raw['bid_tm']}", "%Y%m%d %H:%M"
        ).replace(tzinfo=ZoneInfo("America/New_York")).timestamp()
    except (KeyError, TypeError, ValueError) as exc:
        raise SizingUnavailable(f"{ticker}: 시세 기준시각 누락 또는 형식 오류") from exc
    age = now - observed
    if not 0 <= age <= REFERENCE_MAX_AGE_SECONDS:
        raise SizingUnavailable(f"{ticker}: 시세 기준시각이 미래이거나 7일을 초과했습니다.")
    try:
        price = abs(Decimal(str(raw.get("cur_prc")).replace(",", "")))
        if not price.is_finite() or price <= 0 or not math.isfinite(float(price)):
            raise ValueError("invalid price")
    except (ValueError, ArithmeticError) as exc:
        raise SizingUnavailable(f"{ticker}: 유효한 가격 누락") from exc
    return {
        "price": float(price),
        "price_as_of": datetime.fromtimestamp(observed, timezone.utc).isoformat(),
        "price_age_seconds": int(age),
        "price_status": "recent" if age <= 60 else "reference",
        "price_source": "usa20101",
    }


def build_live_allocation_sizing(report: dict[str, Any]) -> dict[str, Any]:
    from trading.asset_allocation_universe import ETF_EXCHANGES, allocation_scope
    from trading.kiwoom_readonly import KiwoomReadOnlyClient, load_profile_configs
    from web.kiwoom_holdings import _get_token, _load_kiwoom_portfolio

    try:
        portfolio = _load_kiwoom_portfolio(force=True)
    except Exception as exc:
        raise SizingUnavailable("계좌 잔고·현금 조회 실패 — 계좌 연결 상태를 확인하세요.") from exc
    summary = next((item for item in portfolio["accounts"]
                    if item.get("account_profile") == "account2"), None)
    if summary is None:
        raise SizingUnavailable("account2 계좌 요약을 확인하지 못했습니다.")
    holdings = portfolio["rows"]
    scope = allocation_scope()
    try:
        targets = weights(report.get("combined_allocations"))
    except RebalanceBlocked as exc:
        raise SizingUnavailable("월별 목표 비중이 유효하지 않습니다. 배분 계산을 갱신하세요.") from exc
    required = set(targets) | {
        str(row.get("ticker") or "").upper() for row in holdings
        if row.get("account_profile") == "account2" and row.get("market") == "US"
        and str(row.get("ticker") or "").upper() in scope
    }
    try:
        config = load_profile_configs()["account2"]
        client = KiwoomReadOnlyClient(config)
        token = _get_token("account2", config, client)
    except Exception as exc:
        raise SizingUnavailable("account2 시세 조회 인증 실패 — 계좌 연결 상태를 확인하세요.") from exc

    quotes, quote_errors = {}, {}
    for ticker in sorted(required):
        if ticker not in ETF_EXCHANGES:
            quote_errors[ticker] = f"{ticker}: 거래소 매핑이 없습니다."
            continue
        try:
            quotes[ticker] = _reference_quote(client, token, ticker, ETF_EXCHANGES[ticker], time.time())
        except SizingUnavailable as exc:
            quote_errors[ticker] = str(exc)

    result = calculate_allocation_sizing(
        {**report, "liquidation_scope": scope}, holdings, summary,
        lambda ticker: quotes.get(ticker, {}).get("price"),
    )
    result["account_as_of"] = summary.get("updated_at")
    result["quote_time_verified"] = not quote_errors
    result["reference_max_age_seconds"] = REFERENCE_MAX_AGE_SECONDS
    result["warnings"] = []
    for row in result["items"]:
        quote = quotes.get(row["ticker"])
        if quote:
            row.update({key: value for key, value in quote.items() if key != "price"})
            if quote["price_status"] == "reference":
                result["warnings"].append(f"{row['ticker']}: 60초가 지난 시세로 계산한 참고용 예상 수량입니다.")
        else:
            row.update(price_as_of=None, price_status="unavailable",
                       price_error=quote_errors.get(row["ticker"]))
    # Replace generic missing-price errors with safe, actionable diagnostics.
    result["validation_errors"] = [message for message in result["validation_errors"]
        if not any(message.startswith(ticker + ":") or message.startswith(ticker + " 가격:")
                   for ticker in quote_errors)] + list(quote_errors.values())
    return result
