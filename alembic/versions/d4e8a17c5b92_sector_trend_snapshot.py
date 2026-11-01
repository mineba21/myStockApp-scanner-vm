"""add sector classification source and recent trend snapshot

Revision ID: d4e8a17c5b92
Revises: b62f8d0e3c71
Create Date: 2026-10-31

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d4e8a17c5b92"
down_revision: Union[str, Sequence[str], None] = "b62f8d0e3c71"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = (
    ("sector_source", sa.String(length=10)),
    ("sector_trend", sa.String(length=16)),
    ("sector_return_20d", sa.Float()),
    ("sector_trend_asof", sa.String(length=10)),
    ("sector_trend_basis", sa.String(length=40)),
    ("sector_trend_sample", sa.Integer()),
    ("sector_trend_members", sa.Integer()),
)


def upgrade() -> None:
    """Display-only sector snapshot. Existing rows stay NULL until rescanned."""
    with op.batch_alter_table("scan_results", schema=None) as batch_op:
        for name, type_ in COLUMNS:
            batch_op.add_column(sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("scan_results", schema=None) as batch_op:
        for name, _ in reversed(COLUMNS):
            batch_op.drop_column(name)
