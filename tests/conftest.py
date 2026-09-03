"""테스트 실행 환경 격리.

운영 호스트(VM)의 ``.env`` 는 ``DATABASE_URL`` 을 실제 Neon 으로, 알림
토큰과 ``KIWOOM_TRADING_ENABLED=true`` 를 모두 실값으로 채워 둔다. 그
상태로 그냥 ``pytest`` 를 실행하면

  * ``database.models`` 의 모듈 레벨 엔진이 **운영 DB 에 바인딩**되고
    (``init_db()`` 를 타는 경로가 있으면 운영 스키마에 DDL 이 나간다),
  * 모킹 없이 ``send_slack_message()`` 를 부르는 테스트가 **실제 채널로
    메시지를 보낸다**.

이 파일은 그 격리를 명령줄 대신 자동으로 건다. ``load_dotenv()`` 는
기존 환경변수를 덮어쓰지 않으므로(``override=False``), 여기서 미리
넣어둔 값이 ``.env`` 를 이긴다.

**import 순서가 핵심이다.** pytest 는 테스트 모듈보다 conftest 를 먼저
import 하므로, 아래 모듈 레벨 코드가 ``config`` / ``database.models``
보다 앞서 실행된다.
"""
import atexit
import importlib
import os
import shutil
import sys
import tempfile

import pytest

_TMPDIR = tempfile.mkdtemp(prefix="mystockapp-tests-")
atexit.register(shutil.rmtree, _TMPDIR, ignore_errors=True)

_SQLITE_URL = "sqlite:///" + os.path.join(_TMPDIR, "test.db")

# 운영 값이 새어 들어오지 못하도록 강제한다. 값을 비우는 쪽(알림)은 각
# 전송 함수가 "미설정" 으로 보고 no-op 하도록 만드는 것이 목적이다.
_FORCED = {
    "DATABASE_URL":            _SQLITE_URL,
    "DATABASE_DIRECT_URL":     _SQLITE_URL,
    "SLACK_WEBHOOK_URL":       "",
    "TELEGRAM_BOT_TOKEN":      "",
    "TELEGRAM_CHAT_ID":        "",
    "KIWOOM_TRADING_ENABLED":  "false",
    "KIWOOM_WEB_ENABLED":      "false",
    # 홈 디렉터리의 실제 캐시/자격증명 파일을 건드리지 않도록.
    "KIWOOM_SELL_CACHE_FILE":  os.path.join(_TMPDIR, "kiwoom_sell_analysis.json"),
    "KIWOOM_PROFILES_FILE":    os.path.join(_TMPDIR, "kiwoom_profiles.json"),
}
os.environ.update(_FORCED)

# conftest 보다 먼저 config 를 읽은 플러그인이 있었다면 지금 값으로 다시 읽는다.
if "config" in sys.modules:
    importlib.reload(sys.modules["config"])


@pytest.fixture(scope="session", autouse=True)
def _assert_not_bound_to_production():
    """엔진이 실제로 SQLite 에 물렸는지 확인한다 (심층 방어).

    격리가 깨지는 경로가 새로 생기면 테스트가 운영 DB 를 건드리기 전에
    여기서 먼저 멈춘다.
    """
    from database.models import engine

    assert engine.dialect.name == "sqlite", (
        f"테스트가 운영 DB 에 연결되려 한다: {engine.dialect.name}"
        f"({engine.url.host}). tests/conftest.py 의 격리가 깨졌다 — "
        "config/database.models 가 conftest 보다 먼저 import 되지 않았는지 확인할 것."
    )
    yield
