"""add weekly volume quality snapshots

Revision ID: 3a9c4d2e1f70
Revises: 8fcb87470f2e
Create Date: 2026-09-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "3a9c4d2e1f70"
down_revision: Union[str, Sequence[str], None] = "8fcb87470f2e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Persist signal-date hard-floor and soft-quality volume snapshots."""
    with op.batch_alter_table("scan_results", schema=None) as batch_op:
        batch_op.add_column(sa.Column("weekly_volume_ratio", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("weekly_volume_ratio_4w", sa.Float(), nullable=True))
        batch_op.add_column(
            sa.Column("weekly_volume_quality_passed", sa.Boolean(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("weekly_volume_quality_threshold", sa.Float(), nullable=True)
        )


def downgrade() -> None:
    """Remove weekly volume quality snapshots."""
    with op.batch_alter_table("scan_results", schema=None) as batch_op:
        batch_op.drop_column("weekly_volume_quality_threshold")
        batch_op.drop_column("weekly_volume_quality_passed")
        batch_op.drop_column("weekly_volume_ratio_4w")
        batch_op.drop_column("weekly_volume_ratio")
