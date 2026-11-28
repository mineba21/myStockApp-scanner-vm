"""자동매수 SHADOW 저널 전용 Alembic 환경 (운영 DB 의 alembic/, S01 저널 Migration 과 완전히 분리).

대상은 ``config.attributes["journal_path"]`` 의 SQLite 파일 하나뿐이다. config·database 모듈을 import 하지 않아
운영 DB URL 을 읽지 않는다. 버전 표는 ``ab_alembic_version``. 전체 upgrade 는 BEGIN IMMEDIATE 트랜잭션 하나다.
리비전 적용 전 같은 트랜잭션에서 원본 정합성 사전 검사(``raw_contract.journal_issues``)를 한다 — 실패하면 전체 롤백.
"""
from alembic import context
from sqlalchemy import create_engine, event, pool

from trading.auto_buy.shadow.schema import VERSION_TABLE

config = context.config
journal_path = config.attributes.get("journal_path")
if not journal_path:
    raise RuntimeError("journal_path 가 지정되지 않았습니다 (scripts.auto_buy_shadow_journal_migrate 로 실행).")
if context.is_offline_mode():
    raise RuntimeError("자동매수 SHADOW 저널 Migration 은 offline 모드를 지원하지 않습니다.")

engine = create_engine(f"sqlite:///{journal_path}", poolclass=pool.NullPool,
                       connect_args={"isolation_level": None, "timeout": 30})


@event.listens_for(engine, "begin")
def _begin(conn):
    conn.exec_driver_sql("BEGIN IMMEDIATE")


with engine.connect() as connection:
    connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    connection.exec_driver_sql("PRAGMA recursive_triggers=ON")          # 제품 쓰기 연결과 같은 설정
    if (connection.exec_driver_sql("PRAGMA recursive_triggers").scalar(),
            connection.exec_driver_sql("PRAGMA foreign_keys").scalar()) != (1, 1):
        raise RuntimeError("SHADOW 저널 Migration 연결 설정(recursive_triggers, foreign_keys) 적용 실패")
    context.configure(connection=connection, target_metadata=None, version_table=VERSION_TABLE,
                      transactional_ddl=True, transaction_per_migration=False)
    with context.begin_transaction():
        # 원본 정합성 사전 검사 (같은 BEGIN IMMEDIATE 트랜잭션, 어떤 리비전보다 먼저): 기존 스냅샷의 정규화 필드가 원본
        # row_json 과 다르면 ab0007 backfill 이 오염을 봉인하기 전에 전체를 중단한다 (원본·리비전 그대로, 자동 수정 없음).
        raw = connection.connection.driver_connection if hasattr(connection.connection, "driver_connection") \
            else connection.connection
        from trading.auto_buy.shadow.raw_contract import journal_issues
        bad = journal_issues(raw)
        if bad:
            raise RuntimeError("기존 스냅샷의 정규화 필드가 원본 row_json 과 달라 Migration 을 적용하지 않습니다 "
                               "(원본·봉인 보존, 자동 수정 없음): " + "; ".join(bad[:5]))
        context.run_migrations()
engine.dispose()
