"""The decision layer picks the submitted value from the candidate distribution."""

import pytest

from avws.decide import choose, consensus_proxy
from avws.estimators.base import Estimate
from avws.ledger import Fact
from avws.registry import get_metric


def _est(value, method, confidence=0.5):
    return Estimate(metric_key="ADI:Revenue", value=value, method=method,
                    derivation="", confidence=confidence)


def _guidance_fact(value, basis="guidance_mid", period="FY2026Q3"):
    return Fact(metric_key="ADI:Revenue", company="Analog Devices", period=period,
                value=value, unit="USDm", basis=basis, source_doc="d",
                source_quote="we are forecasting revenue of $3.9 billion", confidence=0.9)


def test_wide_disagreement_uses_the_median_not_the_mean():
    """With candidates far apart the mean lands between clusters that no estimator
    considers plausible; expected absolute error is minimised at the median."""
    metric = get_metric("ADI:Revenue")
    candidates = [_est(3900, "guidance_anchor"), _est(3850, "build_up"),
                  _est(1200, "seasonal_trend")]
    weights = {"guidance_anchor": 0.5, "build_up": 0.4, "seasonal_trend": 0.1}
    decision = choose(metric, candidates, weights, [])
    assert decision.method == "weighted_median"
    assert decision.value in (3850, 3900)
    assert decision.value > 3000, "the outlier must not drag the submitted value"


def test_close_agreement_uses_the_mean():
    metric = get_metric("ADI:Revenue")
    candidates = [_est(3900, "guidance_anchor"), _est(3950, "build_up")]
    weights = {"guidance_anchor": 0.5, "build_up": 0.5}
    decision = choose(metric, candidates, weights, [])
    assert decision.method == "weighted_mean"
    assert 3900 <= decision.value <= 3950


def test_consensus_proxy_uses_the_guidance_midpoint():
    metric = get_metric("ADI:Revenue")
    assert consensus_proxy(metric, [_guidance_fact(3900)]) == 3900


def test_consensus_proxy_derives_a_midpoint_from_a_range():
    metric = get_metric("ADI:Revenue")
    facts = [_guidance_fact(3800, "guidance_low"), _guidance_fact(4000, "guidance_high")]
    assert consensus_proxy(metric, facts) == 3900


def test_consensus_proxy_is_none_without_guidance_rather_than_invented():
    metric = get_metric("ADI:Revenue")
    assert consensus_proxy(metric, []) is None


def test_deviation_from_consensus_is_reported():
    metric = get_metric("ADI:Revenue")
    candidates = [_est(4000, "guidance_anchor")]
    decision = choose(metric, candidates, {"guidance_anchor": 1.0}, [_guidance_fact(3900)])
    assert decision.consensus_proxy == 3900
    assert decision.deviation_pct == pytest.approx((4000 - 3900) / 3900)


def test_no_usable_candidate_raises_rather_than_returning_zero():
    metric = get_metric("ADI:Revenue")
    with pytest.raises(ValueError):
        choose(metric, [_est(0, "seasonal_trend", confidence=0.0)], {}, [])
