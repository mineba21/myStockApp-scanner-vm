"""자동매수 SHADOW 저널 스키마 v6: 테이블별 데이터 불변성 계약 (숨은 rowid·변경 허용 목록·상태 전환·최초 1회 입력·
판정 충돌 정책).

Revision ID: ab0006
Revises: ab0005
Create Date: 2026-11-27

- 구조가 정확히 ab0005 이고 기존 데이터가 계약과 맞을 때만 적용한다 (다르면 중단, 원본 무변경).
- 트리거를 더하고, 정책이 바뀐 ab0005 트리거 ``ab_eligibility_no_replace_insert`` 1개만 교체한다. 기존 행은 바꾸지 않는다.
- downgrade 는 보호 해제가 되므로 지원하지 않는다.
"""
from alembic import op

from trading.auto_buy.shadow.journal_migrations import ddl_v6
from trading.auto_buy.shadow.schema import foreign_objects, structure_diff

revision = "ab0006"
down_revision = "ab0005"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    raw = conn.connection.driver_connection if hasattr(conn.connection, "driver_connection") else conn.connection
    foreign = foreign_objects(raw)
    if foreign:
        raise RuntimeError("자동매수 SHADOW 저널이 아닌 객체가 있습니다: " + ", ".join(sorted(foreign)[:10]))
    diff = structure_diff(raw, "ab0005")
    if diff:
        raise RuntimeError("ab0005 구조와 달라 ab0006 을 적용하지 않습니다: " + "; ".join(diff[:5]))
    bad = ddl_v6.contract_violations(raw)
    if bad:
        raise RuntimeError("기존 데이터가 ab0006 계약과 달라 적용하지 않습니다: " + "; ".join(bad[:5]))
    for stmt in ddl_v6.DDL_V6:
        op.execute(stmt)


def downgrade():
    raise RuntimeError("자동매수 SHADOW 저널 downgrade 는 원장 보호 해제가 되므로 지원하지 않습니다.")
