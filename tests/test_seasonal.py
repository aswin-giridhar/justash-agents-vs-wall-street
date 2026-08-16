"""The seasonal estimator must never mix period types.

Every case here comes from a real defect the adversarial critic found: pooling
full-year figures with quarterly ones, and pairing different quarters, produced
Deere segment operating profit of 441 against a true level in the low thousands.
"""

from avws.estimators import seasonal
from avws.ledger import Fact
from avws.registry import get_metric


def _fact(period, value, metric_key="DE:Production & Precision Ag operating profit"):
    return Fact(
        metric_key=metric_key, company="Deere & Company", period=period,
        value=value, unit="USDm", basis="reported", source_doc="d",
        source_quote="q", confidence=0.8,
    )


def test_ignores_full_year_figures_when_the_target_is_a_quarter():
    metric = get_metric("DE:Production & Precision Ag operating profit")  # FY2026Q3
    facts = [
        _fact("FY2024Q3", 1_500.0),
        _fact("FY2025Q3", 1_600.0),
        _fact("FY2025", 6_200.0),   # a full year - must not enter the series
        _fact("FY2024", 5_900.0),
    ]
    est = seasonal.estimate(metric, facts)
    # Anchored on FY2025Q3 = 1600 and grown, so it must stay in the segment's range
    # rather than being dragged toward the annual numbers.
    assert 1_000 < est.value < 2_500, est.derivation


def test_ignores_other_quarters():
    metric = get_metric("DE:Production & Precision Ag operating profit")  # Q3
    facts = [
        _fact("FY2024Q3", 1_000.0),
        _fact("FY2025Q3", 1_100.0),
        _fact("FY2026Q1", 300.0),   # a different quarter - must not enter
        _fact("FY2026Q2", 2_900.0),
    ]
    est = seasonal.estimate(metric, facts)
    assert 900 < est.value < 1_400, est.derivation


def test_full_year_target_uses_only_full_year_actuals():
    metric = get_metric("HAS:Net fees")  # FY2026, a full year
    facts = [
        _fact("FY2024", 1_113.6, "HAS:Net fees"),
        _fact("FY2025", 972.4, "HAS:Net fees"),
        _fact("FY2025Q3", 240.0, "HAS:Net fees"),   # a quarter - must not enter
    ]
    est = seasonal.estimate(metric, facts)
    # FY2025 = 972.4 grown at the FY2024->FY2025 rate of about -12.7%
    assert 780 < est.value < 1_000, est.derivation


def test_growth_ignores_non_consecutive_pairs():
    """DE segment profit had FY2020Q3, FY2021Q3, FY2024Q3, FY2025Q3 - two years
    missing. A 2021->2024 ratio is not an annual growth rate."""
    metric = get_metric("DE:Production & Precision Ag operating profit")  # FY2026Q3
    facts = [
        _fact("FY2020Q3", 605.0),
        _fact("FY2021Q3", 906.0),    # consecutive with 2020: +49.8%
        _fact("FY2024Q3", 1162.0),   # NOT consecutive with 2021
        _fact("FY2025Q3", 580.0),    # consecutive with 2024: -50.1%
    ]
    est = seasonal.estimate(metric, facts)
    # Anchored on FY2025Q3 = 580; growth is the median of the two real pairs.
    assert 200 < est.value < 1_000, est.derivation
    assert "consecutive" in est.derivation


def test_compounds_across_a_gap_instead_of_carrying_a_stale_value_forward():
    """Carrying FY2024Q3 forward flat put a two-year-old figure into FY2026Q3."""
    metric = get_metric("DE:Diluted EPS (GAAP)")  # FY2026Q3
    facts = [
        _fact("FY2022Q3", 5.00, "DE:Diluted EPS (GAAP)"),
        _fact("FY2023Q3", 6.00, "DE:Diluted EPS (GAAP)"),
        _fact("FY2024Q3", 6.29, "DE:Diluted EPS (GAAP)"),  # no FY2025Q3
    ]
    est = seasonal.estimate(metric, facts)
    assert est.value != 6.29, "stale value carried forward unchanged"
    assert "compounded over 2 year" in est.derivation, est.derivation


def test_clamps_a_runaway_compounded_value_to_the_plausible_band():
    """Compounding across a gap has no upper bound. A large growth rate over three
    years produced ADI revenue of 71,165 against company guidance of 3,900."""
    metric = get_metric("ADI:Revenue")  # band 1,000-12,000 USDm
    facts = [
        _fact("FY2021Q3", 1_000.0, "ADI:Revenue"),
        _fact("FY2022Q3", 3_000.0, "ADI:Revenue"),   # +200% consecutive pair
        _fact("FY2023Q3", 9_000.0, "ADI:Revenue"),   # +200% again; no 2024/2025
    ]
    est = seasonal.estimate(metric, facts)
    assert 1_000 <= est.value <= 12_000, est.derivation
    assert "CLAMPED" in est.derivation
    assert est.confidence <= 0.15


def test_a_plausible_value_is_not_clamped():
    metric = get_metric("ADI:Revenue")
    facts = [
        _fact("FY2024Q3", 3_000.0, "ADI:Revenue"),
        _fact("FY2025Q3", 3_200.0, "ADI:Revenue"),
    ]
    est = seasonal.estimate(metric, facts)
    assert "CLAMPED" not in est.derivation


def test_returns_a_placeholder_rather_than_nothing_when_no_comparable_history():
    metric = get_metric("DE:Production & Precision Ag operating profit")
    est = seasonal.estimate(metric, [_fact("FY2025Q1", 300.0)])
    assert est.value is not None
    assert est.confidence <= 0.2
