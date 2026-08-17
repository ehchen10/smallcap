"""Earnings-quality checks: two ways the P&L and the cash story can disagree.

- cash flow vs. net income divergence: reported profit isn't the same thing
  as cash in the door. A company can show positive net income while burning
  cash (revenue booked before it's collected, non-cash gains inflating the
  number) — the classic accruals red flag.
- restructuring charges recurring 3+ consecutive fiscal years: calling
  something "one-time" three years running is a labeling problem, not a
  one-time event.

Both reuse sec/normalize.py's de-cumulation and dedup — no new XBRL-handling
code, just a different pair of concepts fed through the same pipeline as
runway.py and dilution.py.

Landmine specific to NetIncomeLoss, confirmed live on OGEN: SEC's pay-vs-
performance rule (Item 402(v), effective 2023) requires DEF 14A proxy filings
to XBRL-tag a "Company Net Income" figure — using the *same*
us-gaap:NetIncomeLoss tag the financial statements use. That figure isn't
guaranteed to match the real GAAP number: OGEN's proxy filed 2026-05-20 tags
NetIncomeLoss as -$1,056,792,000 for FY2024, sitting next to real 10-Q
quarters of -$2M to -$3M. companyfacts() doesn't separate the two, so
resolving NET_INCOME without filtering by form pulls in the proxy's figure
and inflates any TTM sum by ~100x. Fix: keep only facts filed on the actual
financial-statement forms (10-K/10-Q, and their amendments) before this
concept is de-cumulated. CASH/OPERATING_CASH_FLOW aren't in the
pay-vs-performance table, so runway.py/dilution.py don't have this landmine.
"""

from __future__ import annotations

from dataclasses import dataclass

from smallcap.sec import client, concepts, normalize

_FINANCIAL_STATEMENT_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A"}


@dataclass
class DivergenceResult:
    ticker: str
    ni_tag_used: str
    ocf_tag_used: str
    ttm_net_income: float | None
    ttm_ocf: float | None
    divergent: bool
    note: str


def cash_flow_vs_net_income(ticker: str, *, refresh: bool = False) -> DivergenceResult:
    cik = client.cik_for_ticker(ticker, refresh=refresh)
    cf = client.companyfacts(cik, refresh=refresh)

    ni_resolved = concepts.resolve(cf, concepts.NET_INCOME)
    ocf_resolved = concepts.resolve(cf, concepts.OPERATING_CASH_FLOW)
    if ni_resolved is None or ocf_resolved is None:
        raise ValueError(f"{ticker}: missing net-income or operating-cash-flow concept")

    ni_facts = [f for f in ni_resolved.facts if f.get("form") in _FINANCIAL_STATEMENT_FORMS]
    if not ni_facts:
        raise ValueError(f"{ticker}: {ni_resolved.tag} has no facts filed on an actual 10-K/10-Q")

    ni_quarters = normalize.discrete_quarters(normalize.dedup(ni_facts))
    ocf_quarters = normalize.discrete_quarters(normalize.dedup(ocf_resolved.facts))

    ttm_ni = normalize.ttm(ni_quarters, n=4)
    ttm_ocf = normalize.ttm(ocf_quarters, n=4)

    divergent = False
    note = ""
    if ttm_ni is not None and ttm_ocf is not None and ttm_ni > 0:
        if ttm_ocf < 0:
            divergent = True
            note = f"TTM net income is positive (${ttm_ni:,.0f}) but TTM operating cash flow is negative (${ttm_ocf:,.0f})"
        elif ttm_ocf < ttm_ni * 0.5:
            divergent = True
            note = f"TTM operating cash flow (${ttm_ocf:,.0f}) covers less than half of TTM net income (${ttm_ni:,.0f})"

    return DivergenceResult(
        ticker=ticker.upper(),
        ni_tag_used=f"{ni_resolved.taxonomy}:{ni_resolved.tag}",
        ocf_tag_used=f"{ocf_resolved.taxonomy}:{ocf_resolved.tag}",
        ttm_net_income=ttm_ni,
        ttm_ocf=ttm_ocf,
        divergent=divergent,
        note=note,
    )


@dataclass
class RestructuringResult:
    ticker: str
    tag_used: str | None
    annual_charges: list[tuple[str, float]]  # (fiscal-year end date, amount), ascending
    consecutive_years: int  # current streak of nonzero years, most recent first
    recurring: bool  # True once the streak hits 3


def restructuring_recurrence(ticker: str, *, refresh: bool = False) -> RestructuringResult:
    cik = client.cik_for_ticker(ticker, refresh=refresh)
    cf = client.companyfacts(cik, refresh=refresh)

    resolved = concepts.resolve(cf, concepts.RESTRUCTURING_CHARGES)
    if resolved is None:
        return RestructuringResult(ticker=ticker.upper(), tag_used=None, annual_charges=[], consecutive_years=0, recurring=False)

    annual = normalize.annual_totals(normalize.dedup(resolved.facts))
    streak = 0
    for _, val in reversed(annual):
        if val and val > 0:
            streak += 1
        else:
            break

    return RestructuringResult(
        ticker=ticker.upper(),
        tag_used=f"{resolved.taxonomy}:{resolved.tag}",
        annual_charges=annual,
        consecutive_years=streak,
        recurring=streak >= 3,
    )
