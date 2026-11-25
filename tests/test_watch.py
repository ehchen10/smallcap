"""watch.py: screens validation, cooldown dedup, digest/ack, event baselining,
and stage isolation. Network is never touched — stages are injected."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from smallcap import watch


def screen_flag(ticker="ABCD", runway=1.0):
    return {"ticker": ticker, "cik": 1, "reason": "screen", "metric": runway,
            "metrics": {"runway_years": runway, "cash": 5e6, "dilution_pct": 0.1, "pe": None, "ttm_eps": -1.0}}


def no_events(watchlist, screens, state):
    return []


def test_validate_rejects_unknown_bad_and_out_of_range():
    with pytest.raises(watch.ScreenError):
        watch.validate("nope", 1)
    with pytest.raises(watch.ScreenError):
        watch.validate("max_runway_years", -1)
    with pytest.raises(watch.ScreenError):
        watch.validate("max_runway_years", True)
    with pytest.raises(watch.ScreenError):
        watch.validate("max_runway_years", None)
    with pytest.raises(watch.ScreenError):
        watch.validate("sectors", ["energy"])
    assert watch.validate("max_pe", None) is None
    assert watch.validate("pinned", ["abcd", "ABCD"]) == ["ABCD"]


def test_set_screen_requires_reason_and_logs(tmp_path):
    with pytest.raises(watch.ScreenError):
        watch.set_screen("max_runway_years", 1.0, " ", tmp_path)
    old, new = watch.set_screen("max_runway_years", 1.0, "too many flags", tmp_path)
    assert (old, new) == (1.5, 1.0)
    assert watch.load_screens(tmp_path)["max_runway_years"] == 1.0
    log = (tmp_path / "changes.jsonl").read_text()
    assert "too many flags" in log


def test_bad_hand_edit_falls_back_to_defaults(tmp_path):
    (tmp_path / "screens.json").write_text('{"max_runway_years": -5, "bogus": 1, "stage2_cap": 7}')
    s = watch.load_screens(tmp_path)
    assert s["max_runway_years"] == watch.DEFAULT_SCREENS["max_runway_years"]
    assert s["stage2_cap"] == 7 and "bogus" not in s
    (tmp_path / "screens.json").write_text("{not json")
    assert watch.load_screens(tmp_path) == watch.DEFAULT_SCREENS


def test_sector_membership():
    assert watch.in_sectors(3674, ["tech", "biomed"])      # semiconductors
    assert watch.in_sectors(2836, ["biomed"])              # biologicals
    assert not watch.in_sectors(2834, ["tech"])
    assert not watch.in_sectors(None, ["tech"])
    assert watch.in_sectors(None, [])                      # empty = no filter


def test_sector_filter_uses_and_caches_sic(tmp_path, monkeypatch):
    calls = []
    sics = {1: 3674, 2: 6021, 3: 2834}
    monkeypatch.setattr(watch.client, "submissions", lambda cik, **k: calls.append(cik) or {"sic": str(sics[cik])})
    rows = [SimpleNamespace(cik=c) for c in (1, 2, 3)]
    out = watch._sector_filter(rows, ["tech", "biomed"], tmp_path)
    assert [r.cik for r in out] == [1, 3]
    watch._sector_filter(rows, ["tech", "biomed"], tmp_path)
    assert calls == [1, 2, 3]  # second pass served from sic.json


def test_cycle_flags_then_cooldown_suppresses_then_big_move_refires(tmp_path):
    r1 = watch.run_cycle(tmp_path, screen_fn=lambda s: [screen_flag(runway=1.0)], events_fn=no_events)
    assert r1["new_flags"] == 1
    r2 = watch.run_cycle(tmp_path, screen_fn=lambda s: [screen_flag(runway=1.05)], events_fn=no_events)
    assert r2["new_flags"] == 0                      # inside cooldown, <20% move
    r3 = watch.run_cycle(tmp_path, screen_fn=lambda s: [screen_flag(runway=0.5)], events_fn=no_events)
    assert r3["new_flags"] == 1                      # >20% move re-fires


def test_cooldown_expiry_refires(tmp_path):
    watch.run_cycle(tmp_path, screen_fn=lambda s: [screen_flag()], events_fn=no_events)
    state = watch.load_state(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    state["cooldowns"]["ABCD|screen"]["ts"] = old
    watch.save_state(state, tmp_path)
    r = watch.run_cycle(tmp_path, screen_fn=lambda s: [screen_flag()], events_fn=no_events)
    assert r["new_flags"] == 1


def test_digest_no_change_then_content_then_ack(tmp_path):
    assert watch.render_digest(tmp_path) == "NO_CHANGE"
    watch.run_cycle(tmp_path, screen_fn=lambda s: [screen_flag()], events_fn=no_events)
    d = watch.render_digest(tmp_path)
    assert "ABCD" in d and "runway 1.00y" in d
    watch.ack(tmp_path)
    assert watch.render_digest(tmp_path) == "NO_CHANGE"
    assert (tmp_path / "digest.md").read_text().strip() == "NO_CHANGE"


def test_digest_respects_line_cap(tmp_path):
    watch.set_screen("digest_max_lines", 8, "test", tmp_path)
    watch.set_screen("max_flags_per_cycle", 50, "test", tmp_path)
    flags = [screen_flag(ticker=f"T{i:02d}", runway=1.0) for i in range(30)]
    watch.run_cycle(tmp_path, screen_fn=lambda s: flags, events_fn=no_events)
    d = watch.render_digest(tmp_path).splitlines()
    assert len(d) <= 8 and "more" in d[-1]


def test_max_flags_per_cycle_caps_screen_hits(tmp_path):
    watch.set_screen("max_flags_per_cycle", 3, "test", tmp_path)
    flags = [screen_flag(ticker=f"T{i}", runway=1.0) for i in range(10)]
    r = watch.run_cycle(tmp_path, screen_fn=lambda s: flags, events_fn=no_events)
    assert r["new_flags"] == 3


def test_stage_failure_is_isolated_and_reported(tmp_path):
    def boom(s):
        raise RuntimeError("sec down")

    ev = [{"ticker": "ABCD", "cik": 1, "reason": "event:8-K", "metric": None, "metrics": {"detail": "new 8-K"}}]
    r = watch.run_cycle(tmp_path, screen_fn=boom, events_fn=lambda w, s, st: ev)
    assert r["errors"] == 1 and r["new_flags"] == 2
    d = watch.render_digest(tmp_path)
    assert "error:screen" in d and "sec down" in d and "event:8-K" in d


def test_events_baseline_then_flag_new_accession(tmp_path, monkeypatch):
    monkeypatch.setattr(watch.client, "ticker_map", lambda **k: {"ABCD": {"cik": 1, "ticker": "ABCD", "title": "x"}})
    def filing(accn):
        return SimpleNamespace(form="10-Q", filing_date="2026-01-01", report_date="2025-12-31", accession_number=accn)
    current = [filing("a1")]
    monkeypatch.setattr(watch.filings, "list_filings", lambda cik, forms, **k: current)
    state = {"seen": {}}
    screens = dict(watch.DEFAULT_SCREENS)
    assert watch.stage_events(["ABCD"], screens, state) == []          # first sight: silent baseline
    current[:] = [filing("a2"), filing("a1")]
    out = watch.stage_events(["ABCD"], screens, state)
    assert len(out) == 1 and out[0]["reason"] == "event:10-Q" and out[0]["metrics"]["accession"] == "a2"
    assert watch.stage_events(["ABCD"], screens, state) == []          # now seen


def test_pinned_tickers_are_watched_even_without_screen_hits(tmp_path):
    watch.set_screen("pinned", ["PINN"], "test", tmp_path)
    seen = {}
    def ev(w, s, st):
        seen["w"] = list(w)
        return []
    watch.run_cycle(tmp_path, screen_fn=lambda s: [], events_fn=ev)
    assert seen["w"] == ["PINN"]


# ------------------------------------------------------------ tiers / review

NOSNAP = lambda t: {"runway_years": 1.0, "pe": None, "cash": 5e6}


def _tmap(monkeypatch, *tickers):
    monkeypatch.setattr(watch.client, "ticker_map", lambda **k: {t: {"cik": i + 1, "ticker": t, "title": t} for i, t in enumerate(tickers)})


def _cycle(tmp_path, screen=None, events=None, snap=NOSNAP, gc=lambda t: None, ins=lambda t, s: None):
    return watch.run_cycle(
        tmp_path,
        screen_fn=lambda s: screen or [],
        events_fn=lambda w, s, st: events or [],
        snapshot_fn=snap, going_concern_fn=gc, insider_fn=ins,
    )


def ev(ticker, form="10-Q", detail=None):
    return {"ticker": ticker, "cik": 1, "reason": f"event:{form}", "metric": None, "metrics": {"detail": detail or f"new {form} filed 2026-11-20"}}


def test_mark_snapshots_and_active_cap(tmp_path, monkeypatch):
    _tmap(monkeypatch, "AAA", "BBB", "CCC")
    watch.set_screen("active_cap", 2, "test", tmp_path)
    rec = watch.mark_ticker("aaa", "active", "good", tmp_path, snapshot_fn=NOSNAP)
    assert rec["snapshot"]["runway_years"] == 1.0 and rec["cik"] == 1
    watch.mark_ticker("BBB", "active", "", tmp_path, snapshot_fn=NOSNAP)
    with pytest.raises(watch.ScreenError, match="full"):
        watch.mark_ticker("CCC", "active", "", tmp_path, snapshot_fn=NOSNAP)
    watch.mark_ticker("AAA", "active", "re-mark ok at cap", tmp_path, snapshot_fn=NOSNAP)
    with pytest.raises(watch.ScreenError):
        watch.mark_ticker("CCC", "bogus", "", tmp_path, snapshot_fn=NOSNAP)


def test_tracked_and_recently_dropped_names_are_not_new_screen_hits(tmp_path, monkeypatch):
    _tmap(monkeypatch, "AAA", "BBB", "CCC")
    watch.mark_ticker("AAA", "park", "", tmp_path, snapshot_fn=NOSNAP)
    watch.mark_ticker("BBB", "drop", "junk", tmp_path, snapshot_fn=NOSNAP)
    r = _cycle(tmp_path, screen=[screen_flag("AAA"), screen_flag("BBB"), screen_flag("CCC")])
    assert r["new_flags"] == 1 and "CCC" in watch.render_digest(tmp_path) and "AAA" not in watch.render_digest(tmp_path)
    # after drop_reenter_months the dropped name can flag again
    tiers = watch.load_tiers(tmp_path)
    tiers["BBB"]["marked_ts"] = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    watch.save_tiers(tiers, tmp_path)
    r = _cycle(tmp_path, screen=[screen_flag("BBB")])
    assert r["new_flags"] == 1


def test_parked_filing_becomes_trigger_not_digest(tmp_path, monkeypatch):
    _tmap(monkeypatch, "AAA")
    watch.mark_ticker("AAA", "park", "later", tmp_path, snapshot_fn=NOSNAP)
    r = _cycle(tmp_path, events=[ev("AAA")])
    assert r["new_triggers"] >= 1 and r["new_flags"] == 0
    assert watch.render_digest(tmp_path) == "NO_CHANGE"
    review = watch.render_review(tmp_path)
    assert "Changed since you last looked (1)" in review and "new 10-Q filed" in review
    watch.mark_ticker("AAA", "park", "seen", tmp_path, snapshot_fn=NOSNAP)   # re-mark clears
    assert "Changed since you last looked (0)" in watch.render_review(tmp_path)


def test_metric_move_over_20pct_and_going_concern_trigger_on_10q(tmp_path, monkeypatch):
    _tmap(monkeypatch, "AAA")
    watch.mark_ticker("AAA", "active", "", tmp_path, snapshot_fn=lambda t: {"runway_years": 1.0})
    _cycle(tmp_path, events=[ev("AAA")], snap=lambda t: {"runway_years": 0.5}, gc=lambda t: "substantial doubt")
    kinds = {tr["kind"] for tr in watch.load_tiers(tmp_path)["AAA"]["triggers"]}
    assert {"filing", "metric", "going-concern"} <= kinds
    assert "runway_years 1.00 -> 0.50" in watch.render_review(tmp_path)


def test_small_metric_move_does_not_trigger(tmp_path, monkeypatch):
    _tmap(monkeypatch, "AAA")
    watch.mark_ticker("AAA", "active", "", tmp_path, snapshot_fn=lambda t: {"runway_years": 1.0})
    _cycle(tmp_path, events=[ev("AAA")], snap=lambda t: {"runway_years": 1.1})
    assert {tr["kind"] for tr in watch.load_tiers(tmp_path)["AAA"]["triggers"]} == {"filing"}


def test_notable_8k_item_triggers_but_plain_8k_only_filing(tmp_path, monkeypatch):
    _tmap(monkeypatch, "AAA", "BBB")
    watch.mark_ticker("AAA", "active", "", tmp_path, snapshot_fn=NOSNAP)
    watch.mark_ticker("BBB", "active", "", tmp_path, snapshot_fn=NOSNAP)
    _cycle(tmp_path, events=[ev("AAA", "8-K", "new 8-K filed 2026-11-20 items 3.02,9.01"), ev("BBB", "8-K", "new 8-K filed 2026-11-20 items 7.01")])
    t = watch.load_tiers(tmp_path)
    assert "8-K" in {x["kind"] for x in t["AAA"]["triggers"]}
    assert "8-K" not in {x["kind"] for x in t["BBB"]["triggers"]}


def test_insider_check_triggers_and_is_rate_limited(tmp_path, monkeypatch):
    _tmap(monkeypatch, "AAA")
    watch.mark_ticker("AAA", "active", "", tmp_path, snapshot_fn=NOSNAP)
    calls = []
    def ins(t, since):
        calls.append(t)
        return "3 insiders bought 2026-11-01..2026-11-10"
    _cycle(tmp_path, ins=ins)
    _cycle(tmp_path, ins=ins)
    assert calls == ["AAA"]                       # 2nd cycle within 7 days: skipped
    assert any(tr["kind"] == "insider" for tr in watch.load_tiers(tmp_path)["AAA"]["triggers"])


def test_parked_expiry_auto_drops_only_when_untriggered(tmp_path, monkeypatch):
    _tmap(monkeypatch, "AAA", "BBB")
    watch.mark_ticker("AAA", "park", "", tmp_path, snapshot_fn=NOSNAP)
    watch.mark_ticker("BBB", "park", "", tmp_path, snapshot_fn=NOSNAP)
    tiers = watch.load_tiers(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    tiers["AAA"]["marked_ts"] = old
    tiers["BBB"]["marked_ts"] = old
    tiers["BBB"]["last_trigger_ts"] = datetime.now(timezone.utc).isoformat()   # recent trigger keeps it alive
    watch.save_tiers(tiers, tmp_path)
    _cycle(tmp_path)
    t = watch.load_tiers(tmp_path)
    assert t["AAA"]["tier"] == "drop" and "auto-expired" in t["AAA"]["note"]
    assert t["BBB"]["tier"] == "park"


def test_parked_polled_at_most_daily_and_tracked_every_cycle(tmp_path, monkeypatch):
    _tmap(monkeypatch, "AAA", "BBB")
    watch.mark_ticker("AAA", "park", "", tmp_path, snapshot_fn=NOSNAP)
    watch.mark_ticker("BBB", "candidate", "", tmp_path, snapshot_fn=NOSNAP)
    seen = []
    def events(w, s, st):
        seen.append(list(w))
        return []
    for _ in range(2):
        watch.run_cycle(tmp_path, screen_fn=lambda s: [], events_fn=events, snapshot_fn=NOSNAP, going_concern_fn=lambda t: None, insider_fn=lambda t, s: None)
    assert seen[0] == ["BBB", "AAA"] and seen[1] == ["BBB"]


def test_review_sections_and_candidate_listing(tmp_path, monkeypatch):
    _tmap(monkeypatch, "AAA", "BBB", "CCC")
    watch.mark_ticker("AAA", "candidate", "cheap, burning slowly", tmp_path, snapshot_fn=NOSNAP)
    watch.mark_ticker("BBB", "active", "", tmp_path, snapshot_fn=NOSNAP)
    watch.mark_ticker("CCC", "drop", "", tmp_path, snapshot_fn=NOSNAP)
    r = watch.render_review(tmp_path)
    assert "Candidates awaiting your decision (1)" in r and "cheap, burning slowly" in r
    assert "Active, no change (1 of 1 active; cap 15)" in r and "Dropped: 1" in r


def test_snapshot_metrics_drops_infinite_runway_for_profitable(monkeypatch):
    from smallcap import runway
    monkeypatch.setattr(runway, "compute", lambda t: SimpleNamespace(runway_quarters_ttm=float("inf"), profitable=True, cash=1e9))
    monkeypatch.setattr(watch.fundamentals, "fundamentals", lambda t: SimpleNamespace(pe=20.0, ttm_eps=2.0, ps=3.0))
    m = watch.snapshot_metrics("X")
    assert "runway_years" not in m and m["cash"] == 1e9 and m["pe"] == 20.0
