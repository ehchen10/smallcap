"""Project 4: screener.

Ranks a universe of tickers on cash runway and per-year dilution using the
``frames`` endpoint — one request per concept covers every filer at once,
instead of one companyfacts request per ticker (5,000 requests for a
Russell 2000-sized universe).

Deliberate design choice vs. runway.py: this uses full-year duration frames
(``CY2024``) rather than stitching four quarterly frames. Verified against
CY2024 OCF: 6,171 filers, zero duplicate CIKs per frame, and non-calendar
fiscal-year filers (e.g. AAR Corp, FY ending May) still resolve correctly — SEC
matches each filer's fiscal year to the nearest label, not by exact calendar
bounds. Stitching quarterly *duration* frames instead would reintroduce the
YTD-cumulation pitfall per filer (see sec/normalize.py) with no per-company
de-cumulation step, since frames returns one pre-selected fact per filer, not
the full history needed to de-cumulate. Full-year frames sidestep that at the
cost of being up to ~12 months stale — an accepted tradeoff for an
idea-generator, not the precision path (use runway.py per-ticker for that).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from smallcap import prices
from smallcap.sec import client, concepts


@dataclass
class ScreenRow:
    ticker: str
    cik: int
    entity_name: str
    cash: float | None
    annual_ocf: float | None  # negative = burning cash
    shares_start: float | None
    shares_end: float | None
    dilution_pct: float | None  # (end - start) / start over the year
    dilution_suspect: bool  # True if dilution_pct was discarded as a likely split-era artifact
    runway_years: float | None  # cash / -annual_ocf, None if profitable or missing


@dataclass
class ScreenResult:
    rows: list[ScreenRow]
    universe_size: int
    matched: int
    period_instant: str
    period_annual_end: str
    period_annual_start: str


def default_annual_periods() -> tuple[str, str]:
    """Most recent full calendar year likely to have complete 10-K coverage,
    and the year before it, for the year-over-year dilution comparison."""
    today = date.today()
    last_full_year = today.year - 1 if today.month >= 4 else today.year - 2
    return f"CY{last_full_year}", f"CY{last_full_year - 1}"


def default_instant_period() -> str:
    today = date.today()
    q = (today.month - 1) // 3  # 0-3, previous completed quarter index roughly
    year, quarter = (today.year, q) if q > 0 else (today.year - 1, 4)
    return f"CY{year}Q{quarter}I"


def _first_present(chain: list[tuple[str, str]]) -> tuple[str, str]:
    return chain[0]


def _year_end_date(period: str) -> str:
    """"CY2024" -> "2024-12-31". Frames period codes are always 4-digit years
    prefixed with "CY"."""
    return f"{period[2:]}-12-31"


def screen(
    tickers: list[str],
    *,
    instant_period: str,
    annual_end_period: str,
    annual_start_period: str,
    max_runway_years: float | None = None,
    max_dilution_pct: float | None = None,
    check_splits: bool = True,
    refresh: bool = False,
) -> ScreenResult:
    """``instant_period`` e.g. "CY2025Q1I"; annual periods e.g. "CY2024"/"CY2023"
    (used to compute year-over-year dilution on shares outstanding).

    ``check_splits=False`` skips the per-ticker yfinance split lookup and leaves
    dilution unset — a cheap frames-only pass for universe-wide prefiltering
    (runway/cash), to be followed by a full pass on the survivors."""
    tmap = client.ticker_map(refresh=refresh)
    wanted = {t.upper(): tmap[t.upper()] for t in tickers if t.upper() in tmap}

    cash_tag_ns, cash_tag = _first_present(concepts.CASH)
    ocf_tag_ns, ocf_tag = _first_present(concepts.OPERATING_CASH_FLOW)
    # us-gaap, not dei, despite dei:EntityCommonStockSharesOutstanding being
    # structurally safer (see dilution.py's docstring: it's a cover-page "as of
    # near this filing" fact, never a retroactively-restated historical
    # comparative, so it can't hit the split-era mismatch this function guards
    # against below). dei does have frames coverage in general, but its dates
    # are the cover-page date, not the fiscal quarter-end — so it essentially
    # never lands on an exact quarter-end frame like "CY2024Q4I" per-ticker.
    # Confirmed empty for OGEN at CY2023/2024/2025 Q4I despite ~2,800 other
    # filers matching that same dei frame. us-gaap's fiscal-period-end dates
    # give usable per-ticker coverage at the cost of the restatement risk.
    shares_tag_ns, shares_tag = _first_present(concepts.SHARES_OUTSTANDING[1:])

    cash_frame = {f["cik"]: f["val"] for f in client.frames(cash_tag, instant_period, taxonomy=cash_tag_ns, refresh=refresh)["data"]}
    ocf_frame = {f["cik"]: f["val"] for f in client.frames(ocf_tag, annual_end_period, taxonomy=ocf_tag_ns, refresh=refresh)["data"]}
    shares_end_frame = {f["cik"]: f["val"] for f in client.frames(shares_tag, annual_end_period + "Q4I", taxonomy=shares_tag_ns, unit="shares", refresh=refresh)["data"]}
    shares_start_frame = {f["cik"]: f["val"] for f in client.frames(shares_tag, annual_start_period + "Q4I", taxonomy=shares_tag_ns, unit="shares", refresh=refresh)["data"]}

    rows: list[ScreenRow] = []
    matched = 0
    for ticker, info in wanted.items():
        cik = info["cik"]
        cash = cash_frame.get(cik)
        ocf = ocf_frame.get(cik)
        s_end = shares_end_frame.get(cik)
        s_start = shares_start_frame.get(cik)

        if cash is not None or ocf is not None:
            matched += 1

        dilution_pct = None
        dilution_suspect = False
        if check_splits and s_end and s_start:
            # `frames` gives one pre-selected fact per (cik, period) with no
            # control over which filing it came from — and SEC retroactively
            # restates comparative-period share counts after a split, so the
            # "2024" fact and the "2025" fact can land in *different* split
            # eras depending on which filings happened to be latest for each.
            # Confirmed on OGEN: frames' 2024-12-31 fact was already restated
            # for the 2025-06-03 split (val matches the pre-split figure ÷30
            # almost exactly) while other periods aren't — so naively
            # split-adjusting the pair can double-count a split that's already
            # baked in. There's no field marking "already restated," and nothing
            # in the frames response lets us pick the original filing instead.
            # Safest option: if any split happened after the window start,
            # don't compute a number — flag it and point at dilution.py, which
            # reconciles a single company's full history instead of one
            # pre-selected fact per period.
            split_events = prices.splits(ticker)
            start_date = _year_end_date(annual_start_period)
            if any(d > start_date for d, _ in split_events):
                dilution_suspect = True
            else:
                dilution_pct = (s_end - s_start) / s_start
        runway_years = cash / -ocf if (cash is not None and ocf is not None and ocf < 0) else None

        rows.append(
            ScreenRow(
                ticker=ticker,
                cik=cik,
                entity_name=info["title"],
                cash=cash,
                annual_ocf=ocf,
                shares_start=s_start,
                shares_end=s_end,
                dilution_pct=dilution_pct,
                dilution_suspect=dilution_suspect,
                runway_years=runway_years,
            )
        )

    if max_runway_years is not None:
        rows = [r for r in rows if r.runway_years is not None and r.runway_years <= max_runway_years]
    if max_dilution_pct is not None:
        rows = [r for r in rows if r.dilution_pct is not None and r.dilution_pct <= max_dilution_pct]

    rows.sort(key=lambda r: (r.runway_years is None, r.runway_years if r.runway_years is not None else float("inf")))

    return ScreenResult(
        rows=rows,
        universe_size=len(tickers),
        matched=matched,
        period_instant=instant_period,
        period_annual_end=annual_end_period,
        period_annual_start=annual_start_period,
    )


def format_report(result: ScreenResult) -> str:
    lines = [
        f"screened {result.universe_size} tickers, {result.matched} matched "
        f"({result.matched / result.universe_size:.0%} coverage) — instant={result.period_instant} "
        f"annual={result.period_annual_start}->{result.period_annual_end}",
        "idea generator only — never a buy signal on its own",
        "",
        f"{'ticker':<8}{'runway(yr)':<12}{'dilution%':<12}{'cash':<16}{'annual OCF':<16}",
    ]
    for r in result.rows:
        runway_s = f"{r.runway_years:.2f}" if r.runway_years is not None else "-"
        if r.dilution_suspect:
            dil_s = "?split?"
        elif r.dilution_pct is not None:
            dil_s = f"{r.dilution_pct*100:.1f}"
        else:
            dil_s = "-"
        cash_s = f"{r.cash:,.0f}" if r.cash is not None else "-"
        ocf_s = f"{r.annual_ocf:,.0f}" if r.annual_ocf is not None else "-"
        lines.append(f"{r.ticker:<8}{runway_s:<12}{dil_s:<12}{cash_s:<16}{ocf_s:<16}")
    if any(r.dilution_suspect for r in result.rows):
        lines.append("\n?split? = a split occurred inside this window; the two share-count facts may be from different restatement eras and aren't safely comparable here — use `smallcap dilution <ticker>` instead")
    return "\n".join(lines)
