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

# Model tiering was tried and REVERTED after measurement.
#
# The hypothesis was that extraction and series building are transcription against
# a strict schema and so would run correctly on a smaller model. That was wrong.
# Deciding whether "£45.6m" is a full year or a half year, and whether a figure is
# the quarter or the year-to-date cumulative, is the judgement that separates a
# correct series from a wrong one. On gpt-5-mini the run was 40% faster and
# materially less accurate: Hays half-year figures were labelled as full years,
# and ADI revenue came out at $71bn against company guidance of $3.9bn.
#
# Kept as a switch so the experiment is reproducible rather than merely asserted.
TRANSCRIPTION_MODEL = os.environ.get("AVWS_TRANSCRIPTION_MODEL", MODEL)
REASONING_MODEL = os.environ.get("AVWS_REASONING_MODEL", MODEL)

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
