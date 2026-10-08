"""Add shadow capture inventory without changing scanner tables."""
from alembic import op
import sqlalchemy as sa

revision = "b62f8d0e3c71"
down_revision = "a41e7c9d2b60"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_scan_captures",
        sa.Column("scan_id", sa.Integer(), primary_key=True),
        sa.Column("expected_json", sa.Text(), nullable=False),
        sa.Column("audit_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("reset_at", sa.DateTime(), nullable=True),
        sa.Column("reset_reason", sa.String(200), nullable=True),
        sa.CheckConstraint("status IN ('CAPTURING', 'COMPLETE', 'GAP')", name="ck_agent_capture_status"),
    )


def downgrade():
    op.drop_table("agent_scan_captures")
