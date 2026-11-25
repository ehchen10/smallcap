"""Turn raw SEC XBRL facts into numbers you can actually do arithmetic on.

Three problems with the raw data, verified live against CIK 1174940
(Oragenics) while designing this module — see the plan doc for the numbers:

1. Duration facts (cash flow, revenue, ...) are YTD-cumulative within a fiscal
   year, not per-quarter. Summing four of them overstates the annual total by
   ~2.5x. Fix: ``discrete_quarters``.
2. The same period is reported multiple times — original filing, amendments,
   and restatement in the next period's comparative column — each with a
   different accession number. Fix: ``dedup``.
3. Share counts are as-reported, not split-adjusted. A reverse split makes the
   share count *drop* right when a holder is being diluted, which is the
   opposite of the signal a dilution tracker exists to show. Fix:
   ``split_adjust``.

Every function here takes/returns the SEC's own list-of-dict fact shape
(``{"start"?, "end", "val", "accn", "filed", "form", ...}``) or a
``(date, value)`` tuple list — never a DataFrame — so callers can drop straight
into pandas at the point they actually need it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date
from typing import Any

Fact = dict[str, Any]


def is_duration(fact: Fact) -> bool:
    return "start" in fact and fact["start"] is not None


def _fact_key(fact: Fact) -> tuple:
    return (fact["start"], fact["end"]) if is_duration(fact) else (fact["end"],)


def dedup(facts: Iterable[Fact], key: Callable[[Fact], tuple] | None = None) -> list[Fact]:
    """Collapse duplicate filings of the same period to the most recent one.

    Keeps, for each period key, the fact with the highest ``(filed, accn)`` —
    i.e. the latest-filed version, which is the most-corrected one. Default key
    is ``(start, end)`` for duration facts and ``(end,)`` for instant facts, so
    a single call handles both without the caller distinguishing them.
    """
    key = key or _fact_key
    best: dict[tuple, Fact] = {}
    for fact in facts:
        k = key(fact)
        current = best.get(k)
        if current is None or (fact["filed"], fact["accn"]) > (current["filed"], current["accn"]):
            best[k] = fact
    return sorted(best.values(), key=key)


def discrete_quarters(facts: Iterable[Fact]) -> list[tuple[str, float]]:
    """De-cumulate YTD duration facts into per-quarter values.

    SEC duration facts for a fiscal year all share the same ``start`` (the
    fiscal-year start) and accumulate: Q1 covers 3 months, Q2's fact covers 6
    months, Q3's covers 9, the 10-K's covers 12. Grouping by ``start`` (not by
    calendar year, which breaks for non-calendar fiscal years) and subtracting
    the running prior value recovers each discrete quarter.

    Expects already-deduped facts. Returns ``(end_date, quarterly_value)``
    sorted ascending by end date.
    """
    by_start: dict[str, list[Fact]] = {}
    for fact in facts:
        if not is_duration(fact):
            continue
        by_start.setdefault(fact["start"], []).append(fact)

    # Income-statement concepts are filed both as YTD-cumulative facts and as
    # standalone 3-month facts (different ``start``, same ``end``). Keep one
    # value per end date, preferring the filer's own shortest-span fact: it's
    # the reported discrete quarter, whereas a de-cumulated YTD difference is
    # derived (and non-additive for per-share data like EPS).
    best: dict[str, tuple[int, float]] = {}
    for group in by_start.values():
        group_sorted = sorted(group, key=lambda f: f["end"])
        prior = 0.0
        for fact in group_sorted:
            span = (date.fromisoformat(fact["end"]) - date.fromisoformat(fact["start"])).days
            cur = best.get(fact["end"])
            if cur is None or span < cur[0]:
                best[fact["end"]] = (span, fact["val"] - prior)
            prior = fact["val"]

    return sorted(((e, v) for e, (_, v) in best.items()), key=lambda q: q[0])


def ttm(quarters: list[tuple[str, float]], n: int = 4) -> float | None:
    """Trailing-``n``-quarter sum. ``None`` if fewer than ``n`` quarters exist —
    don't annualize a lumpy small-cap off one or two data points."""
    if len(quarters) < n:
        return None
    return sum(v for _, v in quarters[-n:])


def annual_totals(facts: Iterable[Fact]) -> list[tuple[str, float]]:
    """Full fiscal-year duration facts (10-K totals), no de-cumulation needed —
    these already cover a single ~12-month span. Useful for CAGR comparisons
    where quarterly granularity isn't needed. Expects already-deduped facts.
    """
    out = []
    for fact in facts:
        if not is_duration(fact):
            continue
        start = date.fromisoformat(fact["start"])
        end = date.fromisoformat(fact["end"])
        if 350 <= (end - start).days <= 380:
            out.append((fact["end"], fact["val"]))
    return sorted(out, key=lambda r: r[0])


def as_of(facts: Iterable[Fact], when: str) -> list[Fact]:
    """Drop facts filed after ``when`` (an ISO date string).

    A no-op today — nothing in a live feed is filed in the future — but it's
    the one place "index by filing date, not period end" lives, so a future
    backtest has a single seam to call instead of re-deriving the rule. Using
    a Q4 number on Dec 31 when it wasn't actually filed until February is
    look-ahead bias; this is where that gets fixed once something needs it.
    """
    return [f for f in facts if f["filed"] <= when]


def latest_instant(facts: Iterable[Fact]) -> Fact | None:
    """Most recent already-deduped instant fact (e.g. cash balance, shares out)."""
    deduped = dedup(facts)
    return deduped[-1] if deduped else None


def split_adjust(
    series: list[tuple[str, float]], splits: list[tuple[str, float]]
) -> list[tuple[str, float, float]]:
    """Restate a historical share-count series in today's post-split terms.

    ``series`` is ``(date, raw_shares)`` pairs. ``splits`` is ``(ex_date,
    ratio)`` pairs as yfinance reports them (2.0 for a 2-for-1 split, 0.1 for a
    1-for-10 reverse split). A data point dated before a split needs to be
    multiplied by that split's ratio to be comparable to points reported after
    it; a data point dated on/after a split is already stated post-split.

    Returns ``(date, raw, adjusted)`` triples, sorted by date.
    """
    splits_sorted = sorted(splits, key=lambda s: s[0])

    def factor_for(date: str) -> float:
        factor = 1.0
        for split_date, ratio in splits_sorted:
            if split_date > date:
                factor *= ratio
        return factor

    out = [(date, val, val * factor_for(date)) for date, val in series]
    return sorted(out, key=lambda r: r[0])
