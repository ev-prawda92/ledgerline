"""Counterparty and description normalisation.

Deduping counterparties is not a side quest -- it is the whole answer to
"are we paying for duplicate software?".  Keep this boring and deterministic;
never let a model do it, or the same vendor will merge differently on
different days and the numbers will move on their own.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

# Card networks and processors prepend their own noise to merchant names.
_NOISE_PREFIXES = (
    "sq *", "sq*", "tst*", "tst *", "pp*", "paypal *", "amzn mktp",
    "chk crd pmt", "ach debit", "ach credit", "pos debit", "recurring payment",
)

_LEGAL_SUFFIXES = (
    "inc", "inc.", "llc", "l.l.c.", "ltd", "ltd.", "limited", "corp", "corp.",
    "corporation", "co", "co.", "company", "gmbh", "plc", "sa", "nv", "bv",
    "pty", "pte", "ag", "ab", "oy", "as", "srl", "spa",
)

# Trailing invoice / reference / location junk: "DATADOG INC 8005551234 NY"
_TRAILING_JUNK = re.compile(r"\b(?:\d{4,}|[a-z]{2}\d{2,}|ref\s*#?\s*\w+|inv\s*#?\s*\w+)\b")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")


def normalize_counterparty(raw: str | None) -> str:
    """Collapse a merchant string to a stable comparison key.

    >>> normalize_counterparty("DATADOG, INC. 8005551234")
    'datadog'
    >>> normalize_counterparty("SQ *DATADOG INC")
    'datadog'
    """
    if not raw:
        return ""
    s = raw.strip().lower()
    for prefix in _NOISE_PREFIXES:
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    s = _TRAILING_JUNK.sub(" ", s)
    s = _NON_ALNUM.sub(" ", s)
    tokens = [t for t in _WS.split(s) if t]
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def counterparty_similarity(a: str | None, b: str | None) -> float:
    """0.0-1.0 token-overlap score between two normalised counterparties.

    Intentionally crude.  It is used to *raise* confidence in a match that
    amount and date already support, never to make a match on its own.
    """
    na, nb = normalize_counterparty(a), normalize_counterparty(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# Institutions we know are our own rails.  A bank line naming one of these with
# no matching counterpart is the dangerous case: it is almost certainly an
# internal transfer whose other leg we have not ingested, and counting it as
# revenue is exactly how cash gets inflated.
INTERNAL_INSTITUTION_HINTS = (
    "stripe", "mercury", "ramp", "gusto", "brex", "wise", "paypal",
    "square", "adyen", "checkout com", "transfer", "internal transfer",
)


# Words that appear in account names but say nothing about the institution.
# Without this filter an account called "Mercury operating" would make every
# merchant with "operating" in its name look like our own rails.
_GENERIC_ACCOUNT_WORDS = frozenset({
    "operating", "checking", "savings", "account", "accounts", "balance",
    "main", "primary", "business", "reserve", "payroll", "card", "credit",
    "debit", "general", "master", "sub", "fund", "cash", "usd", "eur", "gbp",
})


def internal_hints_for_accounts(accounts: Iterable) -> tuple[str, ...]:
    """Derive the internal-rail hint set from the org's *own* connected accounts.

    The static list below is a floor, not the answer.  A company banking
    somewhere we never enumerated -- Column, Increase, Arc, a regional bank --
    would otherwise have its own money silently booked as revenue, which is the
    one error that loses a CFO permanently.  Every internal account contributes
    its source name and the distinctive tokens of its account name.
    """
    hints = set(INTERNAL_INSTITUTION_HINTS)
    for a in accounts:
        if not getattr(a, "is_internal", False):
            continue
        source = normalize_counterparty(getattr(a, "source", None))
        if source:
            hints.add(source)
        for token in normalize_counterparty(getattr(a, "name", None)).split():
            if len(token) >= 4 and token not in _GENERIC_ACCOUNT_WORDS:
                hints.add(token)
    return tuple(sorted(hints))


def looks_like_internal_movement(
    text: str | None,
    hints: Sequence[str] = INTERNAL_INSTITUTION_HINTS,
) -> bool:
    """Does this string name one of our own rails?

    `hints` should come from `internal_hints_for_accounts` so the check reflects
    where this org actually banks, not where we guessed they might.
    """
    n = normalize_counterparty(text)
    return any(hint in n for hint in hints)
