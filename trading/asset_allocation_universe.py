"""Broker routing and ownership scope for the account2 allocation strategies."""
from __future__ import annotations

import os


# Union of the easy/original VAA, LAA and Original Dual Momentum universes in
# the deployed calculator. account2 is dedicated to these allocation ETFs.
ETF_EXCHANGES = {
    "AGG": "NY",
    "BIL": "NY",
    "EEM": "NY",
    "EFA": "NY",
    "GLD": "NY",
    "IEF": "ND",
    "IEMG": "NY",
    "LQD": "NY",
    "QQQ": "ND",
    "SHY": "ND",
    "SPY": "NY",
    "VTV": "NY",
}
ETF_ALLOCATION_SCOPE = tuple(sorted(ETF_EXCHANGES))


def allocation_scope() -> list[str]:
    """Return an optional explicit subset, otherwise the owned full universe."""
    configured = [
        ticker.strip().upper()
        for ticker in os.getenv("ALLOCATION_LIQUIDATION_SCOPE", "").split(",")
        if ticker.strip()
    ]
    return configured or list(ETF_ALLOCATION_SCOPE)
