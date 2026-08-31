"""Stripe mapping, against recorded fixture shapes.

No API key and no network: the mapper is a pure function, so the interesting
behaviour is testable exhaustively. What matters is not that the fields copy
across, but that the *reconciliation-relevant* ones are set — a payout that
loses its batch id turns a processor fee into a silent hole in burn.
"""

from __future__ import annotations

from decimal import Decimal

from ledgerline.integrations.stripe import (
    StripeIntegration,
    map_balance_transaction,
    to_decimal,
)
from ledgerline.reconciliation.types import SourceRole

ACCOUNT = "org_1:stripe:balance"


def charge(txn_id: str, amount: int, source: str = "ch_1") -> dict:
    return {
        "id": txn_id, "type": "charge", "amount": amount, "currency": "usd",
        "created": 1755820800, "source": source, "description": "Acme Corp subscription",
    }


def payout(txn_id: str = "txn_po", amount: int = -8240000) -> dict:
    return {
        "id": txn_id, "type": "payout", "amount": amount, "currency": "usd",
        "created": 1755907200, "source": "po_1abc", "description": "STRIPE PAYOUT",
    }


# --- money ------------------------------------------------------------------

def test_cents_convert_exactly():
    assert to_decimal(8240000, "usd") == Decimal("82400")
    assert to_decimal(1, "usd") == Decimal("0.01")
    assert to_decimal(-2600, "usd") == Decimal("-26")


def test_zero_decimal_currencies_are_not_divided():
    """Dividing yen by 100 would shrink a Japanese company's revenue 100x."""
    assert to_decimal(185000, "jpy") == Decimal("185000")
    assert to_decimal(185000, "JPY") == Decimal("185000")


# --- the fields reconciliation depends on -----------------------------------

def test_charge_in_a_payout_carries_its_batch_id():
    """Without batch_id the payout_net_of_fees rule cannot recover the fee."""
    row = map_balance_transaction(charge("txn_1", 4000000), account_id=ACCOUNT,
                                  payout_id="po_1abc")
    assert row.batch_id == "po_1abc"
    assert row.external_refs["stripe_payout_id"] == "po_1abc"
    assert row.amount == Decimal("40000")
    assert row.currency == "USD"


def test_charge_outside_a_payout_has_no_batch():
    row = map_balance_transaction(charge("txn_2", 4000000), account_id=ACCOUNT)
    assert row.batch_id is None
    assert "stripe_payout_id" not in row.external_refs


def test_payout_is_keyed_by_the_payout_id_the_bank_will_reference():
    """Mercury's deposit names po_1abc; explicit_reference links on that."""
    row = map_balance_transaction(payout(), account_id=ACCOUNT)
    assert row.source_id == "po_1abc"
    assert row.external_refs["self"] == "po_1abc"
    assert row.amount == Decimal("-82400")
    assert row.category_hint == "payout"


def test_source_id_is_stable_so_resync_is_idempotent():
    """Same input twice must produce the same source_id, or the unique
    constraint on (org_id, source, source_id) stops protecting anything."""
    first = map_balance_transaction(charge("txn_3", 100), account_id=ACCOUNT)
    second = map_balance_transaction(charge("txn_3", 100), account_id=ACCOUNT)
    assert first.source_id == second.source_id == "txn_3"


def test_expanded_source_objects_are_handled():
    """Stripe returns `source` as an id or, when expanded, an object."""
    bt = charge("txn_4", 100)
    bt["source"] = {"id": "ch_expanded", "object": "charge"}
    row = map_balance_transaction(bt, account_id=ACCOUNT)
    assert row.external_refs["stripe_source_id"] == "ch_expanded"


def test_stripe_fees_are_hinted_as_fees():
    bt = {"id": "txn_5", "type": "stripe_fee", "amount": -2600, "currency": "usd",
          "created": 1755907200, "source": None, "description": "Billing"}
    assert map_balance_transaction(bt, account_id=ACCOUNT).category_hint == "fee"


# --- the account ------------------------------------------------------------

def test_stripe_balance_is_internal_so_payouts_are_transfers_not_revenue():
    account = StripeIntegration("sk_test", "org_1").balance_account
    assert account.is_internal is True
    assert account.role is SourceRole.PROCESSOR
