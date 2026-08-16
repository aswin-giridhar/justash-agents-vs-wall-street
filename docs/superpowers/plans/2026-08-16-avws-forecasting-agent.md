# AVWS Forecasting Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a repeatable agent that researches four companies from a frozen document corpus and produces four completed OpenStocks forecast workbooks containing 12 traceable figures.

**Architecture:** An append-only evidence ledger holds facts extracted from cited documents (never forecasts). Three deterministic estimator families — guidance-anchor, build-up identity, and seasonal-trend — turn those facts into candidate numbers; a reconciler weights them by measured backtest error; a validation gate checks scale, cross-metric identities, and runs an adversarial critic before any number reaches a workbook.

**Tech Stack:** Python 3.14, `uv` for dependency management, `openpyxl` for xlsx, OpenAI API (structured outputs) for extraction and critique, Node 25 for the supplied `npm run check:submission` validator.

**Spec:** `docs/superpowers/specs/2026-08-16-avws-forecasting-agent-design.md`

## Global Constraints

- Output files, exact names: `submission/HD-FY2026Q2.xlsx`, `submission/ADI-FY2026Q3.xlsx`, `submission/HAS-FY2026.xlsx`, `submission/DE-FY2026Q3.xlsx`
- Do not rename the `Summary` sheet, metric labels, units, or the fiscal-period column. Fill only forecast cells.
- Percentage metrics are entered in percentage points: `4.5` means 4.5%. Hays EPS is in pence: `6.2` means 6.2 pence.
- No metric may ever be blank. A missing forecast scores 5.0. Every chain terminates in a number.
- `.env`, `entry.json`, and `challenge/offline-data/` are gitignored and must never be committed.
- All LLM calls use temperature 0 and are logged to `logs/llm/` with prompt hash and response.
- Single final command with no human interaction: `python -m avws.run --all`
- Metric labels must be read from `challenge/companies.json`, never hardcoded as string literals in estimator logic.

---

## File Structure

```
avws/
  config.py          paths, model name, constants
  llm.py             OpenAI provider behind a thin interface; call logging
  corpus.py          document index + BM25 retrieval
  tables.py          markdown pipe-table parsing
  units.py           number/unit normalisation and scale bands
  registry.py        the 12 metric definitions, loaded from companies.json
  ledger.py          Fact record + append-only JSONL store
  extract.py         LLM fact extraction into the ledger
  estimators/
    base.py          Estimate dataclass
    guidance.py      GuidanceAnchor
    buildup.py       BuildUp identity composition
    seasonal.py      SeasonalTrend fallback
  reconcile.py       inverse-error weighting
  validate.py        scale bands, identities, adversarial critic
  backtest.py        historical replay harness
  workbook.py        write xlsx from templates
  report.py          per-company evidence markdown
  run.py             CLI entry point
tests/
  test_units.py  test_tables.py  test_corpus.py
  test_registry.py  test_estimators.py  test_validate.py  test_workbook.py
```

---

### Task 0: De-risk the output format

Nothing downstream matters if the workbook cannot be written and accepted. Do this first.

**Files:**
- Create: `pyproject.toml`
- Create: `scratch/roundtrip_check.py` (throwaway, deleted at the end of this task)

**Interfaces:**
- Consumes: nothing
- Produces: a verified answer to "does openpyxl round-trip the templates and still pass `npm run check:submission`?"

- [ ] **Step 1: Create the Python project**

```toml
[project]
name = "avws"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["openpyxl>=3.1", "openai>=1.40", "python-dotenv>=1.0"]

[tool.setuptools.packages.find]
include = ["avws*"]
```

- [ ] **Step 2: Install dependencies and the Node validator**

Run: `uv venv && uv pip install -e . && npm install`
Expected: both complete without error.

- [ ] **Step 3: Inspect one template to learn its真 layout**

```python
# scratch/roundtrip_check.py
from openpyxl import load_workbook
wb = load_workbook("challenge/templates/HD-FY2026Q2.xlsx")
ws = wb["Summary"]
for row in ws.iter_rows(min_row=1, max_row=ws.max_row):
    for c in row:
        if c.value is not None:
            print(c.coordinate, repr(c.value))
```

Run: `uv run python scratch/roundtrip_check.py`
Expected: prints the metric labels, units and the fiscal-period header, revealing which cells are the forecast cells.

- [ ] **Step 4: Write a value, save, and run the official checker**

Extend the scratch script to write a plausible number into each forecast cell and save to `submission/HD-FY2026Q2.xlsx`. Repeat for all four templates, then run:

Run: `npm run check:forecasts`
Expected: PASS for all four workbooks. If it fails, the failure message names the exact structural expectation to satisfy — fix and rerun before proceeding.

- [ ] **Step 5: Verify the OpenAI key works headlessly**

```python
import os, openai
from dotenv import load_dotenv
load_dotenv()
c = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
print(c.chat.completions.create(model="gpt-5",
      messages=[{"role":"user","content":"reply with OK"}]).choices[0].message.content)
```

Run: `uv run python scratch/key_check.py`
Expected: prints `OK`. A 401 here at 17:15 would end the run, so it must be confirmed now.

- [ ] **Step 6: Delete the scratch directory and commit**

```bash
rm -rf scratch
git add pyproject.toml package-lock.json .gitignore
git commit -m "chore: python project setup; verify xlsx round-trip and API access"
```

---

### Task 1: Walking skeleton — registry, workbook writer, run command

By the end of this task the system produces four structurally valid workbooks. Everything after this improves numbers rather than creating the ability to submit.

**Files:**
- Create: `avws/config.py`, `avws/registry.py`, `avws/workbook.py`, `avws/run.py`
- Test: `tests/test_registry.py`, `tests/test_workbook.py`

**Interfaces:**
- Consumes: `challenge/companies.json`, `challenge/templates/*.xlsx`
- Produces:
  - `registry.load_metrics() -> list[Metric]` where `Metric` has fields `key: str`, `company: str`, `ticker: str`, `period: str`, `label: str`, `units: str`, `output_file: str`
  - `Metric.key` format is `"{ticker}:{label}"`, e.g. `"HD:Net sales"`
  - `workbook.write_workbook(ticker: str, values: dict[str, float]) -> Path` where `values` maps metric label to number

- [ ] **Step 1: Write the failing registry test**

```python
# tests/test_registry.py
from avws.registry import load_metrics

def test_loads_twelve_metrics_with_exact_labels():
    metrics = load_metrics()
    assert len(metrics) == 12
    keys = {m.key for m in metrics}
    assert "HD:Comparable sales, total company" in keys
    assert "HAS:Pre-exceptional basic EPS" in keys
    hd_sales = next(m for m in metrics if m.key == "HD:Net sales")
    assert hd_sales.units == "USDm"
    assert hd_sales.output_file == "HD-FY2026Q2.xlsx"

def test_percentage_metrics_are_flagged():
    metrics = load_metrics()
    pct = {m.key for m in metrics if m.is_percentage}
    assert "ADI:Adjusted gross margin" in pct
    assert "ADI:Revenue" not in pct
```

- [ ] **Step 2: Run it to confirm failure**

Run: `uv run pytest tests/test_registry.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'avws.registry'`

- [ ] **Step 3: Implement the registry**

```python
# avws/registry.py
import json
from dataclasses import dataclass
from avws.config import CHALLENGE_DIR

@dataclass(frozen=True)
class Metric:
    key: str
    company: str
    ticker: str
    period: str
    label: str
    units: str
    output_file: str

    @property
    def is_percentage(self) -> bool:
        return self.units.strip() == "%"

def load_metrics() -> list[Metric]:
    data = json.loads((CHALLENGE_DIR / "companies.json").read_text(encoding="utf-8"))
    out = []
    for c in data["companies"]:
        short = c["ticker"].split(":")[-1]
        for m in c["metrics"]:
            out.append(Metric(
                key=f"{short}:{m['label']}", company=c["company"], ticker=short,
                period=c["period"], label=m["label"], units=m["units"],
                output_file=c["outputFile"]))
    return out
```

- [ ] **Step 4: Run to confirm pass**

Run: `uv run pytest tests/test_registry.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Write the failing workbook test**

```python
# tests/test_workbook.py
from openpyxl import load_workbook
from avws.workbook import write_workbook

def test_writes_all_three_values_and_keeps_summary_sheet(tmp_path):
    path = write_workbook("HD", {
        "Net sales": 45000.0,
        "Adjusted diluted EPS": 4.60,
        "Comparable sales, total company": 1.2,
    })
    wb = load_workbook(path)
    assert "Summary" in wb.sheetnames
    found = [c.value for row in wb["Summary"].iter_rows() for c in row]
    assert 45000.0 in found and 4.60 in found and 1.2 in found

def test_refuses_to_write_a_missing_metric():
    import pytest
    with pytest.raises(ValueError, match="missing"):
        write_workbook("HD", {"Net sales": 45000.0})
```

- [ ] **Step 6: Run to confirm failure**

Run: `uv run pytest tests/test_workbook.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'avws.workbook'`

- [ ] **Step 7: Implement the workbook writer**

Locate the forecast cell for each metric by scanning the `Summary` sheet for the row whose cell text equals the metric label, then writing into the forecast column identified in Task 0 Step 3. Raise `ValueError("missing metric: ...")` when a label has no value — this enforces the never-blank constraint at the last possible moment.

- [ ] **Step 8: Run to confirm pass, then check structurally**

Run: `uv run pytest tests/test_workbook.py -v && npm run check:forecasts`
Expected: tests PASS; checker PASS.

- [ ] **Step 9: Add the run command producing naive numbers**

`avws/run.py` exposes `python -m avws.run --all`. For now each metric takes the most recent comparable actual found by a crude regex, or a hardcoded prior-year value. This is deliberately temporary; it exists so a submittable artifact exists from this point onward.

- [ ] **Step 10: Commit**

```bash
git add avws tests pyproject.toml
git commit -m "feat: metric registry, workbook writer and walking-skeleton run command"
```

---

### Task 2: Corpus index and BM25 retrieval

**Files:**
- Create: `avws/corpus.py`
- Test: `tests/test_corpus.py`

**Interfaces:**
- Consumes: `avws/config.py`
- Produces:
  - `corpus.build_index() -> None` writing `.cache/corpus_index.json`
  - `corpus.Doc` with fields `path, company, ticker, published_at, document_type, period, source_url`
  - `corpus.search(query: str, ticker: str | None = None, doc_type: str | None = None, since: str | None = None, k: int = 8) -> list[tuple[Doc, float, str]]` returning doc, BM25 score, and the best-matching chunk

- [ ] **Step 1: Write the failing test**

```python
# tests/test_corpus.py
from avws.corpus import build_index, search, load_index

def test_index_covers_all_four_companies():
    build_index()
    docs = load_index()
    assert len({d.ticker for d in docs}) == 4
    assert len(docs) > 1000

def test_search_finds_adi_q3_guidance():
    hits = search("third quarter outlook revenue adjusted EPS", ticker="ADI",
                  doc_type="FILING", k=5)
    assert hits, "no hits for ADI guidance"
    best_text = hits[0][2]
    assert "3.9 billion" in best_text or "3,900" in best_text

def test_metadata_filter_excludes_other_companies():
    hits = search("net fees", ticker="HAS", k=10)
    assert all(d.ticker == "HAS" for d, _, _ in hits)
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/test_corpus.py -v`
Expected: FAIL, module not found.

- [ ] **Step 3: Implement frontmatter parsing and the index**

Parse the leading `---` YAML block of each `.md` under `challenge/offline-data/`. Store one record per document. Cache to `.cache/corpus_index.json` so later runs skip the filesystem walk.

- [ ] **Step 4: Implement BM25**

Chunk each document into ~1,200-character windows on paragraph boundaries. Standard BM25 with `k1=1.5`, `b=0.75` over lowercased word tokens. Apply metadata filters *before* scoring so filtering is exact rather than a ranking hint.

- [ ] **Step 5: Run to confirm pass**

Run: `uv run pytest tests/test_corpus.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Add a human-facing search CLI**

`python -m avws.corpus --query "net fees" --ticker HAS --k 5` prints ranked results with document path, date and matching chunk. This is a tooling deliverable the architecture rubric scores directly, and it is how you will sanity-check retrieval during the build.

- [ ] **Step 7: Commit**

```bash
git add avws/corpus.py tests/test_corpus.py
git commit -m "feat: corpus index with metadata-filtered BM25 retrieval and search CLI"
```

---

### Task 3: Table parsing and unit normalisation

The defence against the 5.0-capped scale error.

**Files:**
- Create: `avws/tables.py`, `avws/units.py`
- Test: `tests/test_tables.py`, `tests/test_units.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `units.parse_number(raw: str) -> float | None`
  - `units.to_millions(value: float, unit_hint: str) -> float`
  - `tables.parse_pipe_tables(markdown: str) -> list[list[list[str]]]`
  - `tables.find_row(tables_, label_substring: str) -> list[str] | None`

- [ ] **Step 1: Write the failing unit tests**

```python
# tests/test_units.py
import pytest
from avws.units import parse_number, to_millions

@pytest.mark.parametrize("raw,expected", [
    ("$ 3,623", 3623.0), ("(123)", -123.0), ("67.3 %", 67.3),
    ("$2.40", 2.40), ("1,234.5", 1234.5), ("—", None), ("", None),
    ("630 bps", 630.0),
])
def test_parse_number(raw, expected):
    assert parse_number(raw) == expected

def test_billions_convert_to_millions():
    assert to_millions(3.9, "billion") == 3900.0
    assert to_millions(3623.0, "million") == 3623.0
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/test_units.py -v`
Expected: FAIL, module not found.

- [ ] **Step 3: Implement `units.py`**

Strip currency symbols, thousands separators, percent signs and `bps`. Treat parentheses as negation. Return `None` for em-dashes and empty strings so absent and malformed both resolve to absent — never to `0.0`, which would silently corrupt arithmetic.

- [ ] **Step 4: Run to confirm pass**

Run: `uv run pytest tests/test_units.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Write the failing table test against a real corpus document**

```python
# tests/test_tables.py
from pathlib import Path
from avws.tables import parse_pipe_tables, find_row
from avws.units import parse_number

ADI_8K = Path("challenge/offline-data/analog-devices/filings/"
              "2026-05-20__adi-us-20260520-q2-8k__1040581.md")

def test_extracts_adi_q2_revenue_from_results_table():
    tabs = parse_pipe_tables(ADI_8K.read_text(encoding="utf-8"))
    row = find_row(tabs, "Revenue")
    nums = [parse_number(c) for c in row]
    assert 3623.0 in [n for n in nums if n is not None]

def test_extracts_adi_adjusted_gross_margin_percentage():
    tabs = parse_pipe_tables(ADI_8K.read_text(encoding="utf-8"))
    row = find_row(tabs, "Adjusted gross margin percentage")
    nums = [n for n in (parse_number(c) for c in row) if n is not None]
    assert 73.0 in nums
```

- [ ] **Step 6: Run to confirm failure, implement, and confirm pass**

Run: `uv run pytest tests/test_tables.py -v`
Expected: FAIL then, after implementing pipe-table splitting on `|` with separator-row skipping, PASS (2 tests).

- [ ] **Step 7: Commit**

```bash
git add avws/tables.py avws/units.py tests/test_tables.py tests/test_units.py
git commit -m "feat: pipe-table extraction and unit normalisation with absent-vs-zero distinction"
```

---

### Task 4: Evidence ledger and LLM fact extraction

**Files:**
- Create: `avws/ledger.py`, `avws/llm.py`, `avws/extract.py`
- Test: `tests/test_ledger.py`

**Interfaces:**
- Consumes: `corpus.search`, `tables.parse_pipe_tables`, `registry.load_metrics`
- Produces:
  - `ledger.Fact` dataclass: `metric_key, company, period, value, unit, basis, source_doc, source_quote, confidence`
  - `basis` ∈ `{"reported","adjusted","guidance_mid","guidance_low","guidance_high","derived"}`
  - `ledger.append(fact: Fact) -> None` writing one JSON object per line to `ledger.jsonl`
  - `ledger.facts_for(metric_key: str) -> list[Fact]`
  - `llm.complete(system: str, user: str, schema: dict) -> dict` — structured output, temperature 0, logs to `logs/llm/`
  - `extract.extract_facts(metric) -> list[Fact]`

- [ ] **Step 1: Write the failing ledger test**

```python
# tests/test_ledger.py
import pytest
from avws.ledger import Fact, append, facts_for, reset

def test_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("avws.ledger.LEDGER_PATH", tmp_path / "l.jsonl")
    reset()
    append(Fact(metric_key="ADI:Revenue", company="Analog Devices", period="FY2026Q3",
                value=3900.0, unit="USDm", basis="guidance_mid",
                source_doc="filings/2026-05-20__adi-us-20260520-q2-8k__1040581.md",
                source_quote="we are forecasting revenue of $3.9 billion, +/- $100 million",
                confidence=0.95))
    got = facts_for("ADI:Revenue")
    assert len(got) == 1 and got[0].basis == "guidance_mid"

def test_rejects_unknown_basis():
    with pytest.raises(ValueError, match="basis"):
        Fact(metric_key="x", company="y", period="z", value=1.0, unit="USDm",
             basis="guess", source_doc="d", source_quote="q", confidence=0.5)

def test_rejects_fact_without_source_quote():
    with pytest.raises(ValueError, match="source_quote"):
        Fact(metric_key="x", company="y", period="z", value=1.0, unit="USDm",
             basis="reported", source_doc="d", source_quote="", confidence=0.5)
```

The third test is the important one: an unsourced fact must be impossible to construct, so traceability is enforced by the type rather than by discipline.

- [ ] **Step 2: Run to confirm failure, implement, confirm pass**

Run: `uv run pytest tests/test_ledger.py -v`
Expected: FAIL then PASS (3 tests).

- [ ] **Step 3: Implement the LLM provider**

`llm.complete` wraps the OpenAI client with `temperature=0` and a JSON schema response format. Before returning, write `logs/llm/<sha256(prompt)[:12]>.json` containing prompt, schema, response and timestamp. Raise on API errors — never return a default, so a broken key fails loudly instead of silently producing empty forecasts.

- [ ] **Step 4: Implement extraction with an extraction-only prompt**

The system prompt states: *you transcribe figures that appear in the supplied text; you never estimate, infer or forecast; if the figure is not present, return an empty list.* Each returned fact must carry a `source_quote` copied verbatim from the supplied text. Reject any fact whose `source_quote` is not a substring of the input — this catches fabrication mechanically rather than trusting the model.

- [ ] **Step 5: Commit**

```bash
git add avws/ledger.py avws/llm.py avws/extract.py tests/test_ledger.py
git commit -m "feat: append-only evidence ledger with quote-verified LLM fact extraction"
```

---

### Task 5: The three estimator families

**Files:**
- Create: `avws/estimators/base.py`, `guidance.py`, `buildup.py`, `seasonal.py`
- Test: `tests/test_estimators.py`

**Interfaces:**
- Consumes: `ledger.facts_for`
- Produces:
  - `base.Estimate` dataclass: `metric_key, value, method, assumptions: dict[str,float], derivation: str, inputs: list[Fact]`
  - `guidance.estimate(metric_key, facts) -> Estimate | None`
  - `buildup.estimate(metric_key, facts) -> Estimate | None`
  - `seasonal.estimate(metric_key, facts) -> Estimate` (never returns None)

- [ ] **Step 1: Write the failing guidance test**

```python
# tests/test_estimators.py
from avws.ledger import Fact
from avws.estimators import guidance

def _f(**kw):
    base = dict(metric_key="ADI:Revenue", company="ADI", period="FY2026Q3",
                unit="USDm", source_doc="d", source_quote="q", confidence=0.9)
    return Fact(**{**base, **kw})

def test_guidance_anchor_uses_midpoint_when_no_history():
    facts = [_f(value=3900.0, basis="guidance_mid")]
    est = guidance.estimate("ADI:Revenue", facts, residual_pct=0.0)
    assert est.value == 3900.0
    assert est.method == "guidance_anchor"

def test_guidance_anchor_applies_calibrated_residual():
    facts = [_f(value=3900.0, basis="guidance_mid")]
    est = guidance.estimate("ADI:Revenue", facts, residual_pct=0.015)
    assert round(est.value, 1) == 3958.5
    assert "residual" in est.assumptions

def test_returns_none_without_guidance_fact():
    facts = [_f(value=3623.0, basis="reported")]
    assert guidance.estimate("ADI:Revenue", facts, residual_pct=0.01) is None
```

- [ ] **Step 2: Run to confirm failure, implement, confirm pass**

Run: `uv run pytest tests/test_estimators.py -v`
Expected: FAIL then PASS (3 tests).

`Estimate.derivation` must be a human-readable string such as
`"guidance midpoint 3900.0 USDm × (1 + 0.0150 historical residual) = 3958.5"`.
This string is what appears in the evidence report and is what a judge reads when asking how a number was produced.

- [ ] **Step 3: Implement `buildup.py` with one composition function per metric**

Register composition functions in a dict keyed by metric key. Each consumes named facts and returns an `Estimate` whose `derivation` shows the arithmetic. Implement in priority order: `HAS:Net fees` (Σ divisional prior-year fees × disclosed growth), `ADI:Adjusted gross margin` (guided adjusted operating margin plus recent opex ratio), `HD:Net sales` (comp sales + net new stores + fx), `DE:Production & Precision Ag operating profit` (segment sales × margin). Metrics with no registered composition return `None`.

- [ ] **Step 4: Implement `seasonal.py`**

Build the historical series for the metric from ledger facts with `basis="reported"`, then apply quarter-share-of-year or YoY momentum. This estimator must always return a value — it is the guarantee against a blank cell.

- [ ] **Step 5: Commit**

```bash
git add avws/estimators tests/test_estimators.py
git commit -m "feat: guidance-anchor, build-up and seasonal-trend estimators with readable derivations"
```

---

### Task 6: Reconciler and backtest harness

**Files:**
- Create: `avws/reconcile.py`, `avws/backtest.py`
- Test: `tests/test_reconcile.py`

**Interfaces:**
- Consumes: `estimators.base.Estimate`
- Produces:
  - `reconcile.combine(estimates: list[Estimate], weights: dict[str,float]) -> Estimate`
  - `backtest.run(metric_key: str, periods: int) -> dict` with keys `mae`, `floor_hit_rate`, `n`

- [ ] **Step 1: Write the failing reconciler test**

```python
# tests/test_reconcile.py
from avws.estimators.base import Estimate
from avws.reconcile import combine

def _e(v, m): return Estimate(metric_key="k", value=v, method=m,
                              assumptions={}, derivation="", inputs=[])

def test_weighted_blend():
    out = combine([_e(100.0, "guidance_anchor"), _e(120.0, "seasonal_trend")],
                  {"guidance_anchor": 0.75, "seasonal_trend": 0.25})
    assert out.value == 105.0

def test_single_estimate_passes_through_unchanged():
    out = combine([_e(100.0, "build_up")], {"build_up": 1.0})
    assert out.value == 100.0 and out.method == "build_up"

def test_derivation_records_every_contributing_method():
    out = combine([_e(100.0, "guidance_anchor"), _e(120.0, "seasonal_trend")],
                  {"guidance_anchor": 0.75, "seasonal_trend": 0.25})
    assert "guidance_anchor" in out.derivation and "seasonal_trend" in out.derivation
```

- [ ] **Step 2: Run to confirm failure, implement, confirm pass**

Run: `uv run pytest tests/test_reconcile.py -v`
Expected: FAIL then PASS (3 tests).

- [ ] **Step 3: Implement the backtest harness**

For a target metric, iterate historical periods whose actual is present in the ledger as a `reported` fact. Hide facts published on or after that period's report date, run the estimator chain, and compare with the actual. Report mean absolute error and **floor-band hit rate** — the fraction of periods where absolute error fell below the competition floor (0.5pp for percentages, 0.5% of actual for money and EPS).

- [ ] **Step 4: Feed backtest output into reconciler weights**

Weights are inverse to measured error, normalised across the estimators available for that metric. Persist to `.cache/weights.json` so the final run does not need to re-backtest.

- [ ] **Step 5: Verify the backtest discriminates before trusting it**

Run it once against a deliberately corrupted estimator that returns the prior-year value unchanged. Its floor-hit-rate must be visibly worse than the real estimator's. A harness that scores everything similarly is measuring nothing and must be fixed before its weights are used.

- [ ] **Step 6: Commit**

```bash
git add avws/reconcile.py avws/backtest.py tests/test_reconcile.py
git commit -m "feat: inverse-error reconciler and backtest harness reporting floor-band hit rate"
```

---

### Task 7: Validation gate with adversarial critic

**Files:**
- Create: `avws/validate.py`
- Test: `tests/test_validate.py`

**Interfaces:**
- Consumes: `Estimate`, `ledger.facts_for`, `llm.complete`
- Produces:
  - `validate.check_scale(metric, value, history: list[float]) -> list[str]`
  - `validate.check_identities(values: dict[str,float], facts) -> list[str]`
  - `validate.critique(estimate: Estimate) -> dict` with keys `objection: str | None`, `is_concrete: bool`
  - `validate.gate(estimate, metric, history) -> tuple[Estimate, list[str]]`

- [ ] **Step 1: Write the failing scale tests**

```python
# tests/test_validate.py
from avws.registry import load_metrics
from avws.validate import check_scale

def _m(key): return next(m for m in load_metrics() if m.key == key)

def test_flags_percentage_entered_as_fraction():
    issues = check_scale(_m("HD:Comparable sales, total company"), 0.012, [0.6, 1.4, 2.1])
    assert any("scale" in i.lower() for i in issues)

def test_flags_revenue_off_by_a_thousand():
    issues = check_scale(_m("ADI:Revenue"), 3_900_000.0, [3623.0, 3200.0, 2900.0])
    assert issues

def test_accepts_a_plausible_value():
    assert check_scale(_m("ADI:Revenue"), 3900.0, [3623.0, 3200.0, 2900.0]) == []
```

The first test encodes the exact failure the scoring cap punishes hardest: entering `0.012` where `1.2` was meant.

- [ ] **Step 2: Run to confirm failure, implement, confirm pass**

Run: `uv run pytest tests/test_validate.py -v`
Expected: FAIL then PASS (3 tests).

Implement `check_scale` as a ratio test against the historical band: flag when the value is outside `[min(history)/4, max(history)*4]`, and additionally flag percentage metrics whose absolute value is below 0.05 when history is above 0.5.

- [ ] **Step 3: Implement cross-metric identities**

Three checks, each returning a message when violated by more than a stated tolerance:
- adjusted diluted EPS × diluted share count ≈ adjusted net income (10% tolerance)
- adjusted gross margin % × revenue ≈ adjusted gross profit (2% tolerance)
- segment operating profit < total operating profit (hard)

Tolerances are wide on purpose: these catch order-of-magnitude errors, not rounding.

- [ ] **Step 4: Implement the adversarial critic**

One `llm.complete` call receiving only the number, its derivation string, its source quotes and any identity-check messages. The system prompt instructs the model to argue the number is **wrong** and to state the single strongest concrete objection, returning `{"objection": str|null, "is_concrete": bool}` where `is_concrete` is true only when the objection names a specific figure, unit or period that can be checked. Vague objections are discarded.

- [ ] **Step 5: Wire the gate with a bounded re-derivation**

`gate()` runs scale, then identities, then the critic. A concrete objection triggers exactly one re-derivation with the objection appended as a constraint. If the second attempt still fails, return the better of the two estimates **with a loud `WARN` in the log** — never `None`, never a blank.

- [ ] **Step 6: Commit**

```bash
git add avws/validate.py tests/test_validate.py
git commit -m "feat: validation gate with scale bands, cross-metric identities and adversarial critic"
```

---

### Task 8: End-to-end run, evidence reports and clear-run log

**Files:**
- Modify: `avws/run.py`
- Create: `avws/report.py`

**Interfaces:**
- Consumes: everything above
- Produces: four workbooks, `forecasts.json`, `research/<TICKER>.md`, `logs/run-<ts>.log`

- [ ] **Step 1: Replace the skeleton pipeline with the real chain**

For each of the 12 metrics: extract facts → run all eligible estimators → reconcile → gate → collect. Then write four workbooks. Log every stage with an ISO-8601 timestamp.

- [ ] **Step 2: Write the evidence report generator**

`research/<TICKER>.md` contains, per metric: the final number, the derivation string, each contributing estimate with its weight, every source quote with its document path, and any validation messages. This is the artifact to open when a judge asks where a number came from.

- [ ] **Step 3: Run end-to-end and confirm the official checker passes**

Run: `uv run python -m avws.run --all && npm run check:forecasts`
Expected: four workbooks written; checker PASS.

- [ ] **Step 4: Verify the log records what actually happened**

Read `logs/run-<ts>.log` and confirm every metric has an extraction count, the estimators that fired, the chosen weights and the gate outcome. A log that only records successes cannot show the system caught anything.

- [ ] **Step 5: Commit**

```bash
git add avws/run.py avws/report.py
git commit -m "feat: end-to-end run producing workbooks, evidence reports and timestamped log"
```

---

### Task 9: Architecture write-up

**Files:**
- Modify: `architecture/index.html`

**Interfaces:**
- Consumes: the spec, the backtest table, the run log
- Produces: a self-contained HTML page under 2 MB with no scripts or external assets

- [ ] **Step 1: Draft the page from the spec**

Sections: what the system does; the scoring insight that shaped it; the per-company information-structure routing; the pipeline diagram; the validation gate; measured backtest results; known weaknesses; how to reproduce.

- [ ] **Step 2: Draw the diagram as inline SVG**

It must match the real module structure, because the rubric awards 10 points for the diagram matching the system and being reproducible from the instructions. Check each labelled box against an actual module in `avws/`.

- [ ] **Step 3: Include the backtest table with real measured numbers**

Copy the numbers from the harness output. If a number is not measured, say so explicitly rather than presenting an estimate as a measurement.

- [ ] **Step 4: Write the weaknesses section from the spec, adding anything discovered during the build**

Six points are available for honesty and self-knowledge. Failed approaches count. Record them as they happen rather than reconstructing them at 15:00.

- [ ] **Step 5: Verify it opens standalone and contains no secrets**

Run: `grep -iE "sk-|api[_-]?key|@gmail" architecture/index.html`
Expected: no matches.

- [ ] **Step 6: Commit**

```bash
git add architecture/index.html
git commit -m "docs: architecture write-up with diagram, backtest results and known weaknesses"
```

---

## Self-Review

**Spec coverage:** §4.1 data/tooling → Tasks 2, 3; §4.2 estimators → Task 5; §4.3 validation → Task 7; §4.4 backtest → Task 6; §4.5 run harness → Tasks 1, 8; §5 provenance → done pre-plan; §6 weaknesses → Task 9 Step 4; §2 scoring constraints → enforced in Task 1 Step 7 (never-blank) and Task 7 Step 1 (scale).

**Type consistency:** `Metric.key` is `"{ticker}:{label}"` throughout. `Fact` fields are identical in Tasks 4, 5, 7. `Estimate` fields are identical in Tasks 5, 6, 7. `basis` vocabulary is fixed in Task 4 and reused unchanged in Task 5.

**Known gap, accepted deliberately:** Task 5 Step 3 implements four build-up compositions, not twelve. The remaining eight metrics fall through to guidance-anchor or seasonal-trend. This is a time-driven choice, and it is disclosed in the architecture write-up rather than hidden.
