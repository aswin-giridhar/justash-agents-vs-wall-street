# Final-window runbook

Deadlines do not move: **17:00** social, **17:15** HTML locks and final run opens,
**17:30** uploads open, **18:00** everything must be in.

---

## Before 17:00

- [ ] Post the X thread and the LinkedIn post, then link both replies
      (`Context/social-posts.md`). $500 each, judged on insight not engagement.
- [ ] GitHub repo created and pushed, URL matches `entry.json`:
      ```
      gh repo create justash-agents-vs-wall-street --public --source=. --remote=origin
      git push -u origin main
      ```
- [ ] Read `docs/judge-crib-sheet.md` once more.

## 16:00 — judge conversation

Five minutes, one judge pair. Lead with the scoring-function reasoning, not the
module list. Have the crib sheet open.

## By 17:15 — lock the architecture HTML

- [ ] `architecture/index.html` describes the system that will actually run
- [ ] Backtest numbers in it are copied from `.cache/backtest.json`, not estimated
- [ ] Under 2 MB, no scripts, no external assets, no secrets:
      ```
      grep -iE "sk-|api[_-]?key|@gmail|<script" architecture/index.html
      ```
- [ ] Commit and note the hash — this is the version judges see

## 17:15–18:00 — the final run

```bash
# 1. confirm the tree is committed and record the hash
git status --short
git rev-parse HEAD

# 2. the one command, no human input
python -m avws.run --all

# 3. structural check
npm run check:submission
```

Expect roughly **10 minutes** for the run with concurrency. If it crashes, fix,
recommit, and rerun — the rules allow retries inside the window provided one clear
run completes.

If the network fails: the run needs the OpenAI API. There is no offline fallback.
If the API is unreachable, the workbooks from the **last successful run** are already
in `submission/` and remain valid to upload — check their timestamps and say so
honestly if asked.

## Then, before 18:00

- [ ] Put the final commit hash into `entry.json` → `submission.finalCommit`
- [ ] `npm run check:submission` passes
- [ ] Copy `logs/run-<ts>.log` — the clear-run log is a required submission item
- [ ] Upload each workbook to its matching company on openstocks.com:
      - `submission/HD-FY2026Q2.xlsx`
      - `submission/ADI-FY2026Q3.xlsx`
      - `submission/HAS-FY2026.xlsx`
      - `submission/DE-FY2026Q3.xlsx`
- [ ] Submit the private form at openstocks.com/hackathon with `entry.json` and
      `architecture/index.html` attached. Agent name **JustAsh**, primary contact
      and repository URL must match the file.

## Sanity check the numbers before uploading

Open `forecasts.json` and ask of each figure:

| Metric | Unit trap to check |
|---|---|
| HD Net sales | USD **millions** (~45,000, not 45 or 45,000,000) |
| HD Adjusted diluted EPS | dollars per share (~4-5) |
| HD Comparable sales | percentage **points** (1.2 means 1.2%, not 0.012) |
| ADI Revenue | USD millions (~3,900) |
| ADI Adjusted diluted EPS | dollars per share (~3.3) |
| ADI Adjusted gross margin | percentage points (~73, not 0.73) |
| HAS Net fees | GBP millions (~900-1,000) |
| HAS Pre-exceptional basic EPS | **pence** (6.2 means 6.2p, not £0.062) |
| HAS Pre-exceptional operating profit | GBP millions (~50-100) |
| DE Worldwide net sales and revenues | USD millions (~11,000) |
| DE Diluted EPS **(GAAP)** | dollars per share — GAAP, not adjusted |
| DE Production & Precision Ag operating profit | USD millions |

A single unit error caps that metric at 5.0, which alone adds 0.42 to the average.
This table is the last line of defence and takes two minutes.
