"""Deterministic counterparty resolution.

Why this is not a matching problem solved with a model at read time:

*   A model asked the same question twice can answer differently.  If vendor
    identity moves, spend by vendor moves, and a customer watches a number
    change with no transaction behind it.  That is unrecoverable trust damage
    in a finance product.
*   Reconciliation runs must be replayable.  A run is only reproducible if every
    input to it is fixed, and vendor identity is an input.

So resolution is a dictionary lookup over confirmed aliases, and nothing else.
Uncertainty is not resolved here -- it is *recorded* here, and routed to a human
through the proposal queue.  The alias table that accumulates is the asset:
every confirmation is permanent, deterministic and compounding.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from ..reconciliation.normalize import normalize_counterparty


@dataclass(frozen=True)
class CanonicalCounterparty:
    """One real-world vendor or customer."""

    id: str
    display_name: str
    #: Free-form and org-specific: "infrastructure", "software", "travel".
    #: Used to scope duplicate-spend candidates, never to decide identity.
    category: str | None = None


@dataclass(frozen=True)
class CounterpartyAlias:
    """A confirmed statement that a normalised string means a canonical vendor.

    `confirmed_by` is required and has no default on purpose: an alias with no
    human behind it should be impossible to construct by accident.
    """

    alias_key: str
    canonical_id: str
    confirmed_by: str
    #: Where the string came from, kept for drill-down and for revoking a bad
    #: confirmation later without hunting through history.
    first_seen_source: str | None = None


@dataclass
class Resolution:
    """The outcome of asking who a raw string refers to."""

    key: str
    canonical: CanonicalCounterparty | None
    #: True when the key has no confirmed alias.  The string still resolves --
    #: to itself -- so totals stay correct; it simply isn't *merged* with
    #: anything.  Unresolved is a safe state, not an error.
    provisional: bool = True

    @property
    def display(self) -> str:
        return self.canonical.display_name if self.canonical else self.key


@dataclass
class CounterpartyResolver:
    """Read-time resolution over a fixed alias table.

    Construct it once per reconciliation run from the confirmed aliases in the
    database, and it will answer identically for the life of that run.
    """

    canonicals: dict[str, CanonicalCounterparty] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    #: Normalised strings seen with no confirmed alias. Feeds the proposal queue.
    unresolved: set[str] = field(default_factory=set)

    @classmethod
    def from_records(
        cls,
        canonicals: Iterable[CanonicalCounterparty],
        aliases: Iterable[CounterpartyAlias],
    ) -> CounterpartyResolver:
        by_id = {c.id: c for c in canonicals}
        table: dict[str, str] = {}
        for alias in aliases:
            if alias.canonical_id not in by_id:
                # An alias pointing at a vendor that no longer exists is skipped
                # rather than raising: a stale row must not stop a sync.
                continue
            # Normalise on ingest rather than trusting the caller. An alias
            # stored as "DD Inc" would never match a lookup of "DD INC", and it
            # would fail silently -- the vendor would just quietly stop merging.
            table[normalize_counterparty(alias.alias_key)] = alias.canonical_id
        return cls(canonicals=by_id, aliases=table)

    def resolve(self, raw: str | None) -> Resolution:
        key = normalize_counterparty(raw)
        if not key:
            return Resolution(key="", canonical=None, provisional=True)
        canonical_id = self.aliases.get(key)
        if canonical_id is None:
            self.unresolved.add(key)
            return Resolution(key=key, canonical=None, provisional=True)
        return Resolution(key=key, canonical=self.canonicals[canonical_id], provisional=False)

    def same_vendor(self, a: str | None, b: str | None) -> bool:
        """Do two strings refer to one confirmed vendor?

        Two provisional strings are never the same vendor, however similar they
        look.  Guessing here is what would make spend totals drift.
        """
        ra, rb = self.resolve(a), self.resolve(b)
        if ra.provisional or rb.provisional:
            return ra.key == rb.key and bool(ra.key)
        return ra.canonical is not None and ra.canonical is rb.canonical


def duplicate_spend_candidates(
    resolver: CounterpartyResolver,
    charges: Sequence[tuple[str | None, str]],
    *,
    min_sources: int = 2,
) -> list[tuple[str, list[str]]]:
    """Vendors billing the org through more than one channel.

    `charges` is (counterparty, source) pairs.  A vendor appearing on both the
    Ramp card and the QuickBooks ledger is ordinary; a vendor appearing on two
    *different cards or subscriptions* is the beginning of the answer to "are we
    paying for duplicate software".

    This returns **candidates for a human to look at**, never a conclusion --
    which is the honest shape for it, since a second charge from the same vendor
    is frequently legitimate.
    """
    by_vendor: dict[str, set[str]] = {}
    for counterparty, source in charges:
        resolution = resolver.resolve(counterparty)
        if not resolution.key:
            continue
        by_vendor.setdefault(resolution.display, set()).add(source)
    return sorted(
        ((vendor, sorted(sources)) for vendor, sources in by_vendor.items()
         if len(sources) >= min_sources),
        key=lambda pair: (-len(pair[1]), pair[0]),
    )
