# PharmaSignal — Database Migration Plan

PostgreSQL 14 (matches local Homebrew). pgvector deferred to Phase 4 because the extension is not installed on this host and Phase 1 does not need it.

## Migration mechanics

- Plain SQL files in `migrations/`, applied in lexical order: `001_*.sql`, `002_*.sql`, …
- Applied by `scripts/migrate.py` (tracks applied files in `schema_migrations(filename, applied_at)` inside a transaction per file; idempotent — a file already recorded is skipped).
- Forward-only for MVP. Rollback = drop database and re-apply (dev), or write a compensating `00N_down_*.sql` when a destructive change is unavoidable.

## Planned migrations

| File | Phase | Contents |
|---|---|---|
| `001_initial_schema.sql` | 1 | `cases`, `case_drugs`, `case_events`, `outcomes`, `signal_scores`, `doc_chunks` (text + metadata columns only), `eval_questions`, `eval_results`, `ingest_runs`, `data_quality_reports`, indexes, FKs, CHECK constraints, timestamps (spec §8) |
| `002_signal_analysis.sql` | 2 | **applied** — persistence/screening/audit columns on `signal_scores`, plus `signal_quarterly` (volume kept separate from disproportionality, spec §12) |
| `003_pgvector.sql` | 4 | `CREATE EXTENSION IF NOT EXISTS vector;` then `doc_chunks.embedding vector(<dim>)` + HNSW/IVFFlat index; dimension from env (`VECTOR_DIMENSION`) — recorded in migration comment |
| `004_rag_evaluation.sql` | 7 | retrieval-metrics tables if `eval_results` columns prove insufficient |

## Schema design notes (001)

- `cases.primaryid` (PK) = FAERS `PRIMARYID` (one row per report VERSION); `case_id` = FAERS `CASEID`. Dedup keeps every version row and records the winner via `is_current`, so version history stays auditable.
- `source_quarter` (e.g. `2024q1`) on `cases` = quarter the kept version came from; `version` = `FDA_DT`/`FDA_VERSION` used for dedup.
- `case_drugs.role` stores the raw FAERS role code: modern files use `PS/SS/C/I`, legacy files `SUS/CON/IFF/UNK` (primary-suspect sensitivity for spec §11 filters on `PS` **and** `SUS`).
- `signal_scores` has `UNIQUE(drug, event, period, primary_suspect_mode)` — the engine is deterministic, so re-runs replace rather than duplicate.
- Raw/accepted/rejected counters live in `ingest_runs` + `data_quality_reports` tables (spec §7, §38) — every transformation measurable, nothing silently discarded.
- All count/stat columns `NUMERIC` or `DOUBLE PRECISION` as appropriate; counts `INTEGER CHECK (>= 0)`.
- Indexes initially: FKs, `(case_id)` on child tables, `(drug_normalized)` , `(event_term)`, `(source_quarter)`, unique on `signal_scores` natural key. More added in Phase 3 only when a measured query pattern demands it (spec §51).

## Databases

- `pharmasignal` — local dev (created by `scripts/create_databases.py` if missing).
- `pharmasignal_test` — used by integration tests; truncated between runs.
- Connection via `DATABASE_URL` env (`.env.example` documents it; `.env` never committed).
