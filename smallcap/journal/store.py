"""Append-only storage for the decision journal.

Unlike predictions/store.py, nothing here is ever overwritten — there's no
"resolve" step that appends a corrected line for the same id. Theses,
follow-ups, trades, and thesis events are pure logs: read every line, done.
That's also why there is no ``update_thesis`` or ``delete_thesis`` function
anywhere in this module — the immutability constraint isn't a convention,
it's the absence of a code path.

To change your mind about a thesis: ``revise()`` writes a brand-new record
with ``parent_id`` set to the old one. The old line in theses.jsonl is never
touched. Status (open/closed/superseded) is computed on read from
thesis_events.jsonl and other theses' parent_id fields, in ``thesis_status``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from smallcap.config import (
    FOLLOWUPS_PATH,
    NOTES_PATH,
    THESES_PATH,
    THESIS_EVENTS_PATH,
    TRADES_PATH,
    ensure_dirs,
)
from smallcap.journal.models import SOURCES, FollowUp, Note, Thesis, ThesisEvent, Trade

VALID_EVENT_KINDS = ("close", "reopen")
VALID_FOLLOWUP_KINDS = ("note", "review", "alert")
VALID_SIDES = ("buy", "sell")

T = TypeVar("T")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _append(path: Path, record) -> None:
    ensure_dirs()
    with path.open("a") as f:
        f.write(json.dumps(asdict(record)) + "\n")


def _read_all(path: Path, cls: type[T]) -> list[T]:
    if not path.exists():
        return []
    out = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(cls(**json.loads(line)))
    return out


# ---------------------------------------------------------------- theses ----


def new_thesis(
    ticker: str,
    claim: str,
    variant_view: str,
    falsifier: str,
    source: str,
    *,
    add_level: float | None = None,
    add_level_reason: str = "",
    exit_trigger: str = "",
    parent_id: str | None = None,
) -> Thesis:
    if not variant_view.strip():
        raise ValueError("variant_view is required: state why the market disagrees with this claim")
    if not falsifier.strip():
        raise ValueError("falsifier is required: state what observable would prove this claim wrong")
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}, got {source!r}")
    if parent_id is not None and thesis_get(parent_id) is None:
        raise KeyError(f"no thesis with id {parent_id!r} to revise")

    thesis = Thesis(
        id=_new_id(),
        created_at=_now_iso(),
        ticker=ticker.upper(),
        claim=claim,
        variant_view=variant_view,
        falsifier=falsifier,
        add_level=add_level,
        add_level_reason=add_level_reason,
        exit_trigger=exit_trigger,
        source=source,
        parent_id=parent_id,
    )
    _append(THESES_PATH, thesis)
    return thesis


def revise(parent_id: str, **fields) -> Thesis:
    """Write a new thesis record superseding ``parent_id``. The parent's line
    is never touched — ``thesis_status(parent_id)`` will read "superseded"
    once this returns, computed from this new record's parent_id, not from
    any change to the parent."""
    parent = thesis_get(parent_id)
    if parent is None:
        raise KeyError(f"no thesis with id {parent_id!r}")
    return new_thesis(
        ticker=fields.get("ticker", parent.ticker),
        claim=fields.get("claim", parent.claim),
        variant_view=fields.get("variant_view", parent.variant_view),
        falsifier=fields.get("falsifier", parent.falsifier),
        source=fields.get("source", parent.source),
        add_level=fields.get("add_level", parent.add_level),
        add_level_reason=fields.get("add_level_reason", parent.add_level_reason),
        exit_trigger=fields.get("exit_trigger", parent.exit_trigger),
        parent_id=parent_id,
    )


def close(thesis_id: str, *, reason: str = "") -> ThesisEvent:
    return _add_event(thesis_id, "close", reason)


def reopen(thesis_id: str, *, reason: str = "") -> ThesisEvent:
    return _add_event(thesis_id, "reopen", reason)


def _add_event(thesis_id: str, kind: str, reason: str) -> ThesisEvent:
    if kind not in VALID_EVENT_KINDS:
        raise ValueError(f"kind must be one of {VALID_EVENT_KINDS}, got {kind!r}")
    if thesis_get(thesis_id) is None:
        raise KeyError(f"no thesis with id {thesis_id!r}")
    event = ThesisEvent(id=_new_id(), thesis_id=thesis_id, created_at=_now_iso(), kind=kind, reason=reason)
    _append(THESIS_EVENTS_PATH, event)
    return event


def all_theses() -> list[Thesis]:
    return sorted(_read_all(THESES_PATH, Thesis), key=lambda t: t.created_at)


def thesis_get(thesis_id: str) -> Thesis | None:
    for t in all_theses():
        if t.id == thesis_id:
            return t
    return None


def theses_for_ticker(ticker: str) -> list[Thesis]:
    return [t for t in all_theses() if t.ticker == ticker.upper()]


def all_events() -> list[ThesisEvent]:
    return sorted(_read_all(THESIS_EVENTS_PATH, ThesisEvent), key=lambda e: e.created_at)


def events_for(thesis_id: str) -> list[ThesisEvent]:
    return [e for e in all_events() if e.thesis_id == thesis_id]


def thesis_status(thesis_id: str) -> str:
    """Derived, never stored: 'superseded' if some other thesis's parent_id
    points here, else 'closed' if the latest event is a close (not reopened
    after), else 'active'."""
    if any(t.parent_id == thesis_id for t in all_theses()):
        return "superseded"
    events = events_for(thesis_id)
    if events and events[-1].kind == "close":
        return "closed"
    return "active"


# -------------------------------------------------------------- followups ---


def followup_add(thesis_id: str, body: str, *, kind: str = "note") -> FollowUp:
    if kind not in VALID_FOLLOWUP_KINDS:
        raise ValueError(f"kind must be one of {VALID_FOLLOWUP_KINDS}, got {kind!r}")
    if thesis_get(thesis_id) is None:
        raise KeyError(f"no thesis with id {thesis_id!r}")
    followup = FollowUp(id=_new_id(), thesis_id=thesis_id, created_at=_now_iso(), kind=kind, body=body)
    _append(FOLLOWUPS_PATH, followup)
    return followup


def all_followups() -> list[FollowUp]:
    return sorted(_read_all(FOLLOWUPS_PATH, FollowUp), key=lambda f: f.created_at)


def followups_for(thesis_id: str) -> list[FollowUp]:
    return [f for f in all_followups() if f.thesis_id == thesis_id]


def latest_alert_of_kind(thesis_id: str, alert_kind: str) -> FollowUp | None:
    """Most recent alert follow-up whose body starts with ``alert_kind`` —
    used by journal/alerts.py to dedup repeated warnings across reviews."""
    matches = [f for f in followups_for(thesis_id) if f.kind == "alert" and f.body.startswith(alert_kind)]
    return matches[-1] if matches else None


# -------------------------------------------------------------------- notes -


def note_add(body: str) -> Note:
    note = Note(id=_new_id(), created_at=_now_iso(), body=body)
    _append(NOTES_PATH, note)
    return note


def all_notes() -> list[Note]:
    return sorted(_read_all(NOTES_PATH, Note), key=lambda n: n.created_at)


# ------------------------------------------------------------------ trades --


def trade_log(
    ticker: str,
    side: str,
    shares: float,
    price: float,
    executed_at: str,
    *,
    thesis_id: str | None = None,
    is_index: bool = False,
    fees: float = 0.0,
    rule_followed: bool | None = None,
    note: str = "",
) -> Trade:
    if side not in VALID_SIDES:
        raise ValueError(f"side must be one of {VALID_SIDES}, got {side!r}")
    if shares <= 0:
        raise ValueError(f"shares must be positive, got {shares}")
    if bool(thesis_id) == bool(is_index):
        raise ValueError("exactly one of thesis_id or is_index must be given")
    if thesis_id is not None and thesis_get(thesis_id) is None:
        raise KeyError(f"no thesis with id {thesis_id!r}")

    trade = Trade(
        id=_new_id(),
        thesis_id=thesis_id,
        is_index=is_index,
        ticker=ticker.upper(),
        side=side,
        shares=shares,
        price=price,
        fees=fees,
        executed_at=executed_at,
        logged_at=_now_iso(),
        rule_followed=rule_followed,
        note=note,
    )
    _append(TRADES_PATH, trade)
    return trade


def all_trades() -> list[Trade]:
    return sorted(_read_all(TRADES_PATH, Trade), key=lambda t: t.executed_at)


def trades_for(thesis_id: str) -> list[Trade]:
    return [t for t in all_trades() if t.thesis_id == thesis_id]
