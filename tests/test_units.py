import pytest

from avws.units import (
    looks_like_fraction_not_percent,
    parse_number,
    pounds_to_pence,
    to_millions,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("$ 3,623", 3623.0),
        ("3,623", 3623.0),
        ("(123)", -123.0),
        ("($1,234.5)", -1234.5),
        ("67.3 %", 67.3),
        ("$2.40", 2.40),
        ("630 bps", 630.0),
        ("£972.4", 972.4),
        ("  1,113.6  ", 1113.6),
        (3900, 3900.0),
    ],
)
def test_parses_real_filing_formats(raw, expected):
    assert parse_number(raw) == expected


@pytest.mark.parametrize("raw", ["", "—", "-", "n/a", "nm", None, "Net fees", "Q4 2026"])
def test_absent_and_malformed_both_give_none_never_zero(raw):
    assert parse_number(raw) is None


def test_billions_convert_to_millions():
    assert to_millions(3.9, "revenue of $3.9 billion") == 3900.0
    assert to_millions(3623.0, "in millions") == 3623.0
    assert to_millions(3623.0, "") == 3623.0


def test_pounds_to_pence():
    assert pounds_to_pence(0.062) == pytest.approx(6.2)


def test_detects_percentage_entered_as_fraction():
    history = [0.6, 1.4, 2.1]
    assert looks_like_fraction_not_percent(0.012, history)
    assert not looks_like_fraction_not_percent(1.2, history)


def test_fraction_check_is_inert_without_history():
    assert not looks_like_fraction_not_percent(0.012, [])
