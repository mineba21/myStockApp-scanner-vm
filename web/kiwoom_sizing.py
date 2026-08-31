"""실계좌 현금과 기존 R-multiple 로직을 결합한 조회 전용 매수 사이징."""

from __future__ import annotations

from typing import Any

from scanner.sizing import calculate_open_risk, calculate_position


TARGET_ACCOUNT_BY_MARKET = {"US": "account2", "KR": "account4"}


def _sizing_config(market: str) -> dict[str, float]:
    from config import market_param

    defaults = {
        "RISK_PCT": 1.0,
        "MAX_POSITION_PCT": 20.0,
        "MAX_TOTAL_HEAT_PCT": 6.0,
        "MIN_R_PCT": 3.0,
        "MAX_R_PCT": 15.0,
    }
    return {
        name: float(market_param(name, market, default))
        for name, default in defaults.items()
    }


def _account_context(summary: dict[str, Any], market: str) -> tuple[float, float]:
    if market == "KR":
        domestic = summary.get("domestic", {})
        equity = float(domestic.get("estimated_assets") or 0)
        if equity <= 0:
            equity = float(domestic.get("cash") or 0) + float(
                domestic.get("evaluation_amount") or 0
            )
        cash = float(domestic.get("orderable_cash") or 0)
        return equity, cash
    overseas = summary.get("overseas", {})
    equity = float(overseas.get("cash") or 0) + float(
        overseas.get("evaluation_amount") or 0
    )
    cash = float(overseas.get("orderable_cash") or 0)
    return equity, cash


def apply_live_position_sizing(
    candidates: list[dict[str, Any]],
    account_summaries: list[dict[str, Any]],
    holdings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """매수 후보에 지정 계좌의 실시간 현금 기반 제안 수량을 붙인다.

    미국 후보는 자유투자(account2), 국내 후보는 ISA(account4)만 사용한다.
    주문 API는 호출하지 않으며 반환값은 참고용 계산 결과다.
    """
    summaries = {
        str(summary.get("account_profile")): summary
        for summary in account_summaries
    }
    contexts: dict[str, dict[str, Any]] = {}
    for market, profile in TARGET_ACCOUNT_BY_MARKET.items():
        summary = summaries.get(profile)
        if not summary:
            continue
        equity, cash = _account_context(summary, market)
        account_holdings = [
            holding
            for holding in holdings
            if holding.get("account_profile") == profile
            and str(holding.get("market") or "").upper() == market
        ]
        contexts[market] = {
            "profile": profile,
            "name": summary.get("display_name") or profile,
            "equity": equity,
            "cash": cash,
            "heat": calculate_open_risk(account_holdings, market),
            "cfg": _sizing_config(market),
        }

    decorated: list[dict[str, Any]] = []
    for candidate in candidates:
        row = dict(candidate)
        market = str(row.get("market") or "").upper()
        context = contexts.get(market)
        entry = row.get("price")
        stop = row.get("stop_loss")
        if not context or not entry or not stop or context["equity"] <= 0:
            row["live_sizing"] = None
            decorated.append(row)
            continue
        result = calculate_position(
            entry=entry,
            stop=stop,
            equity=context["equity"],
            cash=context["cash"],
            open_risk_sum=context["heat"]["open_risk_sum"],
            market=market,
            cfg=context["cfg"],
        )
        row["live_sizing"] = {
            **result,
            "account_profile": context["profile"],
            "account_name": context["name"],
            "equity": round(context["equity"], 6),
            "available_cash": round(context["cash"], 6),
            "entry": float(entry),
            "stop": float(stop),
            "heat_warnings": list(context["heat"].get("warnings") or []),
            "read_only": True,
        }
        decorated.append(row)
    return decorated
