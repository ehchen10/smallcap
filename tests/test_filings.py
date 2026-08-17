"""Pure-logic tests for sec/filings.py's 8-K item-number regex — no network."""

from __future__ import annotations

from smallcap.sec.filings import ITEM_8K_RE


def test_matches_standard_item_heading():
    text = "Item 5.02 Departure of Directors or Certain Officers ..."
    assert {m.group(1) for m in ITEM_8K_RE.finditer(text)} == {"5.02"}


def test_matches_multiple_items_case_insensitive():
    text = "ITEM 1.01 Entry into a Material Agreement ... item 2.02 Results of Operations"
    assert {m.group(1) for m in ITEM_8K_RE.finditer(text)} == {"1.01", "2.02"}


def test_no_match_on_unrelated_text():
    text = "The item purchased for $1.01 was returned."
    assert ITEM_8K_RE.findall(text) == []
