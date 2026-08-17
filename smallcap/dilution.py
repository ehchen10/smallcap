"""Project 2: dilution tracker.

Split-adjusted shares outstanding over time, an ownership-decay column (what a
fixed 1.0% stake at t0 shrinks to), and a CAGR comparison against revenue — the
"revenue grew 50%, your stake shrank enough to erase it" check that's invisible
on a price chart.

Ownership-decay math is split-invariant by construction: a real split scales
both a holder's literal share count and shares outstanding by the same factor,
so as long as both sides of the ratio are stated in the same split-adjusted
terms, split events cancel out and only genuine issuance moves the number.

Why this module is safe from a landmine screener.py has to guard against: SEC
retroactively restates a *historical balance-sheet date's* share count when a
later filing reprints it as a comparative column after a split — so two facts
for the same nominal date can be in different split "eras" depending which
filing's dedup won (confirmed on OGEN; see screener.py's docstring for the
full story). concepts.SHARES_OUTSTANDING tries dei:EntityCommonStockSharesOutstanding
first specifically because it doesn't have this failure mode: it's a
cover-page "shares outstanding as of [near this filing's date]" fact, never a
reprinted historical comparative, so every fact is definitionally as-original
at the time it was filed and safe to feed into split_adjust's own
future-multiplication logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from smallcap import prices
from smallcap.sec import client, concepts, normalize


@dataclass
class DilutionResult:
    ticker: str
    tag_used: str
    dates: list[str]
    raw_shares: list[float]
    adjusted_shares: list[float]
    ownership_pct_of_initial: list[float]  # 100.0 at t0, decays as shares are issued
    split_events: list[tuple[str, float]]
    share_cagr: float | None
    revenue_cagr: float | None
    years_covered: float


def compute(ticker: str, years: int = 5, *, refresh: bool = False) -> DilutionResult:
    cik = client.cik_for_ticker(ticker, refresh=refresh)
    cf = client.companyfacts(cik, refresh=refresh)

    resolved = concepts.resolve(cf, concepts.SHARES_OUTSTANDING)
    if resolved is None:
        raise ValueError(f"{ticker}: no share-count concept found in any fallback tag")

    deduped = normalize.dedup(resolved.facts)
    cutoff = date.today().replace(year=date.today().year - years).isoformat()
    windowed = [f for f in deduped if f["end"] >= cutoff]
    if len(windowed) < 2:
        windowed = deduped[-8:]  # fall back to whatever history exists

    raw_series = [(f["end"], float(f["val"])) for f in windowed]

    split_events = prices.splits(ticker)
    adjusted = normalize.split_adjust(raw_series, split_events)  # (date, raw, adjusted)

    dates = [d for d, _, _ in adjusted]
    raw_vals = [r for _, r, _ in adjusted]
    adj_vals = [a for _, _, a in adjusted]

    initial = adj_vals[0]
    ownership_pct = [100.0 * initial / v if v else float("nan") for v in adj_vals]

    share_cagr = _cagr(adj_vals[0], adj_vals[-1], dates[0], dates[-1])

    rev_resolved = concepts.resolve(cf, concepts.REVENUE)
    revenue_cagr = None
    if rev_resolved is not None:
        rev_deduped = normalize.dedup(rev_resolved.facts)
        annual = normalize.annual_totals(rev_deduped)
        annual = [(d, v) for d, v in annual if d >= cutoff] or annual[-2:]
        if len(annual) >= 2:
            revenue_cagr = _cagr(annual[0][1], annual[-1][1], annual[0][0], annual[-1][0])

    years_covered = (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days / 365.25

    return DilutionResult(
        ticker=ticker.upper(),
        tag_used=f"{resolved.taxonomy}:{resolved.tag}",
        dates=dates,
        raw_shares=raw_vals,
        adjusted_shares=adj_vals,
        ownership_pct_of_initial=ownership_pct,
        split_events=split_events,
        share_cagr=share_cagr,
        revenue_cagr=revenue_cagr,
        years_covered=years_covered,
    )


def _cagr(start: float, end: float, start_date: str, end_date: str) -> float | None:
    if start <= 0 or end <= 0:
        return None
    years = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days / 365.25
    if years <= 0:
        return None
    return (end / start) ** (1 / years) - 1
