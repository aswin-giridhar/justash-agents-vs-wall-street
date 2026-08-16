import pytest

from avws.ledger import Fact, append, facts_for, history, reset


def _fact(**overrides) -> Fact:
    base = dict(
        metric_key="ADI:Revenue", company="Analog Devices", period="FY2026Q3",
        value=3900.0, unit="USDm", basis="guidance_mid",
        source_doc="analog-devices/filings/2026-05-20__adi-us-20260520-q2-8k__1040581.md",
        source_quote="we are forecasting revenue of $3.9 billion, +/- $100 million",
        confidence=0.95,
    )
    return Fact(**{**base, **overrides})


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr("avws.ledger.LEDGER_PATH", tmp_path / "ledger.jsonl")
    reset()


def test_roundtrip_preserves_basis_and_quote():
    append(_fact())
    got = facts_for("ADI:Revenue")
    assert len(got) == 1
    assert got[0].basis == "guidance_mid"
    assert "3.9 billion" in got[0].source_quote


def test_rejects_unknown_basis():
    with pytest.raises(ValueError, match="basis"):
        _fact(basis="guess")


def test_rejects_fact_without_source_quote():
    """An unsourced number must be impossible to construct, not merely discouraged."""
    with pytest.raises(ValueError, match="source_quote"):
        _fact(source_quote="   ")


def test_rejects_non_finite_value():
    with pytest.raises(ValueError, match="non-finite"):
        _fact(value=float("nan"))


def test_history_returns_only_past_actuals_oldest_first():
    append(_fact(period="FY2026Q1", value=2640.0, basis="reported"))
    append(_fact(period="FY2026Q2", value=3623.0, basis="reported"))
    append(_fact(period="FY2026Q3", value=3900.0, basis="guidance_mid"))
    assert history("ADI:Revenue") == [2640.0, 3623.0]


def test_facts_are_isolated_by_metric_key():
    append(_fact())
    append(_fact(metric_key="ADI:Adjusted gross margin", value=73.0, unit="%",
                 basis="adjusted"))
    assert len(facts_for("ADI:Revenue")) == 1
    assert len(facts_for("ADI:Adjusted gross margin")) == 1
