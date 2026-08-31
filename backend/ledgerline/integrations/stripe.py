"""Stripe.

Stripe is a PROCESSOR: authoritative for revenue, and the source of the payouts
that later appear as deposits in the bank.  Getting three details right here is
what makes the reconciliation engine actually fire on real data:

1.  **Balance transactions, not charges.**  `/v1/charges` shows what customers
    were billed.  `/v1/balance_transactions` shows what moved in Stripe's
    ledger, including fees, refunds, disputes and adjustments.  Only the second
    reconciles against a bank.

2.  **`batch_id` on every component of a payout.**  A payout is one bank deposit
    covering hundreds of charges, net of fees.  Tagging components with their
    payout id is what lets `payout_net_of_fees` recover the fee as the gap
    between the components' gross and the payout's net, instead of the engine
    guessing or the fee vanishing from burn.

3.  **`external_refs` carrying the payout id.**  Mercury's deposit description
    contains the same id, so `explicit_reference` links the two with certainty
    rather than matching on amount and date and hoping.

Money at this boundary: Stripe reports integer minor units (cents).  Ledgerline
records `Decimal` major units.  The conversion is exact -- `Decimal(cents) / 100`
never rounds -- and it happens here, once, rather than being repeated wherever
a Stripe number is read.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from ..reconciliation.types import Account, RawTxn, SourceRole
from .base import Integration, SyncCursor, SyncResult

API = "https://api.stripe.com/v1"
SOURCE = "stripe"

# Currencies Stripe reports without a minor unit -- amounts are already whole.
# Dividing these by 100 would silently shrink a Japanese company's revenue
# hundredfold, which is exactly the class of error this codebase exists to stop.
ZERO_DECIMAL = frozenset({
    "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga",
    "pyg", "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
})

# Balance-transaction types that are money leaving Stripe to our own bank.
# These become the outbound leg of a transfer, never spend.
PAYOUT_TYPES = frozenset({"payout", "transfer"})


def to_decimal(minor: int, currency: str) -> Decimal:
    """Stripe's integer amount as a Decimal in major units. Exact, never rounds."""
    if currency.lower() in ZERO_DECIMAL:
        return Decimal(minor)
    return Decimal(minor) / Decimal(100)


def _ts(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=UTC)


def map_balance_transaction(
    bt: dict[str, Any],
    *,
    account_id: str,
    payout_id: str | None = None,
) -> RawTxn:
    """One Stripe balance transaction as a raw row.

    Pure and side-effect free so it can be tested exhaustively against recorded
    fixtures without an API key or a network.
    """
    currency = (bt.get("currency") or "usd").lower()
    bt_type = bt.get("type") or "unknown"
    source_ref = bt.get("source")

    refs: dict[str, str] = {"self": bt["id"]}
    if isinstance(source_ref, str):
        refs["stripe_source_id"] = source_ref
    elif isinstance(source_ref, dict) and source_ref.get("id"):
        refs["stripe_source_id"] = source_ref["id"]
    if payout_id:
        # The identifier Mercury's deposit description will also carry.
        refs["stripe_payout_id"] = payout_id

    if bt_type in PAYOUT_TYPES:
        # A payout leaves the Stripe balance. Its id is the batch key that its
        # component charges point at.
        batch = refs.get("stripe_source_id") or bt["id"]
        # Overwrite, not setdefault: `self` was seeded with the balance
        # transaction id, but the identifier Mercury's deposit will carry is
        # the payout id. Getting this wrong breaks explicit_reference silently.
        refs["self"] = batch
        return RawTxn(
            id=f"stripe:{bt['id']}",
            source=SOURCE,
            source_id=batch,
            account_id=account_id,
            amount=to_decimal(int(bt["amount"]), currency),
            currency=currency.upper(),
            occurred_at=_ts(int(bt["created"])),
            description=bt.get("description") or "Stripe payout",
            counterparty="Stripe",
            category_hint="payout",
            external_refs=refs,
        )

    hint = "fee" if bt_type in {"stripe_fee", "application_fee"} else bt_type
    return RawTxn(
        id=f"stripe:{bt['id']}",
        source=SOURCE,
        source_id=bt["id"],
        account_id=account_id,
        amount=to_decimal(int(bt["amount"]), currency),
        currency=currency.upper(),
        occurred_at=_ts(int(bt["created"])),
        description=bt.get("description") or bt_type,
        counterparty=bt.get("description") or None,
        category_hint=hint,
        external_refs=refs,
        # Set only when this row was paid out; the engine uses it to recover the
        # processor fee as gross-minus-net.
        batch_id=payout_id,
    )


class StripeIntegration(Integration):
    source = SOURCE
    role = SourceRole.PROCESSOR

    def __init__(self, api_key: str, org_id: str, *, page_size: int = 100,
                 client: httpx.Client | None = None) -> None:
        self.api_key = api_key
        self.org_id = org_id
        self.page_size = page_size
        self._client = client or httpx.Client(
            base_url=API,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )

    # -- account ---------------------------------------------------------

    @property
    def balance_account(self) -> Account:
        """Stripe's own balance, modelled as an internal account.

        Marking it internal is what makes payout -> bank deposit a *transfer*
        rather than revenue appearing twice.
        """
        return Account(
            id=f"{self.org_id}:stripe:balance",
            source=SOURCE,
            role=SourceRole.PROCESSOR,
            name="Stripe balance",
            is_internal=True,
        )

    # -- http ------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET with backoff. Stripe rate-limits per second, so retrying works."""
        delay = 1.0
        for attempt in range(6):
            response = self._client.get(path, params=params)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 5:
                    response.raise_for_status()
                time.sleep(delay)
                delay *= 2
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("unreachable")

    def _paginate(self, path: str, params: dict[str, Any]) -> Iterator[list[dict[str, Any]]]:
        cursor: str | None = None
        while True:
            page = dict(params, limit=self.page_size)
            if cursor:
                page["starting_after"] = cursor
            body = self._get(path, page)
            rows = body.get("data", [])
            if not rows:
                return
            yield rows
            if not body.get("has_more"):
                return
            cursor = rows[-1]["id"]

    # -- sync ------------------------------------------------------------

    def _payout_batch(self, payout_id: str) -> set[str]:
        """Which balance transactions a payout covers."""
        covered: set[str] = set()
        for page in self._paginate("/balance_transactions", {"payout": payout_id}):
            covered.update(row["id"] for row in page)
        return covered

    def fetch(self, since: SyncCursor) -> Iterator[SyncResult]:
        account = self.balance_account
        params: dict[str, Any] = {}
        if since.synced_through:
            # `gte` rather than `gt`: overlapping a already-synced second is
            # free, because the unique constraint makes re-ingest a no-op, while
            # skipping one loses a row permanently.
            params["created[gte]"] = int(since.synced_through.timestamp())

        # Map each transaction to the payout that swept it, so components can
        # carry their batch id.
        batch_of: dict[str, str] = {}
        for page in self._paginate("/payouts", dict(params)):
            for payout in page:
                for txn_id in self._payout_batch(payout["id"]):
                    batch_of[txn_id] = payout["id"]

        latest = since.synced_through
        for page in self._paginate("/balance_transactions", dict(params)):
            rows, skipped = [], []
            for bt in page:
                try:
                    rows.append(map_balance_transaction(
                        bt, account_id=account.id, payout_id=batch_of.get(bt["id"])))
                except (KeyError, TypeError, ValueError) as exc:
                    skipped.append(f"{bt.get('id', '?')}: {exc}")
            if rows:
                latest = max([latest, *(r.occurred_at for r in rows)]) if latest \
                    else max(r.occurred_at for r in rows)
            yield SyncResult(
                rows=rows,
                accounts=[account],
                cursor=SyncCursor(position=page[-1]["id"], synced_through=latest, complete=False),
                skipped=skipped,
            )

        yield SyncResult(accounts=[account],
                         cursor=SyncCursor(synced_through=latest, complete=True))
