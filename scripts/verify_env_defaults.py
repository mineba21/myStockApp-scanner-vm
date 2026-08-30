"""
.env.example 이 config.py 의 코드 기본값과 일치하는지 검증 (Step 2 Codex 리뷰 대응).

배경: config.py 는 ``os.getenv("X", "코드기본값")`` 패턴을 쓰므로, 만약
.env.example 에 옛날 값이 남아 있는 채로 배포자가 ``cp .env.example .env``
하면 load_dotenv() 가 그 옛 값으로 코드 기본값을 **덮어써 버린다** — 코드를
아무리 최신으로 유지해도 실제 스캔은 옛 임계값으로 동작하게 된다.

이 스크립트는 서브프로세스 2개를 띄워 비교한다:
  (a) "코드 기본값" — 환경변수 없이 config 를 import 했을 때의 값
  (b) ".env.example 적용값" — .env.example 의 KEY=VALUE 를 전부 환경변수로
      주입한 뒤 config 를 import 했을 때의 값
(a)==(b) 여야 ".env.example 이 코드 기본값과 동기화돼 있다" 고 말할 수 있다.
불일치가 있으면 배포자가 .env.example 을 그대로 복사해 쓸 때 실제로
적용되는 값이 코드가 의도한 기본값과 달라진다는 뜻이다.

실행: python3 scripts/verify_env_defaults.py
"""
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_EXAMPLE = os.path.join(REPO_ROOT, ".env.example")

# 검증 대상 — Step 2 가 손댄 파라미터 (BREAKOUT_* 3개 + 신규 base 파라미터 9개).
PARAMS = [
    "BREAKOUT_WEEKLY_VOL_RATIO",
    "BREAKOUT_DAILY_VOL_RATIO",
    "BREAKOUT_MAX_EXTENDED_PCT",
    "BASE_MODE",
    "BASE_LOOKBACK_DAYS",
    "BASE_MAX_WIDTH_PCT",
    "US_BASE_MAX_WIDTH_PCT",
    "KR_BASE_MAX_WIDTH_PCT",
    "TIGHT_LOOKBACK_DAYS",
    "TIGHT_MAX_WIDTH_PCT",
    "US_TIGHT_MAX_WIDTH_PCT",
    "KR_TIGHT_MAX_WIDTH_PCT",
    "TIGHT_CONTRACTION_RATIO",
]


def _parse_env_file(path: str) -> dict:
    """.env 형식(KEY=VALUE, # 주석, 빈 줄)을 단순 파싱 — python-dotenv 문법의
    핵심 부분만 다룬다(따옴표 없는 값 기준, 이 저장소의 .env.example 은
    전부 이 형태)."""
    values = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
            if m:
                values[m.group(1)] = m.group(2)
    return values


def _effective_values(extra_env: dict) -> dict:
    """`extra_env` 를 환경변수로 주입한 새 서브프로세스에서 config.py 를
    import 하고 PARAMS 값을 JSON 으로 뽑아온다. 부모 프로세스의 캐시된
    import 상태와 완전히 격리하기 위해 서브프로세스를 쓴다."""
    import json
    code = (
        "import sys, json, os; sys.path.insert(0, %r)\n"
        "import config\n"
        "print(json.dumps({k: getattr(config, k, '__MISSING__') for k in %r}))"
    ) % (REPO_ROOT, PARAMS)
    env = {k: v for k, v in os.environ.items()
          if not k.startswith(tuple(PARAMS)) and k not in PARAMS}
    # PYTHONPATH 오염 방지 — python-dotenv 의 load_dotenv() 는 cwd 의 .env 를
    # 찾으므로, REPO_ROOT 가 아닌 임시 cwd 에서 실행해 .env.example 자체가
    # 실수로 .env 로 읽히는 일이 없게 한다.
    env.update(extra_env)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd="/tmp", env=env, capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def main() -> int:
    code_defaults = _effective_values({})
    env_example_values = _parse_env_file(ENV_EXAMPLE)
    applied = _effective_values(env_example_values)

    print(f"{'파라미터':<28}{'코드 기본값':<16}{'.env.example 적용값':<22}{'일치'}")
    print("-" * 78)
    all_match = True
    for p in PARAMS:
        cd = code_defaults.get(p, "__MISSING__")
        ap = applied.get(p, "__MISSING__")
        match = str(cd) == str(ap)
        all_match &= match
        print(f"{p:<28}{str(cd):<16}{str(ap):<22}{'OK' if match else 'MISMATCH'}")

    print()
    if all_match:
        print("PASS — .env.example 이 코드 기본값과 완전히 동기화되어 있다.")
    else:
        print("FAIL — 위 MISMATCH 항목은 .env.example 을 .env 로 복사하는 순간"
              " 코드 기본값 대신 옛 값으로 덮어써진다.")
    return 0 if all_match else 1


if __name__ == "__main__":
    raise SystemExit(main())
