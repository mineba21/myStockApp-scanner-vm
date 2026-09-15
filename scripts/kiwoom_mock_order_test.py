#!/usr/bin/env python3
"""Explicit command-line steps for the overseas mock one-share test."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trading.kiwoom_execution_evidence import ExecutionEvidence
from trading.kiwoom_mock_order_test import (
    KiwoomMockTestBroker,
    MockOrderTest,
    MockOrderTestBlocked,
)
from trading.kiwoom_orders import KiwoomOrderClient
from trading.kiwoom_readonly import KiwoomConfig, KiwoomError, KiwoomReadOnlyClient

DEFAULT_PROFILES = Path.home() / ".config/mystockapp/kiwoom_overseas_mock_profiles.json"


def parser():
    result = argparse.ArgumentParser(description="키움 해외 모의투자 1주 주문·취소 단계별 검증")
    result.add_argument("--journal", required=True, help="격리된 SQLite 테스트 기록 경로")
    result.add_argument("--profiles-file", default=str(DEFAULT_PROFILES))
    result.add_argument("--profile", default="account2")
    commands = result.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="조회만 하고 1주 테스트 계획 저장")
    prepare.add_argument("--test-key", required=True)
    prepare.add_argument("--ticker", required=True)
    prepare.add_argument("--exchange", choices=("NA", "ND", "NY"), required=True)
    prepare.add_argument("--limit-price", required=True)
    prepare.add_argument(
        "--allow-unvalidated-capacity-for-mock-test",
        action="store_true",
        help="종목별 수량 API가 없는 모의계좌에서만 1주 진단 계획 허용",
    )
    for name in ("status", "verify-open", "verify-cancel"):
        command = commands.add_parser(name)
        command.add_argument("--test-id", required=True)
    buy = commands.add_parser("buy", help="정확한 확인 문구가 있어야 실제 모의 주문 전송")
    buy.add_argument("--test-id", required=True)
    buy.add_argument("--confirm", required=True)
    cancel = commands.add_parser("cancel", help="미체결 검증 후 모의 주문 취소")
    cancel.add_argument("--test-id", required=True)
    cancel.add_argument("--confirm", required=True)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        config = KiwoomConfig.from_profile(args.profile, args.profiles_file)
        readonly = KiwoomReadOnlyClient(config)
        token = str(readonly.issue_token()["token"])
        day = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
        evidence = ExecutionEvidence(readonly, token)
        broker = KiwoomMockTestBroker(
            config, token, KiwoomOrderClient(config, readonly.session), evidence, day=day,
        )
        workflow = MockOrderTest(args.journal, broker)
        if args.command == "prepare":
            output = workflow.prepare(args.test_key, ticker=args.ticker,
                                      exchange=args.exchange, price=args.limit_price,
                                      allow_unvalidated_capacity=
                                      args.allow_unvalidated_capacity_for_mock_test)
        elif args.command == "status":
            output = workflow.status(args.test_id)
        elif args.command == "buy":
            output = workflow.submit_buy(args.test_id, args.confirm)
        elif args.command == "verify-open":
            output = workflow.verify_open(args.test_id)
        elif args.command == "cancel":
            output = workflow.submit_cancel(args.test_id, args.confirm)
        else:
            output = workflow.verify_cancel(args.test_id)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (KiwoomError, MockOrderTestBlocked) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
