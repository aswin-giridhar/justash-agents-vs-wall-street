# Judge conversation crib sheet — 5 minutes

Judges want to understand what was built and why. They said explicitly: no polished
pitch needed. Lead with the reasoning, not the feature list.

---

## The 45-second opener

> "We started by reading the scoring function rather than the companies. Two things
> fall out of it. First, the denominator floor means you don't have to beat consensus
> to score under 1.0 — if your absolute error is inside the floor, you win that metric
> whatever Wall Street did. Second, the cap is 5.0 while the floor band is 1.0, so one
> scale error costs more than several good forecasts earn back. That's why our
> validation layer is a subsystem with its own tests rather than a lint pass.
>
> The second thing we found was in the corpus, not the model: the four companies are
> in completely different information states. Hays' financial year *already ended* on
> 30 June and its Q4 update discloses divisional net-fee growth, so that's a
> reconstruction problem. ADI guided Q3 explicitly — $3.9 billion plus or minus a
> hundred million — so that's an anchoring problem. Home Depot and Deere only have
> full-year guidance, so those need phasing. We route each metric to the estimator its
> evidence actually supports rather than running one prompt over all four."

Then stop and let them ask. Do not narrate the module list.

## If they ask: "how does it reason instead of just asking a model for a number?"

Three-part answer:

1. **Extraction never forecasts.** The model transcribes figures that physically
   appear in the text. Its `source_quote` is checked as a substring of what we
   supplied — fabrication is caught mechanically, not trusted away.
2. **The ledger enforces the analyst distinction as a type.** A `Fact` cannot be
   constructed without a `basis` — reported / adjusted / guidance — or without a
   quote. Both raise. ADI reported GAAP EPS of $2.40 and adjusted EPS of $3.09 in the
   same quarter; five of our twelve metrics are adjusted and Deere's is explicitly
   GAAP.
3. **The model sets named assumptions; Python does the arithmetic.** The build-up
   estimator has a registered formula per metric. The model's only job is to source
   each component and say which fact it used. So the output is
   "H1 actual + H2 prior-year grown at x%" — checkable line by line.

## If they ask: "what did you try that didn't work?"

Lead with this one, it's the most interesting:

> "Our seasonal estimator forecast from the wrong anchor — it extrapolated from the
> latest quarter instead of the same quarter a year earlier, so it carried a Q2 level
> into a Q3 slot and produced $4,964m for ADI against company guidance of $3,900m.
> The adversarial critic caught it on the first end-to-end run."

And the sharpest one:

> "We had plausibility bands that would have rejected a bad reading instantly — but
> we'd only wired them into the deterministic table path, not the model path. So a
> line saying 'FX negatively impacted comparable sales by approximately 40 basis
> points' entered the ledger as minus 40 percentage points and dragged Home Depot's
> comparable sales to minus 25 percent. The critic diagnosed it unprompted — it wrote
> 'that figure is 40 bps, not −40%, and it refers to the FX impact component, not
> total comparable sales.' We'd built the defence and left half the door open."

That answer does three things at once: shows honesty, shows the gate does real work,
and shows you understand the domain distinction it caught.

## If they ask: "isn't guidance anchoring obvious?" — YES, say this

This is the question most likely to separate you from the field. Answer it before
they ask if you can.

> "It is, and a forecast equal to consensus scores exactly 1.0 — par. So anchoring is
> our baseline, not our answer. Two things move us off it.
>
> First, we measure how the company has historically landed against its *own*
> guidance, rather than assuming the midpoint is unbiased. ADI's last release says
> outright that Q2 came in above the high end of its outlook.
>
> Second — and this is the part I think is underused — the frozen corpus contains
> documents published *after* the guidance was issued. ADI guided Q3 on 20 May; there's
> a conference transcript from 2 June. Deere's Q2 call was 21 May with an investor
> presentation on 26 May. That window is strictly newer information than the anchor,
> and it's where management talks about bookings, backlog, channel inventory and
> pricing — leading indicators for the quarter we're forecasting.
>
> So we extract those signals, verify each quote against the source, and convert them
> into a bounded tilt: guidance midpoint, times the measured historical residual, times
> a post-guidance signal capped at 2.5% on revenue. It's a nudge, not a driver, because
> the scoring function punishes a big miss ten times harder than it rewards a small
> win.
>
> On ADI it read a pricing signal and tilted +0.75%. On Deere it found nothing and
> returned zero rather than inventing something. An estimator that always finds a
> signal isn't measuring one."

## If they ask: "why no embeddings / why no live web?"

> "Financial retrieval here is literal — 'adjusted gross margin', 'net fees',
> 'Production & Precision Ag' are exact strings in the filings. BM25 finds them, costs
> no tokens, and is byte-for-byte reproducible between runs. And a local-only run has
> no network dependency at 17:15, which mattered more than usual because this venue's
> network intercepts TLS and broke both our package installs and our first API calls."

## If they ask: "how do you know it's any good?"

> "We can't measure what we're graded on, because the Wall Street benchmark isn't
> visible. But we can measure the statistic the scoring function is built from: how
> often our absolute error lands inside the denominator floor. `python -m avws.backtest`
> replays the identical chain over historical periods whose answers are in the corpus
> and reports floor-band hit rate per metric."

Be honest about the leak: facts are filtered by reporting period, not by document
publication date, so a restatement could carry later knowledge.

## Numbers to have in your head

| Fact | Value |
|---|---|
| Documents in corpus | 1,139 |
| Tests | 69 |
| Metrics with a registered build-up | 9 of 12 |
| Metrics directly guided (need no build-up) | 2 (ADI revenue, ADI adj EPS) |
| Weakest single metric | DE worldwide net sales — no guidance, no composition |
| Signal tilt caps | 2.5% revenue, 6% EPS, 1.0pp margins |
| ADI Q3 guidance | revenue $3.9bn ±$100m; adj EPS $3.30 ±$0.15 |
| ADI Q2 GAAP vs adjusted EPS | $2.40 vs $3.09 |
| Hays FY2025 group net fees | £972.4m |
| Hays FY2026 year end | 30 June 2026 (closed) |
| Denominator floors | 0.5pp; 0.5% of reported |

## Things NOT to say

- Do not claim the backtest proves accuracy. It measures floor-band hit rate on
  history, which is evidence, not proof.
- Do not describe the system as multi-agent. It is a pipeline with one adversarial
  review pass, and calling it more than it is invites a question you'll lose.
- Do not oversell the Hays edge as certainty. The year is closed and *partly*
  disclosed; the operating profit still needs modelling.
- Do not read the module list aloud. They have the repo and the HTML.

## The closing line if you have 20 seconds left

> "The through-line is that every design decision came from the scoring function or
> from something we found in the filings — not from taste about how agents should be
> built. Complexity earns no points by itself, so we deliberately stopped at three
> estimators and one critic pass."
