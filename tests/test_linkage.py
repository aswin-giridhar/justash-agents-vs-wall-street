"""The linked derivation must never overwrite a value with an implausible one.

Bounds guard facts, components and the seasonal estimator. The one place that
overwrites a value wholesale had no guard on what it wrote, and a mis-sourced opex
ratio produced an ADI adjusted EPS of -90.12 against company guidance of 3.30.
"""

from avws import linkage


def test_negative_operating_margin_refuses_to_derive():
    """A gross margin below the opex ratio means the opex ratio is wrong. Failing
    is far better than propagating it into an EPS."""
    values = {"Revenue": 3900.0, "Adjusted gross margin": 20.0,
              "Adjusted diluted EPS": 3.30}
    drivers = {"weighted_average_diluted_shares_m": 497.0,
               "recent_opex_ratio_pct": 60.0,  # implies -40% operating margin
               "effective_tax_rate_pct": 12.0}
    assert linkage.derive_adi_eps(values, drivers) is None


def test_plausible_inputs_still_derive():
    values = {"Revenue": 3900.0, "Adjusted gross margin": 73.0,
              "Adjusted diluted EPS": 3.30}
    drivers = {"weighted_average_diluted_shares_m": 497.0,
               "recent_opex_ratio_pct": 24.0, "effective_tax_rate_pct": 12.0,
               "net_finance_charge": 0.0}
    derivation = linkage.derive_adi_eps(values, drivers)
    assert derivation is not None
    assert 2.0 < derivation.derived_value < 5.0, derivation.arithmetic


def test_out_of_band_derivation_does_not_overwrite(monkeypatch):
    """The exact failure: a derived EPS far outside the plausible band must leave
    the independent estimate standing rather than replacing it."""
    def fake_drivers(ticker, company):
        return ({"weighted_average_diluted_shares_m": 497.0,
                 "recent_opex_ratio_pct": 24.0,
                 "effective_tax_rate_pct": 12.0,
                 "net_finance_charge": 0.0}, [])

    monkeypatch.setattr(linkage, "fetch_drivers", fake_drivers)
    # A revenue two orders of magnitude too large drives the derived EPS far out.
    values = {"Revenue": 390000.0, "Adjusted gross margin": 73.0,
              "Adjusted diluted EPS": 3.30}
    out, derivations, notes = linkage.apply("ADI", "Analog Devices", values,
                                            guided_labels=set())
    assert out["Adjusted diluted EPS"] == 3.30, "an out-of-band derivation overwrote"
    # Either guard may catch it first - the band check or the divergence limit.
    # What matters is that a wildly wrong derivation never reaches a workbook.
    assert any("REJECTED" in n or "substitution limit" in n for n in notes), notes


def test_guided_metric_is_checked_not_substituted(monkeypatch):
    def fake_drivers(ticker, company):
        return ({"weighted_average_diluted_shares_m": 497.0,
                 "recent_opex_ratio_pct": 24.0,
                 "effective_tax_rate_pct": 12.0,
                 "net_finance_charge": 0.0}, [])

    monkeypatch.setattr(linkage, "fetch_drivers", fake_drivers)
    values = {"Revenue": 3900.0, "Adjusted gross margin": 73.0,
              "Adjusted diluted EPS": 3.30}
    out, derivations, notes = linkage.apply(
        "ADI", "Analog Devices", values,
        guided_labels={"Adjusted diluted EPS"},
    )
    assert out["Adjusted diluted EPS"] == 3.30
    assert any("consistency check" in n for n in notes)
    # The caller substitutes every derivation it is handed, so a check-only path
    # must hand back none. Returning one "just for reporting" overwrote the guided
    # value with the check that was supposed to protect it.
    assert derivations == [], "a check-only derivation was returned as an instruction"
