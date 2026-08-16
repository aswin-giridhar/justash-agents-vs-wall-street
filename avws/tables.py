"""Markdown pipe-table extraction.

The corpus stores filing tables as pipe-delimited markdown. Financial statements
are the highest-signal part of any filing, so parsing them into structured rows
turns "search returned a chunk of text" into "here is the revenue row".
"""

from __future__ import annotations

import re

_SEPARATOR = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")

Row = list[str]
Table = list[Row]


def _split_row(line: str) -> Row:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def parse_pipe_tables(markdown: str) -> list[Table]:
    """Return every pipe table in the document as a list of rows."""
    tables: list[Table] = []
    current: Table = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.count("|") >= 2:
            if _SEPARATOR.match(stripped):
                continue
            current.append(_split_row(stripped))
        else:
            if current:
                tables.append(current)
                current = []
    if current:
        tables.append(current)
    return tables


def find_rows(tables: list[Table], label: str, prefix: bool = True) -> list[Row]:
    """Rows whose first non-empty cell matches `label`, case-insensitive.

    `prefix=True` requires the cell to START with the label rather than merely
    contain it. Substring matching silently pulled "Cost of revenues" into a
    search for "Revenue" and turned a cost line into a revenue fact - a wrong
    number that looks entirely authoritative in a ledger.
    """
    needle = label.lower().strip()
    out: list[Row] = []
    for table in tables:
        for row in table:
            head = next((c for c in row if c.strip()), "").lower().strip()
            head = head.lstrip("*_ ").rstrip(":*_ ")
            if (head.startswith(needle) if prefix else needle in head):
                out.append(row)
    return out


def find_row(tables: list[Table], label_substring: str) -> Row | None:
    rows = find_rows(tables, label_substring)
    return rows[0] if rows else None


def row_numbers(row: Row) -> list[float]:
    """Numeric cells of a row, in order, skipping labels and blanks."""
    from avws.units import parse_number

    return [n for n in (parse_number(cell) for cell in row) if n is not None]
