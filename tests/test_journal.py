"""Journal module: immutability constraints, validation, derived status,
and the scorecard math that depends on them."""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta

import pytest

from smallcap.journal import scorecard, store
from smallcap.predictions import store as pred_store


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "THESES_PATH", tmp_path / "theses.jsonl")
    monkeypatch.setattr(store, "THESIS_EVENTS_PATH", tmp_path / "thesis_events.jsonl")
    monkeypatch.setattr(store, "FOLLOWUPS_PATH", tmp_path / "followups.jsonl")
    monkeypatch.setattr(store, "TRADES_PATH", tmp_path / "trades.jsonl")
    monkeypatch.setattr(pred_store, "PREDICTIONS_PATH", tmp_path / "predictions.jsonl")
    yield


def _thesis(**overrides):
    fields = dict(
        ticker="OGEN",
        claim="claim",
        variant_view="market thinks X, I think Y",
        falsifier="if Z happens I'm wrong",
        source="own-use",
    )
    fields.update(overrides)
    return store.new_thesis(**fields)


def test_new_thesis_requires_variant_view():
    with pytest.raises(ValueError, match="variant_view"):
        _thesis(variant_view="")


def test_new_thesis_requires_falsifier():
    with pytest.raises(ValueError, match="falsifier"):
        _thesis(falsifier="  ")


def test_new_thesis_requires_valid_source():
    with pytest.raises(ValueError, match="source"):
        _thesis(source="vibes")


def test_no_creation_function_accepts_created_at():
    """Constraint 3: server timestamps on write. None of these can be handed
    a caller-supplied created_at/logged_at — that's the hole retroactive
    editing walks through."""
    assert "created_at" not in inspect.signature(store.new_thesis).parameters
    assert "created_at" not in inspect.signature(store.close).parameters
    assert "created_at" not in inspect.signature(store.followup_add).parameters
    assert "logged_at" not in inspect.signature(store.trade_log).parameters

    # revise() forwards a **fields dict but only ever reads specific known
    # keys out of it — a caller passing created_at is silently ignored, not
    # threaded through to the new record.
    t = _thesis()
    revised = store.revise(t.id, created_at="2000-01-01T00:00:00+00:00")
    assert revised.created_at != "2000-01-01T00:00:00+00:00"


def test_no_update_or_delete_symbol_exists():
    assert not hasattr(store, "update_thesis")
    assert not hasattr(store, "delete_thesis")


def test_revise_leaves_parent_line_untouched():
    parent = _thesis(claim="original claim")
    original_line = store.THESES_PATH.read_text().splitlines()[0]

    child = store.revise(parent.id, claim="revised claim")

    lines = store.THESES_PATH.read_text().splitlines()
    assert lines[0] == original_line  # byte-identical, never rewritten
    assert child.parent_id == parent.id
    assert child.claim == "revised claim"
    assert store.thesis_get(parent.id).claim == "original claim"

    assert store.thesis_status(parent.id) == "superseded"
    assert store.thesis_status(child.id) == "active"


def test_close_and_reopen_status():
    t = _thesis()
    assert store.thesis_status(t.id) == "active"
    store.close(t.id, reason="thesis played out")
    assert store.thesis_status(t.id) == "closed"
    store.reopen(t.id, reason="new information")
    assert store.thesis_status(t.id) == "active"


def test_trade_log_requires_exactly_one_of_thesis_or_index():
    t = _thesis()
    with pytest.raises(ValueError, match="exactly one"):
        store.trade_log("OGEN", "buy", 100, 1.0, "2026-01-01T00:00:00+00:00")
    with pytest.raises(ValueError, match="exactly one"):
        store.trade_log("OGEN", "buy", 100, 1.0, "2026-01-01T00:00:00+00:00", thesis_id=t.id, is_index=True)


def test_cooling_off_and_thesis_before_trade():
    t = _thesis()
    executed = (datetime.fromisoformat(t.created_at) + timedelta(hours=3)).isoformat()
    store.trade_log("OGEN", "buy", 100, 1.0, executed, thesis_id=t.id)

    r = scorecard.compute()
    assert r.pct_thesis_before_trade == 1.0
    assert r.median_cooling_off_hours == pytest.approx(3.0, abs=0.01)
    assert r.cooling_off_flagged is False


def test_cooling_off_flags_fast_trades():
    t = _thesis()
    executed = (datetime.fromisoformat(t.created_at) + timedelta(minutes=10)).isoformat()
    store.trade_log("OGEN", "buy", 100, 1.0, executed, thesis_id=t.id)

    r = scorecard.compute()
    assert r.cooling_off_flagged is True


def test_add_level_agreement_cross_check():
    t = _thesis(add_level=5.0, add_level_reason="support level")
    now = t.created_at
    store.trade_log("OGEN", "buy", 100, 4.5, now, thesis_id=t.id, rule_followed=True)
    store.trade_log("OGEN", "buy", 50, 6.0, now, thesis_id=t.id, rule_followed=True)

    r = scorecard.compute()
    assert r.n_add_checks == 2
    assert r.n_add_agree == 1  # first hit the level and was logged as such; second didn't but was logged as if it did


def test_holding_period_for_flat_thesis():
    t = _thesis()
    buy_at = t.created_at
    sell_at = (datetime.fromisoformat(t.created_at) + timedelta(days=5)).isoformat()
    store.trade_log("OGEN", "buy", 100, 1.0, buy_at, thesis_id=t.id)
    store.trade_log("OGEN", "sell", 100, 2.0, sell_at, thesis_id=t.id)

    r = scorecard.compute()
    assert r.n_flat_theses == 1
    assert r.median_holding_days == pytest.approx(5.0, abs=0.01)


def test_scorecard_excludes_pnl_by_default():
    r = scorecard.compute()
    assert r.show_pnl is False
    assert r.realized_pnl is None
    assert r.unrealized_pnl is None
