"""Per-ticker headline fundamentals: last FY + last quarter revenue, net income,
EPS, plus P/S (market cap / TTM revenue). Market cap = yfinance price x latest
dei cover-page shares (not split-era-fragile, see concepts.SHARES_OUTSTANDING).
"""

from __future__ import annotations

from dataclasses import dataclass

import yfinance as yf

from smallcap.sec import client, concepts, normalize


@dataclass
class Fundamentals:
    ticker: str
    fy_end: str | None = None
    fy_revenue: float | None = None
    fy_net_income: float | None = None
    fy_eps: float | None = None
    q_end: str | None = None
    q_revenue: float | None = None
    q_net_income: float | None = None
    q_eps: float | None = None
    ttm_revenue: float | None = None
    ttm_eps: float | None = None
    pe: float | None = None
    shares: float | None = None
    price: float | None = None
    market_cap: float | None = None
    ps: float | None = None
    tags: dict | None = None


def _freshest(facts, chain):
    """Like ``concepts.resolve`` but picks the chain tag with the most recent
    data, not just the first one present. Filers switch tags over time (e.g.
    Revenues -> RevenueFromContractWithCustomer...), leaving the old tag as a
    stale but non-empty series — first-present would silently report years-old
    figures. Ties go to the earlier chain entry."""
    best, best_end = None, ""
    for entry in chain:
        r = concepts.resolve(facts, [entry])
        if r is None or not r.facts:
            continue
        end = max(f["end"] for f in r.facts)
        if end > best_end:
            best, best_end = r, end
    return best


def _series(facts, chain, name, tags):
    r = _freshest(facts, chain)
    if r is None:
        tags[name] = None
        return [], []
    tags[name] = f"{r.taxonomy}:{r.tag}"
    d = normalize.dedup(r.facts)
    return normalize.annual_totals(d), normalize.discrete_quarters(d)


def fundamentals(ticker: str, *, with_price: bool = True) -> Fundamentals:
    cik = client.cik_for_ticker(ticker)
    facts = client.companyfacts(cik)
    tags: dict = {}
    out = Fundamentals(ticker=ticker.upper(), tags=tags)

    rev_a, rev_q = _series(facts, concepts.REVENUE, "revenue", tags)
    ni_a, ni_q = _series(facts, concepts.NET_INCOME, "net_income", tags)
    eps_a, eps_q = _series(facts, concepts.EPS_DILUTED, "eps", tags)

    if rev_a:
        out.fy_end, out.fy_revenue = rev_a[-1]
    ni_a_d, eps_a_d = dict(ni_a), dict(eps_a)
    out.fy_net_income = ni_a_d.get(out.fy_end)
    out.fy_eps = eps_a_d.get(out.fy_end)
    if rev_q:
        out.q_end, out.q_revenue = rev_q[-1]
        out.q_net_income = dict(ni_q).get(out.q_end)
        out.q_eps = dict(eps_q).get(out.q_end)
    out.ttm_revenue = normalize.ttm(rev_q)
    out.ttm_eps = normalize.ttm(eps_q)

    sh = concepts.resolve(facts, concepts.SHARES_OUTSTANDING)
    if sh:
        inst = normalize.latest_instant(sh.facts)
        out.shares = inst["val"] if inst else None
    if with_price:
        out.price = yf.Ticker(ticker).fast_info.get("lastPrice")
        # P/E undefined (None) when TTM EPS <= 0 — a negative ratio is meaningless.
        if out.price and out.ttm_eps and out.ttm_eps > 0:
            out.pe = out.price / out.ttm_eps
        if out.price and out.shares:
            out.market_cap = out.price * out.shares
            if out.ttm_revenue:
                out.ps = out.market_cap / out.ttm_revenue
    return out
