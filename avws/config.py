"""Paths, constants and process-wide setup.

Importing this module injects the OS trust store into Python's TLS stack. The
venue network intercepts TLS with a private CA that Python's bundled certificate
store does not carry, so without this every outbound API call fails with
APIConnectionError. See docs/build-log.md, 11:30.
"""

from __future__ import annotations

import os
from pathlib import Path

import truststore
from dotenv import find_dotenv, load_dotenv

truststore.inject_into_ssl()

# .env deliberately lives above the repository root so it cannot be committed.
load_dotenv(find_dotenv(usecwd=True))

ROOT = Path(__file__).resolve().parents[1]
CHALLENGE_DIR = ROOT / "challenge"
CORPUS_DIR = CHALLENGE_DIR / "offline-data"
TEMPLATE_DIR = CHALLENGE_DIR / "templates"
SUBMISSION_DIR = ROOT / "submission"
RESEARCH_DIR = ROOT / "research"
LOG_DIR = ROOT / "logs"
LLM_LOG_DIR = LOG_DIR / "llm"
CACHE_DIR = ROOT / ".cache"
LEDGER_PATH = CACHE_DIR / "ledger.jsonl"

MODEL = os.environ.get("AVWS_MODEL", "gpt-5")

# Competition denominator floors, from JUDGING.md. Used by the backtest harness to
# report floor-band hit rate - the statistic that maps onto the scoring function.
PERCENTAGE_FLOOR_PP = 0.5
MONEY_FLOOR_FRACTION = 0.005

# Ticker -> corpus subdirectory.
CORPUS_DIRS = {
    "HD": "home-depot",
    "ADI": "analog-devices",
    "HAS": "hays",
    "DE": "deere",
}


def ensure_dirs() -> None:
    for d in (SUBMISSION_DIR, RESEARCH_DIR, LOG_DIR, LLM_LOG_DIR, CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)
