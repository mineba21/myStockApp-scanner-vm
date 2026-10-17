"""운영용 CLI.

  python -m risk_guard --config guard.toml status
  python -m risk_guard --config guard.toml kill "FOMC 전 일시정지"
  python -m risk_guard --config guard.toml unkill
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .config import GuardConfig
from .guard import RiskGuard
from .infra import trading_day


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="risk_guard")
    p.add_argument("--config", default="guard.toml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="킬 스위치·실주문 여부·오늘 원장 보기")
    k = sub.add_parser("kill", help="킬 스위치 켜기 (모든 신규 주문 차단)")
    k.add_argument("reason", nargs="?", default="manual")
    sub.add_parser("unkill", help="킬 스위치 끄기")
    args = p.parse_args(argv)

    guard = RiskGuard(GuardConfig.from_toml(args.config))
    if args.cmd == "kill":
        guard.kill_switch.activate(args.reason)
        print(f"킬 스위치 ON: {args.reason}")
    elif args.cmd == "unkill":
        guard.kill_switch.deactivate()
        print("킬 스위치 OFF")
    else:
        now = datetime.now(timezone.utc)
        view = guard.ledger.view(trading_day(now, guard.config.market_timezone))
        print(json.dumps({
            "kill_switch": guard.kill_switch.is_active(),
            "kill_reason": guard.kill_switch.reason(),
            "live_enabled": guard.live_enabled,
            "orders_today": view.orders_today,
            "buy_notional_today": view.buy_notional_today,
            "new_positions_today": sorted(view.new_positions_today),
        }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
