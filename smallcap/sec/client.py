"""Cached, rate-limited HTTP client for SEC's free XBRL/EDGAR endpoints.

No API key. Endpoints:

- ticker -> CIK map          https://www.sec.gov/files/company_tickers.json
- all facts, one company     https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json
- one concept, one company   https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/{taxonomy}/{tag}.json
- one concept, all filers    https://data.sec.gov/api/xbrl/frames/{taxonomy}/{tag}/{unit}/{period}.json
- filing list, one company   https://data.sec.gov/submissions/CIK{cik:010d}.json
- a filing document (10-K/10-Q htm, Form 4 xml, ...)
  https://www.sec.gov/Archives/edgar/data/{cik}/{accession-no-dashes}/{document}

SEC asks for a real identifying User-Agent and <=10 req/s. Every response is
cached to disk keyed by URL, so re-running a command costs nothing until the
TTL expires or --refresh is passed.

``submissions()`` only returns each filer's *recent* filings (SEC caps this at
roughly the last ~1000) — a heavy filer like a large-cap with years of Form 4s
can have older filings roll off into paginated ``filings.files`` pages that
this client does not fetch. Fine for "latest 10-K" / "recent Form 4s"; not a
complete filing history for a decades-old large-cap.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import requests

from smallcap.config import CONFIG, SEC_CACHE_DIR, ensure_dirs

_last_request_time = 0.0


class SecClientError(RuntimeError):
    """Raised for non-200 responses or malformed payloads from SEC endpoints."""


def _cache_path(url: str, *, suffix: str = "json") -> Path:
    digest = hashlib.sha256(url.encode()).hexdigest()[:24]
    return SEC_CACHE_DIR / f"{digest}.{suffix}"


def _rate_limit() -> None:
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    wait = CONFIG.sec_min_interval_seconds - elapsed
    if wait > 0:
        time.sleep(wait)
    _last_request_time = time.monotonic()


def _get_json(url: str, *, refresh: bool = False, ttl_seconds: int | None = None) -> Any:
    """Fetch ``url`` as JSON, using the on-disk cache unless ``refresh`` is set."""
    ensure_dirs()
    ttl = CONFIG.sec_cache_ttl_seconds if ttl_seconds is None else ttl_seconds
    cache_file = _cache_path(url)

    if not refresh and cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < ttl:
            return json.loads(cache_file.read_text())

    _rate_limit()
    resp = requests.get(url, headers={"User-Agent": CONFIG.sec_user_agent}, timeout=30)
    if resp.status_code != 200:
        # Fall back to a stale cache rather than failing outright, if one exists.
        if cache_file.exists():
            return json.loads(cache_file.read_text())
        raise SecClientError(f"GET {url} -> {resp.status_code}")

    data = resp.json()
    cache_file.write_text(json.dumps(data))
    return data


def _get_text(url: str, *, refresh: bool = False, ttl_seconds: int | None = None) -> str:
    """Fetch ``url`` as raw text (HTML/XML filing documents), disk-cached like
    ``_get_json`` but under a ``.txt`` suffix so a filing document never gets
    handed to ``json.loads``."""
    ensure_dirs()
    ttl = CONFIG.sec_cache_ttl_seconds if ttl_seconds is None else ttl_seconds
    cache_file = _cache_path(url, suffix="txt")

    if not refresh and cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < ttl:
            return cache_file.read_text(encoding="utf-8", errors="ignore")

    _rate_limit()
    resp = requests.get(url, headers={"User-Agent": CONFIG.sec_user_agent}, timeout=30)
    if resp.status_code != 200:
        if cache_file.exists():
            return cache_file.read_text(encoding="utf-8", errors="ignore")
        raise SecClientError(f"GET {url} -> {resp.status_code}")

    cache_file.write_text(resp.text, encoding="utf-8")
    return resp.text


def ticker_map(*, refresh: bool = False) -> dict[str, dict[str, Any]]:
    """Ticker (upper-cased) -> {"cik": int, "ticker": str, "title": str}.

    Cached for a week by default since it's an ~800KB file that changes rarely.
    """
    url = "https://www.sec.gov/files/company_tickers.json"
    raw = _get_json(url, refresh=refresh, ttl_seconds=7 * 24 * 3600)
    out: dict[str, dict[str, Any]] = {}
    for entry in raw.values():
        out[entry["ticker"].upper()] = {
            "cik": int(entry["cik_str"]),
            "ticker": entry["ticker"].upper(),
            "title": entry["title"],
        }
    return out


def cik_for_ticker(ticker: str, *, refresh: bool = False) -> int:
    mapping = ticker_map(refresh=refresh)
    entry = mapping.get(ticker.upper())
    if entry is None:
        raise SecClientError(f"No CIK found for ticker {ticker!r}")
    return entry["cik"]


def companyfacts(cik: int, *, refresh: bool = False) -> dict[str, Any]:
    """All XBRL facts SEC has for one filer. ~1-2MB; cached, not refetched by default."""
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
    return _get_json(url, refresh=refresh)


def companyconcept(
    cik: int, tag: str, *, taxonomy: str = "us-gaap", refresh: bool = False
) -> dict[str, Any]:
    """All facts for one concept, one filer. Cheaper than companyfacts when you
    only need a single tag and don't already have companyfacts cached."""
    url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/{taxonomy}/{tag}.json"
    return _get_json(url, refresh=refresh)


def frames(
    tag: str, period: str, *, taxonomy: str = "us-gaap", unit: str = "USD", refresh: bool = False
) -> dict[str, Any]:
    """One concept, every filer that reported it for ``period`` in one request.

    ``period`` is e.g. "CY2025Q1I" (instantaneous, balance-sheet items) or
    "CY2025Q1" (duration, flow items) or "CY2025" (full year). Only filers whose
    reporting period matches exactly are included — non-calendar fiscal years
    are silently dropped.
    """
    url = f"https://data.sec.gov/api/xbrl/frames/{taxonomy}/{tag}/{unit}/{period}.json"
    return _get_json(url, refresh=refresh)


def submissions(cik: int, *, refresh: bool = False) -> dict[str, Any]:
    """Filer metadata + recent filings list (form, accession number, filing
    date, report date, primary document) for one CIK."""
    url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
    return _get_json(url, refresh=refresh, ttl_seconds=6 * 3600)


def filing_document(cik: int, accession_number: str, document: str, *, refresh: bool = False) -> str:
    """Raw text of one document inside one filing (a 10-K/10-Q .htm, or a Form
    4's .xml). ``document`` is the ``primaryDocument`` value from
    ``submissions()`` — for ownership forms (3/4/5) that's an XSL-rendered
    path like ``xslF345X05/primary_doc.xml``; the raw XML behind it always
    sits at the accession root under the same basename, so that prefix is
    stripped here rather than pushed onto every caller."""
    accession_no_dashes = accession_number.replace("-", "")
    basename = document.rsplit("/", 1)[-1]
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/{basename}"
    return _get_text(url, refresh=refresh)
