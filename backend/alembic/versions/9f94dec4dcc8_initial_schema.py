"""Initial schema.

The whole reconciliation model in one migration: organizations and their
connected integrations, the accounts those integrations expose, immutable
raw_transactions, and everything a reconciliation run produces --
economic_events, event_members, reconciliation_links, reconciliation_exceptions
and manual_links.

Two constraints here carry most of the weight:

    uq_raw_txn_source_revision   (org_id, source, source_id, revision)
        makes re-syncing idempotent and lets a restatement sit beside the row
        it replaces instead of overwriting it.

    uq_exc_org_key               (org_id, dedupe_key)
        keeps the exception queue from re-raising the same item on every run,
        so a human's decision survives the next night's reconciliation.

Verified up, down and up again.

Revision ID: 9f94dec4dcc8
Revises:
Create Date: 2026-08-31
"""
from alembic import op
import sqlalchemy as sa


revision = '9f94dec4dcc8'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('organizations',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('base_currency', sa.String(length=3), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('integrations',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('org_id', sa.String(), nullable=False),
    sa.Column('source', sa.String(), nullable=False),
    sa.Column('role', sa.String(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('credential_ref', sa.String(), nullable=True),
    sa.Column('config', sa.JSON(), nullable=True),
    sa.CheckConstraint("role in ('bank','processor','card','payroll','ledger')", name='ck_integration_role'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('org_id', 'source', name='uq_integration_org_source')
    )
    op.create_table('reconciliation_runs',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('org_id', sa.String(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('input_fingerprint', sa.String(), nullable=False),
    sa.Column('ruleset_version', sa.String(), nullable=False),
    sa.Column('event_count', sa.Integer(), nullable=True),
    sa.Column('exception_count', sa.Integer(), nullable=True),
    sa.Column('triggered_by', sa.String(), nullable=True),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('accounts',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('org_id', sa.String(), nullable=False),
    sa.Column('integration_id', sa.String(), nullable=False),
    sa.Column('external_id', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('role', sa.String(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('is_internal', sa.Boolean(), nullable=False),
    sa.Column('is_cash', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['integration_id'], ['integrations.id'], ),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('integration_id', 'external_id', name='uq_account_external')
    )
    op.create_table('reconciliation_exceptions',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('org_id', sa.String(), nullable=False),
    sa.Column('run_id', sa.String(), nullable=False),
    sa.Column('reason', sa.String(), nullable=False),
    sa.Column('severity', sa.String(), nullable=False),
    sa.Column('dedupe_key', sa.String(), nullable=False),
    sa.Column('txn_ids', sa.JSON(), nullable=False),
    sa.Column('candidate_txn_ids', sa.JSON(), nullable=True),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('resolved_by', sa.String(), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolution', sa.JSON(), nullable=True),
    sa.CheckConstraint("severity in ('review','blocking')", name='ck_exc_severity'),
    sa.CheckConstraint("status in ('open','resolved','dismissed')", name='ck_exc_status'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ),
    sa.ForeignKeyConstraint(['run_id'], ['reconciliation_runs.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('org_id', 'dedupe_key', name='uq_exc_org_key')
    )
    op.create_index('ix_exc_open', 'reconciliation_exceptions', ['org_id', 'status', 'severity'], unique=False)
    op.create_index(op.f('ix_reconciliation_exceptions_dedupe_key'), 'reconciliation_exceptions', ['dedupe_key'], unique=False)
    op.create_table('balances',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('org_id', sa.String(), nullable=False),
    sa.Column('account_id', sa.String(), nullable=False),
    sa.Column('as_of', sa.DateTime(timezone=True), nullable=False),
    sa.Column('amount', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('account_id', 'as_of', name='uq_balance_point')
    )
    op.create_index('ix_balance_latest', 'balances', ['org_id', 'account_id', 'as_of'], unique=False)
    op.create_table('raw_transactions',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('org_id', sa.String(), nullable=False),
    sa.Column('account_id', sa.String(), nullable=False),
    sa.Column('source', sa.String(), nullable=False),
    sa.Column('source_id', sa.String(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('counterparty', sa.String(), nullable=True),
    sa.Column('counterparty_key', sa.String(), nullable=True),
    sa.Column('category_hint', sa.String(), nullable=True),
    sa.Column('external_refs', sa.JSON(), nullable=True),
    sa.Column('batch_id', sa.String(), nullable=True),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('voided', sa.Boolean(), nullable=False),
    sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('raw_payload', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('org_id', 'source', 'source_id', 'revision', name='uq_raw_txn_source_revision')
    )
    op.create_index(op.f('ix_raw_transactions_batch_id'), 'raw_transactions', ['batch_id'], unique=False)
    op.create_index(op.f('ix_raw_transactions_counterparty_key'), 'raw_transactions', ['counterparty_key'], unique=False)
    op.create_index('ix_raw_txn_current', 'raw_transactions', ['org_id', 'source', 'source_id', 'revision'], unique=False)
    op.create_index('ix_raw_txn_match', 'raw_transactions', ['org_id', 'currency', 'amount', 'occurred_at'], unique=False)
    op.create_index('ix_raw_txn_org_time', 'raw_transactions', ['org_id', 'occurred_at'], unique=False)
    op.create_table('economic_events',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('org_id', sa.String(), nullable=False),
    sa.Column('run_id', sa.String(), nullable=False),
    sa.Column('kind', sa.String(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('counterparty', sa.String(), nullable=True),
    sa.Column('counterparty_key', sa.String(), nullable=True),
    sa.Column('primary_txn_id', sa.String(), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('dedupe_key', sa.String(), nullable=False),
    sa.CheckConstraint("kind in ('revenue','spend','payroll','fee','transfer','unknown')", name='ck_event_kind'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ),
    sa.ForeignKeyConstraint(['primary_txn_id'], ['raw_transactions.id'], ),
    sa.ForeignKeyConstraint(['run_id'], ['reconciliation_runs.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('run_id', 'dedupe_key', name='uq_event_run_key')
    )
    op.create_index(op.f('ix_economic_events_counterparty_key'), 'economic_events', ['counterparty_key'], unique=False)
    op.create_index('ix_event_org_time', 'economic_events', ['org_id', 'occurred_at'], unique=False)
    op.create_table('manual_links',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('org_id', sa.String(), nullable=False),
    sa.Column('txn_id_a', sa.String(), nullable=False),
    sa.Column('txn_id_b', sa.String(), nullable=False),
    sa.Column('relation', sa.String(), nullable=False),
    sa.Column('created_by', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ),
    sa.ForeignKeyConstraint(['txn_id_a'], ['raw_transactions.id'], ),
    sa.ForeignKeyConstraint(['txn_id_b'], ['raw_transactions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('txn_id_a', 'txn_id_b', name='uq_manual_link')
    )
    op.create_table('event_members',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('event_id', sa.String(), nullable=False),
    sa.Column('txn_id', sa.String(), nullable=False),
    sa.Column('contribution', sa.String(), nullable=False),
    sa.CheckConstraint("contribution in ('contributing','suppressed')", name='ck_member_contribution'),
    sa.ForeignKeyConstraint(['event_id'], ['economic_events.id'], ),
    sa.ForeignKeyConstraint(['txn_id'], ['raw_transactions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('event_id', 'txn_id', name='uq_event_member')
    )
    op.create_table('reconciliation_links',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('run_id', sa.String(), nullable=False),
    sa.Column('event_id', sa.String(), nullable=True),
    sa.Column('rule', sa.String(), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('txn_ids', sa.JSON(), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['event_id'], ['economic_events.id'], ),
    sa.ForeignKeyConstraint(['run_id'], ['reconciliation_runs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('reconciliation_links')
    op.drop_table('event_members')
    op.drop_table('manual_links')
    op.drop_index('ix_event_org_time', table_name='economic_events')
    op.drop_index(op.f('ix_economic_events_counterparty_key'), table_name='economic_events')
    op.drop_table('economic_events')
    op.drop_index('ix_raw_txn_org_time', table_name='raw_transactions')
    op.drop_index('ix_raw_txn_match', table_name='raw_transactions')
    op.drop_index('ix_raw_txn_current', table_name='raw_transactions')
    op.drop_index(op.f('ix_raw_transactions_counterparty_key'), table_name='raw_transactions')
    op.drop_index(op.f('ix_raw_transactions_batch_id'), table_name='raw_transactions')
    op.drop_table('raw_transactions')
    op.drop_index('ix_balance_latest', table_name='balances')
    op.drop_table('balances')
    op.drop_index(op.f('ix_reconciliation_exceptions_dedupe_key'), table_name='reconciliation_exceptions')
    op.drop_index('ix_exc_open', table_name='reconciliation_exceptions')
    op.drop_table('reconciliation_exceptions')
    op.drop_table('accounts')
    op.drop_table('reconciliation_runs')
    op.drop_table('integrations')
    op.drop_table('organizations')
