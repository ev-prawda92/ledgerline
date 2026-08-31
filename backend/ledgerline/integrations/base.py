"""The contract every source integration must satisfy.

An integration does exactly three things: authenticate, page through a source's
history, and emit immutable :class:`~ledgerline.reconciliation.types.RawTxn`
records.  It is deliberately the dumbest layer in the system.

What an integration must NOT do, and why:

*   **Never deduplicate.**  Two sources describing the same money is not an
    error to be cleaned up at ingest -- it is the signal reconciliation runs on.
    A payout that appears in both Stripe and Mercury must arrive as two rows.
*   **Never compute a total.**  The moment an integration sums anything, there
    are two places in the system that produce financial figures and they will
    disagree.
*   **Never normalise away detail.**  Keep the source's own identifiers,
    descriptions and cross-references intact; `external_refs` in particular is
    what lets the engine link records with certainty rather than inference.
*   **Never delete or edit a previously emitted row.**  Corrections arrive as
    new rows.  History is replayable only if it is immutable.

Idempotency is structural rather than careful: every row carries a stable
`source_id`, and `raw_transactions` has a unique constraint on
`(org_id, source, source_id)`.  A re-sync that overlaps a previous one is
therefore a no-op, and an interrupted sync is safe to simply run again.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime

from ..reconciliation.metrics import Balance
from ..reconciliation.types import Account, RawTxn, SourceRole


@dataclass(frozen=True)
class SyncCursor:
    """Where the last successful sync got to.

    Kept per integration.  `position` is opaque and source-defined -- a Stripe
    object id, a QuickBooks change token, an ISO timestamp -- because every API
    paginates differently and pretending otherwise leaks into the caller.

    `complete` is False while backfilling history, which is how a first sync of
    several years of data survives being interrupted: each page advances the
    cursor, and the next run resumes rather than restarting.
    """

    position: str | None = None
    synced_through: datetime | None = None
    complete: bool = True


@dataclass
class SyncResult:
    rows: list[RawTxn] = field(default_factory=list)
    accounts: list[Account] = field(default_factory=list)
    # Cash is a *stock*, taken from what the institution says the balance is --
    # never a sum of transactions, because one missed row makes the number drift
    # forever. An integration that can report a balance must, or runway is built
    # on arithmetic instead of on the bank's own answer.
    balances: list[Balance] = field(default_factory=list)
    cursor: SyncCursor = field(default_factory=SyncCursor)
    # Anything the integration could see but could not faithfully represent.
    # Surfaced rather than dropped: a silently skipped row is a number that is
    # quietly wrong later.
    skipped: list[str] = field(default_factory=list)


class Integration(ABC):
    """One connected source."""

    #: Stable key stored on every row this integration emits.
    source: str
    #: Decides how much reconciliation trusts these rows.  Getting this wrong
    #: is severe: marking a ledger as PRIMARY makes bookkeeping echoes count as
    #: real money.
    role: SourceRole

    @abstractmethod
    def fetch(self, since: SyncCursor) -> Iterator[SyncResult]:
        """Yield one batch per page, oldest first.

        Yielding per page rather than returning everything means a long backfill
        can be committed incrementally and resumed after a failure.  Callers are
        expected to persist each batch's rows and cursor together, in one
        transaction, before requesting the next.
        """
        raise NotImplementedError
