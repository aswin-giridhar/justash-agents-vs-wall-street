"""These tests exist because comparing period labels as raw strings crashed the
first end-to-end run: "FY2026Q3"[:4] is "FY20", not a year."""

import pytest

from avws.periods import Period, parse, same_quarter_prior_year, sort_key


@pytest.mark.parametrize(
    "label,expected",
    [
        ("FY2026Q3", Period(2026, 3)),
        ("FY2026", Period(2026, 0)),
        ("Q4 2026", Period(2026, 4)),
        ("Q2 FY2025", Period(2025, 2)),
        ("FY 2026", Period(2026, 0)),
        ("2025", Period(2025, 0)),
    ],
)
def test_parses_the_labels_this_corpus_actually_uses(label, expected):
    assert parse(label) == expected


@pytest.mark.parametrize("label", ["", "unknown", None, "Note 5"])
def test_unparseable_labels_give_none_not_a_wrong_guess(label):
    assert parse(label) is None


def test_same_quarter_prior_year_matches_across_label_formats():
    assert same_quarter_prior_year("FY2026Q3", "Q3 2025")
    assert same_quarter_prior_year("FY2026", "FY2025")


def test_same_quarter_prior_year_rejects_a_different_quarter():
    assert not same_quarter_prior_year("FY2026Q3", "FY2025Q2")
    assert not same_quarter_prior_year("FY2026Q3", "FY2024Q3")


def test_sort_key_orders_quarters_chronologically():
    labels = ["FY2026Q2", "FY2025Q3", "FY2026Q1", "FY2025Q4"]
    assert sorted(labels, key=sort_key) == [
        "FY2025Q3", "FY2025Q4", "FY2026Q1", "FY2026Q2",
    ]
