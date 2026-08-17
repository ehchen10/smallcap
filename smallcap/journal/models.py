"""Journal record shapes. Dataclasses only — no I/O, no validation, no status
logic. See journal/store.py for the append-only write path that enforces the
"theses are immutable" constraint these types don't enforce on their own.

A Thesis has no ``status`` field by design: it's derived on read from
ThesisEvent + parent_id, in store.py's ``thesis_status()``. That's what makes
immutability real instead of aspirational — there is no field here a caller
could mutate and re-serialize over the original record.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SOURCES = ("own-use", "family-industry", "social", "screener", "other")


@dataclass
class Thesis:
    id: str
    created_at: str  # system clock, set at write time — never accepted as input
    ticker: str
    claim: str  # plain English: what I believe
    variant_view: str  # why the market disagrees — required, non-empty
    falsifier: str  # what specific observable would prove this wrong — required, non-empty
    add_level: float | None  # price at which I'd add more
    add_level_reason: str
    exit_trigger: str  # a business event, not a price
    source: str  # one of SOURCES
    parent_id: str | None = None  # set when this thesis supersedes an earlier one


@dataclass
class ThesisEvent:
    id: str
    thesis_id: str
    created_at: str
    kind: str  # "close" | "reopen"
    reason: str = ""


@dataclass
class FollowUp:
    id: str
    thesis_id: str
    created_at: str
    kind: str  # "note" | "review" | "alert"
    body: str


@dataclass
class Note:
    """A strategy note not tied to any ticker or thesis — market regime
    observations, process reminders, general reading. If it's about one
    ticker's thesis, it belongs in FollowUp instead."""

    id: str
    created_at: str
    body: str


@dataclass
class Trade:
    id: str
    thesis_id: str | None  # None only when is_index is True
    is_index: bool
    ticker: str
    side: str  # "buy" | "sell"
    shares: float
    price: float
    fees: float
    executed_at: str  # user-supplied fill time
    logged_at: str  # system clock, set at write time
    rule_followed: bool | None  # did this match a pre-written add/exit level?
    note: str = ""
