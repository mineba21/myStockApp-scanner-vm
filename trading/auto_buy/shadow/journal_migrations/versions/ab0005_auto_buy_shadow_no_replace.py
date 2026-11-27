"""자동매수 SHADOW 저널 스키마 v5: 충돌 교체(REPLACE·UPSERT) 차단과 남은 감사 자료 불변 트리거.

Revision ID: ab0005
Revises: ab0004
Create Date: 2026-11-27

- 구조가 정확히 ab0004 일 때만 트리거를 추가한다 (다르면 중단). 기존 행은 바꾸지 않는다.
- downgrade 는 보호 해제가 되므로 지원하지 않는다.
"""
from alembic import op

from trading.auto_buy.shadow.journal_migrations.ddl_v5 import DDL_V5
from trading.auto_buy.shadow.schema import foreign_objects, structure_diff

revision = "ab0005"
down_revision = "ab0004"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    raw = conn.connection.driver_connection if hasattr(conn.connection, "driver_connection") else conn.connection
    foreign = foreign_objects(raw)
    if foreign:
        raise RuntimeError("자동매수 SHADOW 저널이 아닌 객체가 있습니다: " + ", ".join(sorted(foreign)[:10]))
    diff = structure_diff(raw, "ab0004")
    if diff:
        raise RuntimeError("ab0004 구조와 달라 ab0005 를 적용하지 않습니다: " + "; ".join(diff[:5]))
    for stmt in DDL_V5:
        op.execute(stmt)


def downgrade():
    raise RuntimeError("자동매수 SHADOW 저널 downgrade 는 원장 보호 해제가 되므로 지원하지 않습니다.")
