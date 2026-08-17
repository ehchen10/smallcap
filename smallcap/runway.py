"""Project 3: runway calculator.

Cash divided by trailing operating cash burn. For an unprofitable small cap
this is often the entire investment question, and it's four lines of code once
the XBRL facts are correctly normalized (see sec/normalize.py) — the naive
version, summing raw YTD cash-flow facts, overstates burn by up to ~2.5x.

Verified end-to-end against CIK 1174940 (Oragenics, ticker OGEN): cash
$6,107,078 (as of 2026-03-31) over TTM OCF -$9,334,941 -> 2.6 quarters.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from smallcap.sec import client, concepts, normalize


@dataclass
class RunwayResult:
    ticker: str
    cash: float
    cash_as_of: str
    cash_filed: str
    cash_tag_used: str
    ocf_tag_used: str
    ttm_burn: float | None  # positive number = burn rate (already sign-flipped)
    quarters: list[tuple[str, float]]
    runway_quarters_ttm: float | None
    runway_quarters_worst: float | None
    latest_quarter_vs_ttm_avg: float | None  # >1 = burning faster than trailing average
    profitable: bool
    cash_staleness_days: int


def compute(ticker: str, *, refresh: bool = False) -> RunwayResult:
    cik = client.cik_for_ticker(ticker, refresh=refresh)
    cf = client.companyfacts(cik, refresh=refresh)

    cash_resolved = concepts.resolve(cf, concepts.CASH)
    if cash_resolved is None:
        raise ValueError(f"{ticker}: no cash concept found in any fallback tag")
    latest_cash = normalize.latest_instant(cash_resolved.facts)
    if latest_cash is None:
        raise ValueError(f"{ticker}: cash concept present but no facts")

    ocf_resolved = concepts.resolve(cf, concepts.OPERATING_CASH_FLOW)
    if ocf_resolved is None:
        raise ValueError(f"{ticker}: no operating-cash-flow concept found in any fallback tag")
    ocf_deduped = normalize.dedup(ocf_resolved.facts)
    quarters = normalize.discrete_quarters(ocf_deduped)

    ttm = normalize.ttm(quarters, n=4)
    profitable = ttm is not None and ttm >= 0

    cash = float(latest_cash["val"])
    ttm_burn = -ttm if ttm is not None else None

    if ttm_burn is None:
        runway_ttm = None  # insufficient quarterly history to compute a TTM figure
    elif ttm_burn <= 0:
        runway_ttm = float("inf")  # cash-flow positive on a TTM basis
    else:
        runway_ttm = cash / (ttm_burn / 4)  # runway is in quarters; ttm_burn is annual
    worst_q = min((v for _, v in quarters[-4:]), default=None) if len(quarters) >= 4 else None
    runway_worst = cash / -worst_q if worst_q is not None and worst_q < 0 else None

    latest_vs_avg = None
    if len(quarters) >= 4 and ttm_burn and ttm_burn > 0:
        latest_burn = -quarters[-1][1]
        avg_burn = ttm_burn / 4
        if avg_burn > 0:
            latest_vs_avg = latest_burn / avg_burn

    staleness = (date.today() - date.fromisoformat(latest_cash["filed"])).days

    return RunwayResult(
        ticker=ticker.upper(),
        cash=cash,
        cash_as_of=latest_cash["end"],
        cash_filed=latest_cash["filed"],
        cash_tag_used=f"{cash_resolved.taxonomy}:{cash_resolved.tag}",
        ocf_tag_used=f"{ocf_resolved.taxonomy}:{ocf_resolved.tag}",
        ttm_burn=ttm_burn,
        quarters=quarters,
        runway_quarters_ttm=runway_ttm,
        runway_quarters_worst=runway_worst,
        latest_quarter_vs_ttm_avg=latest_vs_avg,
        profitable=profitable,
        cash_staleness_days=staleness,
    )


def format_report(r: RunwayResult) -> str:
    lines = [f"{r.ticker} — runway", f"  cash: ${r.cash:,.0f} (as of {r.cash_as_of}, filed {r.cash_filed}, {r.cash_staleness_days}d ago; tag {r.cash_tag_used})"]
    if r.profitable:
        lines.append(f"  TTM operating cash flow: +${-r.ttm_burn:,.0f} (positive — profitable on a cash basis, no runway concern)")
        return "\n".join(lines)
    if r.ttm_burn is None:
        lines.append("  fewer than 4 quarters of OCF history — cannot compute a TTM burn rate")
        return "\n".join(lines)
    lines.append(f"  TTM burn: ${r.ttm_burn:,.0f}/yr (${r.ttm_burn/4:,.0f}/quarter avg; tag {r.ocf_tag_used})")
    lines.append(f"  runway (TTM avg burn): {r.runway_quarters_ttm:.1f} quarters")
    if r.runway_quarters_worst is not None:
        lines.append(f"  runway (worst of last 4 quarters): {r.runway_quarters_worst:.1f} quarters")
    if r.latest_quarter_vs_ttm_avg is not None:
        trend = "faster" if r.latest_quarter_vs_ttm_avg > 1.05 else ("slower" if r.latest_quarter_vs_ttm_avg < 0.95 else "in line with")
        lines.append(f"  latest quarter burning {trend} the TTM average ({r.latest_quarter_vs_ttm_avg:.2f}x)")
    if r.cash_staleness_days > 60:
        lines.append(f"  ! cash figure is {r.cash_staleness_days} days old — runway today is shorter than shown")
    return "\n".join(lines)
