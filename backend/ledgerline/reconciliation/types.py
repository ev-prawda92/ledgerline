"""Core types for the Ledgerline reconciliation layer.

The engine is deliberately pure: it takes plain records in and returns plain
records out.  Nothing here touches the database, so the matching logic can be
tested exhaustively without Postgres, and re-run over history at any time.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum


class SourceRole(str, Enum):
    """What a connected source *is*, which decides how much we trust it.

    The single most important distinction is PRIMARY vs LEDGER.  A primary
    source observed the money move.  A ledger (QuickBooks, NetSuite) is a
    bookkeeping restatement of something a primary source already reported --
    it corroborates, it never adds.
    """

    BANK = "bank"              # Mercury -- authoritative for cash balances
    PROCESSOR = "processor"    # Stripe -- authoritative for revenue
    CARD = "card"              # Ramp -- authoritative for card spend
    PAYROLL = "payroll"        # Gusto -- authoritative for payroll
    LEDGER = "ledger"          # QuickBooks / NetSuite -- derived, never counted

    @property
    def is_primary(self) -> bool:
        return self is not SourceRole.LEDGER


class EventKind(str, Enum):
    REVENUE = "revenue"
    SPEND = "spend"
    PAYROLL = "payroll"
    FEE = "fee"
    TRANSFER = "transfer"      # internal movement -- never revenue, never burn
    UNKNOWN = "unknown"


class MatchRule(str, Enum):
    """Ordered by descending confidence.  Recorded on every link for attestation."""

    EXPLICIT_REFERENCE = "explicit_reference"   # one source names the other's id
    TRANSFER_PAIR = "transfer_pair"             # equal+opposite across own accounts
    PAYOUT_NET_OF_FEES = "payout_net_of_fees"   # processor gross -> bank net + fee
    AGGREGATE = "aggregate"                     # N components sum to one record
    SPECIALIST_OVER_BANK = "specialist_over_bank"  # payroll/card detail vs bank line
    DUPLICATE_RECORD = "duplicate_record"       # same source reported it twice
    LEDGER_ECHO = "ledger_echo"                 # bookkeeping restatement
    UNMATCHED = "unmatched"                     # stands alone (still a real event)


@dataclass(frozen=True)
class Account:
    id: str
    source: str
    role: SourceRole
    name: str
    currency: str = "USD"
    # True for accounts the org owns.  Movement between two owned accounts is a
    # transfer, not revenue or spend -- this flag is what makes that decidable.
    is_internal: bool = True


@dataclass(frozen=True)
class RawTxn:
    """One record as a single source reported it.  Immutable, never edited.

    `amount` is signed from the account's point of view: positive is money
    arriving in that account, negative is money leaving it.
    """

    id: str
    source: str
    source_id: str            # id in the originating system
    account_id: str
    amount: Decimal
    currency: str
    occurred_at: datetime
    description: str = ""
    counterparty: str | None = None
    category_hint: str | None = None
    # Cross-system identifiers this record carries, e.g.
    # {"self": "po_1abc", "stripe_payout_id": "po_1abc"}.  Any value that
    # matches another record's identifier links the two with high confidence.
    external_refs: Mapping[str, str] = field(default_factory=dict)
    # Set when a source reports a batch that expands into components
    # (a Stripe payout covering 214 charges).
    batch_id: str | None = None

    @property
    def direction(self) -> int:
        return 1 if self.amount > 0 else -1

    @property
    def abs_amount(self) -> Decimal:
        return abs(self.amount)


@dataclass(frozen=True)
class Evidence:
    """Why the engine believes two or more records are the same event.

    This is what the AI analyst cites and what the attestation trail stores.
    Every number on the dashboard can be walked back to these.
    """

    rule: MatchRule
    confidence: float
    txn_ids: tuple[str, ...]
    note: str = ""


@dataclass
class EconomicEvent:
    """One thing that actually happened to the company's money.

    Metrics are computed from these, never from RawTxn, which is the whole
    point of the layer.
    """

    key: str
    kind: EventKind
    amount: Decimal               # signed, from the company's point of view
    currency: str
    occurred_at: datetime
    counterparty: str | None
    primary_txn_id: str           # the record we treat as authoritative
    member_txn_ids: tuple[str, ...]
    evidence: tuple[Evidence, ...]
    confidence: float
    # Records that describe this same event but contribute no value --
    # the other leg of a transfer, the QuickBooks echo.  Kept for drill-down.
    suppressed_txn_ids: tuple[str, ...] = ()

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(sorted(set(self._sources)))

    _sources: tuple[str, ...] = ()


@dataclass
class ReconciliationException:
    """Something the engine will not guess about.  Goes to a human.

    These are the Cortex approval-queue items, and the honest answer to
    'why is this number different from my bank'.
    """

    reason: str
    txn_ids: tuple[str, ...]
    detail: str = ""
    candidates: tuple[str, ...] = ()
    severity: str = "review"      # review | blocking


@dataclass
class ReconciliationResult:
    events: list[EconomicEvent]
    exceptions: list[ReconciliationException]
    # input hash -> lets a run be proved deterministic for attestation
    input_fingerprint: str = ""

    def by_kind(self, kind: EventKind) -> list[EconomicEvent]:
        return [e for e in self.events if e.kind is kind]
