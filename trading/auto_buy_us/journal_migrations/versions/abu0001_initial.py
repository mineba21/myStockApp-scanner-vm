"""자동매수 RT1 저널 스키마 v1: abu_* 테이블·인덱스·트리거.

Revision ID: abu0001
Revises:
Create Date: 2026-11-28

- abu_ 객체가 하나도 없으면 v1 DDL 을 실행한다.
- 이미 있으면 정의가 v1 과 정확히 같고 무결성·외래키 검사를 통과할 때만 버전만 기록한다. 다르면 중단한다.
- 다른 소유자(자동 손절 auto_stop_*, Scanner 매수 저널 등)의 객체는 읽지도 바꾸지도 않는다.
- downgrade 는 기록 삭제가 되므로 지원하지 않는다.
"""
from alembic import op

from trading.auto_buy_us.journal_migrations.ddl_v1 import DDL_V1
from trading.auto_buy_us.schema import owned_objects, structure_diff

revision = "abu0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    raw = conn.connection.driver_connection if hasattr(conn.connection, "driver_connection") else conn.connection
    if not owned_objects(raw):
        for stmt in DDL_V1:
            op.execute(stmt)
        return
    diff = structure_diff(raw)
    if diff:
        raise RuntimeError("기존 abu_ 객체가 v1 정의와 다릅니다. Migration 을 중단합니다: " + "; ".join(diff))
    integrity = raw.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"저널 무결성 검사 실패: {integrity}")
    fk = [r for r in raw.execute("PRAGMA foreign_key_check") if str(r[0]).startswith("abu_")]
    if fk:
        raise RuntimeError(f"abu_ 테이블 외래키 불일치 {len(fk)}건. Migration 을 중단합니다.")


def downgrade():
    raise RuntimeError("자동매수 저널 downgrade 는 기록 삭제가 되므로 지원하지 않습니다.")
