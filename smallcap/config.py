"""Central configuration for smallcap."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("SMALLCAP_DATA_DIR", PROJECT_ROOT / "data_store"))
SEC_CACHE_DIR = DATA_DIR / "sec_cache"
CHARTS_DIR = DATA_DIR / "charts"
PREDICTIONS_PATH = DATA_DIR / "predictions.jsonl"

JOURNAL_DIR = DATA_DIR / "journal"
THESES_PATH = JOURNAL_DIR / "theses.jsonl"
THESIS_EVENTS_PATH = JOURNAL_DIR / "thesis_events.jsonl"
FOLLOWUPS_PATH = JOURNAL_DIR / "followups.jsonl"
TRADES_PATH = JOURNAL_DIR / "trades.jsonl"
NOTES_PATH = JOURNAL_DIR / "notes.jsonl"

# SEC requires a real identifying User-Agent ("name email") on every request
# and asks that you stay under ~10 req/s. See
# https://www.sec.gov/os/webmaster-faq#developers
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "smallcap-toolkit ehchen10@gmail.com")
SEC_MIN_INTERVAL_SECONDS = 0.11  # ~9 req/s, under the SEC's stated limit

SEC_CACHE_TTL_SECONDS = 24 * 3600


@dataclass(frozen=True)
class Config:
    sec_user_agent: str = SEC_USER_AGENT
    sec_cache_ttl_seconds: int = SEC_CACHE_TTL_SECONDS
    sec_min_interval_seconds: float = SEC_MIN_INTERVAL_SECONDS
    runway_lookback_quarters: int = 4
    dilution_default_years: int = 5
    prediction_reliability_min_n: int = 20
    screener_default_period: str = ""  # filled in at call time (e.g. "CY2025Q1I")


CONFIG = Config()


def ensure_dirs() -> None:
    for d in (DATA_DIR, SEC_CACHE_DIR, CHARTS_DIR, JOURNAL_DIR):
        d.mkdir(parents=True, exist_ok=True)
