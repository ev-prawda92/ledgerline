"""Mercury.

Mercury is a BANK: authoritative for cash balances, and the place a Stripe
payout finally becomes money. Four things here are load-bearing.

**Pending is a revision, not a separate transaction.**  Mercury reports a
transaction as `pending` and later as `sent`, and the amount can change in
between -- a card authorisation settles for a different figure, a wire lands net
of a correspondent fee.  Treating those as two rows double-counts; treating the
second as an UPDATE destroys replayability.  So status maps onto the revision
model: pending is revision 0, settled is revision 1, and `cancelled`/`failed`
set `voided`.  A settled amount that differs from the pending one raises
`restated_amount`, which is the honest thing to show a user whose burn moved.

**Floats must not be trusted through binary.**  Mercury returns amounts as JSON
numbers in dollars.  `Decimal(18400.1)` carries binary float error into money;
`Decimal(str(18400.1))` does not.  The conversion goes through the string form,
once, here.

**Stripe's payout id is sitting in the bank description.**  Mercury's
`bankDescription` for a processor deposit typically contains the payout
identifier.  Lifting it into `external_refs` is what lets `explicit_reference`
link the deposit to the Stripe payout with certainty, instead of the engine
matching on amount and date and hoping.  This is the single highest-value line
in the file.

**Treasury balances are cash.**  A company with money swept into a money-market
account has runway that is invisible if only the operating account is read.
Every account Mercury lists contributes its balance.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from ..reconciliation.metrics import Balance
from ..reconciliation.types import Account, RawTxn, SourceRole
from .base import Integration, SyncCursor, SyncResult

API = "https://api.mercury.com/api/v1"
SOURCE = "mercury"

#: Mercury statuses that mean the money did not move.
VOID_STATUSES = frozenset({"cancelled", "failed"})
#: Settled statuses. Anything not settled and not void is still pending.
SETTLED_STATUSES = frozenset({"sent", "posted", "completed"})

#: Movements between accounts the org owns. Never revenue, never burn -- the
#: engine's transfer_pair pass claims them, and naming them here means it does
#: not have to infer from description text.
INTERNAL_KINDS = frozenset({"internalTransfer", "treasuryTransfer"})

FEE_KINDS = frozenset({"wireFee", "fee"})

#: Identifiers other systems will also carry, lifted out of free text so the
#: engine can link on certainty rather than coincidence.
REF_PATTERNS = {
    "stripe_payout_id": re.compile(r"\b(po_[A-Za-z0-9]{8,})\b"),
    "stripe_charge_id": re.compile(r"\b(ch_[A-Za-z0-9]{8,})\b"),
}


def to_decimal(amount: Any) -> Decimal:
    """Mercury's JSON number as an exact Decimal.

    Via `str` deliberately: Decimal(0.1) is 0.1000000000000000055511151231,
    Decimal("0.1") is 0.1. Money must never inherit binary float error.
    """
    return Decimal(str(amount))


def extract_refs(*texts: str | None) -> dict[str, str]:
    """Pull known cross-system identifiers out of free-text bank fields."""
    found: dict[str, str] = {}
    for text in texts:
        if not text:
            continue
        for name, pattern in REF_PATTERNS.items():
            match = pattern.search(text)
            if match and name not in found:
                found[name] = match.group(1)
    return found


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def map_transaction(txn: dict[str, Any], *, account_id: str) -> RawTxn:
    """One Mercury transaction as a raw row.

    Pure, so the mapping is testable against recorded payloads with no network
    and no API key.
    """
    status = (txn.get("status") or "").lower()
    kind = txn.get("kind") or "other"
    voided = status in VOID_STATUSES
    # Settled rows supersede the pending row they came from; both share the
    # transaction id, so the engine keeps only the later one.
    revision = 1 if status in SETTLED_STATUSES or voided else 0

    description = txn.get("bankDescription") or txn.get("externalMemo") or ""
    counterparty = txn.get("counterpartyNickname") or txn.get("counterpartyName")

    refs: dict[str, str] = {"self": txn["id"]}
    refs.update(extract_refs(description, txn.get("externalMemo"), txn.get("note")))

    if kind in INTERNAL_KINDS:
        hint: str | None = "internal_transfer"
    elif kind in FEE_KINDS:
        hint = "fee"
    else:
        hint = txn.get("mercuryCategory")

    occurred = _parse_time(txn.get("postedAt")) or _parse_time(txn.get("createdAt")) \
        or datetime.now(UTC)

    return RawTxn(
        id=f"mercury:{txn['id']}:{revision}",
        source=SOURCE,
        source_id=txn["id"],
        account_id=account_id,
        amount=to_decimal(txn["amount"]),
        currency=(txn.get("currency") or "USD").upper(),
        occurred_at=occurred,
        description=description,
        counterparty=counterparty,
        category_hint=hint,
        external_refs=refs,
        revision=revision,
        voided=voided,
    )


def map_account(account: dict[str, Any], org_id: str) -> Account:
    return Account(
        id=f"{org_id}:mercury:{account['id']}",
        source=SOURCE,
        role=SourceRole.BANK,
        name=account.get("nickname") or account.get("name") or "Mercury account",
        currency=(account.get("currency") or "USD").upper(),
        is_internal=True,
    )


class MercuryIntegration(Integration):
    source = SOURCE
    role = SourceRole.BANK

    def __init__(self, api_key: str, org_id: str, *, page_size: int = 500,
                 client: httpx.Client | None = None) -> None:
        self.api_key = api_key
        self.org_id = org_id
        self.page_size = page_size
        self._client = client or httpx.Client(
            base_url=API,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        delay = 1.0
        for attempt in range(6):
            response = self._client.get(path, params=params or {})
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 5:
                    response.raise_for_status()
                time.sleep(delay)
                delay *= 2
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("unreachable")

    def accounts(self) -> list[dict[str, Any]]:
        return self._get("/accounts").get("accounts", [])

    def fetch(self, since: SyncCursor) -> Iterator[SyncResult]:
        raw_accounts = self.accounts()
        accounts = [map_account(a, self.org_id) for a in raw_accounts]

        # Balances first: cash on hand should be right even if a transaction
        # page fails partway through.
        as_of = datetime.now(UTC)
        balances = [
            Balance(
                account_id=f"{self.org_id}:mercury:{a['id']}",
                as_of=as_of,
                amount=to_decimal(a.get("currentBalance", 0)),
                currency=(a.get("currency") or "USD").upper(),
            )
            for a in raw_accounts
        ]
        yield SyncResult(accounts=accounts, balances=balances,
                         cursor=SyncCursor(synced_through=since.synced_through,
                                           complete=False))

        latest = since.synced_through
        for raw in raw_accounts:
            account_id = f"{self.org_id}:mercury:{raw['id']}"
            offset = 0
            while True:
                params: dict[str, Any] = {"limit": self.page_size, "offset": offset}
                if since.synced_through:
                    # Overlap deliberately: a pending row that settles keeps its
                    # original timestamp, so a strict cursor would never see the
                    # settled revision.
                    params["start"] = since.synced_through.date().isoformat()
                body = self._get(f"/account/{raw['id']}/transactions", params)
                page = body.get("transactions", [])
                if not page:
                    break

                rows, skipped = [], []
                for txn in page:
                    try:
                        rows.append(map_transaction(txn, account_id=account_id))
                    except (KeyError, TypeError, ValueError) as exc:
                        skipped.append(f"{txn.get('id', '?')}: {exc}")
                if rows:
                    newest = max(r.occurred_at for r in rows)
                    latest = max(latest, newest) if latest else newest

                yield SyncResult(
                    rows=rows, accounts=accounts, skipped=skipped,
                    cursor=SyncCursor(position=f"{raw['id']}:{offset}",
                                      synced_through=latest, complete=False),
                )
                if len(page) < self.page_size:
                    break
                offset += self.page_size

        yield SyncResult(accounts=accounts,
                         cursor=SyncCursor(synced_through=latest, complete=True))
