"""record first detection time for new scanner results

Revision ID: 7b21d9c4e6a0
Revises: 3a9c4d2e1f70
"""
from alembic import op
import sqlalchemy as sa

revision = "7b21d9c4e6a0"
down_revision = "3a9c4d2e1f70"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing scan_time is mutable, so it cannot establish first detection.
    with op.batch_alter_table("scan_results") as batch_op:
        batch_op.add_column(sa.Column("first_detected_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("scan_results") as batch_op:
        batch_op.drop_column("first_detected_at")
