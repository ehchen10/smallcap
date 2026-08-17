"""Form 4 (insider transaction) listing and parsing.

Form 3/4/5 filings are XML, not XBRL — a different shape from everything else
in ``sec/``. ``submissions()`` lists them with a ``primaryDocument`` pointing
at an XSL-rendered *display* path (``xslF345X05/primary_doc.xml``); the raw
XML behind that render is what actually gets parsed here (see
``client.filing_document``'s docstring for why the same call handles both).

Transaction codes worth knowing (full list in SEC's Form 4 instructions):
P = open-market purchase, S = open-market sale, A = grant/award,
M = option exercise, F = shares withheld for taxes, G = gift, C = conversion.
``P``/``S`` are the ones that read as a genuine buy/sell signal; the rest are
compensation mechanics.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date as date_cls

from smallcap.sec import client
from smallcap.sec.filings import Filing, list_filings

OPEN_MARKET_CODES = ("P", "S")


@dataclass
class Transaction:
    owner_name: str
    owner_title: str
    is_director: bool
    is_officer: bool
    is_ten_pct_owner: bool
    security_title: str
    transaction_date: str
    transaction_code: str
    acquired_disposed: str | None  # "A" or "D"
    shares: float | None
    price_per_share: float | None
    shares_owned_after: float | None
    derivative: bool
    filing_date: str
    accession_number: str


def _text(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    found = el.find(path)
    return found.text.strip() if found is not None and found.text else None


def _float(el: ET.Element | None, path: str) -> float | None:
    raw = _text(el, path)
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def list_form4(cik: int, *, limit: int | None = None, refresh: bool = False) -> list[Filing]:
    """Recent Form 4 filings, most recent first."""
    return list_filings(cik, forms=("4",), limit=limit, refresh=refresh)


def parse_form4(cik: int, filing: Filing, *, refresh: bool = False) -> list[Transaction]:
    """All transactions (non-derivative and derivative) reported in one Form 4."""
    raw = client.filing_document(cik, filing.accession_number, filing.primary_document, refresh=refresh)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []

    owner_name = _text(root, ".//reportingOwnerId/rptOwnerName") or "?"
    rel = root.find(".//reportingOwnerRelationship")
    owner_title = _text(rel, "officerTitle") or ""
    is_director = _text(rel, "isDirector") == "1"
    is_officer = _text(rel, "isOfficer") == "1"
    is_ten_pct = _text(rel, "isTenPercentOwner") == "1"

    out = []
    for derivative, table, tag in (
        (False, root.find("nonDerivativeTable"), "nonDerivativeTransaction"),
        (True, root.find("derivativeTable"), "derivativeTransaction"),
    ):
        if table is None:
            continue
        for tx in table.findall(tag):
            out.append(
                Transaction(
                    owner_name=owner_name,
                    owner_title=owner_title,
                    is_director=is_director,
                    is_officer=is_officer,
                    is_ten_pct_owner=is_ten_pct,
                    security_title=_text(tx, "securityTitle/value") or "?",
                    transaction_date=_text(tx, "transactionDate/value") or filing.report_date,
                    transaction_code=_text(tx, "transactionCoding/transactionCode") or "?",
                    acquired_disposed=_text(tx, "transactionAmounts/transactionAcquiredDisposedCode/value"),
                    shares=_float(tx, "transactionAmounts/transactionShares/value"),
                    price_per_share=_float(tx, "transactionAmounts/transactionPricePerShare/value"),
                    shares_owned_after=_float(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value"),
                    derivative=derivative,
                    filing_date=filing.filing_date,
                    accession_number=filing.accession_number,
                )
            )
    return out


def recent_transactions(cik: int, *, limit: int = 20, open_market_only: bool = False, refresh: bool = False) -> list[Transaction]:
    """Transactions across the most recent Form 4 filings, newest first.
    ``limit`` bounds the number of *filings* fetched, not transactions
    returned — one filing can report several."""
    txs: list[Transaction] = []
    for filing in list_form4(cik, limit=limit, refresh=refresh):
        txs.extend(parse_form4(cik, filing, refresh=refresh))
    if open_market_only:
        txs = [t for t in txs if t.transaction_code in OPEN_MARKET_CODES]
    return sorted(txs, key=lambda t: t.transaction_date, reverse=True)


@dataclass
class BuyCluster:
    start_date: str
    end_date: str
    owners: list[str]


def cluster_buys(transactions: list[Transaction], *, min_insiders: int = 2, window_days: int = 30) -> list[BuyCluster]:
    """Windows of >= ``min_insiders`` distinct insiders reporting open-market
    ``P`` purchases within ``window_days`` of each other — the "multiple
    people who'd know are buying at once" signal, distinct from any single
    purchase. Greedy non-overlapping scan over dates ascending: a cluster
    consumes every buy up to its window edge before starting the next one, so
    one stretch of activity isn't reported as several overlapping clusters.
    Returns clusters oldest first.
    """
    buys = sorted((t for t in transactions if t.transaction_code == "P"), key=lambda t: t.transaction_date)
    clusters: list[BuyCluster] = []
    i, n = 0, len(buys)
    while i < n:
        start = date_cls.fromisoformat(buys[i].transaction_date)
        j = i
        owners: dict[str, None] = {}
        while j < n and (date_cls.fromisoformat(buys[j].transaction_date) - start).days <= window_days:
            owners[buys[j].owner_name] = None
            j += 1
        if len(owners) >= min_insiders:
            clusters.append(BuyCluster(start_date=buys[i].transaction_date, end_date=buys[j - 1].transaction_date, owners=list(owners)))
            i = j
        else:
            i += 1
    return clusters
