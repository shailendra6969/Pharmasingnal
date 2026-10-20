# PharmaSignal

**Detect signals. Retrieve evidence. Verify claims.**

PharmaSignal is an evidence-verified adverse-event signal intelligence prototype built on public FAERS data and FDA documentation. It processes multi-quarter FAERS data, computes full-background disproportionality statistics (PRR/ROR), analyzes quarterly trends, retrieves FDA label evidence via section-aware RAG, and runs a tool-using agentic investigation workflow whose claims are **independently verified** before display.

> **Status:** Phases 1–2 implemented (data foundation + statistical engine). See `docs/plans/` for the full plan.

## Scientific position

Decision-support prototype based on public spontaneous-reporting data. **Not** a clinical decision-support, diagnostic, treatment-recommendation, causality-assessment, or medical-advice system. FAERS disproportionality results are **screening hypotheses, not proof of causation**. Signal ≠ causation, ≠ incidence, ≠ risk probability.

## Architecture (target)

```
Web UI → Flask API → Signal Service / RAG Service / LangGraph Agent
                        ↓                ↓              ↓
              PostgreSQL (FAERS, signals) pgvector   Tools (SQL/Signal/Trend/RAG)
                        ↑                             ↓
              FAERS pipeline                  Verification engine
                                        (numeric / citation / causal-language)
```

## Repository layout

See spec §55 — `app/` (layered backend), `frontend/`, `data/` (raw/processed/samples, raw never committed), `scripts/`, `tests/`, `migrations/`, `docs/`.

## Status: Phases 1–2 — data foundation + statistical engine (implemented)

Reproducible FAERS ingestion pipeline: **download → parse → validate → dedup/version handling → normalize → PostgreSQL → data-quality report**.

### Setup

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env            # adjust DATABASE_URL if needed
.venv/bin/python scripts/create_databases.py
.venv/bin/python scripts/migrate.py          # dev DB
.venv/bin/python scripts/migrate.py --test   # test DB
```

Activating `.venv` is optional: a bare `pytest`, `python scripts/<tool>.py` or
`./scripts/<tool>.py` run on any interpreter (Anaconda base, Homebrew, system
`python3`) detects — via `app/bootstrap.py` — that a pinned dependency such as
`psycopg2` is missing and silently re-runs the same command under `.venv/`,
keeping the exit code. In a complete interpreter (CI, Docker, an activated
virtualenv) the bootstrap does nothing at all.

To see which interpreter a command would actually run under, and whether that
fallback is armed, ask the bootstrap itself:

```bash
python -m app.bootstrap --check
```

It prints the repository root, the running interpreter, the pinned-dependency
status, the `.venv/` target, the interpreter the bootstrap would *select*, and
a machine-readable `venv_fallback_armed: true|false` line naming the condition
behind it. Exit codes: **0** = configuration valid and an interpreter
resolvable (complete here, or armed to re-run under `.venv/`), **1** = invalid
or unresolvable (no `.venv/`, a broken `.venv/`, or an invocation that cannot
be replayed), **2** = bad usage — so it doubles as a pre-flight probe in a CI
job or runbook step before the real command is attempted — both project
workflows open with it, so a misconfigured runner fails in seconds rather
than minutes into the real work. It is strictly
read-only: it never creates or repairs `.venv/`, never invokes pip, and never
opens the database or the network.

One caveat the self-test makes explicit: replaying a command line normally
reads `sys.orig_argv`, which only exists on Python 3.10+. Interpreters older
than that — notably macOS's `/usr/bin/python3` shim, which is 3.9 — have no
real command line, so the bootstrap reconstructs one from what such an
interpreter does keep (`sys.argv`, `__main__.__spec__`, and runpy's frame in
the instant before the spec exists) and replays exactly as usual. The
reconstruction cannot recover interpreter flags (`-i`, `-O`, `-X`), and an
invocation that leaves no trace at all — a REPL, `python -c`, `python -` —
still refuses with a message naming the interpreter's age and tells you to
activate `.venv` instead.

### Doctor

The self-test answers one question — which interpreter would this command run
under? The doctor answers the rest of the local setup in one pass:

```bash
python scripts/doctor.py            # offline; add --network to also ping DailyMed
```

It prints one `PASS` / `WARN` / `FAIL` line per check: the interpreter and
bootstrap state in force, every `requirements.txt` pin importable *at the
pinned version* (version drift included), `.env` configuration (key names
only — never values) and which API keys are set, both database servers with
their migration state, the
committed test fixtures, and the FAERS / processed data directories. Warnings
are normal on a fresh clone (no raw data, no LLM keys) and do not fail the
run; only a `FAIL` marks a broken setup, each with the command that would fix
it. Exit codes: **0** = no failing check, **1** = at least one check failed,
**2** = bad usage. Like the self-test it is strictly read-only — PostgreSQL
is touched with `SELECT`s only, and nothing is ever created or repaired
inline.

### Run the pipeline

```bash
# Sample fixtures (fast; used by tests)
.venv/bin/python scripts/run_ingestion.py --fixtures

# Real FAERS quarters: download first, then ingest
.venv/bin/python scripts/download_faers.py --quarters 2024q1,2024q2,2024q3,2024q4
.venv/bin/python scripts/run_ingestion.py --quarters 2024q1,2024q2,2024q3,2024q4

# Add a NEW quarter end-to-end: download + ingest + recompute signals
.venv/bin/python scripts/refresh_quarter.py --quarter 2026q1
```

**Quarterly refresh** ([refresh_quarter.py](scripts/refresh_quarter.py)) is the one-command update path once a new FAERS quarter is published: it downloads the archive, ingests it (with the cross-quarter dedup pass, so superseded versions from *all* quarters are re-flagged), recomputes `signal_scores` / `signal_quarterly` over the enlarged background, re-runs the SIDER label-concordance validation and records its numeric delta vs the previous refresh, audits the refreshed screen for reporting-regime artifacts, adds quarterly-PRR profile verdicts for the top signals, and re-runs the indication-bias (confounding) probe. Every stage is verified before the next starts:

- **download** — extracted DEMO/DRUG/REAC files must exist (a partial or unpublished archive fails loudly; FAERS publishes each quarter ~2 months after close, so a 404 usually means “not published yet”).
- **ingest** — the quarter must have loaded cases, and the dedup invariant (exactly one current version per case, current rows = distinct `case_id`s) must hold.
- **signals** — the engine's recorded background N must equal the live current-case count, and `signal_scores` / `signal_quarterly` row counts must match what the engine reports it wrote.
- **concordance** — the SIDER validation (same `build_concordance` code path as [validate_concordance.py](scripts/validate_concordance.py), so numbers are identical to the standalone tool) is re-run on the refreshed screen and the report stores per-mode recall + top-N metrics **with the numeric delta vs the previous refresh's report** at the same path — so each quarter shows how label-concordance moved, e.g. `recall_prr 0.4945->0.5012`. Skipped (not failed) when the reference files are absent, since they are not committed.
- **regimes** — the quarterly regime detector ([regime.py](app/statistics/regime.py)) audits the top screened pairs by latest-quarter case depth and flags **bulk single-event submission bursts** and **notoriety ramps** for the just-refreshed quarter; every flag lands in the report with its metrics and reason (see the detector section below).
- **profiles** — quarterly-PRR profile verdicts ([qprofile.py](app/statistics/qprofile.py)) for the top `--profile-top` (default 200) screened signals: each pair's per-quarter PRR (Haldane-corrected) is compared against the pooled PRR that got it screened, yielding `elevation_is_new` (flagged: pooled elevation resting on a single latest-quarter regime), `consistent` (elevated in every evaluable quarter), `mixed`, or `insufficient`. Skippable with `--skip-profiles`.
- **confounding** — the indication-bias probe ([confounding.py](app/statistics/confounding.py), same `build_confounding` path as [probe_confounding.py](scripts/probe_confounding.py)) re-run on the refreshed top `--confounding-top` (default 200) screen, recording which signals flag as possibly confounded by indication or treatment failure. Skippable with `--skip-confounding`.

Exit code 0 only when all seven stages pass; a machine-readable run report lands in `data/processed/quarterly_refresh_latest.json`. Useful flags: `--mode` / `--min-cases` (signal options), `--skip-download` (archive already extracted under `data/raw/FAERS/<quarter>/`), `--quarter-dir` (data supplied elsewhere; skips download), `--report`, `--skip-concordance`, `--reference-dir`, `--concordance-top`, `--skip-regimes`, `--regime-candidates`, `--skip-profiles`, `--profile-top`, `--skip-confounding`, `--confounding-top`. After a refresh of a quarter not in `FAERS_QUARTERS`, the command prints a reminder to add it to `.env` so default pipeline commands include it. Re-running a refresh for an already-loaded quarter is safe: ingestion is idempotent and the engine recompute replaces scores per mode.

Outputs:

- `data/raw/FAERS/` — raw archives + extracted ASCII files (never committed)
- `data/processed/data_quality_report_latest.json` — measured counts for every stage
- `ingest_runs` / `data_quality_reports` tables — run history in the database

### Admin API (authenticated refresh)

The quarterly refresh is also reachable over HTTP on the Flask API ([`app/api/__init__.py`](app/api/__init__.py)) with durable job tracking ([`refresh_service.py`](app/services/refresh_service.py), `refresh_jobs` table — status survives API restarts):

| Endpoint | Auth | Behaviour |
|---|---|---|
| `POST /api/admin/refresh` | required | Validates `quarter` / `mode` / `min_cases` / `skip_*`, creates a job row, starts the SAME `scripts/refresh_quarter.py` orchestrator on a worker thread → `202` with `job` + `status_url` (`409` if a job is already running, `400` on bad input) |
| `GET /api/admin/refresh/<id>` | required | Job status: `queued → running → succeeded/failed`, timestamps, `error`, and the refresh `report` JSON (all seven stages incl. concordance delta, regime flags, profile verdicts, and confounding flags) |
| `GET /api/admin/refresh` | required | Latest jobs (default 20, max 100) |

Auth is a shared secret: set `ADMIN_API_KEY` (see `.env.example`) and send `Authorization: Bearer <key>` or `X-Admin-Key: <key>` (constant-time compare). No key configured → the routes return `503` (disabled, visibly) rather than silently open or silently locked; wrong/missing key → `401`. Single-flight: one refresh at a time, and a `running` row whose worker thread is gone (API restarted mid-job) is marked `failed` as orphaned before a new job starts — the same recovery rule `run_ingestion.py` applies to `ingest_runs`. Each job's report is written to `data/processed/refresh_job_<id>.json` and stored on the job row.

### Tests

```bash
pytest                   # unit + integration (integration needs local PostgreSQL)
```

Works with or without activating `.venv` — the interpreter bootstrap reruns
the suite under `.venv/` if the interpreter on PATH lacks the pinned
dependencies (see Setup above).

### Scheduled figures-drift guard (CI)

[.github/workflows/figures-drift.yml](.github/workflows/figures-drift.yml)
runs `scripts/report_figures.py --check` nightly (06:17 UTC, plus manual
dispatch) against the **production read-only replica** and fails on any drift
between the committed migration 004 report and the figures production
actually serves — exit 1 lists every drifted row, exit 2 (unreachable
database) is also red, because an unverifiable guard is not a pass. It is the
only workflow allowed to hold a production credential: it needs the
`PRODUCTION_REPLICA_DATABASE_URL` repository secret (a read-only role) and
fails loudly when that secret is absent. Independently of the role, every
connection in the check step is forced read-only server-side
(`PGOPTIONS="-c default_transaction_read_only=on"`), so even a future script
regression cannot write to production from CI. A second, independent gate in
the same job runs `scripts/report_figures.py --diff` against the committed
baseline snapshot
(`docs/migrations/004_focus_drug_brand_aliases/figures_baseline_snapshot.json`):
every figure must sit exactly at the recorded pre-execution baseline or at
precisely the value consolidation produces, so ingestion or any other
movement in the migration window — and partial executions — turn the job red
even when the report tables were regenerated to match. The diff gate runs
after a failing check (so one red run shows which gate fired) but is skipped
when the secret is missing, and CI never re-takes the snapshot. Never
triggered by `pull_request`/`push`, so the DSN is unreachable from fork PRs;
the contract is pinned by `tests/unit/test_workflow_figures_drift.py`.

The weekly companion workflow
[.github/workflows/figures-drift-alert.yml](.github/workflows/figures-drift-alert.yml)
(Monday 07:43 UTC) reads the guard's completed-run history and keeps exactly
one open GitHub issue per ongoing failure — created on the first failing
check, refreshed while it keeps failing with a weekly digest comment stating
how long the drift has persisted (the issue's own age in days and full
weeks, uninterrupted since creation) plus the newest run link, and closed
automatically once the guard is green again. Every failing issue is also
triaged into a queue — a `severity: high` label (created on demand) plus the
repository's assignee — asserted when the issue opens and re-asserted at
every refresh, so it lands in someone's assigned-issues view rather than
only in the tracker. When that open issue has
survived **four consecutive weekly failures (28 days)**, the alert escalates
it: an `escalated` label plus an `[ESCALATED]` title prefix, applied exactly
once (the marker stays in the title, so the dedupe search still finds it,
and the label stops week five from re-escalating). It uses only the built-in
`GITHUB_TOKEN` (actions:read + issues:write, no secrets), never runs the
check itself, and does nothing while the guard has no completed runs; its
contract is pinned by `tests/unit/test_workflow_figures_drift_alert.py`.

### Phase 1 methodology (summary; full detail in docs/)

- **Version handling / deduplication:** reports sharing a `CASEID` are versions of one case. The current version is `max(explicit FDA_VERSION, else FDA_DT, then PRIMARYID)` — implemented once in `app/faers/dedupe.py`, applied across all quarters combined. Losers are flagged `is_current = false`, never deleted.
- **Normalization:** drug names uppercased/whitespace-collapsed through a small explicit alias table (no guessing); age converted to years via `AGE_COD`; sex → M/F only; event terms uppercased; partial FAERS dates preserved with their granularity.
- **Validation:** file-level required columns, row-level rejections with reasons, orphan rows counted, blank fields counted as missingness — every number lands in the quality report (nothing silently discarded).
- **Loading:** PostgreSQL `COPY` staging + `ON CONFLICT DO NOTHING`, so re-runs are idempotent.

### Phase 1 verification (measured, 2026-10-08)

Real FDA data, quarter **2024q1** (67 MB archive from `fis.fda.gov`, extracted DEMO/DRUG/REAC):

| Metric | Measured value |
|---|---|
| DEMO rows parsed / accepted / rejected | 406,184 / 406,184 / 0 |
| DRUG rows parsed / accepted | 1,909,327 / 1,909,327 (92 duplicate rows removed) |
| REAC rows parsed / accepted | 1,445,416 / 1,445,416 (21,934 duplicate event rows removed) |
| Loaded cases / drug rows / event rows | 406,184 / 1,909,235 / 1,423,482 |
| Distinct normalized drugs / events | 26,840 / 12,361 |
| Missing age / unparseable age / unknown sex | 157,772 / 1,590 / 64,107 |
| Orphan drug/event rows | 0 / 0 |
| Ingestion wall time (parse→load→dedup→report) | ~101 s |

Full machine-readable report: `data/processed/data_quality_report_latest.json`.

### Full 8-quarter dataset (2024q1–2025q4, ingested 2026-10-10)

All eight FAERS quarterly archives are downloaded, ingested, and deduplicated across quarters. Consolidated report: `data/processed/data_quality_summary_8q.json`.

| Metric | Measured value |
|---|---|
| Case rows loaded (all versions) | 3,236,977 |
| Distinct cases = current versions (`is_current`) | 2,831,775 |
| Superseded versions (retained, flagged stale) | 405,202 |
| Drug rows / event rows | 15,164,870 / 11,299,153 |
| Distinct normalized drugs / events | 98,689 / 18,607 |
| Rows rejected at parse (demo/drug/reac) | 0 / 0 / 0 |
| Orphan drug / event rows | 0 / 0 |
| Missing age / unknown sex (all rows) | 1,288,362 (39.8%) / 606,636 (18.7%) |

Per-quarter current-version counts:

| Quarter | Case rows | Current | Age missing | Sex missing |
|---|---|---|---|---|
| 2024q1 | 406,184 | 338,828 | 39.2% | 15.8% |
| 2024q2 | 397,118 | 336,410 | 41.1% | 16.9% |
| 2024q3 | 405,513 | 347,678 | 41.5% | 17.5% |
| 2024q4 | 410,849 | 339,554 | 39.3% | 20.4% |
| 2025q1 | 400,514 | 333,349 | 39.2% | 20.7% |
| 2025q2 | 392,999 | 338,468 | 39.7% | 21.5% |
| 2025q3 | 438,512 | 412,200 | 41.3% | 17.0% |
| 2025q4 | 385,288 | 385,288 | 38.4% | 20.5% |

Dedup invariant verified: current-version rows (2,831,775) equal distinct `case_id`s (2,831,775) — exactly one current version per case. Re-run ingestion any time with `--quarters <list>`; loading is idempotent and dedup is applied across all loaded quarters combined.

Format notes discovered against real files (handled by the parser): 2024+ FIS exports are **`$`-delimited** (legacy files are tab-delimited — the parser sniffs the delimiter), headers are lowercase with `caseversion` used as the explicit version field, and drug roles are `PS/SS/C/I` (legacy: `SUS/CON/IFF`).

### Phase 2 — statistical engine (implemented)

Full-background disproportionality over **all** current reports (Rule 6: the focus drugs are never the background):

```bash
.venv/bin/python scripts/compute_signals.py                 # both modes
.venv/bin/python scripts/compute_signals.py --mode primary-suspect
```

- **2×2 assembly in SQL** (pair co-occurrence, drug totals, event totals, background N — case-level DISTINCT counts), **statistics in Python** via [disproportionality.py](app/statistics/disproportionality.py) (PRR, ROR, 95% log CIs, Yates χ², screening rules), persisted to `signal_scores` by [engine.py](app/statistics/engine.py).
- **Screening rules (documented as screening only, never causality):** PRR ≥ 2 AND χ² ≥ 4 AND a ≥ 3; ROR lower 95% bound > 1. Zero cells → Haldane–Anscombe 0.5 correction, flagged `continuity_corrected`.
- **Primary-suspect sensitivity (spec §11):** separate run with role ∈ {PS, SUS}; both rows coexist for comparison.
- **Persistence:** `quarters_observed` / `quarters_screening` on `all` rows; quarterly co-occurrence counts live in `signal_quarterly` — volume is kept **separate** from disproportionality (spec §12) with a rolling-z volume flag in [trend.py](app/statistics/trend.py).

#### Phase 2 verification (measured, 2026-10-08, quarter 2024q1)

| Mode | Pairs ≥ 3 cases | PRR-screened | ROR-screened | Rows written | Runtime |
|---|---|---|---|---|---|
| all suspect drugs | 519,792 | 363,265 | 390,164 | 1,039,584 | 70.9 s |
| primary suspect only | 87,739 | 51,210 | 55,631 | 175,478 | 16.1 s |

Independent verification: `MOUNJARO/NAUSEA` stored **a=1207, b=10233, c=15370, d=379374, PRR=2.7097** exactly matches a separate SQL recount from `cases`/`case_drugs`/`case_events` and a hand-recomputed PRR (cells sum to N=406,184). Top screened pairs match known FAERS concentration patterns (device/dose problems, off-label use).

Two real-data bugs were found and fixed with regression tests: duplicate drug_seq rows inflated `a` (case-level DISTINCT fix), and `c=0` crashed PRR with a `ZeroDivisionError` (continuity-correction fix).

#### 8-quarter background screening (measured, 2026-10-11)

`compute_signals.py` re-run over the full 2024q1–2025q4 background (N = 2,831,775 current reports). Full comparison vs the 2024q1-only baseline: [screening_comparison_8q_vs_q1.json](data/processed/screening_comparison_8q_vs_q1.json).

| Mode | Pairs ≥ 3 cases | PRR-screened | ROR-screened | Runtime |
|---|---|---|---|---|
| all suspect drugs | 1,735,502 | 1,040,362 | 1,148,975 | 610.8 s |
| primary suspect only | 403,757 | 204,801 | 226,148 | 150.4 s |

Screen stability (all-mode, shared pairs, PRR screen): **324,562 kept / 37,816 dropped / 35,068 newly flagged**. Key findings:

- **89.5% of q1-screened signals survive** on 7× more data — the screen is stable, not noise.
- **Dropped signals had thin q1 evidence** (median 6 cases): their q1 PRR ≥ 2 was small-sample inflation (e.g. PREDNISONE/NAUSEA: PRR 2.26 at n=1,205 → 1.97 at n=6,468).
- **New detections** include large coherent signals only visible on the full background: DUPIXENT/CONDITION AGGRAVATED (11,013 cases, PRR 2.82), ORGOVYX/FATIGUE (4,771, PRR 7.58), LANTUS SOLOSTAR/OFF LABEL USE (4,877, PRR 5.19).
- **949 baseline pairs vanished** entirely — verified as cross-quarter version supersession (supporting cases replaced by newer FDA versions that no longer contain the pair), not data loss.
- Median/p90 case counts of screened signals rose 7→8 / 42→71: the 8q list rests on deeper evidence.

The q1-only baseline is preserved in `signal_scores_q1only` (snapshot taken before the recompute); quarterly series in `signal_quarterly_q1only`.

Both headline new detections were then investigated for data artifacts ([signal_investigation_orgovyx_dupixent.json](data/processed/signal_investigation_orgovyx_dupixent.json)): **ORGOVYX/FATIGUE** survives — it is label-listed (ORGOVYX/Xtandi/Lupron US labels), class-wide across androgen-deprivation therapies (PRR 2.1–7.6), and its co-event profile is coherent ADT pharmacology (HOT FLUSH in 50% of pair cases) with no indication-term enrichment. **DUPIXENT/CONDITION AGGRAVATED** does not — "condition aggravated" is not a listed adverse reaction, its co-events are the treated diseases themselves plus utilization terms (21.8% PRODUCT USE IN UNAPPROVED INDICATION), and peer dermatology biologics show PRR < 1 for the same term: it is predominantly confounding by indication/treatment failure, systematic across all 8 quarters (quarterly PRR 2.2–2.6).

#### Pipeline robustness (added after real incidents, 2026-10-11)

Three hardening measures landed after client-disconnect incidents during the 8-quarter ingest:

- **Per-quarter report persistence:** [run_ingestion.py](scripts/run_ingestion.py) writes the run's quality report after *every* quarter (`status: partial`), so a killed client can no longer lose completed quarters' stats. The API surfaces the `status` field.
- **Orphaned-run recovery:** an advisory lock (`pg_try_advisory_lock`) makes ingestion single-writer; any `running` row found while holding the lock is provably orphaned and is marked `failed` on the next start.
- **Reusable summary:** `scripts/summarize_quality.py` rebuilds the consolidated cross-quarter summary from the database itself (live counts + DB-stored run reports) — it never depends on on-disk report JSONs, which `--fixtures` test runs overwrite.

#### Label-concordance validation (measured, 2026-10-11)

The 8-quarter PRR screen is checked against an external reference standard: **SIDER 4.1** label-derived MedDRA drug/side-effect pairs (140,677 pairs, 1,344 drugs, `data/reference/`). SIDER is built from product labels rather than FAERS report counts, so it is independent of the screen. License: **CC BY-SA 4.0** (Creative Commons Attribution-ShareAlike) — attribution: SIDER, EMBL-EBI (Kuhn et al., *Nucleic Acids Research* 2016). Reproduce with [validate_concordance.py](scripts/validate_concordance.py):

```bash
.venv/bin/python scripts/validate_concordance.py           # SIDER reference
.venv/bin/python scripts/validate_concordance.py --rank chi2  # alternate ordering
```

Full machine-readable report: [concordance_validation_latest.json](data/processed/concordance_validation_latest.json).

**Recall** — of SIDER label pairs evaluable in `signal_scores` (both terms in our vocabulary, ≥ 3 co-occurring cases), the fraction passing each screen:

| Mode | Evaluable label pairs | PRR-screened | ROR-screened |
|---|---|---|---|
| all suspect drugs | 54,281 | 26,843 (**49.5%**) | 33,523 (**61.8%**) |
| primary suspect only | 22,883 | 9,936 (43.4%) | 11,501 (50.3%) |

**Top-N concordance** — top signals ranked by `case_count` (evidence depth), matched against SIDER labels. `judgable` = the drug appears in SIDER's vocabulary at all (its absence can never prove a false positive, so `concordance_all` is a lower bound):

| Rank slice | Label matches | Judgable | Concordance (judgable) | Non-adverse-event terms | Disease-flare terms |
|---|---|---|---|---|---|
| top 100 | 19 | 37 | **51.4%** | 18 (18.0%) | 5 (5.0%) |
| top 500 | 91 | 213 | 42.7% | 68 (13.6%) | 19 (3.8%) |
| top 1,000 | 142 | 383 | 37.1% | 94 (9.4%) | 32 (3.2%) |

Interpretation, stated as screening evidence only (never causality):

- **About half of evaluable known labels are recovered** by the PRR screen (61.8% by ROR) on the full background; the highest-evidence signals agree with established labels more than half the time once the drug is in SIDER's vocabulary.
- **Ranking method matters.** Ranking by χ² alone is dominated by rare-event artifacts (a few cases against a near-zero expected count give χ² in the millions), which collapses top-100 concordance to ~0. `case_count` ranking is the default because it reflects evidence depth; `--rank chi2` reproduces the artifact-heavy ordering.
- **"Unmatched" is not "false."** 18% of top-100 signals are utilization/administration terms (`OFF LABEL USE`, `DRUG INEFFECTIVE`, `PRODUCT USE IN UNAPPROVED INDICATION`) that product labels never list as side effects, so SIDER can never match them — the report counts them separately (`non_adverse_event_*`) instead of inflating the false-positive impression.
- **Disease-flare terms are flagged independently of SIDER** (`disease_flare_*`): non-specific terms like `CONDITION AGGRAVATED` / `DISEASE PROGRESSION` report the treated condition worsening, not a drug reaction, and they typically carry drugs SIDER does not cover at all — the SIDER-based indication flag is blind to them (the DUPIXENT finding above). The flag fires on the event term alone; top examples are `DUPIXENT/PREDNISONE/METHOTREXATE/INFLIXIMAB` + `CONDITION AGGRAVATED` and `PLUVICTO/GENERAL PHYSICAL HEALTH DETERIORATION` (5% of top-100).
- **Confounding by indication** is flagged, not discarded: top signals whose event is the drug's own labeled indication (e.g. METHOTREXATE/RHEUMATOID ARTHRITIS, PREDNISONE/PNEUMONIA) are counted as `indication_events`.
- **vs the 2024q1-only baseline:** on the 36,079 label pairs evaluable in *both* runs, recall moved 56.8% → 55.8% (17,331 kept / 3,147 dropped / 2,800 newly screened). The apparent drop in overall recall (57.8% → 49.5%) is a denominator effect: the 7× larger background made 25,000+ additional label pairs evaluable, and those newly-scored pairs are, by construction, lower-frequency ones.

Reference files are **not** committed; re-download with the `curl` commands printed by the script if `data/reference/` is absent. Matching is exact after uppercasing; FAERS brand-name rows recorded under the generic name in SIDER count as unmatched (a stated limitation, not silently corrected).

#### Automated indication-bias probe (added 2026-10-12)

[probe_confounding.py](scripts/probe_confounding.py) scores every top signal for the two confounding dimensions the DUPIXENT investigation exposed, **without needing SIDER coverage** ([confounding.py](app/statistics/confounding.py) holds the metric and the shared term sets):

- **co-event enrichment** — share of pair cases that also report treated-disease terms (the drug's own SIDER indications where covered, a grounded generic list otherwise) vs the same share across all cases of the drug.
- **treatment-failure enrichment** — same construction over utilization/failure terms (`OFF LABEL USE`, `DRUG INEFFECTIVE`, ...).

Flag threshold: enrichment ≥ 1.5× with pair-share ≥ 10% → "review as possibly confounded", never "false". Live verification on the two investigated headline signals: DUPIXENT/CONDITION AGGRAVATED flags (treatment-failure 1.57×: 27.8% vs 17.7%), ORGOVYX/FATIGUE does not (enrichments ≈1.0). A signal's own event never counts as its own co-event. Batched as per-signal term **arrays** (a per-term join multiplies the drug-case scan ~30× and took >5 min on blockbuster drugs); top-25 costs minutes, dominated by the base scans of blockbuster drugs.

```bash
.venv/bin/python scripts/probe_confounding.py                # top 200, all mode
.venv/bin/python scripts/probe_confounding.py --top 50 --min-enrichment 2.0
```

Output: `data/processed/confounding_probe_latest.json` (all probed signals + flagged subset + audit co-events).

#### Next-tier artifact investigation (measured, 2026-10-12)

The next tier of new detections (largest pairs newly screened vs the q1-only baseline) was run through the same battery — quarterly-PRR profiles, co-event/single-event shares, reporter forensics, the enrichment probe, and label checks ([signal_investigation_next_tier.json](data/processed/signal_investigation_next_tier.json)):

- **DAPAGLIFLOZIN/DEATH (3,117 cases, PRR 5.17) is an artifact.** Quarterly PRR sat *below* background (0.3–0.9) for six quarters, then a 2025q3–q4 mass US submission of **single-event DEATH-only reports (97%/95%, reporter unknown 91%)** produced the pooled elevation — the drug's own report volume jumped 7× in one quarter. Farxiga does not list death as an adverse reaction.
- **DEPO-PROVERA/MENINGIOMA (1,612, PRR 2,429) is a real labeled risk with notoriety-inflated magnitude.** Meningioma is now labeled (US §5.4, revised Dec 2025; French ANSM 2023–24; EMA PRAC Sep 2024), but the within-drug share ramped 0% → 73% tracking regulatory/litigation publicity — the PRR value is a reporting artifact, the association is not.
- **VEDOLIZUMAB/DRUG INEFFECTIVE (3,028, PRR 2.04) is not a safety signal** — treatment-failure reporting (not a labeled Entyvio reaction) with IBD-disease co-events, stable across all 8 quarters.
- **Real and label-concordant:** DUPIXENT/ARTHRALGIA (9,326; labeled §6.1, enrichment <1), METFORMIN/DIARRHOEA (textbook GI intolerance), AMLODIPINE/FATIGUE (labeled; flagged for review at 1.9× hypertension co-reporting — the only probe flag of this tier).
- **ALBUTEROL/DEVICE DELIVERY SYSTEM ISSUE (2,414, PRR 24.1) is a device-reporting stream** (bursty manufacturer submissions, device-mechanics co-events, 98% age missing) — it belongs in device-quality monitoring, not pharmacovigilance.

Two artifact classes need two detectors, and the pooled PRR catches neither: **submission-regime bursts** are exposed by the quarterly profile + single-event share + reporter forensics; **indication/treatment-failure confounding** by the enrichment probe.

#### Quarterly regime detector (added 2026-10-13)

The first of those two detectors is now automated: [`app/statistics/regime.py`](app/statistics/regime.py) runs as **stage 5 of every quarterly refresh** (and standalone via `detect_regimes(conn, ...)`), replacing the manual quarterly-profile/single-event forensics for the burst class:

- **Candidates** — pairs that pass the current screen (`signal_scores period='all'`, `prr_screen OR ror_screen`) with `a >= min_cases` in the **latest quarter**, ranked by that quarter's case depth and capped at the top 1000 (`--regime-candidates`) — the same case-depth ranking the manual investigations used, bounded so the stage stays ~20s on the live 8-quarter database.
- **`bulk_single_event`** — latest-quarter volume ≥ 3× the median of all prior quarters **and** ≥ 70% of the pair's cases are single-event reports (the case's whole REAC listing is that one event), at least 15 points above the pair's own baseline share. A one-quarter burst of minimal reports, not new risk evidence.
- **`notoriety_ramp`** — the event's share of the drug's own quarterly cases (drug ≥ 10 cases/quarter) is ≥ 3× its baseline mean, rises monotonically over the last transitions (steps < 1pp don't count as trend), and peaks now. Volume-normalized, so a busy drug can't trigger it — the DEPO-PROVERA/MENINGIOMA within-drug-share pattern.
- The two flags are **mutually exclusive per pair**: a single-event bulk artifact can't also claim to be a gradual ramp; a multi-event volume spike gets neither flag (it stays a candidate for the normal screen).

**Measured on the live 8-quarter database (2025q4 evaluation, 1,000 candidates, 22s):** 11 flags — 2 bulk + 9 notoriety ramps. The bulk detector independently re-discovered **DAPAGLIFLOZIN/DEATH** (608 cases in 2025q4 vs baseline median 28, 95% single-event) — the artifact the manual next-tier investigation caught by hand — plus TYLENOL/EXPOSURE DURING PREGNANCY (1,357 vs 90, 86% single-event, baseline 24%). The ramp flags are device/manufacturer-reporting and GLP-1/administration categories (OMNITROPE device issues, TRULICITY vomiting/impaired gastric emptying, BIMZELX hidradenitis, INFLIXIMAB overdose, ESTRADIOL device adhesion) — exactly the notoriety-prone classes, each with its share metrics recorded. Screening judgments only, never causality or risk (spec §5); thresholds are recorded under `parameters` in every report so a flag can be audited after the fact.

## Documentation

- `docs/plans/01-repository-assessment-and-gap-analysis.md`
- `docs/plans/02-implementation-plan.md`
- `docs/plans/03-dependency-plan.md`
- `docs/plans/04-database-migration-plan.md`

(README grows per spec §57 as phases land: methodology, limitations, installation, running the pipeline, tests, deployment.)
