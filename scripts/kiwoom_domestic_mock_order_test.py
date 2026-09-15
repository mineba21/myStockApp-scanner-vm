#!/usr/bin/env python3
"""Explicit CLI for durable domestic mock buy/sell/cancel verification."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from trading.kiwoom_domestic_execution_evidence import DomesticExecutionEvidence
from trading.kiwoom_domestic_mock_order_test import (
    DomesticMockOrderTest, DomesticMockTestBlocked, KiwoomDomesticMockBroker,
)
from trading.kiwoom_orders import KiwoomOrderClient
from trading.kiwoom_readonly import KiwoomConfig, KiwoomError, KiwoomReadOnlyClient

DEFAULT_PROFILES = Path.home() / ".config/mystockapp/kiwoom_mock_profiles.json"


def parser():
    result = argparse.ArgumentParser(description="키움 국내 모의투자 1주 주문·취소 단계별 검증")
    result.add_argument("--journal", required=True, help="격리된 SQLite 기록 경로")
    result.add_argument("--profiles-file", default=str(DEFAULT_PROFILES))
    result.add_argument("--profile", required=True, help="모의 키에 연결된 계좌 프로필")
    commands = result.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="현금·미체결·매도가능수량을 조회하고 계획 저장")
    prepare.add_argument("--test-key", required=True)
    prepare.add_argument("--side", choices=("BUY", "SELL"), required=True)
    prepare.add_argument("--ticker", required=True)
    prepare.add_argument("--limit-price", required=True, type=int)
    prepare.add_argument("--trade-type", choices=("0", "62"), default="0")
    for name in ("status", "verify-open", "verify-fill", "verify-cancel"):
        command = commands.add_parser(name)
        command.add_argument("--test-id", required=True)
    submit = commands.add_parser("submit", help="확인 문구가 있어야 모의 주문 전송")
    submit.add_argument("--test-id", required=True)
    submit.add_argument("--confirm", required=True)
    cancel = commands.add_parser("cancel", help="미체결 확인 뒤 1주 취소")
    cancel.add_argument("--test-id", required=True)
    cancel.add_argument("--confirm", required=True)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        config = KiwoomConfig.from_profile(args.profile, args.profiles_file)
        readonly = KiwoomReadOnlyClient(config)
        token = str(readonly.issue_token()["token"])
        evidence = DomesticExecutionEvidence(readonly, token, account_profile=args.profile)
        broker = KiwoomDomesticMockBroker(
            config, token, readonly, KiwoomOrderClient(config, readonly.session), evidence,
        )
        workflow = DomesticMockOrderTest(args.journal, broker, account_profile=args.profile)
        if args.command == "prepare":
            output = workflow.prepare(args.test_key, side=args.side, ticker=args.ticker,
                                      price=args.limit_price, trade_type=args.trade_type)
        elif args.command == "status":
            output = workflow.status(args.test_id)
        elif args.command == "submit":
            output = workflow.submit(args.test_id, args.confirm)
        elif args.command == "verify-open":
            output = workflow.verify_open(args.test_id)
        elif args.command == "verify-fill":
            output = workflow.verify_filled(args.test_id)
        elif args.command == "cancel":
            output = workflow.submit_cancel(args.test_id, args.confirm)
        else:
            output = workflow.verify_cancel(args.test_id)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (KiwoomError, DomesticMockTestBlocked) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
