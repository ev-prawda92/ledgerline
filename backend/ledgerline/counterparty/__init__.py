"""Counterparty identity: who the money actually went to.

This package exists because "AWS", "Amazon Web Services", "AWS EMEA SARL" and
"AMZN WS US EAST" are one vendor, and nothing in a bank feed says so.  Vendor
identity is the prerequisite for the most valuable question the product can
answer -- *are we paying twice for the same thing?* -- and for spend totals that
survive a CFO checking them.

The design rule is the same one the reconciliation engine holds to: **resolution
is deterministic at read time.**  A model may propose that two names are the
same vendor; only a confirmed alias changes a number.  See
:mod:`ledgerline.proposals` for that boundary.
"""

from .resolver import CanonicalCounterparty, CounterpartyAlias, CounterpartyResolver

__all__ = ["CanonicalCounterparty", "CounterpartyAlias", "CounterpartyResolver"]
