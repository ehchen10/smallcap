"""Predictions log + calibration scoring — no SEC dependency, runs fully offline."""

from __future__ import annotations

import pytest

from smallcap.predictions import score as score_mod
from smallcap.predictions import store


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PREDICTIONS_PATH", tmp_path / "predictions.jsonl")
    yield


def test_add_requires_resolution_source():
    with pytest.raises(ValueError, match="resolution_source"):
        store.add("X", "claim", 0.5, "2027-01-01", "")


def test_add_requires_valid_probability():
    with pytest.raises(ValueError):
        store.add("X", "claim", 1.5, "2027-01-01", "source")


def test_resolve_round_trip():
    p = store.add("OGEN", "claim", 0.7, "2027-01-01", "10-K filing")
    assert p.status == "open"

    resolved = store.resolve(p.id, "yes")
    assert resolved.status == "resolved"
    assert resolved.outcome == "yes"
    assert store.open_predictions() == []
    assert len(store.resolved_predictions()) == 1


def test_cannot_resolve_twice():
    p = store.add("OGEN", "claim", 0.7, "2027-01-01", "10-K filing")
    store.resolve(p.id, "yes")
    with pytest.raises(ValueError, match="already resolved"):
        store.resolve(p.id, "no")


def test_due_lists_only_overdue_open_predictions():
    store.add("A", "past due", 0.5, "2020-01-01", "source")
    store.add("B", "not due yet", 0.5, "2099-01-01", "source")
    overdue = store.due(as_of="2026-01-01")
    assert [p.ticker for p in overdue] == ["A"]


def test_score_empty_log():
    r = score_mod.score([])
    assert r.n == 0
    assert r.brier is None
    assert r.show_reliability_chart is False


def test_score_brier_and_skill():
    p1 = store.add("A", "c1", 1.0, "2027-01-01", "s")
    p2 = store.add("B", "c2", 0.0, "2027-01-01", "s")
    store.resolve(p1.id, "yes")
    store.resolve(p2.id, "no")

    r = score_mod.score(store.all_predictions())
    assert r.n == 2
    assert r.brier == 0.0  # perfect predictions
    assert r.skill_score == 1.0


def test_reliability_chart_suppressed_below_threshold():
    for i in range(5):
        p = store.add(f"T{i}", f"claim {i}", 0.5, "2027-01-01", "s")
        store.resolve(p.id, "yes" if i % 2 == 0 else "no")
    r = score_mod.score(store.all_predictions())
    assert r.n == 5
    assert r.show_reliability_chart is False


def test_void_outcome_excluded_from_brier():
    p1 = store.add("A", "c1", 0.9, "2027-01-01", "s")
    p2 = store.add("B", "c2", 0.1, "2027-01-01", "s")
    store.resolve(p1.id, "void")
    store.resolve(p2.id, "no")

    r = score_mod.score(store.all_predictions())
    assert r.n == 1  # the void resolution doesn't count toward calibration
    assert r.brier == pytest.approx(0.01)


def test_early_resolution_records_days_early():
    p = store.add("A", "c1", 0.9, "2099-01-01", "s")
    resolved = store.resolve(p.id, "yes")
    assert resolved.days_early is not None
    assert resolved.days_early > 0


def test_on_time_resolution_leaves_days_early_none():
    p = store.add("A", "c1", 0.9, "2020-01-01", "s")  # already past due
    resolved = store.resolve(p.id, "yes")
    assert resolved.days_early is None


def test_old_jsonl_row_without_thesis_id_or_days_early_still_loads(tmp_path):
    import json

    store.PREDICTIONS_PATH.write_text(json.dumps({
        "id": "abc123",
        "created": "2026-01-01T00:00:00+00:00",
        "ticker": "OGEN",
        "claim": "old-format row",
        "prob": 0.5,
        "resolve_by": "2027-01-01",
        "resolution_source": "s",
        "status": "open",
        "outcome": None,
        "resolved_at": None,
        "tags": [],
        "notes": "",
    }) + "\n")

    preds = store.all_predictions()
    assert len(preds) == 1
    assert preds[0].thesis_id is None
    assert preds[0].days_early is None
