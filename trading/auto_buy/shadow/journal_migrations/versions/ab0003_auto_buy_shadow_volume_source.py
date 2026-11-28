"""자동매수 SHADOW 저널 스키마 v3: 일봉 거래량 출처·원본 근거.

Revision ID: ab0003
Revises: ab0002
Create Date: 2026-11-26

- 구조가 정확히 ab0002 일 때만 열을 추가한다 (다르면 중단). 기존 행은 바꾸지 않는다 (새 열 NULL = 출처 미확인).
- downgrade 는 기록 삭제가 되므로 지원하지 않는다.
"""
from alembic import op

from trading.auto_buy.shadow.journal_migrations.ddl_v3 import DDL_V3
from trading.auto_buy.shadow.schema import foreign_objects, structure_diff

revision = "ab0003"
down_revision = "ab0002"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    raw = conn.connection.driver_connection if hasattr(conn.connection, "driver_connection") else conn.connection
    foreign = foreign_objects(raw)
    if foreign:
        raise RuntimeError("자동매수 SHADOW 저널이 아닌 객체가 있습니다: " + ", ".join(sorted(foreign)[:10]))
    diff = structure_diff(raw, "ab0002")
    if diff:
        raise RuntimeError("ab0002 구조와 달라 ab0003 을 적용하지 않습니다: " + "; ".join(diff[:5]))
    for stmt in DDL_V3:
        op.execute(stmt)


def downgrade():
    raise RuntimeError("자동매수 SHADOW 저널 downgrade 는 기록 삭제가 되므로 지원하지 않습니다.")
