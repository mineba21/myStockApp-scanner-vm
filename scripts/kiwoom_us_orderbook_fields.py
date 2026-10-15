#!/usr/bin/env python3
"""Read-only: print usa20101 response fields to find the best ask/bid names.

Run on the VM with the real account2 profile. It issues a token and calls the
US order book read API only; no order, balance or account field is requested.
Pick the fields whose values match the broker app's best ask/bid, then set
KIWOOM_US_ORDERBOOK_ASK_FIELD and KIWOOM_US_ORDERBOOK_BID_FIELD.
"""
from __future__ import annotations

import argparse

from trading.asset_allocation_universe import ETF_EXCHANGES
from trading.kiwoom_readonly import KiwoomConfig, KiwoomReadOnlyClient


def main(argv=None):
    parser = argparse.ArgumentParser(description="usa20101 호가 응답 필드 확인 (조회 전용)")
    parser.add_argument("--profile", default="account2")
    parser.add_argument("--ticker", default="SPY", choices=sorted(ETF_EXCHANGES))
    args = parser.parse_args(argv)

    config = KiwoomConfig.from_profile(args.profile)
    client = KiwoomReadOnlyClient(config)
    token = str(client.issue_token()["token"])
    quote = client.get_overseas_orderbook(
        token, exchange=ETF_EXCHANGES[args.ticker], ticker=args.ticker)["quote"]
    print(f"{args.ticker} usa20101 응답 필드 ({len(quote)}개)")
    for key in sorted(quote):
        print(f"  {key:<24} {quote[key]}")
    print("\n증권사 앱의 매도1호가·매수1호가와 값이 같은 필드를 찾아 아래처럼 설정하세요:")
    print("  KIWOOM_US_ORDERBOOK_ASK_FIELD=<매도1호가 필드>")
    print("  KIWOOM_US_ORDERBOOK_BID_FIELD=<매수1호가 필드>")


if __name__ == "__main__":
    main()
