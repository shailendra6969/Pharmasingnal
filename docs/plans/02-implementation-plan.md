# PharmaSignal — Implementation Plan

Follows spec §64 (development order). No phase begins until the prior phase's tests pass or failures are explicitly reported and fixed.

## Phase 0 — Planning (this document set)

- [x] Repository assessment & gap analysis — `01-repository-assessment-and-gap-analysis.md`
- [x] Implementation plan — this file
- [x] Dependency plan — `03-dependency-plan.md`
- [x] Database migration plan — `04-database-migration-plan.md`

## Phase 1 — Data Foundation (spec §64.1, §7)

Deliverables:

1. `scripts/download_faers.py` — reproducible downloader for ASCII quarterly zips into `data/raw/` (never committed).
2. `app/faers/parse.py` — header-driven parser for DEMO/DRUG/REAC tab-delimited files (latin-1 safe).
3. `app/faers/validate.py` — structural validation: required columns, row-level acceptance/rejection with reasons.
4. `app/faers/dedupe.py` — cross-quarter duplicate resolution: per `CASEID` keep latest report (rule: max `FDA_DT`, tie → max `PRIMARYID`; supports explicit `FDA_VERSION` when present).
5. `app/faers/normalize.py` — drug normalization (trim/upper/collapse + alias table), age→years (`AGE_COD` aware), sex, event terms.
6. `app/faers/quality.py` — data quality report (raw/accepted/rejected/duplicates/missingness/per-quarter coverage/duration), JSON output.
7. `migrations/001_initial_schema.sql` — cases, case_drugs, case_events, outcomes, signal_scores, doc_chunks (text cols only; vectors in 002), eval_questions, eval_results + indexes/constraints.
8. `app/repositories/loader.py` — COPY-based load into PostgreSQL with transactional quarter replace.
9. `scripts/run_ingestion.py` — orchestrates parse → validate → dedupe → normalize → load → quality report.
10. Tests: unit tests for parse/validate/dedupe/normalize/quality on fixtures; integration test loading fixtures into a real test database.

Exit criteria: all Phase 1 tests pass; ingestion of the sample fixture set produces a populated `pharmasignal_test` DB and a quality report with no silent data loss.

## Phase 2 — Statistical Engine (spec §9–12)

- `app/statistics/contingency.py` — 2×2 construction over full background.
- `app/statistics/disproportionality.py` — PRR, ROR, 95% CI (log method), chi-square, screening rules (PRR≥2, χ²≥4, n≥3; ROR lower bound >1).
- Zero-cell handling (Haldane–Anscombe 0.5 continuity option, documented), low-count flags, overflow-safe math.
- Primary-suspect sensitivity mode; quarterly persistence.
- Trend module kept separate from disproportionality (report counts vs. scores).
- Hand-calculated test fixtures; reproducibility tests (same input → identical output).

## Phase 3 — Analytics Product (spec §14–17, §40, §47)

- Flask app with layered structure (routes → services → repositories).
- REST endpoints per spec §47 with validation/pagination/errors.
- Pages: `/`, `/dashboard`, `/signals`, `/signals/<id>`, `/drugs/<drug>`, `/events/<event>`.
- Frontend: server-rendered or light static SPA — decide by what keeps the stack simple (prefer Flask templates + minimal JS charts; revisit only if UX demands more).

## Phase 4 — FDA Evidence / RAG (spec §18–22)

- Install/enable pgvector; migration 002 adds `embedding vector(dim)`.
- FDA label + safety communication ingestion with provenance metadata.
- Section-aware chunking + fixed-size baseline.
- Configurable embedding providers (env-driven), embedding cache.
- Hybrid retrieval: keyword + vector + metadata filter + reranker.
- Retrieval evaluation dataset + precision/recall/top-k metrics; embedding A/B and chunking A/B.

## Phase 5 — Agentic AI (spec §23–26)

- `POST /api/investigate` → single LangGraph workflow.
- Tools: `query_database`, `get_signal_scores`, `search_labels`, `get_trend`.
- Structured trace metadata (no chain-of-thought exposure).

## Phase 6 — Verification (spec §27–32) — critical

- Claim extraction, numeric verifier (DB authoritative), citation verifier, causal-language guard (rule-based, independent of LLM).
- Retry-once loop, failure surfacing, rule-based confidence engine.

## Phase 7 — Evaluation (spec §35–37)

- 60–100 question dataset across the six categories.
- Metrics: numeric accuracy, citation correctness, retrieval P/R, verification accuracy, tool-selection accuracy, causal-language safety, latency, tokens/cost — measured, never invented.
- Failure catalogue (≥10 real discovered failures) with regression tests.

## Phase 8 — Product Polish (spec §40–45, §57–58)

- UI polish, responsive, accessibility, loading/error states, README + methodology + limitations docs, architecture diagrams.

## Phase 9 — Deployment (spec §39, §48–54)

- Dockerfile + docker-compose (web + postgres + pgvector), health checks, structured logging, monitoring endpoints, CI config.

## Reporting discipline (spec §73)

After each phase: run tests → report passes/failures → fix failures → update docs → proceed. No fabricated results; unverifiable items (e.g., Docker without Docker installed) are reported as unverified.
