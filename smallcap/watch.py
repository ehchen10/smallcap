"""Always-on watcher: cheap deterministic screening in a loop, with a small
digest for a Claude Code session to steer.

Python does all the continuous work (zero tokens). A Claude session wakes
occasionally, runs ``smallcap watch digest``, and — only if something changed —
triages flags, tightens/loosens thresholds via ``watch set``, and dives into a
few names. All state is plain files under ``config.WATCH_DIR`` so a restart of
either side loses nothing and nothing here is machine-specific.

Idea generator only. Nothing here is a buy/sell signal.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import signal
import threading
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from smallcap import config, fundamentals, screener
from smallcap.sec import client, filings

DEFAULT_SCREENS: dict[str, Any] = {
    "interval_minutes": 60,
    "max_runway_years": 1.5,
    "min_cash": 1_000_000,
    "max_cash": 500_000_000,
    "max_dilution_pct": 0.5,
    "max_pe": None,
    "stage2_cap": 60,
    "max_flags_per_cycle": 10,
    "cooldown_days": 14,
    "watchlist_cap": 50,
    "active_cap": 15,
    "parked_expiry_months": 12,
    "drop_reenter_months": 6,
    "digest_max_lines": 40,
    "sectors": ["tech", "biomed"],
    "event_forms": ["8-K", "10-K", "10-Q"],
    "pinned": [],
}

# key -> (python types, min, max). None bounds = unbounded; nullable keys also accept None.
_NUM = (int, float)
_SPEC: dict[str, tuple[tuple[type, ...], float | None, float | None, bool]] = {
    "interval_minutes": (_NUM, 1, 24 * 60, False),
    "max_runway_years": (_NUM, 0, 50, False),
    "min_cash": (_NUM, 0, None, False),
    "max_cash": (_NUM, 0, None, False),
    "max_dilution_pct": (_NUM, -1, 100, False),
    "max_pe": (_NUM, 0, 10_000, True),
    "stage2_cap": (_NUM, 1, 500, False),
    "max_flags_per_cycle": (_NUM, 1, 100, False),
    "cooldown_days": (_NUM, 0, 365, False),
    "watchlist_cap": (_NUM, 0, 500, False),
    "active_cap": (_NUM, 1, 200, False),
    "parked_expiry_months": (_NUM, 1, 120, False),
    "drop_reenter_months": (_NUM, 1, 120, False),
    "digest_max_lines": (_NUM, 5, 200, False),
}
_LIST_KEYS = {"event_forms", "pinned", "sectors"}

# Sector universe by SEC SIC code (from each filer's submissions JSON — free, no key).
# Inclusive (lo, hi) ranges. Empty ``sectors`` = no sector filter.
SECTOR_SIC: dict[str, list[tuple[int, int]]] = {
    "tech": [(3570, 3579), (3661, 3679), (7370, 7379)],  # computers, comms equip, electronic components/semis, software & IT services
    "biomed": [(2833, 2836), (3826, 3826), (3841, 3845), (8071, 8071), (8731, 8731)],  # pharma, biologicals/diagnostics, lab instruments, med devices, labs, bio research
}
SIC_LOOKUPS_PER_CYCLE = 400  # first cycle spreads SIC fetches over a few cycles instead of one long stall
_ALLOWED_FORMS = {"8-K", "10-K", "10-Q", "4"}

METRIC_MOVE = 0.20  # re-flag inside the cooldown if the key metric moved >20%


class ScreenError(ValueError):
    pass


# ---------------------------------------------------------------- paths/io


def _paths(base: Path | None = None) -> dict[str, Path]:
    d = base or config.WATCH_DIR
    return {
        "dir": d,
        "screens": d / "screens.json",
        "flags": d / "flags.jsonl",
        "digest": d / "digest.md",
        "state": d / "state.json",
        "notes": d / "notes.md",
        "changes": d / "changes.jsonl",
        "tiers": d / "tiers.json",
        "review": d / "review.md",
        "lock": d / "lock",
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn final line after a crash shouldn't wedge the watcher
    return out


# ---------------------------------------------------------------- screens


def validate(key: str, value: Any) -> Any:
    """Return the cleaned value for ``key`` or raise ``ScreenError``."""
    if key in _LIST_KEYS:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ScreenError(f"{key} must be a list of strings")
        if key == "event_forms":
            bad = set(value) - _ALLOWED_FORMS
            if bad:
                raise ScreenError(f"event_forms allows only {sorted(_ALLOWED_FORMS)}, got {sorted(bad)}")
        if key == "sectors":
            bad = set(value) - set(SECTOR_SIC)
            if bad:
                raise ScreenError(f"sectors allows only {sorted(SECTOR_SIC)} (empty list = no filter), got {sorted(bad)}")
        if key == "pinned":
            value = sorted({v.upper() for v in value})
        return value
    if key not in _SPEC:
        raise ScreenError(f"unknown screen key {key!r}; valid: {sorted(DEFAULT_SCREENS)}")
    types, lo, hi, nullable = _SPEC[key]
    if value is None:
        if nullable:
            return None
        raise ScreenError(f"{key} cannot be null")
    if isinstance(value, bool) or not isinstance(value, types):
        raise ScreenError(f"{key} must be a number")
    if lo is not None and value < lo:
        raise ScreenError(f"{key} must be >= {lo}")
    if hi is not None and value > hi:
        raise ScreenError(f"{key} must be <= {hi}")
    return value


def load_screens(base: Path | None = None) -> dict[str, Any]:
    """Defaults overlaid with screens.json. Invalid or unknown entries in the
    file are ignored (defaults win) so a bad hand edit can't stop the daemon."""
    p = _paths(base)["screens"]
    screens = dict(DEFAULT_SCREENS)
    if p.exists():
        try:
            raw = json.loads(p.read_text())
        except json.JSONDecodeError:
            return screens
        for k, v in raw.items():
            try:
                screens[k] = validate(k, v)
            except ScreenError:
                pass
    return screens


def set_screen(key: str, value: Any, reason: str, base: Path | None = None) -> tuple[Any, Any]:
    """Validate, persist, and audit-log one change. Returns (old, new)."""
    if not reason.strip():
        raise ScreenError("a reason is required so every screen change is auditable")
    new = validate(key, value)
    p = _paths(base)
    screens = load_screens(base)
    old = screens.get(key)
    screens[key] = new
    _atomic_write(p["screens"], json.dumps(screens, indent=2, sort_keys=True) + "\n")
    _append_jsonl(p["changes"], [{"ts": _now(), "key": key, "old": old, "new": new, "reason": reason}])
    return old, new


def parse_value(raw: str) -> Any:
    """CLI string -> JSON value ("1.5", "null", '["8-K"]'); bare words fall through as strings."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


# ---------------------------------------------------------------- state


def load_state(base: Path | None = None) -> dict[str, Any]:
    p = _paths(base)["state"]
    state: dict[str, Any] = {"cooldowns": {}, "seen": {}, "watchlist": [], "acked_ts": "", "last_cycle": None}
    if p.exists():
        try:
            state.update(json.loads(p.read_text()))
        except json.JSONDecodeError:
            pass
    return state


def save_state(state: dict[str, Any], base: Path | None = None) -> None:
    _atomic_write(_paths(base)["state"], json.dumps(state, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------- dedup


def _should_flag(state: dict, ticker: str, reason: str, metric: float | None, cooldown_days: float, now: datetime) -> bool:
    prior = state["cooldowns"].get(f"{ticker}|{reason}")
    if prior is None:
        return True
    age = now - datetime.fromisoformat(prior["ts"])
    if age >= timedelta(days=cooldown_days):
        return True
    old = prior.get("metric")
    if metric is not None and old:
        return abs(metric - old) / abs(old) > METRIC_MOVE
    return False


def _mark(state: dict, ticker: str, reason: str, metric: float | None, now: datetime) -> None:
    state["cooldowns"][f"{ticker}|{reason}"] = {"ts": now.isoformat(), "metric": metric}


# ---------------------------------------------------------------- stages


def in_sectors(sic: int | None, sectors: list[str]) -> bool:
    if not sectors:
        return True
    if sic is None:
        return False
    return any(lo <= sic <= hi for sec in sectors for lo, hi in SECTOR_SIC[sec])


def _sector_filter(rows: list, sectors: list[str], base: Path | None = None) -> list:
    """Keep rows whose filer's SIC falls in ``sectors``. SIC is looked up only
    for rows that already passed the cheap frames screen, then cached forever
    in sic.json (SIC essentially never changes)."""
    if not sectors:
        return rows
    path = _paths(base)["dir"] / "sic.json"
    cache: dict[str, int | None] = {}
    if path.exists():
        try:
            cache = json.loads(path.read_text())
        except json.JSONDecodeError:
            cache = {}
    fetched = 0
    for r in rows:
        key = str(r.cik)
        if key in cache or fetched >= SIC_LOOKUPS_PER_CYCLE:
            continue
        try:
            raw = client.submissions(r.cik).get("sic")
            cache[key] = int(raw) if raw else None
        except Exception:
            continue  # leave uncached; retried next cycle
        fetched += 1
    if fetched:
        _atomic_write(path, json.dumps(cache))
    return [r for r in rows if in_sectors(cache.get(str(r.cik)), sectors)]


def stage_screen(screens: dict[str, Any]) -> list[dict]:
    """Universe-wide frames pass (no per-ticker yfinance), then split-checked
    pass + P/E on the top survivors. Returns flag dicts, best runway first."""
    end, start = screener.default_annual_periods()
    instant = screener.default_instant_period()
    tickers = sorted(client.ticker_map())

    s1 = screener.screen(
        tickers,
        instant_period=instant,
        annual_end_period=end,
        annual_start_period=start,
        max_runway_years=screens["max_runway_years"],
        check_splits=False,
    )
    rows = [r for r in s1.rows if r.cash is not None and screens["min_cash"] <= r.cash <= screens["max_cash"]]
    rows = _sector_filter(rows, screens["sectors"])
    survivors = [r.ticker for r in rows[: int(screens["stage2_cap"])]]
    if not survivors:
        return []

    s2 = screener.screen(
        survivors,
        instant_period=instant,
        annual_end_period=end,
        annual_start_period=start,
        check_splits=True,
    )
    max_dil = screens["max_dilution_pct"]
    out: list[dict] = []
    for r in s2.rows:
        # dilution unknown (split-era suspect / missing shares) is kept and labelled, not silently dropped
        if r.dilution_pct is not None and r.dilution_pct > max_dil:
            continue
        out.append(
            {
                "ticker": r.ticker,
                "cik": r.cik,
                "reason": "screen",
                "metric": r.runway_years,
                "metrics": {
                    "runway_years": r.runway_years,
                    "cash": r.cash,
                    "annual_ocf": r.annual_ocf,
                    "dilution_pct": r.dilution_pct,
                    "dilution_suspect": r.dilution_suspect,
                },
            }
        )
    out.sort(key=lambda f: (f["metric"] is None, f["metric"] if f["metric"] is not None else 0.0))

    # P/E only for the names we might actually flag — bounded network cost.
    cap = int(screens["max_flags_per_cycle"]) * 3
    kept: list[dict] = []
    for f in out[:cap]:
        try:
            fund = fundamentals.fundamentals(f["ticker"])
            f["metrics"]["pe"] = fund.pe
            f["metrics"]["ttm_eps"] = fund.ttm_eps
            f["metrics"]["ps"] = fund.ps
        except Exception:
            f["metrics"]["pe"] = None
        if screens["max_pe"] is not None:
            pe = f["metrics"].get("pe")
            if pe is None or pe > screens["max_pe"]:
                continue
        kept.append(f)
    return kept


def stage_events(watchlist: list[str], screens: dict[str, Any], state: dict) -> list[dict]:
    """New 10-K/10-Q/8-K(/4) accessions for watched tickers vs the last seen set.
    First sight of a CIK is a silent baseline, not a flood of old filings."""
    forms = tuple(screens["event_forms"])
    out: list[dict] = []
    if not forms:
        return out
    tmap = client.ticker_map()
    for ticker in watchlist:
        info = tmap.get(ticker)
        if not info:
            continue
        cik = info["cik"]
        try:
            recent = filings.list_filings(cik, forms, limit=10, refresh=True)
        except Exception:
            continue
        seen = set(state["seen"].get(str(cik), []))
        accns = [f.accession_number for f in recent]
        if str(cik) in state["seen"]:
            for f in recent:
                if f.accession_number in seen:
                    continue
                detail = f"new {f.form} filed {f.filing_date} (period {f.report_date})"
                if f.form == "8-K":
                    try:
                        items = filings.eight_k_item_types(cik, f)
                        if items:
                            detail += f" items {','.join(sorted(items))}"
                    except Exception:
                        pass
                out.append({"ticker": ticker, "cik": cik, "reason": f"event:{f.form}", "metric": None, "metrics": {"detail": detail, "accession": f.accession_number}})
        state["seen"][str(cik)] = sorted(seen | set(accns))[-30:]
    return out



# ---------------------------------------------------------------- tiers / review
#
# Raw flags are cheap and plentiful; the review file is the scarce, human-sized
# thing. A name moves  flag -> candidate (Claude: "interesting") -> you decide
# active | park | drop. Parked/dropped names cost nothing until a *change*
# (new filing, metric move, going-concern, notable 8-K, insider cluster) —
# so review effort scales with changes, not with how many names you've seen.

TIERS = ("candidate", "active", "park", "drop")
NOTABLE_8K_ITEMS = {"1.03", "2.03", "3.01", "3.02", "4.02", "5.02"}  # bankruptcy, new debt, delisting, dilutive issuance, restatement, exec change
PARKED_POLL_HOURS = 24
INSIDER_CHECK_DAYS = 7
INSIDER_CHECKS_PER_CYCLE = 10
MAX_TRIGGERS = 10
SNAPSHOT_KEYS = ("runway_years", "pe")  # compared for the >20% "metric moved" trigger


def load_tiers(base: Path | None = None) -> dict[str, dict]:
    p = _paths(base)["tiers"]
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_tiers(tiers: dict[str, dict], base: Path | None = None) -> None:
    _atomic_write(_paths(base)["tiers"], json.dumps(tiers, indent=2, sort_keys=True) + "\n")


def snapshot_metrics(ticker: str) -> dict[str, Any]:
    """Metrics recorded when a name is marked and re-measured on new filings.
    Each source fails soft — a partial snapshot beats no mark."""
    from smallcap import runway

    m: dict[str, Any] = {}
    try:
        r = runway.compute(ticker)
        # profitable filers come back with an infinite runway — that's "n/a", not a number to diff against
        if r.runway_quarters_ttm is not None and not r.profitable and math.isfinite(r.runway_quarters_ttm):
            m["runway_years"] = r.runway_quarters_ttm / 4
        m["cash"] = r.cash
    except Exception:
        pass
    try:
        f = fundamentals.fundamentals(ticker)
        m["pe"], m["ttm_eps"], m["ps"] = f.pe, f.ttm_eps, f.ps
    except Exception:
        pass
    return m


def mark_ticker(
    ticker: str,
    tier: str,
    note: str = "",
    base: Path | None = None,
    *,
    snapshot_fn: Callable[[str], dict] | None = None,
) -> dict:
    """Set a name's tier, re-snapshot its metrics, and clear its pending
    triggers (re-marking = "I've looked at the changes")."""
    t = ticker.upper()
    if tier not in TIERS:
        raise ScreenError(f"tier must be one of {TIERS}")
    tiers = load_tiers(base)
    if tier == "active":
        cap = int(load_screens(base)["active_cap"])
        active = [k for k, v in tiers.items() if v["tier"] == "active" and k != t]
        if len(active) >= cap:
            raise ScreenError(f"active tier is full ({cap}): park or drop one first ({', '.join(sorted(active))})")
    info = client.ticker_map().get(t)
    snap = (snapshot_fn or snapshot_metrics)(t) if tier != "drop" else {}
    prev = tiers.get(t, {})
    now = _now()
    rec = {
        "tier": tier,
        "note": note,
        "cik": info["cik"] if info else prev.get("cik"),
        "marked_ts": now,
        "snapshot": snap,
        "triggers": [],
        "last_trigger_ts": None,
        "history": (prev.get("history", []) + [{"ts": now, "tier": tier, "note": note}])[-20:],
    }
    tiers[t] = rec
    save_tiers(tiers, base)
    return rec


def _add_trigger(rec: dict, kind: str, detail: str, now: str) -> bool:
    if any(tr["kind"] == kind and tr["detail"] == detail for tr in rec["triggers"]):
        return False
    rec["triggers"] = (rec["triggers"] + [{"ts": now, "kind": kind, "detail": detail}])[-MAX_TRIGGERS:]
    rec["last_trigger_ts"] = now
    return True


def _moved(old: dict, new: dict) -> list[str]:
    out = []
    for k in SNAPSHOT_KEYS:
        a, b = old.get(k), new.get(k)
        if a and b is not None and abs(b - a) / abs(a) > METRIC_MOVE:
            out.append(f"{k} {a:.2f} -> {b:.2f}")
    return out


def _going_concern_new(ticker: str) -> str | None:
    cik = client.cik_for_ticker(ticker)
    gc = filings.going_concern_newly_appeared(cik)
    return (gc.snippet or "going-concern language")[:140] if gc and gc.found else None


def _insider_cluster_since(ticker: str, since: str) -> str | None:
    from smallcap.sec import insiders

    cik = client.cik_for_ticker(ticker)
    clusters = insiders.cluster_buys(insiders.recent_transactions(cik, limit=20, open_market_only=True))
    fresh = [c for c in clusters if c.end_date >= since[:10]]
    return f"{len(fresh[-1].owners)} insiders bought {fresh[-1].start_date}..{fresh[-1].end_date}" if fresh else None


def expire_and_reenter(tiers: dict, screens: dict, now: datetime) -> list[str]:
    """Parked names with no trigger for ``parked_expiry_months`` are dropped
    (list can't grow forever). Returns the tickers auto-dropped."""
    out = []
    horizon = timedelta(days=30 * float(screens["parked_expiry_months"]))
    for t, rec in tiers.items():
        if rec["tier"] != "park":
            continue
        ref = datetime.fromisoformat(rec["last_trigger_ts"] or rec["marked_ts"])
        if now - ref > horizon:
            rec["tier"] = "drop"
            rec["note"] = (rec["note"] + " | " if rec["note"] else "") + f"auto-expired: no trigger in {screens['parked_expiry_months']} months"
            rec["marked_ts"] = now.isoformat()
            rec["history"] = (rec["history"] + [{"ts": now.isoformat(), "tier": "drop", "note": "auto-expired"}])[-20:]
            out.append(t)
    return out


def screen_suppressed(rec: dict | None, screens: dict, now: datetime) -> bool:
    """Tracked names (candidate/active/park) don't re-enter as new screen hits;
    dropped ones stay out until ``drop_reenter_months`` have passed."""
    if rec is None:
        return False
    if rec["tier"] != "drop":
        return True
    age = now - datetime.fromisoformat(rec["marked_ts"])
    return age < timedelta(days=30 * float(screens["drop_reenter_months"]))


def apply_triggers(
    tiers: dict,
    events: list[dict],
    state: dict,
    now: datetime,
    *,
    snapshot_fn: Callable[[str], dict],
    going_concern_fn: Callable[[str], str | None],
    insider_fn: Callable[[str, str], str | None],
) -> int:
    """Turn filing events + periodic insider checks for tiered names into
    review triggers. Returns number of new triggers."""
    n = 0
    ts = now.isoformat()
    for ev in events:
        rec = tiers.get(ev["ticker"])
        if not rec or rec["tier"] == "drop":
            continue
        detail = ev["metrics"].get("detail", "")
        form = ev["reason"].split(":", 1)[-1]
        n += _add_trigger(rec, "filing", detail, ts)
        if form == "8-K":
            items = set()
            if " items " in detail:
                items = set(detail.rsplit(" items ", 1)[1].split(","))
            hot = sorted(items & NOTABLE_8K_ITEMS)
            if hot:
                n += _add_trigger(rec, "8-K", f"notable 8-K items {','.join(hot)}", ts)
        elif form in ("10-K", "10-Q"):
            try:
                moved = _moved(rec["snapshot"], snapshot_fn(ev["ticker"]))
                if moved:
                    n += _add_trigger(rec, "metric", "; ".join(moved) + " vs snapshot", ts)
            except Exception:
                pass
            try:
                gc = going_concern_fn(ev["ticker"])
                if gc:
                    n += _add_trigger(rec, "going-concern", gc, ts)
            except Exception:
                pass
    checked = state.setdefault("insider_checked", {})
    budget = INSIDER_CHECKS_PER_CYCLE
    for t, rec in tiers.items():
        if budget <= 0:
            break
        if rec["tier"] == "drop":
            continue
        last = checked.get(t)
        if last and now - datetime.fromisoformat(last) < timedelta(days=INSIDER_CHECK_DAYS):
            continue
        budget -= 1
        checked[t] = ts
        try:
            d = insider_fn(t, rec["marked_ts"])
            if d:
                n += _add_trigger(rec, "insider", d, ts)
        except Exception:
            pass
    return n


def _fmt_snap(m: dict) -> str:
    parts = []
    if m.get("runway_years") is not None:
        parts.append(f"runway {m['runway_years']:.2f}y")
    if m.get("cash") is not None:
        parts.append(f"cash ${m['cash'] / 1e6:.1f}M")
    if m.get("pe") is not None:
        parts.append(f"P/E {m['pe']:.1f}")
    elif m.get("ttm_eps") is not None and m["ttm_eps"] <= 0:
        parts.append("P/E n/m")
    return ", ".join(parts) or "no snapshot"


def render_review(base: Path | None = None, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    screens = load_screens(base)
    tiers = load_tiers(base)
    cap = int(screens["active_cap"])
    by = {k: sorted(t for t, r in tiers.items() if r["tier"] == k) for k in TIERS}

    def day(ts: str) -> str:
        return ts[:10]

    def block(t: str, rec: dict, with_triggers: bool = True) -> list[str]:
        head = f"{t:<7} [{rec['tier']}, marked {day(rec['marked_ts'])}] {rec['note']}".rstrip()
        lines = [head, f"        at mark: {_fmt_snap(rec['snapshot'])}"]
        if with_triggers:
            lines += [f"        ! {day(tr['ts'])} {tr['kind']}: {tr['detail']}" for tr in rec["triggers"]]
        return lines

    out = [f"# Review — generated {day(now.isoformat())}", "", "Decide with: smallcap watch mark TICKER active|park|drop --note \"...\"  (re-marking clears its triggers)", ""]

    out.append(f"## Candidates awaiting your decision ({len(by['candidate'])})")
    for t in by["candidate"]:
        out += block(t, tiers[t])
    if not by["candidate"]:
        out.append("(none)")

    triggered = [t for t in by["active"] + by["park"] if tiers[t]["triggers"]]
    out += ["", f"## Changed since you last looked ({len(triggered)})"]
    for t in triggered:
        out += block(t, tiers[t])
    if not triggered:
        out.append("(none)")

    quiet = [t for t in by["active"] if not tiers[t]["triggers"]]
    out += ["", f"## Active, no change ({len(quiet)} of {len(by['active'])} active; cap {cap})"]
    for t in quiet:
        out += block(t, tiers[t], with_triggers=False)

    horizon = timedelta(days=30 * float(screens["parked_expiry_months"]))
    expiring = sorted(
        t for t in by["park"]
        if horizon - (now - datetime.fromisoformat(tiers[t]["last_trigger_ts"] or tiers[t]["marked_ts"])) < timedelta(days=30)
    )
    out += ["", f"## Parked: {len(by['park'])} names ({sum(1 for t in by['park'] if tiers[t]['triggers'])} changed, listed above)"]
    if expiring:
        out.append(f"expiring within 30 days: {', '.join(expiring)}")
    out += [f"## Dropped: {len(by['drop'])} names (re-enter the screen after {screens['drop_reenter_months']} months)"]
    return "\n".join(out)


def write_review(base: Path | None = None) -> None:
    _atomic_write(_paths(base)["review"], render_review(base) + "\n")


# ---------------------------------------------------------------- cycle


def _detail(flag: dict) -> str:
    m = flag["metrics"]
    if flag["reason"] == "screen":
        parts = []
        if m.get("runway_years") is not None:
            parts.append(f"runway {m['runway_years']:.2f}y")
        if m.get("cash") is not None:
            parts.append(f"cash ${m['cash'] / 1e6:.1f}M")
        if m.get("dilution_suspect"):
            parts.append("dil ?split?")
        elif m.get("dilution_pct") is not None:
            parts.append(f"dil {m['dilution_pct'] * 100:.0f}%")
        if m.get("pe") is not None:
            parts.append(f"P/E {m['pe']:.1f}")
        elif m.get("ttm_eps") is not None and m["ttm_eps"] <= 0:
            parts.append("P/E n/m (TTM EPS<=0)")
        return ", ".join(parts)
    return m.get("detail", "")


def run_cycle(
    base: Path | None = None,
    *,
    screen_fn: Callable[[dict], list[dict]] | None = None,
    events_fn: Callable[[list[str], dict, dict], list[dict]] | None = None,
    snapshot_fn: Callable[[str], dict] | None = None,
    going_concern_fn: Callable[[str], str | None] | None = None,
    insider_fn: Callable[[str, str], str | None] | None = None,
) -> dict[str, Any]:
    """One full pass. Stages are isolated: a failure becomes an error flag and
    the other stages still run. Returns a small summary dict."""
    screen_fn = screen_fn or stage_screen
    events_fn = events_fn or stage_events
    p = _paths(base)
    p["dir"].mkdir(parents=True, exist_ok=True)
    screens = load_screens(base)
    state = load_state(base)
    tiers = load_tiers(base)
    now = datetime.now(timezone.utc)
    cycle_id = now.isoformat()
    candidates: list[dict] = []
    errors: list[dict] = []

    expire_and_reenter(tiers, screens, now)

    try:
        candidates += screen_fn(screens)
    except Exception as e:
        errors.append({"ticker": "-", "cik": None, "reason": "error:screen", "metric": None, "metrics": {"detail": f"{type(e).__name__}: {e}"}})
    # tracked names (and recently dropped ones) aren't "new" screen hits
    candidates = [f for f in candidates if not (f["reason"] == "screen" and screen_suppressed(tiers.get(f["ticker"]), screens, now))]

    # Who gets filing-polled: pinned + candidate/active every cycle, parked at most daily,
    # plus transient screen hits (capped). Tiered names don't count against the cap.
    polled = state.setdefault("polled", {})
    parked_due = [
        t for t, r in tiers.items()
        if r["tier"] == "park" and (t not in polled or now - datetime.fromisoformat(polled[t]) >= timedelta(hours=PARKED_POLL_HOURS))
    ]
    tracked = [t for t, r in tiers.items() if r["tier"] in ("candidate", "active")]
    transient = [f["ticker"] for f in candidates if f["reason"] == "screen"] + list(state["watchlist"])
    transient = [t for t in dict.fromkeys(transient) if t not in tiers][: int(screens["watchlist_cap"])]
    state["watchlist"] = transient
    watch = list(dict.fromkeys(list(screens["pinned"]) + tracked + parked_due + transient))

    events: list[dict] = []
    try:
        events = events_fn(watch, screens, state)
    except Exception as e:
        errors.append({"ticker": "-", "cik": None, "reason": "error:events", "metric": None, "metrics": {"detail": f"{type(e).__name__}: {e}"}})
    for t in parked_due:
        polled[t] = cycle_id

    new_triggers = apply_triggers(
        tiers, events, state, now,
        snapshot_fn=snapshot_fn or snapshot_metrics,
        going_concern_fn=going_concern_fn or _going_concern_new,
        insider_fn=insider_fn or _insider_cluster_since,
    )
    # parked names surface only via the review file, not the digest
    candidates += [e for e in events if tiers.get(e["ticker"], {}).get("tier") != "park"]

    # events outrank screen hits in the digest: a new filing is time-sensitive
    candidates.sort(key=lambda f: 0 if f["reason"].startswith("event") else 1)
    fresh: list[dict] = []
    for f in candidates + errors:
        if len(fresh) >= int(screens["max_flags_per_cycle"]) and not f["reason"].startswith("error"):
            break
        if _should_flag(state, f["ticker"], f["reason"], f["metric"], screens["cooldown_days"], now):
            _mark(state, f["ticker"], f["reason"], f["metric"], now)
            f["ts"] = cycle_id
            fresh.append(f)

    _append_jsonl(p["flags"], fresh)
    state["last_cycle"] = cycle_id
    save_state(state, base)
    save_tiers(tiers, base)
    write_digest(base)
    write_review(base)
    return {"cycle": cycle_id, "candidates": len(candidates), "new_flags": len(fresh), "errors": len(errors), "new_triggers": new_triggers}


# ---------------------------------------------------------------- digest


def unacked_flags(base: Path | None = None) -> list[dict]:
    p = _paths(base)
    acked = load_state(base)["acked_ts"]
    return [f for f in _read_jsonl(p["flags"]) if f["ts"] > acked]


def render_digest(base: Path | None = None) -> str:
    flags = unacked_flags(base)
    if not flags:
        return "NO_CHANGE"
    cap = int(load_screens(base)["digest_max_lines"])
    # latest flag per (ticker, reason) only, events first
    latest: dict[tuple[str, str], dict] = {}
    for f in flags:
        latest[(f["ticker"], f["reason"])] = f
    rows = sorted(latest.values(), key=lambda f: (0 if f["reason"].startswith("event") else 1 if f["reason"] == "screen" else 2, f["ts"]))
    lines = [f"{len(flags)} new flag(s) since last ack; showing {min(len(rows), cap - 2)} of {len(rows)} unique"]
    for f in rows[: cap - 2]:
        lines.append(f"{f['ticker']:<7} {f['reason']:<11} {_detail(f)}")
    if len(rows) > cap - 2:
        lines.append(f"... +{len(rows) - (cap - 2)} more (see flags.jsonl)")
    return "\n".join(lines)


def write_digest(base: Path | None = None) -> None:
    _atomic_write(_paths(base)["digest"], render_digest(base) + "\n")


def ack(base: Path | None = None) -> None:
    state = load_state(base)
    state["acked_ts"] = _now()
    save_state(state, base)
    write_digest(base)


# ---------------------------------------------------------------- daemon


def run_forever(base: Path | None = None, *, once: bool = False, log: Callable[[str], None] = print) -> None:
    p = _paths(base)
    p["dir"].mkdir(parents=True, exist_ok=True)
    lock = p["lock"].open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("another smallcap watch is already running against this data dir")

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    while not stop.is_set():
        try:
            summary = run_cycle(base)
            log(f"{summary['cycle']} cycle ok: {summary['new_flags']} new flag(s), {summary['errors']} error(s)")
        except Exception:
            log("cycle crashed:\n" + traceback.format_exc())
        if once:
            return
        stop.wait(float(load_screens(base)["interval_minutes"]) * 60)
    log("stopped")


# ---------------------------------------------------------------- dive


def dive(ticker: str) -> str:
    """Compact (~15 line) deep dive: fundamentals, runway, going-concern,
    new 8-K items, insider buy clusters. Each section fails soft."""
    from smallcap import runway
    from smallcap.sec import insiders

    t = ticker.upper()
    lines = [f"== {t} =="]
    cik = client.cik_for_ticker(t)

    def money(v: float | None) -> str:
        return "-" if v is None else f"${v / 1e6:,.1f}M"

    try:
        f = fundamentals.fundamentals(t)
        lines.append(f"FY {f.fy_end}: rev {money(f.fy_revenue)}, NI {money(f.fy_net_income)}, EPS {f.fy_eps}")
        lines.append(f"Q  {f.q_end}: rev {money(f.q_revenue)}, NI {money(f.q_net_income)}, EPS {f.q_eps}")
        pe = f"{f.pe:.1f}" if f.pe else ("n/m" if f.ttm_eps is not None else "-")
        ps = f"{f.ps:.2f}" if f.ps else "-"
        lines.append(f"TTM rev {money(f.ttm_revenue)}, TTM EPS {f.ttm_eps}, mkt cap {money(f.market_cap)}, P/E {pe}, P/S {ps}")
    except Exception as e:
        lines.append(f"fundamentals: unavailable ({type(e).__name__}: {e})")

    try:
        r = runway.compute(t)
        q = f"{r.runway_quarters_ttm:.1f}q" if r.runway_quarters_ttm is not None else "n/a"
        lines.append(f"runway: cash {money(r.cash)} (as of {r.cash_as_of}), TTM burn {money(r.ttm_burn)}, {q}, profitable={r.profitable}")
    except Exception as e:
        lines.append(f"runway: unavailable ({type(e).__name__}: {e})")

    try:
        gc = filings.going_concern_newly_appeared(cik)
        lines.append("going concern: " + ("NEWLY APPEARED — " + (gc.snippet or "")[:160] if gc and gc.found else "no new language"))
    except Exception as e:
        lines.append(f"going concern: unavailable ({type(e).__name__})")

    try:
        n8 = filings.new_8k_item_types(cik)
        lines.append("8-K never-before items: " + (",".join(sorted(n8.new_items)) if n8.new_items else "none"))
    except Exception as e:
        lines.append(f"8-K items: unavailable ({type(e).__name__})")

    try:
        txs = insiders.recent_transactions(cik, limit=20, open_market_only=True)
        clusters = insiders.cluster_buys(txs)
        lines.append(f"insider open-market buy clusters (recent 20 Form 4s): {len(clusters)}" + (f", latest {clusters[-1].start_date}..{clusters[-1].end_date} ({len(clusters[-1].owners)} owners)" if clusters else ""))
    except Exception as e:
        lines.append(f"insiders: unavailable ({type(e).__name__})")

    lines.append("(idea generator only — not a recommendation)")
    return "\n".join(lines)
