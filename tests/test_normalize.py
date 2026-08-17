"""Regression tests for the three correctness fixes in sec/normalize.py,
verified originally against live SEC data for CIK 1174940 (Oragenics, OGEN).
The fixture is a trimmed copy of that real companyfacts payload — these
numbers must not drift.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from smallcap.sec import concepts, normalize

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "oragenics_companyfacts.json").read_text())


def test_dedup_collapses_ocf_duplicates():
    resolved = concepts.resolve(FIXTURE, concepts.OPERATING_CASH_FLOW)
    assert resolved is not None
    assert len(resolved.facts) == 108  # raw fact count, duplicated across amendments/comparatives

    deduped = normalize.dedup(resolved.facts)
    assert len(deduped) == 58  # verified against live SEC data


def test_discrete_quarters_de_cumulates_ytd_facts():
    resolved = concepts.resolve(FIXTURE, concepts.OPERATING_CASH_FLOW)
    deduped = normalize.dedup(resolved.facts)
    quarters = normalize.discrete_quarters(deduped)

    fy2023 = {end: val for end, val in quarters if end.startswith("2023")}
    assert fy2023 == {
        "2023-03-31": -2108695,
        "2023-06-30": -2576628,
        "2023-09-30": -1503012,
        "2023-12-31": -1102545,
    }


def test_ttm_sums_last_four_discrete_quarters():
    resolved = concepts.resolve(FIXTURE, concepts.OPERATING_CASH_FLOW)
    deduped = normalize.dedup(resolved.facts)
    quarters = normalize.discrete_quarters(deduped)

    assert normalize.ttm(quarters) == -9334941  # verified against live SEC data (2026-03-31 TTM)


def test_ttm_returns_none_with_insufficient_history():
    assert normalize.ttm([("2026-01-01", -100.0)], n=4) is None


def test_dedup_keeps_latest_filed_for_duplicate_period():
    facts = [
        {"start": "2023-01-01", "end": "2023-03-31", "val": 100, "filed": "2023-05-01", "accn": "a1"},
        {"start": "2023-01-01", "end": "2023-03-31", "val": 105, "filed": "2023-08-01", "accn": "a2"},  # restated later
    ]
    deduped = normalize.dedup(facts)
    assert len(deduped) == 1
    assert deduped[0]["val"] == 105


def test_split_adjust_scales_pre_split_values_only():
    series = [("2022-01-01", 1000.0), ("2024-01-01", 1000.0)]
    splits = [("2023-01-01", 0.1)]  # 1-for-10 reverse split
    adjusted = normalize.split_adjust(series, splits)

    assert adjusted[0] == ("2022-01-01", 1000.0, 100.0)  # before the split: scaled down
    assert adjusted[1] == ("2024-01-01", 1000.0, 1000.0)  # after the split: untouched


def test_latest_instant_picks_most_recent_end_date():
    resolved = concepts.resolve(FIXTURE, concepts.CASH)
    latest = normalize.latest_instant(resolved.facts)
    assert latest["val"] == 6107078
    assert latest["end"] == "2026-03-31"


def test_annual_totals_filters_to_full_year_spans():
    facts = [
        {"start": "2023-01-01", "end": "2023-03-31", "val": 10},  # quarter, excluded
        {"start": "2023-01-01", "end": "2023-12-31", "val": 40},  # full year, included
    ]
    annual = normalize.annual_totals(facts)
    assert annual == [("2023-12-31", 40)]
