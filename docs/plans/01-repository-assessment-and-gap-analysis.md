# PharmaSignal — Repository Assessment & Architecture Gap Analysis

Date: 2026-10-08
Author: Build agent (Phase 0 deliverable, spec §73)

## 1. Current repository assessment

| Item | Finding |
|---|---|
| Workspace contents | Empty except `.freebuff/` (tool metadata). No source files. |
| Git | **Not a git repository.** No history, no remote. |
| Existing architecture | None. Greenfield build. |
| Conflicts with spec | None — nothing to preserve or reconcile. |
| Spec §69 obligations | N/A for conflicts; still applies: build incrementally, test after each subsystem, keep docs synchronized. |

### Environment inventory (verified, not assumed)

| Tool | Status | Notes |
|---|---|---|
| Python | 3.12.12 at `/opt/homebrew/bin/python3.12` (system default is 3.9 — too old) | Project venv uses 3.12 |
| PostgreSQL | 14.17 running, accepting connections on `localhost:5432`, superuser access works | Homebrew install |
| pgvector | **Not installed** | Required by spec §8 `doc_chunks.embedding`; deferred to Phase 4 migration |
| Docker | Not installed | Deliverables still include Dockerfile/compose (§54) but they cannot be executed/verified locally |
| Node | 25.4.0 available | Frontend (Phase 3/8) can use it |
| Network → FDA | `api.fda.gov` returns 200 | Downloads feasible |

## 2. Architecture gap analysis

Everything in the spec is a gap. Ordered by dependency (what blocks what):

| # | Gap | Spec ref | Blocked by | Phase |
|---|---|---|---|---|
| 1 | FAERS ingestion pipeline (download → validate → parse → dedup → normalize → load) | §7 | — | 1 |
| 2 | PostgreSQL schema (cases, case_drugs, case_events, outcomes, signal_scores, doc_chunks, eval_*) | §8 | — | 1 |
| 3 | Data quality report generation | §7, §38 | 1 | 1 |
| 4 | Statistical engine: 2×2, PRR, ROR, CIs, screening, primary-suspect sensitivity, persistence | §9–11 | 1, 2 | 2 |
| 5 | Trend analysis (separate from disproportionality) | §12 | 4 | 2 |
| 6 | Signal Explorer / Detail / Drug / Event / Dashboard pages + REST API | §14–17, §40, §47 | 4, 5 | 3 |
| 7 | FDA evidence ingestion + section-aware chunking + embeddings + pgvector | §18–20 | 2 | 4 |
| 8 | Hybrid retrieval + reranking + retrieval evaluation | §21–22 | 7 | 4 |
| 9 | LangGraph agent: intent → tools → draft | §23–26 | 4, 6, 8 | 5 |
| 10 | Claim extraction + numeric/citation/causal verification + 1 retry | §27–31 | 9 | 6 |
| 11 | Rule-based confidence engine | §32 | 4, 8, 10 | 6 |
| 12 | Evaluation system (60–100 questions, metrics, failure catalogue) | §35–37 | 8, 9, 10 | 7 |
| 13 | UI polish, accessibility, docs, diagrams | §40–45, §57–58 | 6 | 8 |
| 14 | Docker, migrations, health checks, logging, monitoring | §39, §48–54 | all | 9 |

### Key risks identified up front

1. **Data volume**: ~8 full FAERS quarters ≈ 3–4M cases, ~10M+ drug rows, ~8M+ reaction rows. Raw files ~1.5–2 GB. Pipeline must stream/batch; must not hold full DRUG table in memory twice.
2. **Deduplication semantics**: FDA's duplicate rule (per `CASEID`, keep latest version) must be applied **across all quarters combined**, not per-quarter, or background counts inflate. This directly affects every statistic (spec §5).
3. **Background vs. focus**: statistics must run over the full loaded population; the 5 focus drugs are an analysis filter only (spec §5, Rule 6). Enforced by making the signal engine query the whole `case_drugs`/`case_events` tables.
4. **pgvector absent locally**: `doc_chunks` vector column deferred to its own migration; embedding work (Phase 4) requires installing pgvector first.
5. **No Docker locally**: Docker artifacts will be written and lint-checked but cannot be verified in this environment — will be reported as unverified, not passed (spec §73 "do not fabricate results").
6. **LLM keys**: `LLM_API_KEY` / `EMBEDDING_API_KEY` not present. Agent phases will build deterministic/testable cores with a mock LLM adapter; live runs are reported only if keys are provided.

## 3. Guiding decisions

- Greenfield: adopt spec §55 repository structure verbatim.
- Python 3.12, Flask (spec §46), PostgreSQL 14 (matches local), pgvector in Phase 4.
- All statistical code deterministic, dependency-light (no SciPy required for MVP; formulas hand-verified in tests per spec §10).
- Every phase ends with tests run and a results report (spec §73).
