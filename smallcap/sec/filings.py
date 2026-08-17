"""10-K/10-Q filing listing and narrative-text search (going-concern language).

Everything in ``normalize.py``/``client.py`` before this file dealt in XBRL
numbers. This module reads the actual filing document text — the free EDGAR
equivalent of "open the 10-K and Ctrl-F".
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from smallcap.sec import client

GOING_CONCERN_PATTERNS = (
    "substantial doubt",
    "going concern",
)

# Item numbers worth naming in an alert; anything else still shows, just as
# a bare number. Not exhaustive — see Form 8-K's instructions for the rest.
ITEM_8K_RE = re.compile(r"item\s+(\d\.\d{2})\b", re.IGNORECASE)
ITEM_8K_LABELS = {
    "1.01": "entry into a material agreement",
    "1.02": "termination of a material agreement",
    "2.01": "completion of acquisition/disposition of assets",
    "2.02": "results of operations and financial condition",
    "2.04": "triggering event under a financial obligation",
    "3.01": "notice of delisting / failure to satisfy a listing rule",
    "3.02": "unregistered sale of equity securities",
    "4.01": "change in registrant's certifying accountant",
    "4.02": "non-reliance on previously issued financials",
    "5.01": "change in control of registrant",
    "5.02": "departure/appointment of a director or officer",
    "5.03": "amendment to articles of incorporation or bylaws",
    "5.07": "submission of matters to a vote of security holders",
    "7.01": "Reg FD disclosure",
    "8.01": "other events",
    "9.01": "financial statements and exhibits",
}


@dataclass
class Filing:
    form: str
    filing_date: str
    report_date: str
    accession_number: str
    primary_document: str


def list_filings(cik: int, forms: tuple[str, ...] = ("10-K", "10-Q"), *, limit: int | None = None, refresh: bool = False) -> list[Filing]:
    """Recent filings of the given form type(s), most recent first. See
    ``client.py``'s module docstring for the "recent only" caveat."""
    data = client.submissions(cik, refresh=refresh)
    recent = data["filings"]["recent"]
    out = []
    for i in range(len(recent["form"])):
        if recent["form"][i] not in forms:
            continue
        out.append(
            Filing(
                form=recent["form"][i],
                filing_date=recent["filingDate"][i],
                report_date=recent["reportDate"][i],
                accession_number=recent["accessionNumber"][i],
                primary_document=recent["primaryDocument"][i],
            )
        )
    if limit is not None:
        out = out[:limit]
    return out


def latest_filing(cik: int, forms: tuple[str, ...] = ("10-K", "10-Q"), *, refresh: bool = False) -> Filing | None:
    filings = list_filings(cik, forms, limit=1, refresh=refresh)
    return filings[0] if filings else None


def filing_text(cik: int, filing: Filing, *, refresh: bool = False) -> str:
    """Filing document with HTML tags stripped to plain text."""
    raw = client.filing_document(cik, filing.accession_number, filing.primary_document, refresh=refresh)
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class GoingConcernResult:
    checked: bool
    filing: Filing | None
    found: bool
    snippet: str | None


def _going_concern_in_filing(cik: int, filing: Filing, *, refresh: bool = False) -> GoingConcernResult:
    text = filing_text(cik, filing, refresh=refresh)
    lower = text.lower()
    for pattern in GOING_CONCERN_PATTERNS:
        idx = lower.find(pattern)
        if idx != -1:
            start = max(0, idx - 150)
            snippet = text[start : idx + len(pattern) + 150]
            return GoingConcernResult(checked=True, filing=filing, found=True, snippet=snippet)
    return GoingConcernResult(checked=True, filing=filing, found=False, snippet=None)


def going_concern_check(cik: int, *, refresh: bool = False) -> GoingConcernResult:
    """Search the most recent 10-K/10-Q for going-concern / substantial-doubt
    language. Plain substring match on stripped filing text — no NLP, no
    judgment about severity. A hit means "these words appear somewhere in the
    filing", not "the auditor issued a going-concern opinion this quarter";
    read the snippet."""
    filing = latest_filing(cik, refresh=refresh)
    if filing is None:
        return GoingConcernResult(checked=False, filing=None, found=False, snippet=None)
    return _going_concern_in_filing(cik, filing, refresh=refresh)


def going_concern_newly_appeared(cik: int, *, refresh: bool = False) -> GoingConcernResult | None:
    """Like ``going_concern_check``, but only reports ``found=True`` when the
    language wasn't already present in the *prior* 10-K/10-Q — an alert
    pipeline calling ``going_concern_check`` every review re-fires the same
    finding every quarter it persists, which is exactly the "fires constantly,
    gets ignored" failure mode. Returns ``None`` if there's no filing at all.
    """
    filings = list_filings(cik, refresh=refresh, limit=2)
    if not filings:
        return None
    latest = _going_concern_in_filing(cik, filings[0], refresh=refresh)
    if not latest.found or len(filings) < 2:
        return latest
    previous = _going_concern_in_filing(cik, filings[1], refresh=refresh)
    if previous.found:
        return GoingConcernResult(checked=True, filing=latest.filing, found=False, snippet=None)
    return latest


def eight_k_item_types(cik: int, filing: Filing, *, refresh: bool = False) -> set[str]:
    """Item numbers (e.g. "5.02") disclosed in one 8-K's primary document."""
    text = filing_text(cik, filing, refresh=refresh)
    return {m.group(1) for m in ITEM_8K_RE.finditer(text)}


@dataclass
class New8KItemsResult:
    checked: bool
    filing: Filing | None
    new_items: set[str]
    known_items: set[str]


def new_8k_item_types(cik: int, *, lookback: int = 15, refresh: bool = False) -> New8KItemsResult:
    """Item numbers in the most recent 8-K that don't appear in any of the
    filer's other recent 8-Ks — "this company has never disclosed this kind
    of event before." ``lookback`` bounds how many prior 8-Ks are checked
    (each is a separate document fetch, cached after the first run)."""
    filings = list_filings(cik, forms=("8-K",), limit=lookback, refresh=refresh)
    if not filings:
        return New8KItemsResult(checked=False, filing=None, new_items=set(), known_items=set())

    latest, *older = filings
    latest_items = eight_k_item_types(cik, latest, refresh=refresh)
    known: set[str] = set()
    for f in older:
        known |= eight_k_item_types(cik, f, refresh=refresh)

    return New8KItemsResult(checked=True, filing=latest, new_items=latest_items - known, known_items=known)
