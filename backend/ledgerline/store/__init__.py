"""Persistence.

The layering rule from :mod:`ledgerline` is enforced here in practice:
integrations write only through :func:`save_raw_transactions`, and every
financial figure is read from what :func:`run_reconciliation` produced.  No
other module writes to `economic_events`, and nothing at all writes to
`raw_transactions` twice for the same fact.
"""

from .raw import (
    IngestResult,
    load_accounts,
    load_raw_transactions,
    save_accounts,
    save_balances,
    save_raw_transactions,
)
from .runs import run_reconciliation

__all__ = [
    "IngestResult",
    "cash_account_ids",
    "ensure_integration",
    "load_accounts",
    "load_raw_transactions",
    "run_reconciliation",
    "save_accounts",
    "save_balances",
    "save_raw_transactions",
]
