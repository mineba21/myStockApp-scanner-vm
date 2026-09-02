"""Alembic 실행 환경.

접속 URL 은 alembic.ini 가 아니라 ``config.DATABASE_URL`` 에서 읽는다 —
운영 자격증명이 저장소에 커밋되지 않도록, 그리고 앱과 마이그레이션이
항상 같은 DB 를 보도록 하기 위함이다.
"""
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DATABASE_DIRECT_URL  # noqa: E402
from database.models import Base  # noqa: E402

config = context.config

# 앱 프로세스 안에서 호출될 때(models.run_migrations)는 이미 설정된 로깅을
# 덮어쓰지 않는다. CLI 로 직접 실행할 때만 alembic.ini 의 로깅을 적용한다.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

# DDL 은 커넥션 풀러가 아니라 direct URL 로 보낸다 (Neon pooler 는 일부
# DDL/세션 기능을 제한한다). 값이 따로 없으면 DATABASE_URL 과 동일하다.
config.set_main_option("sqlalchemy.url", DATABASE_DIRECT_URL.replace("%", "%%"))

target_metadata = Base.metadata


def _is_sqlite() -> bool:
    return DATABASE_DIRECT_URL.startswith("sqlite")


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # SQLite 는 ALTER 지원이 빈약해 batch 모드(테이블 재작성)가 필요하다.
            render_as_batch=_is_sqlite(),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
