"""Plausibility bands and scale repair.

Every case here is a real reading the pipeline produced, not an invented one.
"""

import pytest

from avws.registry import get_metric
from avws.table_facts import normalise


def test_repairs_thousands_reported_where_millions_are_required():
    """ADI's 10-Q reports revenue as 3,623,465 (thousands); the workbook wants USDm."""
    metric = get_metric("ADI:Revenue")
    value, note = normalise(metric, 3_623_465.0)
    assert value == pytest.approx(3623.465)
    assert "rescaled" in note


def test_leaves_a_correctly_scaled_value_untouched():
    metric = get_metric("ADI:Revenue")
    value, note = normalise(metric, 3623.0)
    assert value == 3623.0
    assert note == ""


def test_rejects_a_bare_column_header_year():
    """Deere's segment tables put 2026 and 2025 in header cells; the band for that
    metric spans 100-5000, so an unguarded parse records 2026 as a profit."""
    metric = get_metric("DE:Production & Precision Ag operating profit")
    assert normalise(metric, 2026.0) is None
    assert normalise(metric, 2025.0) is None


def test_accepts_a_real_segment_profit():
    metric = get_metric("DE:Production & Precision Ag operating profit")
    value, _ = normalise(metric, 1673.0)
    assert value == 1673.0


def test_rejects_basis_points_mistaken_for_percentage_points():
    """Home Depot: 'foreign exchange rates negatively impacted total company
    comparable sales by approximately 40 basis points' - that is 0.4pp, not 40pp."""
    metric = get_metric("HD:Comparable sales, total company")
    assert normalise(metric, -40.0) is None
    assert normalise(metric, 55.0) is None


def test_accepts_a_realistic_comparable_sales_figure():
    metric = get_metric("HD:Comparable sales, total company")
    value, _ = normalise(metric, 0.6)
    assert value == 0.6


def test_zero_is_never_a_fact():
    assert normalise(get_metric("ADI:Revenue"), 0.0) is None
