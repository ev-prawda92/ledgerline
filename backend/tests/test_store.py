"""Persistence: ingest, runs, and the exception queue across runs.

SQLite in memory, so this needs no Postgres and runs in CI. The behaviours
under test are database-agnostic; the Postgres-specific parts (row-level
security, LISTEN/NOTIFY) are not here and are not claimed to be.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ledgerline.integrations.mercury import map_account as map_mercury_account
from ledgerline.integrations.mercury import map_transaction as map_mercury
from ledgerline.integrations.stripe import StripeIntegration, map_balance_transaction
from ledgerline.reconciliation.metrics import Balance
from ledgerline.reconciliation.schema import (
    Base,
    EconomicEvent,
    EventMember,
    Organization,
    ReconciliationExceptionRow,
)
from ledgerline.reconciliation.types import EventKind
from ledgerline.store import (
    load_raw_transactions,
    run_reconciliation,
    save_accounts,
    save_balances,
    save_raw_transactions,
)

ORG = "org_1"
PAYOUT_ID = "po_1abcdefgh"
CHARGED_AT = int(datetime(2026, 8, 21, 12, 0, tzinfo=UTC).timestamp())
PAID_OUT_AT = int(datetime(2026, 8, 22, 8, 0, tzinfo=UTC).timestamp())

STRIPE_ACCOUNT = StripeIntegration("sk_test", ORG).balance_account
MERCURY_ACCOUNT = map_mercury_account({"id": "acc_1", "nickname": "Mercury operating"}, ORG)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Organization(id=ORG, name="Northwind"))
        save_accounts(s, ORG, [STRIPE_ACCOUNT, MERCURY_ACCOUNT])
        s.flush()
        yield s


def stripe_rows():
    charges = [
        map_balance_transaction(
            {"id": f"txn_{i}", "type": "charge", "amount": cents, "currency": "usd",
             "created": CHARGED_AT, "source": f"ch_{i}", "description": name},
            account_id=STRIPE_ACCOUNT.id, payout_id=PAYOUT_ID)
        for i, (cents, name) in enumerate(
            [(4000000, "Acme"), (3000000, "Vertex"), (1500000, "Loom")])
    ]
    payout = map_balance_transaction(
        {"id": "txn_payout", "type": "payout", "amount": -8240000, "currency": "usd",
         "created": PAID_OUT_AT, "source": PAYOUT_ID, "description": "STRIPE PAYOUT"},
        account_id=STRIPE_ACCOUNT.id)
    return [*charges, payout]


def mercury_deposit(status: str = "sent", amount: float = 82400.00):
    return map_mercury(
        {"id": "txn_wire", "amount": amount, "status": status,
         "kind": "incomingDomesticWire", "createdAt": "2026-08-22T09:00:00Z",
         "postedAt": "2026-08-22T09:14:00Z", "counterpartyName": "STRIPE",
         "bankDescription": f"STRIPE TRANSFER {PAYOUT_ID} ACH CREDIT",
         "currency": "USD"},
        account_id=MERCURY_ACCOUNT.id)


# --- ingest -----------------------------------------------------------------

def test_rows_round_trip_without_losing_anything_the_engine_needs(session):
    save_raw_transactions(session, ORG, stripe_rows())
    loaded = {r.source_id: r for r in load_raw_transactions(session, ORG)}
    charge = loaded["txn_0"]
    assert charge.amount == Decimal("40000")
    assert charge.batch_id == PAYOUT_ID
    assert charge.external_refs["stripe_source_id"] == "ch_0"
    assert charge.occurred_at.tzinfo is not None


def test_resyncing_the_same_rows_inserts_nothing(session):
    first = save_raw_transactions(session, ORG, stripe_rows())
    second = save_raw_transactions(session, ORG, stripe_rows())
    assert first.inserted == 4 and first.already_present == 0
    assert second.inserted == 0 and second.already_present == 4
    assert len(load_raw_transactions(session, ORG)) == 4


def test_a_batch_containing_the_same_row_twice_inserts_it_once(session):
    rows = stripe_rows()
    result = save_raw_transactions(session, ORG, [*rows, rows[0]])
    assert result.inserted == 4 and result.already_present == 1


def test_a_restatement_is_stored_beside_the_row_it_replaces(session):
    save_raw_transactions(session, ORG, [mercury_deposit(status="pending")])
    save_raw_transactions(session, ORG, [mercury_deposit(status="sent")])
    stored = load_raw_transactions(session, ORG)
    assert sorted(r.revision for r in stored) == [0, 1]


# --- runs -------------------------------------------------------------------

def test_a_run_reconciles_what_was_stored(session):
    save_raw_transactions(session, ORG, [*stripe_rows(), mercury_deposit()])
    summary, result = run_reconciliation(session, ORG)
    kinds = sorted(e.kind.value for e in result.events)
    assert kinds == ["fee", "revenue", "revenue", "revenue", "transfer"]
    assert summary.fingerprint == result.input_fingerprint


def test_events_and_their_members_are_stored_for_drill_down(session):
    save_raw_transactions(session, ORG, [*stripe_rows(), mercury_deposit()])
    run_reconciliation(session, ORG)
    transfer = session.execute(
        select(EconomicEvent).where(EconomicEvent.kind == "transfer")).scalars().one()
    members = session.execute(
        select(EventMember).where(EventMember.event_id == transfer.id)).scalars().all()
    assert {m.txn_id for m in members} == {"stripe:txn_payout", "mercury:txn_wire:1"}


def test_two_identical_runs_produce_the_same_fingerprint(session):
    save_raw_transactions(session, ORG, [*stripe_rows(), mercury_deposit()])
    first, _ = run_reconciliation(session, ORG)
    second, _ = run_reconciliation(session, ORG)
    assert first.fingerprint == second.fingerprint
    assert first.run_id != second.run_id


def test_event_keys_are_stable_across_runs_so_the_timeline_does_not_churn(session):
    save_raw_transactions(session, ORG, [*stripe_rows(), mercury_deposit()])
    run_reconciliation(session, ORG)
    first = {e.dedupe_key for e in session.execute(select(EconomicEvent)).scalars()}
    run_reconciliation(session, ORG)
    keys = [e.dedupe_key for e in session.execute(select(EconomicEvent)).scalars()]
    assert set(keys) - first == set()


# --- the queue across runs --------------------------------------------------

def open_exceptions(session):
    return session.execute(
        select(ReconciliationExceptionRow).where(
            ReconciliationExceptionRow.status == "open")).scalars().all()


def test_an_unresolved_item_is_not_re_raised_every_night(session):
    """The failure this prevents: a queue that grows by one row per run."""
    save_raw_transactions(session, ORG, [mercury_deposit()])
    first, _ = run_reconciliation(session, ORG)
    assert first.exceptions_opened == 1
    second, _ = run_reconciliation(session, ORG)
    assert second.exceptions_opened == 0 and second.exceptions_carried == 1
    assert len(open_exceptions(session)) == 1


def test_a_dismissed_item_does_not_come_back(session):
    """A human's decision has to outlive the run that produced the item."""
    save_raw_transactions(session, ORG, [mercury_deposit()])
    run_reconciliation(session, ORG)
    item = open_exceptions(session)[0]
    item.status = "dismissed"
    item.resolved_by = "evan"
    session.flush()

    run_reconciliation(session, ORG)
    assert open_exceptions(session) == []
    assert session.execute(select(ReconciliationExceptionRow)).scalars().one().status \
        == "dismissed"


def test_an_item_the_engine_stops_raising_is_closed(session):
    """The Stripe side arriving answers the orphaned deposit on its own."""
    save_raw_transactions(session, ORG, [mercury_deposit()])
    run_reconciliation(session, ORG)
    assert len(open_exceptions(session)) == 1

    save_raw_transactions(session, ORG, stripe_rows())
    summary, result = run_reconciliation(session, ORG)
    assert result.exceptions == []
    assert summary.exceptions_closed == 1
    assert open_exceptions(session) == []


# --- balances ---------------------------------------------------------------

def test_balances_are_stored_as_points_in_time(session):
    when = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)
    save_balances(session, ORG, [
        Balance(account_id=MERCURY_ACCOUNT.id, as_of=when, amount=Decimal("4200000")),
    ])
    again = save_balances(session, ORG, [
        Balance(account_id=MERCURY_ACCOUNT.id, as_of=when, amount=Decimal("4200000")),
    ])
    assert again == 0


def test_transfers_never_reach_revenue_after_a_round_trip(session):
    """The end-to-end property, but through the database rather than in memory."""
    save_raw_transactions(session, ORG, [*stripe_rows(), mercury_deposit()])
    _, result = run_reconciliation(session, ORG)
    transfers = result.by_kind(EventKind.TRANSFER)
    assert len(transfers) == 1
    assert sum(e.amount for e in result.by_kind(EventKind.REVENUE)) == Decimal("85000")
