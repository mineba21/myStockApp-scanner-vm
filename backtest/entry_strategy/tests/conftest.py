"""백테스트 테스트가 운영 테스트 세션의 환경변수를 바꾸지 않도록 격리."""
import os

import pytest


@pytest.fixture(autouse=True)
def _restore_environ():
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
