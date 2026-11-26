"""자동매수 SHADOW 저널 스키마 v2: 일봉 거래량·거래량 상태, 체결 근거 거래량, 보유 중 거래량 0 기록.

Revision ID: ab0002
Revises: ab0001
Create Date: 2026-11-26

- ab0001 구조가 정확히 v1 일 때만 열을 추가한다 (다르면 중단). 기존 행은 바꾸지 않는다 (새 열 NULL / 기본값 0).
- downgrade 는 기록 삭제가 되므로 지원하지 않는다.
"""
from alembic import op

from trading.auto_buy.shadow.journal_migrations.ddl_v2 import DDL_V2
from trading.auto_buy.shadow.schema import foreign_objects, structure_diff

revision = "ab0002"
down_revision = "ab0001"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    raw = conn.connection.driver_connection if hasattr(conn.connection, "driver_connection") else conn.connection
    foreign = foreign_objects(raw)
    if foreign:
        raise RuntimeError("자동매수 SHADOW 저널이 아닌 객체가 있습니다: " + ", ".join(sorted(foreign)[:10]))
    diff = structure_diff(raw, "ab0001")
    if diff:
        raise RuntimeError("ab0001 구조가 v1 과 달라 ab0002 를 적용하지 않습니다: " + "; ".join(diff[:5]))
    for stmt in DDL_V2:
        op.execute(stmt)


def downgrade():
    raise RuntimeError("자동매수 SHADOW 저널 downgrade 는 기록 삭제가 되므로 지원하지 않습니다.")
