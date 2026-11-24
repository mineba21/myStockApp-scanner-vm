#!/usr/bin/env python3
"""주문 저널(SCANNER_ORDER_JOURNAL_PATH) 온라인 백업. 원본은 읽기만 한다.

  python -m scripts.auto_stop_journal_backup --dest /var/backups/mystockapp

SQLite backup API 로 일관된 사본을 만든다 (파일 복사보다 안전). 백업을 원본 위치로 되돌려 덮어쓰지 않는다:
과거 사본으로 복원하면 이미 전송한 주문 기록이 사라져 중복 전송 방지가 깨진다 (AGENTS.md).
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import time
from pathlib import Path


def backup(src: str, dest_dir: str, keep: int = 30) -> Path:
    src_path = Path(src).resolve()
    dest = Path(dest_dir).resolve()
    if dest == src_path.parent:
        raise SystemExit("백업 위치는 원본 폴더와 달라야 합니다.")
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / f"{src_path.stem}-{time.strftime('%Y%m%dT%H%M%S')}.sqlite3"
    source = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    out = sqlite3.connect(target)
    try:
        source.backup(out)
        ok = out.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise SystemExit(f"백업 무결성 검사 실패: {ok}")
    finally:
        out.close()
        source.close()
    os.chmod(target, 0o600)           # 주문 기록 사본: 소유자만 읽기
    olds = sorted(dest.glob(f"{src_path.stem}-*.sqlite3"))
    for old in olds[:-keep]:
        old.unlink()
    return target


def main(argv=None):
    p = argparse.ArgumentParser(description="주문 저널 온라인 백업")
    p.add_argument("--src", default=os.getenv("SCANNER_ORDER_JOURNAL_PATH", ""))
    p.add_argument("--dest", required=True)
    p.add_argument("--keep", type=int, default=30)
    args = p.parse_args(argv)
    if not args.src:
        raise SystemExit("SCANNER_ORDER_JOURNAL_PATH 또는 --src 가 필요합니다.")
    print(backup(args.src, args.dest, args.keep))


if __name__ == "__main__":
    main()
