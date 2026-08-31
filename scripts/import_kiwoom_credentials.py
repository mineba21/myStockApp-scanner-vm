"""키움 계좌별 TXT 10개를 화면 노출 없이 다계좌 프로필로 가져온다."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


KEY_PATTERN = re.compile(r"^(\d+)_appkey\.txt$", re.IGNORECASE)
SECRET_PATTERN = re.compile(r"^(\d+)_secretkey\.txt$", re.IGNORECASE)
DEFAULT_OUTPUT = Path.home() / ".config" / "mystockapp" / "kiwoom_profiles.json"


def _collect(source: Path) -> list[tuple[str, Path, Path]]:
    keys: dict[str, Path] = {}
    secrets: dict[str, Path] = {}
    for path in source.glob("*.txt"):
        key_match = KEY_PATTERN.fullmatch(path.name)
        secret_match = SECRET_PATTERN.fullmatch(path.name)
        if key_match:
            keys[key_match.group(1)] = path
        elif secret_match:
            secrets[secret_match.group(1)] = path

    missing_secret = sorted(set(keys) - set(secrets))
    missing_key = sorted(set(secrets) - set(keys))
    if missing_secret or missing_key:
        raise ValueError("App Key와 Secret Key 파일의 계좌별 짝이 맞지 않습니다.")
    if not keys:
        raise ValueError("가져올 키움 App Key/Secret 파일을 찾지 못했습니다.")
    return [(account, keys[account], secrets[account]) for account in sorted(keys)]


def _read_secret(path: Path) -> str:
    value = path.read_text(encoding="utf-8-sig").strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError(f"단일 값 TXT 형식이 아닙니다: {path.name}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="키움 5계좌 자격 증명 보안 가져오기")
    parser.add_argument("--source", type=Path, default=Path.home() / "Downloads")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--expected", type=int, default=5)
    args = parser.parse_args(argv)

    try:
        pairs = _collect(args.source.expanduser())
        if len(pairs) != args.expected:
            raise ValueError(
                f"계좌 파일 쌍이 {len(pairs)}개입니다. 예상한 {args.expected}개와 다릅니다."
            )
        output = args.output.expanduser()
        if output.exists():
            raise ValueError(f"기존 보안 파일이 있어 덮어쓰지 않았습니다: {output}")

        profiles = {}
        hints = []
        for index, (account, key_path, secret_path) in enumerate(pairs, start=1):
            name = f"account{index}"
            profiles[name] = {
                "app_key": _read_secret(key_path),
                "app_secret": _read_secret(secret_path),
            }
            hints.append((name, f"****{account[-4:]}"))

        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(output.parent, 0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(output, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"mode": "real", "profiles": profiles}, handle, indent=2)
            handle.write("\n")
        os.chmod(output, 0o600)

        print(f"완료: {len(profiles)}개 계좌 프로필을 보안 파일에 저장했습니다.")
        print(f"저장 위치: {output}")
        for name, hint in hints:
            print(f"  {name}: {hint}")
        print("App Key와 Secret Key 원문은 출력하지 않았습니다.")
        return 0
    except (OSError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
