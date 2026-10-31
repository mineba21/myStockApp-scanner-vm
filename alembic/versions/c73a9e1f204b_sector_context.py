"""Persist display-only sector trend snapshots."""
from alembic import op
import sqlalchemy as sa

revision = 'c73a9e1f204b'
down_revision = 'b62f8d0e3c71'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('scan_results', sa.Column('sector_context', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('scan_results') as batch:
        batch.drop_column('sector_context')
