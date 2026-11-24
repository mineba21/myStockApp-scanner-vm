#!/usr/bin/env python3
"""조회 전용: 자동 손절 LIVE 전 미검증 API 계약을 account1 실계좌 응답으로 확인한다.

주문·취소 API 는 import 하지도 호출하지도 않는다. VM 에서 실행한다 (자격 증명은 KIWOOM_PROFILES_FILE).

  python -m scripts.auto_stop_readonly_probe quote   --ticker AAPL --exchange ND --samples 5 --interval 20
  python -m scripts.auto_stop_readonly_probe balance --ticker AAPL
  python -m scripts.auto_stop_readonly_probe orders  --date 20261124

결과로 확인할 것 (docs/auto_stop_sell.md "LIVE 활성화 차단 조건"):
  quote   → USA20101_QUOTE_TIME: 정규장 중 dt/bid_tm 이 분 단위로 갱신되고 현재가가 증권사 앱과 같은지.
            USA20101_BID: 매수1호가와 같은 값을 가진 필드명.
  balance → UST21070_SELLABLE: sell_alowq 가 앱의 매도가능수량과 같은지 (미체결 매도가 있으면 차감되는지도).
  orders  → UST21150_STATUS: ord_qty/cntr_qty/ord_remnq/cncl_qty/ord_stat_nm 이 앱 주문내역과 같은지,
            체결 평균가 후보 필드(이름에 uv/pric/prc 포함)가 있는지.
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from trading.kiwoom_execution_evidence import ExecutionEvidence
from trading.kiwoom_readonly import REAL_BASE_URL, KiwoomConfig, KiwoomReadOnlyClient

NY = ZoneInfo("America/New_York")
PRICE_HINTS = ("uv", "pric", "prc", "amt")


def _client(profile):
    config = KiwoomConfig.from_profile(profile)
    if config.mode != "real" or config.base_url != REAL_BASE_URL:
        raise SystemExit("account1 실계좌 프로필이 아닙니다.")
    client = KiwoomReadOnlyClient(config)
    return client, str(client.issue_token()["token"])


def cmd_quote(args):
    client, token = _client(args.profile)
    for i in range(args.samples):
        q = client.get_overseas_orderbook(token, exchange=args.exchange, ticker=args.ticker)["quote"]
        now = datetime.now(NY)
        if i == 0:
            print(f"usa20101 필드 ({len(q)}개):")
            for k in sorted(q):
                print(f"  {k:<24} {q[k]}")
        try:
            as_of = datetime.strptime(f"{q['dt']} {q['bid_tm']}", "%Y%m%d %H:%M").replace(tzinfo=NY)
            age = f"{(now - as_of).total_seconds():.0f}s"
        except (KeyError, ValueError):
            age = "시각 해석 불가"
        print(f"[{now:%H:%M:%S} ET] cur_prc={q.get('cur_prc')} dt={q.get('dt')} bid_tm={q.get('bid_tm')} age={age}")
        if i + 1 < args.samples:
            time.sleep(args.interval)


def cmd_balance(args):
    client, token = _client(args.profile)
    bal = client.get_overseas_account_balance(token)
    print(f"complete={bal.get('complete')} 보유 행 {len(bal.get('holdings', []))}개")
    rows = [r for r in bal.get("holdings", []) if str(r.get("stk_cd", "")).strip().upper() == args.ticker]
    print(f"{args.ticker} 행 {len(rows)}개 (정확히 1개여야 자동 손절 등록 가능)")
    for r in rows:
        for k in sorted(r):
            print(f"  {k:<24} {r[k]}")


def cmd_orders(args):
    client, token = _client(args.profile)
    ev = ExecutionEvidence(client, token, account="account1")
    raw = ev.rows("ust21150", {"ord_dt": args.date, "query_tp": "1", "slby_tp": "0", "stex_tp": "",
                              "stk_cd": "", "oppo_trde_tp": "%", "fr_ord_no": ""})
    keys = sorted({k for r in raw for k in r})
    print(f"ust21150 {args.date}: {len(raw)}행, 필드 {len(keys)}개")
    print("  필드:", ", ".join(keys))
    print("  가격·금액 후보 필드:", ", ".join(k for k in keys if any(h in k.lower() for h in PRICE_HINTS)))
    for r in raw[: args.limit]:
        print("  ---")
        for k in sorted(r):
            print(f"    {k:<24} {r[k]}")
        try:
            n = ExecutionEvidence.normalize(r, args.date)
            print(f"    => 정규화 state={n['state']} qty={n['quantity']} filled={n['filled_quantity']} "
                  f"remaining={n['remaining_quantity']}")
        except Exception as exc:          # 정규화 실패도 계약 확인 결과다
            print(f"    => 정규화 실패: {exc}")


def main(argv=None):
    p = argparse.ArgumentParser(description="자동 손절 API 계약 조회 전용 확인")
    p.add_argument("--profile", default="account1")
    sub = p.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("quote"); q.add_argument("--ticker", required=True); q.add_argument("--exchange", required=True,
                                                                                          choices=["NA", "ND", "NY"])
    q.add_argument("--samples", type=int, default=5); q.add_argument("--interval", type=float, default=20)
    b = sub.add_parser("balance"); b.add_argument("--ticker", required=True)
    o = sub.add_parser("orders"); o.add_argument("--date", required=True); o.add_argument("--limit", type=int, default=5)
    args = p.parse_args(argv)
    if hasattr(args, "ticker"):
        args.ticker = args.ticker.strip().upper()
    {"quote": cmd_quote, "balance": cmd_balance, "orders": cmd_orders}[args.cmd](args)


if __name__ == "__main__":
    main()
