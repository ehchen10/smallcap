"""Price history and split events via yfinance (free, no key).

Split events are the other half of the dilution-tracker fix: they're what
``sec.normalize.split_adjust`` needs to restate historical share counts in
today's terms. Confirmed on OGEN (Oragenics, CIK 1174940) that yfinance's
split dates line up with the cliffs in the raw XBRL share-count series — e.g.
a 2023-01-23 reverse split (ratio 0.016667, i.e. 1-for-60) matches the exact
date the raw share count collapses from ~116M to 2M.
"""

from __future__ import annotations

import pandas as pd
import yfinance as yf


def price_history(ticker: str, *, period: str = "6y") -> pd.DataFrame:
    """Daily OHLCV, tz-naive index, for overlay charts."""
    hist = yf.Ticker(ticker).history(period=period)
    hist.index = hist.index.tz_localize(None)
    return hist


def splits(ticker: str) -> list[tuple[str, float]]:
    """(ex_date, ratio) pairs, ratio >1 for forward splits, <1 for reverse.

    Empty for tickers yfinance has no corporate-actions data for — callers
    should treat that as "no adjustment available", not "no splits occurred",
    since coverage on thin small caps is inconsistent.
    """
    s = yf.Ticker(ticker).splits
    return [(idx.strftime("%Y-%m-%d"), float(val)) for idx, val in s.items()]
