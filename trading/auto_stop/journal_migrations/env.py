"""자동 손절 저널 전용 Alembic 환경 (운영 DB 의 alembic/ 과 완전히 분리).

대상은 ``config.attributes["journal_path"]`` 의 SQLite 파일 하나뿐이다. config·database 모듈을 import 하지 않아
운영 DB URL 을 읽지 않는다. 버전 표는 ``auto_stop_alembic_version`` (운영 DB 의 alembic_version 과 별개).
전체 upgrade 는 BEGIN IMMEDIATE 트랜잭션 하나로 실행돼, 실패하면 DDL 과 버전 기록이 함께 되돌려진다.
"""
from alembic import context
from sqlalchemy import create_engine, event, pool

from trading.auto_stop.schema import VERSION_TABLE

config = context.config
journal_path = config.attributes.get("journal_path")
if not journal_path:
    raise RuntimeError("journal_path 가 지정되지 않았습니다 (scripts.auto_stop_journal_migrate 로 실행).")
if context.is_offline_mode():
    raise RuntimeError("저널 Migration 은 offline(SQL 출력) 모드를 지원하지 않습니다.")

engine = create_engine(f"sqlite:///{journal_path}", poolclass=pool.NullPool,
                       connect_args={"isolation_level": None, "timeout": 30})


@event.listens_for(engine, "begin")
def _begin(conn):
    # pysqlite 는 DDL 앞에서 트랜잭션을 열지 않는다. 직접 BEGIN IMMEDIATE 로 열어 DDL 까지 원자적으로 만든다.
    conn.exec_driver_sql("BEGIN IMMEDIATE")


with engine.connect() as connection:
    connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    context.configure(connection=connection, target_metadata=None, version_table=VERSION_TABLE,
                      transactional_ddl=True, transaction_per_migration=False)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
