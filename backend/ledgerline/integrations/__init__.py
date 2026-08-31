"""Source integrations.

One module per connected source.  An integration's only job is to authenticate,
paginate, and write immutable rows into `raw_transactions` with a stable
`source_id`.  An integration must never normalise away detail, deduplicate
across sources, or compute a total -- that is the reconciliation layer's work,
and doing it here is how the two layers start disagreeing.

Build order (see docs/plan.md): stripe and mercury first, because they overlap
and therefore actually exercise the engine.  Then quickbooks, the first
ledger-role source.
"""
