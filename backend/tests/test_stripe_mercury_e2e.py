"""Stripe and Mercury, reconciled end to end.

Every previous test of the engine used rows written by hand to make a rule
fire. These rows come out of the two real integration mappers, from payload
shapes the APIs actually return. It is the first honest test of whether the
matching rules survive contact with data nobody wrote for them.

The scenario is the one on the mockup: three charges totalling $85,000 are
swept into a single Stripe payout of $82,400, which lands in Mercury as a wire.
Naive summation sees $167,400 of income. Reality is $85,000 of revenue, one
internal transfer, and $2,600 of processor fees.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from ledgerline.integrations.mercury import map_account as map_mercury_account
from ledgerline.integrations.mercury import map_transaction as map_mercury
from ledgerline.integrations.stripe import StripeIntegration, map_balance_transaction
from ledgerline.reconciliation import EventKind, reconcile
from ledgerline.reconciliation.metrics import burn, naive_revenue, revenue

PAYOUT_ID = "po_1abcdefgh"
ORG = "org_1"
WINDOW = (datetime(2026, 1, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC))

# Derived rather than hardcoded: the first version of this file had the Stripe
# epochs a year off the Mercury timestamps, which is not a scenario worth
# testing and quietly hid whether the rules worked.
CHARGED_AT = int(datetime(2026, 8, 21, 12, 0, tzinfo=UTC).timestamp())
PAID_OUT_AT = int(datetime(2026, 8, 22, 8, 0, tzinfo=UTC).timestamp())

STRIPE_ACCOUNT = StripeIntegration("sk_test", ORG).balance_account
MERCURY_ACCOUNT = map_mercury_account({"id": "acc_1", "nickname": "Mercury operating"}, ORG)


def build_rows():
    """Three charges, the payout that sweeps them, and the deposit it becomes."""
    charges = [
        map_balance_transaction(
            {"id": f"txn_{i}", "type": "charge", "amount": cents, "currency": "usd",
             "created": CHARGED_AT, "source": f"ch_{i}", "description": name},
            account_id=STRIPE_ACCOUNT.id, payout_id=PAYOUT_ID)
        for i, (cents, name) in enumerate(
            [(4000000, "Acme Corp"), (3000000, "Vertex Inc"), (1500000, "Loom LLC")])
    ]
    payout = map_balance_transaction(
        {"id": "txn_payout", "type": "payout", "amount": -8240000, "currency": "usd",
         "created": PAID_OUT_AT, "source": PAYOUT_ID, "description": "STRIPE PAYOUT"},
        account_id=STRIPE_ACCOUNT.id)
    deposit = map_mercury(
        {"id": "txn_wire", "amount": 82400.00, "status": "sent",
         "kind": "incomingDomesticWire", "createdAt": "2026-08-22T09:00:00Z",
         "postedAt": "2026-08-22T09:14:00Z", "counterpartyName": "STRIPE",
         "bankDescription": f"STRIPE TRANSFER {PAYOUT_ID} ACH CREDIT",
         "currency": "USD"},
        account_id=MERCURY_ACCOUNT.id)
    return [*charges, payout, deposit]


def run():
    return reconcile(build_rows(), [STRIPE_ACCOUNT, MERCURY_ACCOUNT])


# --- the number that matters ------------------------------------------------

def test_naive_summation_double_counts_the_payout():
    """What a dashboard without this layer would show."""
    assert naive_revenue(build_rows(), *WINDOW) == Decimal("167400")


def test_reconciled_revenue_counts_the_charges_once():
    assert revenue(run().events, *WINDOW).value == Decimal("85000")


def test_the_deposit_is_a_transfer_not_revenue():
    """Money arriving from our own Stripe balance did not appear; it moved."""
    transfers = run().by_kind(EventKind.TRANSFER)
    assert len(transfers) == 1
    assert transfers[0].amount == Decimal("82400")


def test_the_processor_fee_is_recovered_as_real_spend():
    """$85,000 swept as $82,400 means $2,600 of fees that must reach burn."""
    fees = run().by_kind(EventKind.FEE)
    assert len(fees) == 1
    assert fees[0].amount == Decimal("-2600")
    assert burn(run().events, *WINDOW).value == Decimal("2600")


# --- how it knew ------------------------------------------------------------

def test_the_link_is_made_on_a_shared_identifier_not_on_amount():
    """Certainty, not coincidence: the payout id appears in both systems.

    This is the reason Mercury was the right second integration -- it is the
    first time explicit_reference fires on identifiers neither of us wrote by
    hand into a fixture.
    """
    transfer = run().by_kind(EventKind.TRANSFER)[0]
    rules = {e.rule.value for e in transfer.evidence}
    assert "explicit_reference" in rules
    notes = " ".join(e.note for e in transfer.evidence)
    assert PAYOUT_ID in notes


def test_both_sources_are_credited_on_the_transfer():
    assert set(run().by_kind(EventKind.TRANSFER)[0].sources) == {"stripe", "mercury"}


def test_nothing_was_guessed():
    """No exception means every merge was made on evidence, not similarity."""
    assert run().exceptions == []


def test_the_run_is_reproducible():
    assert run().input_fingerprint == run().input_fingerprint


# --- and if the identifier is missing ---------------------------------------

def test_without_the_shared_identifier_it_still_matches_but_less_confidently():
    """A bank that strips the reference should degrade, not break.

    The amounts and dates still pair the two legs; the engine just says so with
    lower confidence, which is the honest outcome.
    """
    rows = [r for r in build_rows() if r.source != "mercury"]
    stripped = map_mercury(
        {"id": "txn_wire", "amount": 82400.00, "status": "sent",
         "kind": "incomingDomesticWire", "createdAt": "2026-08-22T09:00:00Z",
         "postedAt": "2026-08-22T09:14:00Z", "counterpartyName": "STRIPE",
         "bankDescription": "ACH CREDIT DEPOSIT", "currency": "USD"},
        account_id=MERCURY_ACCOUNT.id)
    result = reconcile([*rows, stripped], [STRIPE_ACCOUNT, MERCURY_ACCOUNT])
    transfers = result.by_kind(EventKind.TRANSFER)
    assert len(transfers) == 1
    assert transfers[0].confidence < 0.99
    assert revenue(result.events, *WINDOW).value == Decimal("85000")
