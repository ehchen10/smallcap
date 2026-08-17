"""Pure-logic tests for adjust.py's residualize() — no network."""

from __future__ import annotations

import pytest

from smallcap.adjust import residualize


def test_demeans_within_sector():
    raw = {"A": 0.30, "B": 0.10, "C": 0.20, "D": 0.00}
    sectors = {"A": "biotech", "B": "biotech", "C": "industrial", "D": "industrial"}
    out = residualize(raw, sectors)
    # biotech mean 0.20, industrial mean 0.10
    assert out["A"].residual == pytest.approx(0.10)
    assert out["B"].residual == pytest.approx(-0.10)
    assert out["C"].residual == pytest.approx(0.10)
    assert out["D"].residual == pytest.approx(-0.10)


def test_z_scores_are_standardized_across_universe():
    raw = {"A": 0.30, "B": 0.10, "C": 0.20, "D": 0.00}
    sectors = {"A": "biotech", "B": "biotech", "C": "industrial", "D": "industrial"}
    out = residualize(raw, sectors)
    residuals = [s.residual for s in out.values()]
    zs = [s.z_score for s in out.values()]
    std = (sum((r - 0) ** 2 for r in residuals) / len(residuals)) ** 0.5
    for r, z in zip(residuals, zs):
        assert z == pytest.approx(r / std)


def test_thin_sectors_bucketed_into_other():
    raw = {"A": 0.30, "B": 0.10, "C": 0.20, "D": 0.05, "E": 0.00}
    sectors = {"A": "biotech", "B": "biotech", "C": "solo-sector", "D": "biotech", "E": "another-solo"}
    out = residualize(raw, sectors, min_sector_n=2)
    assert out["C"].sector_bucket == "other"
    assert out["E"].sector_bucket == "other"
    assert out["A"].sector_bucket == "biotech"


def test_size_regression_removes_linear_size_effect():
    # raw value is an exact linear function of log(size); after size
    # adjustment the residual should collapse to ~0 for every name.
    import math

    sizes = {"A": 1e6, "B": 1e7, "C": 1e8, "D": 1e9}
    raw = {t: 0.5 * math.log(sizes[t]) for t in sizes}
    sectors = {t: "same-sector" for t in sizes}  # sector term contributes nothing
    out = residualize(raw, sectors, sizes)
    for s in out.values():
        assert s.residual == pytest.approx(0.0, abs=1e-9)


def test_missing_size_for_some_tickers_still_demeans():
    raw = {"A": 0.30, "B": 0.10, "C": 0.20, "D": 0.00}
    sectors = {"A": "biotech", "B": "biotech", "C": "industrial", "D": "industrial"}
    sizes = {"A": 1e6, "B": 1e7}  # C, D missing
    out = residualize(raw, sectors, sizes)
    assert set(out) == {"A", "B", "C", "D"}


def test_raises_below_three_tickers():
    with pytest.raises(ValueError):
        residualize({"A": 0.1, "B": 0.2}, {"A": "x", "B": "y"})


def test_tickers_without_sector_are_dropped_not_errored():
    raw = {"A": 0.1, "B": 0.2, "C": 0.3, "D": 0.4}
    sectors = {"A": "x", "B": "y", "C": "z"}  # D has no sector
    out = residualize(raw, sectors)
    assert set(out) == {"A", "B", "C"}
