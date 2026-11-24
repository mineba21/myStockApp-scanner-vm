"""자동 손절 저널 스키마 v1: auto_stop_* 테이블·인덱스.

Revision ID: as0001
Revises:
Create Date: 2026-11-25

- 자동 손절 객체가 하나도 없으면 v1 DDL 을 실행한다.
- 이전 자체 DDL(AutoStopStore 생성자) 방식으로 이미 만들어진 저널은 객체 목록과 정의(sqlite_master)가
  v1 과 정확히 같고 무결성·외래키 검사를 통과할 때만 그대로 두고 버전을 기록한다. 하나라도 다르면 중단한다.
- 기존 Scanner 주문 저널 테이블(scanner_buy_orders 등)은 읽지도 바꾸지도 않는다.
- downgrade 는 자동 손절 주문 기록을 지우게 되므로 지원하지 않는다.
"""
from alembic import op

from trading.auto_stop.journal_migrations.ddl_v1 import DDL_V1
from trading.auto_stop.schema import owned_objects, structure_diff

revision = "as0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    raw = conn.connection.driver_connection if hasattr(conn.connection, "driver_connection") else conn.connection
    existing = owned_objects(raw)
    if not existing:
        for stmt in DDL_V1:
            op.execute(stmt)
        return
    diff = structure_diff(raw)
    if diff:
        raise RuntimeError("기존 자동 손절 객체가 v1 정의와 다릅니다. Migration 을 중단합니다: " + "; ".join(diff))
    integrity = raw.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"저널 무결성 검사 실패: {integrity}")
    fk = [r for r in raw.execute("PRAGMA foreign_key_check") if str(r[0]).startswith("auto_stop_")]
    if fk:
        raise RuntimeError(f"자동 손절 테이블 외래키 불일치 {len(fk)}건. Migration 을 중단합니다.")


def downgrade():
    raise RuntimeError("자동 손절 저널 downgrade 는 주문 기록 삭제가 되므로 지원하지 않습니다.")
