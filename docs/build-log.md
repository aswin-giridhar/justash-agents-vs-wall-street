# Build log — 16 August 2026

Decisions, problems and dead ends, recorded as they happened. Reconstructing this
at 15:00 from memory would produce a tidier and less truthful account.

---

## 11:00 — Scoring analysis drove the architecture

Read the accuracy formula before designing anything:

```
score = min(5.0, |team − actual| / max(|WallSt − actual|, floor))
floor = 0.5pp (percentages) | 0.5% × |actual| (money, EPS)
```

Three conclusions that shaped every later choice:

1. The floor **decouples us from Wall Street when consensus is accurate**. Absolute
   error below the floor scores under 1.0 regardless of how good consensus was. The
   objective is absolute precision, not contrarianism.
2. The cap is 5.0 and the floor band is 1.0, so **downside is 10× the upside band**.
   Eleven metrics at 0.6 plus one at 5.0 averages 0.97 — par. One scale error erases
   the entire edge.
3. A **missing forecast scores 5.0**, so no chain may terminate in `None`.

This is why validation is a subsystem rather than a lint pass, and why
`SeasonalTrend` exists purely as a guaranteed-non-null fallback.

## 11:05 — The four companies have different information structures

The exploitable fact of this challenge, found by reading the corpus rather than
assuming symmetry:

| Company | State | Method |
|---|---|---|
| HAS | FY2026 ended 30 June 2026; 10 July trading update discloses Q4 divisional net-fee growth | Reconstruction |
| ADI | Q2 8-K guides Q3: revenue $3.9bn ±$100m, adj EPS $3.30 ±$0.15 | Guidance anchor |
| HD | FY2026 guidance reaffirmed; Q2 not separately guided | Phasing + build-up |
| DE | FY guidance; Q3 not separately guided | Phasing + build-up |

A single uniform prompt across four companies discards this. Routing each metric to
the estimator its evidence supports is both the accuracy edge and the architecture
argument.

## 11:12 — Chose not to republish the document corpus

`THIRD_PARTY_NOTICES.md` states the documents under `challenge/offline-data/` carry
no redistribution licence. Rather than fork the starter's history, git was
re-initialised so the first commit is timestamped today and labelled as vendored
scaffolding, with the corpus gitignored. This also gives the organisers exactly what
the anti-pre-built rule asks for: a history containing only today's work.

## 11:25 — Verified the output format before writing any logic

The first thing built was not the forecaster. It was proof that `openpyxl` can write
the supplied templates and still pass the official `npm run check:forecasts`.

Layout, identical across all four templates: sheet `Summary`, headers on row 6,
metrics on rows 7–9 with the label in column A, units in column B, and the **empty
forecast cell in column C**. Checker returned PASS on all four with placeholder
numbers.

Consequence: a submittable artifact existed at 11:25. Everything after this improves
numbers rather than creating the ability to submit at all.

## 11:30 — Venue network intercepts TLS; two tools broke, one diagnosis fixed both

`uv pip install` failed with `invalid peer certificate: UnknownIssuer`, and the
OpenAI SDK then failed with `APIConnectionError`. Two symptoms, and the temptation
was to treat them as separate problems.

Instead, one discriminating test: `curl` against `api.openai.com` returned **HTTP 401
with `ssl_verify_result=0`** — reachable, and its certificate verified. curl uses the
Windows certificate store; Python and `uv` use their own bundled stores. So the
network was not blocking anything; it was presenting a private CA that only the OS
store trusts.

Fixes:
- Dependencies: `pip --trusted-host pypi.org --trusted-host files.pythonhosted.org`
  (`uv` had no venv-local pip, so the venv was rebuilt with `py -3 -m venv`).
- Runtime: `truststore.inject_into_ssl()`, which routes Python TLS through the OS
  trust store. Added as a real dependency, called at process start in `avws/config.py`.

Rejected `verify=False` — it would have worked and would have disabled certificate
checking for every request the agent makes, including the API calls carrying the key.

Worth recording because the same interception will still be present at 17:15. A run
that works now works then; one relying on an ad-hoc environment variable might not.

## 11:33 — `.env` lives outside the repository

`python-dotenv`'s `find_dotenv` walks parent directories, so the key sits in the
project root *above* the git repository. It cannot be committed even by accident.
