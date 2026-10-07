"""Add optional runtime storage (no existing-table mutation).

Revision ID: a41e7c9d2b60
Revises: 7b21d9c4e6a0
"""
from alembic import op
import sqlalchemy as sa

revision = "a41e7c9d2b60"
down_revision = "7b21d9c4e6a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('agent_observations',
        sa.Column('id', sa.String(length=128), nullable=False),
        sa.Column('episode_id', sa.String(length=128), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('holding_id', sa.Integer(), nullable=False),
        sa.Column('market', sa.String(length=10), nullable=False),
        sa.Column('ticker', sa.String(length=20), nullable=False),
        sa.Column('scan_sequence', sa.Integer(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('observed_at', sa.DateTime(), nullable=False),
        sa.Column('data_as_of', sa.DateTime(), nullable=True),
        sa.Column('strategy_version', sa.String(length=128), nullable=False),
        sa.Column('input_version', sa.String(length=128), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=True),
        sa.Column('data_quality', sa.String(length=20), nullable=False),
        sa.Column('payload_json', sa.Text(), nullable=False),
        sa.Column('fingerprint', sa.String(length=64), nullable=False),
        sa.CheckConstraint('scan_sequence > 0 AND revision >= 0', name='ck_agent_observation_cursor'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('episode_id', 'scan_sequence', 'revision', name='uq_agent_observation_cursor')
    )
    op.create_table('agent_events',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('observation_id', sa.String(length=128), nullable=False),
        sa.Column('episode_id', sa.String(length=128), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('event_key', sa.String(length=128), nullable=False),
        sa.Column('event_type', sa.String(length=40), nullable=False),
        sa.Column('severity', sa.String(length=10), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('payload_json', sa.Text(), nullable=False),
        sa.CheckConstraint('sequence > 0', name='ck_agent_event_sequence'),
        sa.ForeignKeyConstraint(['observation_id'], ['agent_observations.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('episode_id', 'sequence', name='uq_agent_event_sequence'),
        sa.UniqueConstraint('observation_id', 'event_key', name='uq_agent_event_input')
    )
    op.create_table('agent_runtime_states',
        sa.Column('episode_id', sa.String(length=128), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('event_sequence', sa.Integer(), nullable=False),
        sa.Column('observation_id', sa.String(length=128), nullable=False),
        sa.Column('scan_sequence', sa.Integer(), nullable=False),
        sa.Column('observation_revision', sa.Integer(), nullable=False),
        sa.Column('data_as_of', sa.DateTime(), nullable=True),
        sa.Column('current_status', sa.String(length=20), nullable=True),
        sa.Column('last_valid_status', sa.String(length=20), nullable=True),
        sa.Column('data_quality', sa.String(length=20), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint('version > 0 AND event_sequence >= 0', name='ck_agent_state_version'),
        sa.ForeignKeyConstraint(['observation_id'], ['agent_observations.id'], ),
        sa.PrimaryKeyConstraint('episode_id')
    )
    op.create_table('agent_deliveries',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('event_id', sa.String(length=36), nullable=False),
        sa.Column('channel', sa.String(length=20), nullable=False),
        sa.Column('destination_key', sa.String(length=128), nullable=False),
        sa.Column('status', sa.String(length=10), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=False),
        sa.Column('lease_token', sa.String(length=36), nullable=True),
        sa.Column('lease_expires_at', sa.DateTime(), nullable=True),
        sa.Column('error_code', sa.String(length=40), nullable=True),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint("status != 'SENDING' OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)", name='ck_agent_delivery_lease'),
        sa.CheckConstraint("status IN ('PENDING', 'SENDING', 'SENT', 'FAILED')", name='ck_agent_delivery_status'),
        sa.CheckConstraint('attempts >= 0', name='ck_agent_delivery_attempts'),
        sa.ForeignKeyConstraint(['event_id'], ['agent_events.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('event_id', 'channel', 'destination_key', name='uq_agent_delivery_route')
    )
    op.create_index('ix_agent_delivery_due', 'agent_deliveries', ['status', 'next_attempt_at'], unique=False)
    op.create_index('ix_agent_delivery_expiry', 'agent_deliveries', ['status', 'lease_expires_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_agent_delivery_expiry', table_name='agent_deliveries')
    op.drop_index('ix_agent_delivery_due', table_name='agent_deliveries')
    op.drop_table('agent_deliveries')
    op.drop_table('agent_runtime_states')
    op.drop_table('agent_events')
    op.drop_table('agent_observations')
