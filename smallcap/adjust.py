"""Cross-sectional noise reduction: residualize a raw metric against
confounds (sector, size) so what's left is the part not explained by
"which industry is this" or "how big is this company" — the same idea
as a Fama-French/Barra factor-neutral score, built from free SEC/yfinance
data (SIC code, market cap) instead of a commercial risk model.

Only two confounds today: sector (SIC description, from submissions())
and size (market cap = last close x latest split-adjusted shares out).
Anything not explained by those two stays in the residual — including
real signal, genuine noise, and any confound the pipeline doesn't model
yet. That's deliberately where a future "hype" factor plugs in: another
regressor added to the same equation, not a separate system bolted on
after the fact.

Method:
1. De-mean within sector — removes "biotechs dilute more than
   industrials" as a component. Sectors with fewer than ``min_sector_n``
   names in the given universe get bucketed into "other" first, since a
   1-company "sector mean" is just the raw value restated.
2. If size data covers at least 3 names, also net out a linear effect of
   log(size) via least squares (plain numpy, no new dependency — numpy
   ships with pandas, already a project dependency).
3. Residuals are z-scored against the *whole* universe passed in, not
   per-sector, so scores stay comparable across sectors.

This is a small-n heuristic, not a calibrated model: with a 10-20 ticker
watchlist, "sector mean" is a mean of 2-3 companies, not a population
estimate. Treat the z-score as "how unusual this reading is relative to
the closest comparables available," not a statistically rigorous
p-value.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from smallcap import dilution, prices
from smallcap.sec import client


@dataclass
class AdjustedScore:
    ticker: str
    raw_value: float
    sector: str
    sector_bucket: str  # sector, or "other" if too few peers in this universe
    size: float | None
    sector_mean: float
    residual: float
    z_score: float


def residualize(
    raw_values: dict[str, float],
    sectors: dict[str, str],
    sizes: dict[str, float] | None = None,
    *,
    min_sector_n: int = 2,
) -> dict[str, AdjustedScore]:
    """``raw_values``/``sectors``/``sizes`` are keyed by ticker. ``sizes`` is
    optional; when given, tickers missing a size still get sector-demeaned,
    just not size-adjusted (their log_size is imputed to the group mean,
    contributing zero to the size term)."""
    tickers = [t for t in raw_values if t in sectors]
    if len(tickers) < 3:
        raise ValueError("need at least 3 tickers with both a value and a sector to residualize")

    df = pd.DataFrame({
        "ticker": tickers,
        "raw": [raw_values[t] for t in tickers],
        "sector": [sectors[t] for t in tickers],
    })

    counts = df["sector"].value_counts()
    thin = set(counts[counts < min_sector_n].index)
    df["sector_bucket"] = df["sector"].where(~df["sector"].isin(thin), "other")

    sector_means = df.groupby("sector_bucket")["raw"].transform("mean")
    demeaned = df["raw"] - sector_means

    has_size = sizes is not None and sum(1 for t in tickers if sizes.get(t)) >= 3
    if has_size:
        log_size = pd.Series(
            [np.log(sizes[t]) if sizes.get(t) and sizes[t] > 0 else np.nan for t in tickers],
            index=df.index,
        )
        log_size = log_size.fillna(log_size.mean())
        x = log_size - log_size.mean()
        denom = float((x**2).sum())
        beta = float((x * demeaned).sum() / denom) if denom else 0.0
        residual = demeaned - beta * x
    else:
        residual = demeaned

    std = float(residual.std(ddof=0))
    z = residual / std if std else residual * 0.0

    out: dict[str, AdjustedScore] = {}
    for i, t in enumerate(tickers):
        out[t] = AdjustedScore(
            ticker=t,
            raw_value=float(df["raw"].iloc[i]),
            sector=df["sector"].iloc[i],
            sector_bucket=df["sector_bucket"].iloc[i],
            size=sizes.get(t) if sizes else None,
            sector_mean=float(sector_means.iloc[i]),
            residual=float(residual.iloc[i]),
            z_score=float(z.iloc[i]),
        )
    return out


def dilution_adjusted(tickers: list[str], *, refresh: bool = False) -> list[AdjustedScore]:
    """Annualized dilution rate (``dilution.compute``'s ``share_cagr``),
    adjusted for sector and company size. Silently skips any ticker
    ``dilution.compute`` can't resolve a share-count concept for, or that
    has no CAGR (too little history) — consistent with how ``screener.py``
    treats missing data as "no number," not an error."""
    raw: dict[str, float] = {}
    sectors: dict[str, str] = {}
    sizes: dict[str, float] = {}

    for ticker in tickers:
        t = ticker.upper()
        try:
            d = dilution.compute(t, refresh=refresh)
            if d.share_cagr is None:
                continue
            cik = client.cik_for_ticker(t, refresh=refresh)
            sub = client.submissions(cik, refresh=refresh)
            raw[t] = d.share_cagr
            sectors[t] = sub.get("sicDescription") or "unknown"

            hist = prices.price_history(t, period="5d")
            if not hist.empty:
                last_close = float(hist["Close"].iloc[-1])
                sizes[t] = last_close * d.adjusted_shares[-1]
        except Exception:
            continue

    scored = residualize(raw, sectors, sizes if sizes else None)
    return sorted(scored.values(), key=lambda s: s.z_score)


def format_report(scores: list[AdjustedScore]) -> str:
    if not scores:
        return "no tickers resolved"
    lines = [f"{'ticker':8s} {'sector':30s} {'raw':>9s} {'sector_mean':>12s} {'z_score':>8s}"]
    for s in scores:
        bucket = f" [{s.sector_bucket}]" if s.sector_bucket != s.sector else ""
        lines.append(
            f"{s.ticker:8s} {s.sector[:30]:30s} {s.raw_value:+8.1%} {s.sector_mean:+11.1%} {s.z_score:+8.2f}{bucket}"
        )
    return "\n".join(lines)
