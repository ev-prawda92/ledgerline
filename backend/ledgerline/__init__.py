"""Ledgerline backend.

Layering rule, enforced by convention and by review:

    integrations -> raw_transactions -> reconciliation -> economic_events -> everything else

Nothing outside `ledgerline.reconciliation` computes a financial figure from a
raw row.  Metrics, the timeline and the AI analyst all read reconciled events.
This is the property the whole product rests on; breaking it silently
reintroduces the double-counting the engine exists to remove.
"""

__version__ = "0.1.0"
