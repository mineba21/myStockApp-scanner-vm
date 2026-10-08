"""Explicit operations only: python -m agent_runtime.shadow_cli status|replay|reset-gap."""
import argparse
from datetime import datetime, timezone
import json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    replay = sub.add_parser('replay')
    replay.add_argument('--limit', type=int, default=200)
    reset = sub.add_parser('reset-gap')
    reset.add_argument('scan_id', type=int)
    reset.add_argument('--reason', required=True)
    args = parser.parse_args()
    from database.models import SessionLocal
    from .shadow import ShadowStore
    store = ShadowStore(SessionLocal)
    if not store.settings.enabled:
        parser.error('AGENT_RUNTIME_ENABLED must be true; this command never enables delivery')
    if args.command == 'replay':
        result = store.replay(limit=args.limit)
    elif args.command == 'reset-gap':
        store.reset_gap(args.scan_id, args.reason, now=datetime.now(timezone.utc))
        result = store.report()
    else:
        result = store.report()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
