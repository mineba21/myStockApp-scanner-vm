"""자동매수 SHADOW 저널 스키마 v7: 신호 집합·관계 단위 보호와 수집 봉인(SSV2).

Revision ID: ab0007
Revises: ab0006
Create Date: 2026-11-28

- 구조가 정확히 ab0006 이고 기존 데이터가 사전 검사(``ddl_v7.precheck``)를 통과할 때만 적용한다 (다르면 중단, 원본 무변경).
- 봉인 표와 트리거를 더하고, 기존 수집 기록마다 봉인 1행(source=AB0007_BACKFILL)을 기록한다. 기존 행·동결 해시는 바꾸지 않는다.
- 한 트랜잭션: 도중에 실패하면 전체 롤백 (ab0006 유지).
- downgrade 는 보호 해제가 되므로 지원하지 않는다.
"""
import time

import sqlalchemy as sa
from alembic import op

from trading.auto_buy.shadow.journal_migrations import ddl_v7
from trading.auto_buy.shadow.schema import foreign_objects, structure_diff

revision = "ab0007"
down_revision = "ab0006"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    raw = conn.connection.driver_connection if hasattr(conn.connection, "driver_connection") else conn.connection
    foreign = foreign_objects(raw)
    if foreign:
        raise RuntimeError("자동매수 SHADOW 저널이 아닌 객체가 있습니다: " + ", ".join(sorted(foreign)[:10]))
    diff = structure_diff(raw, "ab0006")
    if diff:
        raise RuntimeError("ab0006 구조와 달라 ab0007 을 적용하지 않습니다: " + "; ".join(diff[:5]))
    bad = ddl_v7.precheck(raw)
    if bad:
        raise RuntimeError("기존 데이터가 ab0007 사전 검사와 달라 적용하지 않습니다: " + "; ".join(bad[:5]))
    seals = ddl_v7.backfill_seal_rows(raw, time.time())
    for stmt in ddl_v7.DDL_V7:
        op.execute(stmt)
    for row in seals:                       # 봉인 INSERT 트리거가 건수를 다시 확인한다
        conn.execute(sa.text(ddl_v7.SEAL_INSERT), {**row, "source": "AB0007_BACKFILL"})


def downgrade():
    raise RuntimeError("자동매수 SHADOW 저널 downgrade 는 원장 보호 해제가 되므로 지원하지 않습니다.")
