"""Project 1: append-only prediction log.

Every thesis gets a numeric probability estimate and a stated resolution
source at creation time — unfalsifiable claims are the main failure mode of a
prediction log, and requiring the field up front is the cheap fix for that.
Storage is JSONL, one record per line, append-only; resolving a prediction
appends a new line with the same ``id`` and the latest one wins (same
last-write-wins pattern as sec/normalize.dedup, applied to a local log instead
of SEC filings).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from smallcap.config import PREDICTIONS_PATH, ensure_dirs

VALID_STATUS = ("open", "resolved")
VALID_OUTCOME = ("yes", "no", "void")


@dataclass
class Prediction:
    id: str
    created: str
    ticker: str
    claim: str
    prob: float  # probability the claim resolves "yes", in [0, 1]
    resolve_by: str
    resolution_source: str  # what data will settle this — required, not optional
    status: str = "open"
    outcome: str | None = None
    resolved_at: str | None = None
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    thesis_id: str | None = None  # optional link to journal/store.py's Thesis
    days_early: int | None = None  # set on resolve if resolved before resolve_by


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def add(
    ticker: str,
    claim: str,
    prob: float,
    resolve_by: str,
    resolution_source: str,
    *,
    tags: list[str] | None = None,
    notes: str = "",
    thesis_id: str | None = None,
) -> Prediction:
    if not (0.0 <= prob <= 1.0):
        raise ValueError(f"prob must be in [0, 1], got {prob}")
    if not resolution_source.strip():
        raise ValueError("resolution_source is required: state what data will settle this claim")
    date.fromisoformat(resolve_by)  # validates format, raises ValueError if malformed

    pred = Prediction(
        id=uuid.uuid4().hex[:8],
        created=_now_iso(),
        ticker=ticker.upper(),
        claim=claim,
        prob=prob,
        resolve_by=resolve_by,
        resolution_source=resolution_source,
        tags=tags or [],
        notes=notes,
        thesis_id=thesis_id,
    )
    _append(pred)
    return pred


def resolve(prediction_id: str, outcome: str, *, notes: str = "") -> Prediction:
    if outcome not in VALID_OUTCOME:
        raise ValueError(f"outcome must be one of {VALID_OUTCOME}, got {outcome!r}")
    current = _latest_by_id().get(prediction_id)
    if current is None:
        raise KeyError(f"no prediction with id {prediction_id!r}")
    if current.status == "resolved":
        raise ValueError(f"prediction {prediction_id!r} is already resolved (outcome={current.outcome!r})")

    today = date.today().isoformat()
    if today < current.resolve_by:
        current.days_early = (date.fromisoformat(current.resolve_by) - date.today()).days

    current.status = "resolved"
    current.outcome = outcome
    current.resolved_at = _now_iso()
    if notes:
        current.notes = (current.notes + " | " + notes).strip(" |")
    _append(current)
    return current


def _append(pred: Prediction) -> None:
    ensure_dirs()
    with PREDICTIONS_PATH.open("a") as f:
        f.write(json.dumps(asdict(pred)) + "\n")


def _latest_by_id() -> dict[str, Prediction]:
    """Last-write-wins: a resolved record appended after the original 'open'
    record for the same id overrides it, since we read the file in order."""
    if not PREDICTIONS_PATH.exists():
        return {}
    latest: dict[str, Prediction] = {}
    with PREDICTIONS_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            latest[row["id"]] = Prediction(**row)
    return latest


def all_predictions() -> list[Prediction]:
    return sorted(_latest_by_id().values(), key=lambda p: p.created)


def open_predictions() -> list[Prediction]:
    return [p for p in all_predictions() if p.status == "open"]


def resolved_predictions() -> list[Prediction]:
    return [p for p in all_predictions() if p.status == "resolved"]


def due(as_of: str | None = None) -> list[Prediction]:
    """Open predictions whose resolve_by date has passed."""
    cutoff = as_of or date.today().isoformat()
    return [p for p in open_predictions() if p.resolve_by < cutoff]
