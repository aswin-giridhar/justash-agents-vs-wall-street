"""Table tests run against real corpus documents, not synthetic fixtures.

A parser that works on a hand-written example and fails on the actual filings is
worse than useless, because it passes CI and breaks at 17:15.
"""

from avws.config import CORPUS_DIR
from avws.tables import find_row, find_rows, parse_pipe_tables, row_numbers

ADI_8K = CORPUS_DIR / "analog-devices/filings/2026-05-20__adi-us-20260520-q2-8k__1040581.md"
HAS_FY25 = CORPUS_DIR / "hays/filings/2025-08-21__has-ln-20250821-h2-8k__143890.md"


def _tables(path):
    return parse_pipe_tables(path.read_text(encoding="utf-8", errors="replace"))


def test_extracts_adi_q2_revenue():
    row = find_row(_tables(ADI_8K), "Revenue")
    assert row is not None
    assert 3623.0 in row_numbers(row)


def test_extracts_adi_adjusted_gross_margin_percentage():
    row = find_row(_tables(ADI_8K), "Adjusted gross margin percentage")
    assert row is not None
    assert 73.0 in row_numbers(row)


def test_adjusted_and_gaap_eps_are_different_rows():
    tables = _tables(ADI_8K)
    gaap = find_row(tables, "Diluted earnings per share")
    adjusted = find_row(tables, "Adjusted diluted earnings per share")
    assert 2.40 in row_numbers(gaap)
    assert 3.09 in row_numbers(adjusted)


def test_extracts_hays_fy25_divisional_net_fees():
    rows = find_rows(_tables(HAS_FY25), "Germany")
    numbers = [row_numbers(r) for r in rows]
    flat = [n for group in numbers for n in group]
    assert 308.9 in flat, "FY25 Germany net fees not found"


def test_separator_rows_are_discarded():
    md = "| A | B |\n|---|---|\n| 1 | 2 |\n"
    assert parse_pipe_tables(md) == [[["A", "B"], ["1", "2"]]]
