"""Tag fallback chains: companies tag the same economic fact under different
XBRL concepts, and small caps are worse about it than large caps. Each chain
is tried in order; the first tag present in a filer's companyfacts wins, and
callers get told which one so results stay auditable.

Verified against CIK 1174940 (Oragenics): has the primary tag for shares,
cash, and operating cash flow, but *not* the restricted-cash cash variant —
so the fallback chain isn't hypothetical, it's needed on a real small cap.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from smallcap.sec.normalize import Fact

# Each entry is (taxonomy, tag).
SHARES_OUTSTANDING: list[tuple[str, str]] = [
    # dei is tried first because it's structurally immune to the split-era
    # restatement mismatch described in dilution.py/screener.py's docstrings —
    # it's a cover-page "as of near this filing" fact, never a reprinted
    # historical comparative. The us-gaap fallbacks below don't have that
    # property: a filer missing the dei tag (rare, but possible for older or
    # non-standard filings) loses that protection.
    ("dei", "EntityCommonStockSharesOutstanding"),
    ("us-gaap", "CommonStockSharesOutstanding"),
    ("us-gaap", "CommonStockSharesIssued"),
]

CASH: list[tuple[str, str]] = [
    ("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
    ("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    ("us-gaap", "CashAndCashEquivalentsAtCarryingValueIncludingDiscontinuedOperations"),
]

OPERATING_CASH_FLOW: list[tuple[str, str]] = [
    ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
    ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
]

DILUTED_SHARES: list[tuple[str, str]] = [
    ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
]

REVENUE: list[tuple[str, str]] = [
    ("us-gaap", "Revenues"),
    ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
    ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
]

NET_INCOME: list[tuple[str, str]] = [
    ("us-gaap", "NetIncomeLoss"),
    ("us-gaap", "ProfitLoss"),
]

# Most filers never report this tag at all — that's expected (no restructuring
# activity), not a data gap, so callers should treat "not found" as "clean",
# unlike CASH/OPERATING_CASH_FLOW where absence means something's wrong.
RESTRUCTURING_CHARGES: list[tuple[str, str]] = [
    ("us-gaap", "RestructuringCharges"),
]

# Preferred unit key per fact, tried in this order before falling back to
# "whatever unit is present".
_PREFERRED_UNITS = ("USD", "shares", "USD/shares")


class ResolvedConcept(NamedTuple):
    facts: list[Fact]
    taxonomy: str
    tag: str
    unit: str


def resolve(company_facts: dict[str, Any], chain: list[tuple[str, str]]) -> ResolvedConcept | None:
    """Walk a fallback chain against a companyfacts payload's ``facts`` dict.

    Returns the first present concept's raw fact list plus which
    taxonomy/tag/unit was actually used, or ``None`` if nothing in the chain
    is reported by this filer.
    """
    facts_by_taxonomy = company_facts.get("facts", company_facts)
    for taxonomy, tag in chain:
        node = facts_by_taxonomy.get(taxonomy, {}).get(tag)
        if not node:
            continue
        units = node.get("units", {})
        if not units:
            continue
        for unit in _PREFERRED_UNITS:
            if unit in units:
                return ResolvedConcept(units[unit], taxonomy, tag, unit)
        unit, facts = next(iter(units.items()))
        return ResolvedConcept(facts, taxonomy, tag, unit)
    return None
