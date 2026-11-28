"""자동매수 SHADOW 저널 스키마 v1.

Revision ID: ab0001
Revises:
Create Date: 2026-11-25

- 자동매수 SHADOW 전용 SQLite 에 ab_* 테이블·인덱스·트리거를 만든다. 다른 객체가 있으면(다른 저널을 가리킨 경우) 중단한다.
- downgrade 는 가상 계좌·스냅샷 기록 삭제가 되므로 지원하지 않는다.
"""
from alembic import op

from trading.auto_buy.shadow.journal_migrations.ddl_v1 import DDL_V1
from trading.auto_buy.shadow.schema import VERSION_TABLE, foreign_objects, owned_objects

revision = "ab0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    raw = conn.connection.driver_connection if hasattr(conn.connection, "driver_connection") else conn.connection
    foreign = foreign_objects(raw)
    if foreign:
        raise RuntimeError("자동매수 SHADOW 저널이 아닌 객체가 있습니다 (다른 DB 를 가리킨 것으로 보고 중단): "
                           + ", ".join(sorted(foreign)[:10]))
    if owned_objects(raw):
        raise RuntimeError("버전 기록 없는 ab_* 객체가 이미 있습니다. Migration 을 중단합니다.")
    for stmt in DDL_V1:
        op.execute(stmt)


def downgrade():
    raise RuntimeError("자동매수 SHADOW 저널 downgrade 는 기록 삭제가 되므로 지원하지 않습니다.")
