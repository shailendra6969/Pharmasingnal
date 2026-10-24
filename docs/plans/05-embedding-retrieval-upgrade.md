# 05 — Embedding-based retrieval upgrade (pgvector + embedding provider)

Status: **scoped, not started.** Implements the Phase 4 rows of
[02-implementation-plan.md](02-implementation-plan.md) (install/enable pgvector,
migration adds `embedding vector(dim)`, env-driven providers, hybrid retrieval,
retrieval metrics) and the `003_pgvector.sql` row of
[04-database-migration-plan.md](04-database-migration-plan.md).

Two deliverables, in this order:

1. **Store** an embedding per `doc_chunks` row in PostgreSQL via pgvector, with
   an index and a resumable backfill.
2. **Retrieve** with vector similarity blended into the existing keyword ranking
   behind a flag — keyword stays the default and the automatic fallback until
   measured recall proves the hybrid path at least as good.

The provider choice is *already committed* by [.env.example](../../.env.example):
`EMBEDDING_PROVIDER=openai`, `EMBEDDING_MODEL=text-embedding-3-small`,
`VECTOR_DIMENSION=1536`. This plan implements that contract rather than
re-opening it, and keeps every one of those values swappable from `.env`.

## In scope

pgvector migration; provider adapter (openai / cohere / voyage / offline hash);
backfill script + ingest hook; hybrid retrieval behind `RETRIEVAL_MODE`;
retrieval recall metrics + golden set; CI coverage that stays offline.

## Out of scope (later, deliberate)

A neural **reranker** (spec §20's "reranker" row in 02 stays Phase 4b — RRF
already gets most of the win); LLM answer generation (`LLM_API_KEY`); chunking
changes (`chunking_method` stays as-is); pgvector on the production read-only
replica; `halfvec`/quantized storage (not needed at 1536 dims).

---

## 1. Current state (measured on this machine, 2026-10-24)

| Fact | Value |
|---|---|
| `doc_chunks` rows | 262 across 5 drugs |
| Total label text | 345,152 chars ≈ **86 K tokens** |
| Retrieval today | [`app/tools/label_tool.py`](../../app/tools/label_tool.py) `search_labels()` — loads **every** chunk for a drug into Python, scores with `SECTION_WEIGHTS` × (term frequency + query coverage), OTC section fallback |
| Vectors | none — no `embedding` column anywhere |
| pgvector | **not installed**: `pg_available_extensions WHERE name='vector'` → no rows |
| Config contract | `.env.example` already documents `EMBEDDING_API_KEY`, `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `VECTOR_DIMENSION=1536`; `scripts/doctor.py` already reports `llm-keys` |
| Migrations present | `001`, `002_signal_analysis`, `003_analytics_indexes`, `003_refresh_jobs` → **next free slot is `004`** (docs/plans/04 calls it `003_pgvector.sql`; that number is taken) |
| Known drift | `001_initial_schema.sql` lines 2/126 promise "002_pgvector.sql (Phase 4)" — audit item 4 in [audit-2026-10-14.md](../audit-2026-10-14.md) |

**What retrieval must not lose.** `InvestigationWorkflow` builds citations from
`search_labels()` output and the evidence gate re-checks quoted spans *verbatim*
against `text`. So the hybrid path must keep the exact return shape
(`chunk_id`, `section`, `subsection`, `text`, `source_url`, `document_date`,
`score`, `matched_terms`, `metadata`, `fallback_searched`) and must never
substitute an embedding for the chunk text.

---

## 2. Prerequisites — blocking, host-level (do first)

1. **Install pgvector for PostgreSQL 14 on the dev host.**
   - Homebrew: `brew install pgvector` (links into the `postgresql@14` lib dir),
     then in a scratch DB: `CREATE EXTENSION IF NOT EXISTS vector;`
   - Debian/Ubuntu: `apt-get install postgresql-14-pgvector`
   - Verify: `SELECT * FROM pg_available_extensions WHERE name = 'vector';` must
     return a row. **This is the step that currently fails.**
2. **Swap the CI service image.** `migrate.py` applies *all* files in
   `migrations/`; once `004_pgvector.sql` lands, every job that runs migrations
   needs an image that ships the extension. The plain `postgres:14` image does
   not have it. Change the `services.postgres.image` in
   [`.github/workflows/migration-004.yml`](../../.github/workflows/migration-004.yml)
   and [`.github/workflows/evaluation.yml`](../../.github/workflows/evaluation.yml)
   to **`pgvector/pgvector:pg14`** (Docker Hub; pin e.g. `0.8.7-pg14` once
   verified). A migration job that fails on a missing extension is the correct
   red — but it must be red for the right reason, so do this in the same PR.

---

## 3. Migration `migrations/004_pgvector.sql`

```sql
-- pgvector: one embedding per label chunk (spec §20). Requires the extension
-- to be installed server-side (docs/plans/05, step 2) — migrate.py reverts the
-- whole file if CREATE EXTENSION fails, so an unprepared host fails loudly.
CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE doc_chunks
    ADD COLUMN IF NOT EXISTS embedding       vector(1536),
    ADD COLUMN IF NOT EXISTS embedding_model TEXT,
    ADD COLUMN IF NOT EXISTS embedding_sha   TEXT,   -- sha256(text): staleness probe
    ADD COLUMN IF NOT EXISTS embedded_at     TIMESTAMPTZ;

-- Cosine distance; HNSW (pgvector >= 0.5) builds incrementally, unlike IVFFlat,
-- which would need a rebuild after every ingestion batch.
CREATE INDEX IF NOT EXISTS idx_doc_chunks_embedding
    ON doc_chunks USING hnsw (embedding vector_cosine_ops);
```

Decisions, and why:

- **`vector(1536)` is a literal, not `VECTOR_DIMENSION`.** `scripts/migrate.py`
  applies static SQL — it does not interpolate env. The literal matches the
  documented default; changing dimension later means a *new* migration plus a
  full re-embed (see §10), never an edit to a shipped file.
- **`embedding_model` + `embedding_sha` are not optional.** They are what makes
  the backfill resumable and detect "the model in `.env` is not the model these
  vectors came from" instead of silently comparing incomparable vectors.
- **HNSW over IVFFlat**: no data-dependent rebuild, fine for incremental label
  ingestion; at 262 rows the index is trivial either way.
- **Column ordering** keeps `embedding` last-ish; `ADD COLUMN` on an existing
  262-row table is instant (no rewrite).
- Same PR: fix the two stale comments in `001_initial_schema.sql` (audit item 4)
  to name `004_pgvector.sql`, so the schema and its own commentary stop
  disagreeing.

---

## 4. Provider adapter — `app/rag/embeddings.py`

```python
class NotConfigured(RuntimeError): ...   # no key / disabled provider

class Embedder:
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...
    def describe(self) -> str: ...        # provider/model/dims — never the key

def get_embedder() -> Embedder: ...       # built from env, cached per process
```

Backends, selected by the **existing** `EMBEDDING_PROVIDER` value:

| provider | package | env key | notes |
|---|---|---|---|
| `openai` (**default**, per `.env.example`) | `openai` | `EMBEDDING_API_KEY` → `OPENAI_API_KEY` | `text-embedding-3-small`: **$0.02 / 1 M tokens**, 8,192-token input, 1,536 default with `dimensions=` supported. Any OpenAI-compatible endpoint works via `EMBEDDING_BASE_URL` (local Ollama/vLLM = free, no code change) |
| `cohere` | `cohere` | `EMBEDDING_API_KEY` | `embed-v4.0` ≈ $0.12 / 1 M, 128 K input, `output_dimension`, **requires `input_type`** (`search_document` vs `search_query`) — the adapter must set it |
| `voyage` | `voyageai` | `EMBEDDING_API_KEY` | `voyage-4-large` $0.12 / 1 M (200 M tokens free), 32 K input, `output_dimension`; domain-tuned models available later |
| `hash` | none | none | **deterministic, offline** — sha256 → 1,536 floats, L2-normalised. Exists *only* so tests and CI exercise the full vector path with zero network |

Rules the adapter must satisfy (each has a unit test):

- No network outside this module. Tests and CI never hold `EMBEDDING_API_KEY`.
- The key is never logged, printed or raised (`scripts/doctor.py` prints
  *whether* it is set, never its value — same discipline).
- A response whose dimension ≠ `VECTOR_DIMENSION` raises; it must never reach
  the database (pgvector would reject it anyway, but with a worse error).
- Timeouts + bounded retries with backoff; exhaustion raises `NotConfigured`
  (or a wrapped provider error) — callers **degrade to keyword**, never fail an
  investigation (see §6).
- Cost is checked in, not guessed: 262 chunks ≈ 86 K tokens ≈ **$0.002** for a
  full backfill at $0.02/1 M; a 100× corpus ≈ $0.20.

Dependencies: `openai` is a new pinned entry in `requirements.txt`
(`cohere`/`voyageai` stay optional extras so the default install stays small —
and `hash` needs nothing, which is what CI uses).

---

## 5. Backfill — `scripts/embed_chunks.py`

```bash
python scripts/embed_chunks.py                 # DATABASE_URL, resume-aware
python scripts/embed_chunks.py --dry-run       # counts + estimated tokens/cost
python scripts/embed_chunks.py --provider hash # offline self-test
```

- **Selection**: `WHERE embedding IS NULL OR embedding_model <> %s
  OR embedding_sha <> encode(sha256(text),'hex')` — re-running after a model
  change re-embeds exactly the stale rows and nothing else.
- Batched (128 texts/request), one transaction per batch, prints progress,
  exit codes `0` done / `2` config-or-database error — the same contract as
  `run_evaluation.py` and `ingest_labels.py`.
- **Ingest hook**: after `doc_repository.replace_label_chunks(...)` in
  [`scripts/ingest_labels.py`](../../scripts/ingest_labels.py), embed the new
  chunks *best-effort*: if the provider is not configured, log one line and
  continue. Ingestion must not become network-dependent — update that file's
  "no pgvector / embedding dependencies" header comment, since it stops being
  true.
- **`scripts/doctor.py`**: new `pgvector` check — extension available, column
  present, `% of doc_chunks embedded` — plus fold the existing `llm-keys` warn
  into "hybrid mode unavailable, running keyword" wording once §6 lands.

---

## 6. Retrieval — `app/tools/label_tool.py`

`search_labels(conn, drug, query, section=None, limit=5)` **keeps its signature
and return shape** (callers: `app/agents/investigation.py`,
`app/evaluation/runner.py`, both test suites). Internally:

1. **Keyword branch** — exactly today's code, unchanged.
2. **Vector branch** (only when `RETRIEVAL_MODE=hybrid`), same `drug` filter
   (plus `section` filter when given):

   ```sql
   SELECT chunk_id, text, section, ... FROM doc_chunks
   WHERE drug = %s AND embedding IS NOT NULL
   ORDER BY embedding <=> %s::vector
   LIMIT 20;
   ```
3. **Fusion** — Reciprocal Rank Fusion, `score = Σ 1/(60 + rank)` over the two
   rankings, then the existing `SECTION_WEIGHTS` multiplier applied as today, so
   a Boxed Warning chunk still outranks an equal-ranking Description chunk.
   Vector-only hits get `matched_terms` from the token overlap pass (possibly
   `[]`) — the field is informational; the evidence gate reads `text`.
4. **Degradation, in order** (one `logger.debug` line each): provider not
   configured → keyword; no query embedding (timeout/bad key) → keyword; zero
   vector hits for the drug → keyword; dimension mismatch → keyword. Keyword
   output is byte-identical to today's, so the flag can be turned off at any
   moment without a code change.

Config (all optional, defaults = current behaviour): `RETRIEVAL_MODE=keyword`,
`RETRIEVAL_TOP_K=20`.

---

## 7. Evaluation and CI

- **CI stays offline.** `scripts/seed_evaluation.py` gains a `--provider hash`
  step (default on in CI) so `doc_chunks` in the evaluation job carries valid
  1,536-dim vectors — the hybrid SQL, index and RRF are then exercised on every
  build with no key and no network.
- **New retrieval metric**: a golden set
  `app/evaluation/retrieval_golden.json` — `[(drug, query, [expected chunk_ids])]`
  harvested from today's keyword ranking for ~10 focus pairs. `run_evaluation`
  reports `recall@5` per case alongside the existing
  `numeric_accuracy / citation_correctness / verification_accuracy`.
- **Gate before flipping the default**: `recall@5(hybrid) ≥ recall@5(keyword)`
  *and* the four baseline evidence cases still `PASS`. Numbers get written into
  this document's §9 before any default change.
- Workflows: `evaluation.yml` runs the gated evaluation twice —
  `RETRIEVAL_MODE=keyword` (the build gate as it stands today) and
  `RETRIEVAL_MODE=hybrid` (new step) — both against the `pgvector/pgvector:pg14`
  service from §2. Service image swap for `migration-004.yml` likewise.
- **Tests added**: adapter unit tests with a fake transport (asserts no key
  logging, dimension enforcement, retry exhaustion); RRF fusion unit tests;
  degradation tests (provider off / empty embeddings / dimension mismatch);
  an integration test for backfill + hybrid search on the test database
  (skipped when the extension is absent, *fail-red in CI* where the image has it).

---

## 8. Rollout order — each step independently verifiable

| # | Step | Verified by |
|---|---|---|
| 0 | Install pgvector locally + swap CI images to `pgvector/pgvector:pg14` | `SELECT * FROM pg_available_extensions WHERE name='vector'` returns a row; CI reaches the migration step |
| 1 | `004_pgvector.sql` + comment fix in `001` + migration tests | fresh DB migrates; re-run is a no-op; `migrate.py` on a host *without* the extension exits non-zero |
| 2 | `app/rag/embeddings.py` adapter (`hash` first, then `openai`) | new unit tests, offline; `python scripts/embed_chunks.py --provider hash --dry-run` |
| 3 | `embed_chunks.py` + ingest hook + doctor `pgvector` check | backfill on dev DB → `count(*) WHERE embedding IS NULL` = 0; doctor shows the new check |
| 4 | Hybrid retrieval behind `RETRIEVAL_MODE=hybrid` | new unit/integration tests; keyword output unchanged when the flag is off |
| 5 | Golden set + `recall@5` in the evaluation report; CI runs both modes | numbers recorded in §9 |
| 6 | Flip default to `hybrid` (its own PR) | §9 criteria met; rollback = one env var |

Steps 0–1 are the critical path (nothing else can land before the extension
exists). 2–4 are independent of each other and parallelisable.

---

## 9. Acceptance criteria

- [ ] `CREATE EXTENSION vector` succeeds locally **and** in both CI workflows
      (pgvector image).
- [ ] Migration is idempotent: second `python scripts/migrate.py` applies
      nothing and exits 0.
- [ ] `SELECT count(*) FROM doc_chunks WHERE embedding IS NULL` = 0 after
      backfill; re-running the backfill does nothing (staleness probe works).
- [ ] With `RETRIEVAL_MODE=keyword` (default), **every existing test passes
      unchanged** — no behavioural diff.
- [ ] Evaluation: 4/4 baseline cases `PASS` in **both** modes; `recall@5`
      hybrid ≥ keyword (numbers below).
- [ ] No test, and no CI job, ever calls a provider; no key appears in any log,
      error, artifact or committed file.
- [ ] With the provider unconfigured or failing, an investigation still returns
      a report (keyword path), and `doctor` says why.

**Measured results** (fill in at step 5):

| mode | recall@5 | baseline cases | latency (p50) |
|---|---|---|---|
| keyword | _tbd_ | 4/4 | |
| hybrid | _tbd_ | 4/4 | |

---

## 10. Risks and open questions

1. **Extension install needs write access to `pkglibdir`** and, for a packaged
   Postgres, a matching server version. Mitigation: step 0 is explicit and
   fails loudly; `migrate.py` already reverts a failed file.
2. **CI image provenance** — `pgvector/pgvector` on Docker Hub is
   community-published; pin an immutable version tag once verified.
3. **Model or dimension change later** = new migration (drop/re-add column or
   change its type) + full re-embed. `embedding_model`/`embedding_sha` make
   that detectable rather than silently wrong; HNSW index must be rebuilt.
4. **HNSW memory at scale**: fine at 262 rows; at millions of rows the build
   needs `work_mem` headroom and should run off the ingestion path.
5. **Provider outage or quota** must degrade to keyword (§6) — never turn a
   retrieval hiccup into a failed investigation, and never a *silent* VERIFIED:
   the evidence gate still requires real chunks, which the keyword path
   provides.
6. **Data class**: FDA label text is public; no PHI leaves the machine. Worth
   one line in the README's data-handling section when this lands.
7. **Docs to update in the same PR**: this file's §9, `docs/plans/04` (the
   `003_pgvector.sql` row → `004`), `001_initial_schema.sql` comments,
   `scripts/ingest_labels.py` header, README's pipeline diagram ("pgvector" is
   already drawn there, so it stops being aspirational).

---

## 11. Sources

- OpenAI embeddings guide & model pages (pricing, 8,192-token limit,
  `dimensions` parameter): https://developers.openai.com/api/docs/guides/embeddings
- Cohere Embed model specs / API reference (input types, `output_dimension`):
  https://docs.cohere.com/docs/cohere-embed
- Voyage AI embeddings & pricing (32 K input, `output_dimension`):
  https://docs.voyageai.com/docs/embeddings
- pgvector (types, HNSW, `vector` max 2,000 dims, Docker Hub `pg14` tags):
  https://github.com/pgvector/pgvector