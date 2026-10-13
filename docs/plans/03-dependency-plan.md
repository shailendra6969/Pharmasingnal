# PharmaSignal — Dependency Plan

Principles: pinned versions (spec §48), lean core, phased additions — nothing is installed before the phase that needs it.

## Local environment facts

- Python **3.12.12** (venv at `.venv/`; system 3.9 is insufficient for pandas ≥2.2 wheels).
- PostgreSQL **14.17** (Homebrew, running). `pgvector` extension **not installed**.
- Docker **not installed** on this machine → Docker artifacts written but unverified (reported as such).

## Phase 1 — Data foundation (installed now)

| Package | Version pin | Why |
|---|---|---|
| pandas | `pandas==2.2.*` | Parsing large tab-delimited FAERS files, dtype control |
| psycopg2-binary | `psycopg2-binary==2.9.*` | PostgreSQL COPY loading |
| python-dotenv | `python-dotenv==1.0.*` | `.env` loading (never commit `.env`, spec §53) |
| pytest | `pytest==8.*` | Unit/integration tests |
| requests | `requests==2.32.*` | FAERS downloader with retries |

Standard library only for: zip extraction, csv parsing edge cases, JSON quality reports, hashing.

## Phase 2 — Statistics

No new runtime dependencies. PRR/ROR/χ²/CIs implemented with `math` and hand-verified tests (spec §10). Adding SciPy would hide the formulas the tests are supposed to pin down.

## Phase 3 — API/product

| Package | Why |
|---|---|
| flask==3.* | API + pages (spec §46) |
| gunicorn | Production server (Phase 9) |

## Phase 4 — RAG

| Package | Why |
|---|---|
| pgvector (system/PG extension) | Vector storage — **must be installed on host first** |
| sentence-transformers *or* API-based embeddings | Configurable provider (spec §20) — decision deferred to Phase 4 with cost/quality comparison |
| rank_bm25 or equivalent | Keyword leg of hybrid retrieval |

## Phase 5–6 — Agent & verification

| Package | Why |
|---|---|
| langgraph, langchain-core | Single workflow (spec §25); Rule 8: no extra agents |
| openai *or* anthropic SDK | LLM provider behind a small adapter interface; key via `LLM_API_KEY` env (absent locally → mock adapter used in tests) |

## Phase 7 — Evaluation

No new required packages; metrics computed in-repo.

## Explicitly excluded (for now)

- SciPy/statsmodels (formulas must be transparent and hand-tested)
- Kubernetes/Redis/Celery (spec §51: no premature distribution)
- Any ML library — Rule 9: ML only for a defensible ranking problem (reranker in Phase 4 is the first legitimate candidate)

## Reproducibility

- `requirements.txt` fully pinned, generated from the active venv.
- Install: `python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt`
