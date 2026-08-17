"""Pure-logic tests for sec/insiders.py's clustering — no network involved."""

from __future__ import annotations

from smallcap.sec.insiders import Transaction, cluster_buys


def _buy(owner: str, when: str, code: str = "P") -> Transaction:
    return Transaction(
        owner_name=owner,
        owner_title="",
        is_director=False,
        is_officer=False,
        is_ten_pct_owner=False,
        security_title="Common Stock",
        transaction_date=when,
        transaction_code=code,
        acquired_disposed="A",
        shares=1000.0,
        price_per_share=1.5,
        shares_owned_after=5000.0,
        derivative=False,
        filing_date=when,
        accession_number="0001-25-000001",
    )


def test_cluster_requires_min_distinct_insiders():
    txs = [_buy("Alice", "2026-01-01"), _buy("Alice", "2026-01-10")]
    assert cluster_buys(txs) == []  # same insider twice isn't a cluster


def test_cluster_found_within_window():
    txs = [_buy("Alice", "2026-01-01"), _buy("Bob", "2026-01-15")]
    clusters = cluster_buys(txs, window_days=30)
    assert len(clusters) == 1
    assert clusters[0].start_date == "2026-01-01"
    assert clusters[0].end_date == "2026-01-15"
    assert sorted(clusters[0].owners) == ["Alice", "Bob"]


def test_cluster_outside_window_not_found():
    txs = [_buy("Alice", "2026-01-01"), _buy("Bob", "2026-03-01")]
    assert cluster_buys(txs, window_days=30) == []


def test_non_p_codes_ignored():
    txs = [_buy("Alice", "2026-01-01", code="S"), _buy("Bob", "2026-01-05", code="A")]
    assert cluster_buys(txs) == []


def test_two_separate_clusters_not_merged():
    txs = [
        _buy("Alice", "2026-01-01"),
        _buy("Bob", "2026-01-10"),
        _buy("Carol", "2026-06-01"),
        _buy("Dave", "2026-06-10"),
    ]
    clusters = cluster_buys(txs, window_days=30)
    assert len(clusters) == 2
    assert {o for c in clusters for o in c.owners} == {"Alice", "Bob", "Carol", "Dave"}
