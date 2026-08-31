"""Mercury mapping, against recorded payload shapes."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from ledgerline.integrations.mercury import (
    extract_refs,
    map_account,
    map_transaction,
    to_decimal,
)
from ledgerline.reconciliation.types import SourceRole

ACCT = "org_1:mercury:acc_1"


def txn(**over) -> dict:
    base = {
        "id": "txn_merc_1", "amount": -18400.00, "status": "sent",
        "kind": "debitCardTransaction", "createdAt": "2026-08-18T10:00:00Z",
        "postedAt": "2026-08-18T14:00:00Z", "counterpartyName": "AMAZON WEB SERVICES",
        "bankDescription": "AWS EMEA 8005551234", "currency": "USD",
    }
    base.update(over)
    return base


# --- money ------------------------------------------------------------------

def test_float_amounts_convert_without_binary_error():
    """Decimal(0.1) carries float error into money; Decimal(str(0.1)) does not."""
    assert to_decimal(0.1) == Decimal("0.1")
    assert to_decimal(-18400.00) == Decimal("-18400.00")
    assert to_decimal(82400.55) == Decimal("82400.55")


def test_amount_survives_the_mapping_exactly():
    assert map_transaction(txn(amount=-0.3), account_id=ACCT).amount == Decimal("-0.3")


# --- the reference lift, which is the whole point of doing Mercury ----------

def test_a_stripe_payout_id_is_lifted_out_of_the_bank_description():
    """This is what lets explicit_reference link the deposit to Stripe.

    Without it the engine falls back to matching on amount and date, which is
    a guess dressed up as a match.
    """
    row = map_transaction(
        txn(amount=82400.00, kind="incomingDomesticWire", counterpartyName="STRIPE",
            bankDescription="STRIPE TRANSFER po_1abcdefgh ACH CREDIT"),
        account_id=ACCT)
    assert row.external_refs["stripe_payout_id"] == "po_1abcdefgh"


def test_references_are_found_across_any_free_text_field():
    row = map_transaction(txn(bankDescription="ACH CREDIT", note="ref po_1zzzzzzzz"),
                          account_id=ACCT)
    assert row.external_refs["stripe_payout_id"] == "po_1zzzzzzzz"


def test_no_reference_is_invented_when_none_is_present():
    row = map_transaction(txn(), account_id=ACCT)
    assert set(row.external_refs) == {"self"}


def test_short_lookalikes_are_not_matched():
    """`po_` followed by noise must not become a confident link."""
    assert extract_refs("payment po_12 something") == {}


# --- pending is a revision, not a second transaction ------------------------

def test_pending_is_revision_zero_and_settled_is_revision_one():
    assert map_transaction(txn(status="pending"), account_id=ACCT).revision == 0
    assert map_transaction(txn(status="sent"), account_id=ACCT).revision == 1


def test_pending_and_settled_share_a_source_id_so_they_collapse():
    """Same source_id is what makes the settled row supersede the pending one."""
    pending = map_transaction(txn(status="pending"), account_id=ACCT)
    settled = map_transaction(txn(status="sent"), account_id=ACCT)
    assert pending.source_id == settled.source_id
    assert pending.id != settled.id


def test_cancelled_and_failed_transactions_are_voided():
    for status in ("cancelled", "failed"):
        row = map_transaction(txn(status=status), account_id=ACCT)
        assert row.voided is True, status


# --- classification ---------------------------------------------------------

def test_internal_and_treasury_transfers_are_named_not_inferred():
    for kind in ("internalTransfer", "treasuryTransfer"):
        row = map_transaction(txn(kind=kind), account_id=ACCT)
        assert row.category_hint == "internal_transfer", kind


def test_wire_fees_are_hinted_as_fees():
    assert map_transaction(txn(kind="wireFee"), account_id=ACCT).category_hint == "fee"


def test_posted_time_wins_over_created_time():
    row = map_transaction(txn(), account_id=ACCT)
    assert row.occurred_at == datetime(2026, 8, 18, 14, 0, tzinfo=UTC)


def test_a_pending_row_with_no_posted_time_falls_back_to_created():
    row = map_transaction(txn(status="pending", postedAt=None), account_id=ACCT)
    assert row.occurred_at == datetime(2026, 8, 18, 10, 0, tzinfo=UTC)


# --- accounts ---------------------------------------------------------------

def test_accounts_are_internal_so_transfers_are_not_revenue():
    account = map_account({"id": "acc_1", "nickname": "Mercury operating"}, "org_1")
    assert account.is_internal is True
    assert account.role is SourceRole.BANK
    assert account.name == "Mercury operating"
