from avws.registry import get_metric, load_metrics, metrics_for, tickers


def test_loads_twelve_metrics_with_exact_labels():
    metrics = load_metrics()
    assert len(metrics) == 12
    keys = {m.key for m in metrics}
    assert "HD:Comparable sales, total company" in keys
    assert "HAS:Pre-exceptional basic EPS" in keys
    assert "DE:Production & Precision Ag operating profit" in keys
    hd_sales = get_metric("HD:Net sales")
    assert hd_sales.units == "USDm"
    assert hd_sales.output_file == "HD-FY2026Q2.xlsx"
    assert hd_sales.period == "FY2026Q2"


def test_percentage_metrics_are_flagged():
    pct = {m.key for m in load_metrics() if m.is_percentage}
    assert pct == {
        "HD:Comparable sales, total company",
        "ADI:Adjusted gross margin",
    }


def test_eps_metrics_include_hays_pence():
    eps = {m.key for m in load_metrics() if m.is_eps}
    assert "HAS:Pre-exceptional basic EPS" in eps
    assert "ADI:Adjusted diluted EPS" in eps
    assert "ADI:Revenue" not in eps


def test_four_tickers_each_with_three_metrics():
    assert tickers() == ["HD", "ADI", "HAS", "DE"]
    for t in tickers():
        assert len(metrics_for(t)) == 3


def test_floor_matches_competition_rules():
    pct = get_metric("ADI:Adjusted gross margin")
    assert pct.floor(73.0) == 0.5  # 0.5 percentage points regardless of level
    money = get_metric("ADI:Revenue")
    assert money.floor(3900.0) == 19.5  # 0.5% of the reported result
