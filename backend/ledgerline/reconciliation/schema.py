"""SQLAlchemy schema for the reconciliation layer.

Shape of the thing:

    raw_transactions   immutable, one row exactly as a source reported it
           |
           |  reconciliation run (deterministic, replayable)
           v
    economic_events    one row per thing that actually happened
    event_members      which raw rows belong to which event, and their role
    reconciliation_links   the evidence: which rule merged what, at what confidence
    reconciliation_exceptions   what the engine refused to guess about
    reconciliation_runs    fingerprint + code version, for attestation

Raw rows are never mutated and never deleted.  Reconciliation is a pure
function of them, so a rule change means re-running history, not migrating
numbers -- and an auditor can reproduce any figure the dashboard ever showed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


# Money: NUMERIC, never float.  18/2 handles anything a private company will
# do; widen the scale if you ever ingest crypto or sub-cent processor fees.
Money = Numeric(18, 2)


class Organization(Base):
    __tablename__ = "organizations"
    id = Column(String, primary_key=True, default=_uuid)
    name = Column(String, nullable=False)
    base_currency = Column(String(3), nullable=False, default="USD")
    created_at = Column(DateTime(timezone=True), default=_now)


class Integration(Base):
    __tablename__ = "integrations"
    id = Column(String, primary_key=True, default=_uuid)
    org_id = Column(String, ForeignKey("organizations.id"), nullable=False)
    source = Column(String, nullable=False)          # "stripe"
    # bank | processor | card | payroll | ledger -- decides trust precedence
    role = Column(String, nullable=False)
    status = Column(String, nullable=False, default="pending")
    last_synced_at = Column(DateTime(timezone=True))
    # Tokens belong in a KMS/secrets manager, not here.  Store the handle only.
    credential_ref = Column(String)
    config = Column(JSON, default=dict)
    __table_args__ = (
        UniqueConstraint("org_id", "source", name="uq_integration_org_source"),
        CheckConstraint(
            "role in ('bank','processor','card','payroll','ledger')",
            name="ck_integration_role",
        ),
    )


class Account(Base):
    __tablename__ = "accounts"
    id = Column(String, primary_key=True, default=_uuid)
    org_id = Column(String, ForeignKey("organizations.id"), nullable=False)
    integration_id = Column(String, ForeignKey("integrations.id"), nullable=False)
    external_id = Column(String, nullable=False)
    name = Column(String, nullable=False)
    role = Column(String, nullable=False)
    currency = Column(String(3), nullable=False, default="USD")
    # Owned by the org: movement between two of these is a transfer.
    is_internal = Column(Boolean, nullable=False, default=True)
    # Counts toward cash on hand.  Card accounts are liabilities, not cash.
    is_cash = Column(Boolean, nullable=False, default=False)
    __table_args__ = (
        UniqueConstraint("integration_id", "external_id", name="uq_account_external"),
    )


class RawTransaction(Base):
    """Immutable. One row as one source reported it.

    The unique constraint is the single most valuable line in this file: it
    makes re-syncs idempotent, which removes the most common cause of a
    silently doubled dashboard number.
    """

    __tablename__ = "raw_transactions"
    id = Column(String, primary_key=True, default=_uuid)
    org_id = Column(String, ForeignKey("organizations.id"), nullable=False)
    account_id = Column(String, ForeignKey("accounts.id"), nullable=False)
    source = Column(String, nullable=False)
    source_id = Column(String, nullable=False)
    amount = Column(Money, nullable=False)           # signed, account's POV
    currency = Column(String(3), nullable=False)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    description = Column(Text, default="")
    counterparty = Column(String)
    counterparty_key = Column(String, index=True)    # normalised, for dedupe
    category_hint = Column(String)
    external_refs = Column(JSON, default=dict)
    batch_id = Column(String, index=True)
    ingested_at = Column(DateTime(timezone=True), default=_now)
    # Exactly what the API returned, kept forever so a rule change can be
    # replayed against the original payload rather than our interpretation.
    raw_payload = Column(JSON)
    __table_args__ = (
        UniqueConstraint("org_id", "source", "source_id", name="uq_raw_txn_source"),
        Index("ix_raw_txn_org_time", "org_id", "occurred_at"),
        Index("ix_raw_txn_match", "org_id", "currency", "amount", "occurred_at"),
    )


class Balance(Base):
    """Point-in-time account balance.  Cash on hand comes from here, not sums."""

    __tablename__ = "balances"
    id = Column(String, primary_key=True, default=_uuid)
    org_id = Column(String, ForeignKey("organizations.id"), nullable=False)
    account_id = Column(String, ForeignKey("accounts.id"), nullable=False)
    as_of = Column(DateTime(timezone=True), nullable=False)
    amount = Column(Money, nullable=False)
    currency = Column(String(3), nullable=False)
    __table_args__ = (
        UniqueConstraint("account_id", "as_of", name="uq_balance_point"),
        Index("ix_balance_latest", "org_id", "account_id", "as_of"),
    )


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"
    id = Column(String, primary_key=True, default=_uuid)
    org_id = Column(String, ForeignKey("organizations.id"), nullable=False)
    started_at = Column(DateTime(timezone=True), default=_now)
    finished_at = Column(DateTime(timezone=True))
    # sha256 of the inputs + the ruleset version: together these prove the run
    # is reproducible, which is what attestation actually means.
    input_fingerprint = Column(String, nullable=False)
    ruleset_version = Column(String, nullable=False)
    event_count = Column(Integer, default=0)
    exception_count = Column(Integer, default=0)
    triggered_by = Column(String, default="agent")


class EconomicEvent(Base):
    """One thing that happened to the company's money. Metrics read only this."""

    __tablename__ = "economic_events"
    id = Column(String, primary_key=True, default=_uuid)
    org_id = Column(String, ForeignKey("organizations.id"), nullable=False)
    run_id = Column(String, ForeignKey("reconciliation_runs.id"), nullable=False)
    kind = Column(String, nullable=False)
    amount = Column(Money, nullable=False)
    currency = Column(String(3), nullable=False)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    counterparty = Column(String)
    counterparty_key = Column(String, index=True)
    primary_txn_id = Column(String, ForeignKey("raw_transactions.id"), nullable=False)
    confidence = Column(Float, nullable=False, default=1.0)
    # Stable across runs so the timeline does not churn when rules change.
    dedupe_key = Column(String, nullable=False)
    members = relationship("EventMember", back_populates="event")
    __table_args__ = (
        UniqueConstraint("run_id", "dedupe_key", name="uq_event_run_key"),
        Index("ix_event_org_time", "org_id", "occurred_at"),
        CheckConstraint(
            "kind in ('revenue','spend','payroll','fee','transfer','unknown')",
            name="ck_event_kind",
        ),
    )


class EventMember(Base):
    __tablename__ = "event_members"
    id = Column(String, primary_key=True, default=_uuid)
    event_id = Column(String, ForeignKey("economic_events.id"), nullable=False)
    txn_id = Column(String, ForeignKey("raw_transactions.id"), nullable=False)
    # contributing | suppressed -- suppressed rows are shown in drill-down but
    # add nothing to the total. This column is why the maths is auditable.
    contribution = Column(String, nullable=False, default="contributing")
    event = relationship("EconomicEvent", back_populates="members")
    __table_args__ = (
        UniqueConstraint("event_id", "txn_id", name="uq_event_member"),
        CheckConstraint(
            "contribution in ('contributing','suppressed')",
            name="ck_member_contribution",
        ),
    )


class ReconciliationLink(Base):
    """The evidence trail: which rule merged which rows, and how sure it was."""

    __tablename__ = "reconciliation_links"
    id = Column(String, primary_key=True, default=_uuid)
    run_id = Column(String, ForeignKey("reconciliation_runs.id"), nullable=False)
    event_id = Column(String, ForeignKey("economic_events.id"))
    rule = Column(String, nullable=False)
    confidence = Column(Float, nullable=False)
    txn_ids = Column(JSON, nullable=False)
    note = Column(Text, default="")


class ReconciliationExceptionRow(Base):
    """What the engine would not guess about. This is the human's work queue."""

    __tablename__ = "reconciliation_exceptions"
    id = Column(String, primary_key=True, default=_uuid)
    org_id = Column(String, ForeignKey("organizations.id"), nullable=False)
    run_id = Column(String, ForeignKey("reconciliation_runs.id"), nullable=False)
    reason = Column(String, nullable=False)
    severity = Column(String, nullable=False, default="review")
    txn_ids = Column(JSON, nullable=False)
    candidate_txn_ids = Column(JSON, default=list)
    detail = Column(Text, default="")
    status = Column(String, nullable=False, default="open")
    resolved_by = Column(String)
    resolved_at = Column(DateTime(timezone=True))
    # How a human resolved it, so the engine can learn the pairing next run
    # without ever having guessed it.
    resolution = Column(JSON, default=dict)
    __table_args__ = (
        CheckConstraint("severity in ('review','blocking')", name="ck_exc_severity"),
        CheckConstraint(
            "status in ('open','resolved','dismissed')", name="ck_exc_status"
        ),
        Index("ix_exc_open", "org_id", "status", "severity"),
    )


class ManualLink(Base):
    """A pairing a human confirmed. Applied before the rules on every future run.

    This is how the system gets better without the engine ever guessing:
    ambiguity goes to a person once, and their answer becomes an input.
    """

    __tablename__ = "manual_links"
    id = Column(String, primary_key=True, default=_uuid)
    org_id = Column(String, ForeignKey("organizations.id"), nullable=False)
    txn_id_a = Column(String, ForeignKey("raw_transactions.id"), nullable=False)
    txn_id_b = Column(String, ForeignKey("raw_transactions.id"), nullable=False)
    relation = Column(String, nullable=False)     # transfer | echo | same_event
    created_by = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now)
    __table_args__ = (
        UniqueConstraint("txn_id_a", "txn_id_b", name="uq_manual_link"),
    )
