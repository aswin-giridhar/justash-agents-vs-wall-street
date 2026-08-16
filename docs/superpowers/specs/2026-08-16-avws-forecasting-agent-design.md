# JustAsh — Agents vs Wall Street forecasting agent

**Date:** 16 August 2026
**Team:** JustAsh (Aswin Giridhar, solo)
**Event:** Agents vs Wall Street, AI Tinkerers London
**Status:** Approved design, pre-implementation

---

## 1. Problem

Forecast 12 figures — three per company — for four companies reporting in the week of 17 August 2026.

| Company | Period | Metrics | Unit |
|---|---|---|---|
| Home Depot (HD) | FY2026 Q2 | Net sales | USDm |
| | | Adjusted diluted EPS | USD/share |
| | | Comparable sales, total company | % |
| Analog Devices (ADI) | FY2026 Q3 | Revenue | USDm |
| | | Adjusted diluted EPS | USD/share |
| | | Adjusted gross margin | % |
| Hays plc (HAS) | FY2026 | Net fees | GBPm |
| | | Pre-exceptional basic EPS | GBp |
| | | Pre-exceptional operating profit | GBPm |
| Deere & Co (DE) | FY2026 Q3 | Worldwide net sales and revenues | USDm |
| | | Diluted EPS (GAAP) | USD/share |
| | | Production & Precision Ag operating profit | USDm |

Output: four `.xlsx` workbooks built from supplied templates, written to `submission/`.

## 2. What the scoring function actually rewards

Accuracy score per metric:

```
score  = min(5.0, |team − actual| / max(|WallSt − actual|, floor))
floor  = 0.5pp                for percentage metrics
floor  = 0.5% × |actual|      for money and EPS metrics
final  = mean of the 12 metric scores
```

Three consequences drive the whole design:

1. **The floor decouples us from Wall Street when consensus is good.** If consensus is near-perfect the denominator does not collapse to zero; it is pinned at the floor. Absolute error below the floor therefore scores below 1.0 regardless of how good consensus was. The objective is *absolute precision inside the floor band*, not contrarianism.
2. **Downside is 10× the upside band.** The cap is 5.0; the floor band is 1.0. Eleven metrics at a strong 0.6 plus one metric capped at 5.0 averages 0.97 — par. A single scale or unit error erases the entire edge.
3. **A missing forecast scores 5.0.** No metric may ever be blank. Every estimator chain must terminate in a number.

Design consequence: **variance control and unit safety outrank cleverness.** This is why validation is a first-class subsystem rather than a lint pass.

## 3. Key insight: the four companies have different information structures

The exploitable fact of this challenge. A uniform prompt over four companies discards it.

| Company | Information state | Dominant method |
|---|---|---|
| **HAS** | FY2026 ended 30 June 2026. The 10 July Q4 trading update discloses actual Q4 net-fee growth by division. Period is closed. | **Reconstruction** — arithmetic from disclosed components |
| **ADI** | Q2 8-K guides Q3 explicitly: revenue $3.9bn ±$100m, adj operating margin 49.0% ±100bps, adj EPS $3.30 ±$0.15 | **Guidance anchor + calibrated residual** |
| **HD** | Full-year guidance only; Q2 not separately guided | **Phasing + build-up** |
| **DE** | Full-year net income guidance; Q3 not separately guided | **Phasing + build-up** |

Routing each metric to the estimator its evidence supports is both the accuracy edge and the architecture story.

## 4. Architecture

```
corpus/           index + BM25 retrieval over frozen markdown
  ↓
extract/          LLM extracts FACTS ONLY into typed evidence ledger
  ↓
ledger.jsonl      append-only: value, unit, basis, source_doc, source_quote
  ↓
estimators/       GuidanceAnchor | BuildUp | SeasonalTrend   (deterministic arithmetic)
  ↓
reconcile/        inverse-backtest-error weighting + declared overrides
  ↓
validate/         scale bands → cross-metric identities → adversarial critic
  ↓
workbook/         write 4 xlsx from templates
```

### 4.1 Data and tooling layer

**Corpus index** (`avws/corpus.py`). Every document carries YAML frontmatter (company, ticker, `published_at`, `document_type`, `period`, `source_url`). Parse into a JSON index. Retrieval = metadata filter + BM25 over chunks.

*Trade-off — no embeddings.* Financial retrieval here is literal ("adjusted gross margin", "net fees", "Production & Precision Ag"). Lexical search matches better, costs no tokens, and is byte-for-byte reproducible. Embeddings would consume build time for worse determinism.

**Table extractor** (`avws/tables.py`). Documents contain pipe-tables. Parse and normalise: `$`, `%`, `(123)` negatives, thousands separators, bps.

**Evidence ledger** (`ledger.jsonl`), append-only:

```json
{"metric_key","company","period","value","unit","basis",
 "source_doc","source_quote","confidence"}
```

`basis` ∈ {reported, adjusted, guidance_mid, guidance_low, guidance_high, derived}. Conflating reported with adjusted is a top failure mode for naive systems; making it a required field forces the distinction.

**Extraction never forecasts.** The LLM's only job at this stage is faithful transcription of a figure plus its verbatim quote.

**Unit normalisation** is its own module, justified by consequence 2 above.

### 4.2 Estimators

The LLM sets *named assumptions*; arithmetic is deterministic code.

- **GuidanceAnchor** — guidance midpoint plus a residual measured from how the company historically landed against its own guidance. Requires a `guidance_*` fact.
- **BuildUp** — identity composition. HAS: Σ(divisional FY25 fees × disclosed growth). ADI: adj GM% from guided adj operating margin plus recent opex ratio. HD: net sales from comp sales + net new stores + fx. DE: segment sales × segment margin.
- **SeasonalTrend** — quarter-share-of-year and YoY momentum from the historical series. Always available, guaranteeing no blank metric.

**Reconciler** — inverse-backtest-error weighting per metric, with explicit declared overrides where one estimator is structurally dominant (HAS reconstruction). Overrides are declared in the write-up, not hidden in a weight.

### 4.3 Validation gate

Ordered, and every rejection is logged:

1. **Scale/unit check** against historical bands.
2. **Cross-metric identities** — EPS × diluted shares ≈ net income; GM% × revenue ≈ gross profit; segment operating profit < total operating profit.
3. **Adversarial critic** — one LLM pass seeing only the number, its evidence chain and identity results, instructed to *refute* it and defaulting to "this is wrong." A concrete checkable objection triggers exactly one re-derivation with the objection as a constraint. Both passes logged.

On repeated failure: emit best estimate with a loud log flag. **Never blank** — a blank scores 5.0.

### 4.4 Backtest harness

Replay the identical pipeline on historical periods whose answers are in the corpus. Report per metric family:

- Mean absolute error
- **Floor-band hit rate** — fraction of periods where absolute error < the floor. This is the statistic that maps directly onto the competition scoring function.

Backtest output calibrates GuidanceAnchor residuals and sets reconciler weights. It is also the evidence for the rubric line "show what improves the result."

### 4.5 Run harness

- One command, no human input: `python -m avws.run --all`
- Outputs: four workbooks in `submission/`, `logs/run-<ts>.log`, per-company evidence reports, `forecasts.json`
- Every LLM call logged (prompt hash + response) to `logs/llm/`; temperature 0
- Provider behind a thin interface so model/key swaps do not touch logic

## 5. Repository and provenance

Git re-initialised on 16 Aug 2026 so history contains only today's work. First commit vendors the official starter scaffolding, explicitly labelled. `challenge/offline-data/` is gitignored — `THIRD_PARTY_NOTICES.md` grants no redistribution licence. `.env` and `entry.json` are gitignored.

The starter repository is declared as a pre-existing component in `entry.json`, as the rules require.

## 6. Known weaknesses

Stated here so they carry into the architecture write-up honestly.

- **Guidance residuals are calibrated on few observations.** A handful of guided quarters per company is a small sample; the residual is a point estimate with real dispersion.
- **BM25 misses paraphrase.** A metric described in unusual wording may not be retrieved. Mitigated by metadata filtering to the right document, not by better ranking.
- **The critic can be wrong in both directions.** It may accept a bad number or reject a good one. It is capped at one re-derivation to bound that damage.
- **HD and DE have the weakest information.** Full-year guidance plus phasing is materially less informative than ADI's explicit quarterly guidance or HAS's closed period. Expect the widest errors there.
- **No live market data.** Consensus is not observable to us; we cannot check our numbers against the benchmark we are scored against.

## 7. Timeline

| Time | Milestone |
|---|---|
| 11:15 | Build starts |
| 12:00 | Corpus index, retrieval, table extractor working |
| 13:30 | End-to-end path producing all 12 numbers — submittable from here |
| 14:30 | Estimators and reconciler complete |
| 15:15 | Validation gate, critic, backtest |
| 14:00→ | Architecture HTML drafted in parallel |
| 15:45 | Judge-ready |
| 16:00 | Judge conversation |
| 17:15 | HTML locks, final run window |
| 18:00 | Hard deadline |
