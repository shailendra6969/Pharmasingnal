-- PharmaSignal initial schema (spec §8)
-- Phase 1. pgvector addition lives in 002_pgvector.sql (Phase 4).
-- Forward-only; applied by scripts/migrate.py.

-- ---------------------------------------------------------------------------
-- Ingestion bookkeeping (spec §7: every transformation measurable)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id          BIGSERIAL PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    quarters        TEXT[] NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'succeeded', 'failed')),
    duration_seconds DOUBLE PRECISION,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS data_quality_reports (
    report_id       BIGSERIAL PRIMARY KEY,
    run_id          BIGINT REFERENCES ingest_runs(run_id),
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    quarters        TEXT[] NOT NULL DEFAULT '{}',
    report          JSONB NOT NULL
);

-- ---------------------------------------------------------------------------
-- cases: one row per FAERS report VERSION (primaryid). Dedup flags the
-- current version per case_id; losers are kept, never deleted (spec §7).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cases (
    primaryid       TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL,
    event_date      TEXT,                -- partial FAERS dates kept as text
    report_date     TEXT,                -- FDA_DT received date
    age_years       DOUBLE PRECISION CHECK (age_years IS NULL OR (age_years >= 0 AND age_years <= 130)),
    sex             TEXT CHECK (sex IS NULL OR sex IN ('M', 'F')),
    country         TEXT,
    reporter_type   TEXT,
    version         TEXT,                -- FDA_VERSION if present, else FDA_DT marker
    source_quarter  TEXT NOT NULL,
    is_current      BOOLEAN NOT NULL DEFAULT TRUE,  -- dedup: one TRUE per case_id
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_cases_case_id ON cases (case_id);
CREATE INDEX IF NOT EXISTS idx_cases_source_quarter ON cases (source_quarter);
CREATE INDEX IF NOT EXISTS idx_cases_current ON cases (is_current) WHERE is_current;
CREATE INDEX IF NOT EXISTS idx_cases_drugsex_age ON cases (age_years) WHERE age_years IS NOT NULL;

-- ---------------------------------------------------------------------------
-- case_drugs: rows for ALL versions; filter via JOIN cases ON is_current
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS case_drugs (
    id              BIGSERIAL PRIMARY KEY,
    primaryid       TEXT NOT NULL REFERENCES cases(primaryid) ON DELETE CASCADE,
    case_id         TEXT NOT NULL,       -- denormalized for query speed
    drug_seq        TEXT,
    drug_normalized TEXT NOT NULL,
    drug_original   TEXT,
    role            TEXT,                -- SUS / CON / IFF / UNK (spec §11)
    route           TEXT,
    dose_text       TEXT
);
CREATE INDEX IF NOT EXISTS idx_case_drugs_primaryid ON case_drugs (primaryid);
CREATE INDEX IF NOT EXISTS idx_case_drugs_case_id ON case_drugs (case_id);
CREATE INDEX IF NOT EXISTS idx_case_drugs_normalized ON case_drugs (drug_normalized);
CREATE INDEX IF NOT EXISTS idx_case_drugs_role ON case_drugs (role);
-- Idempotent re-loads: one drug row per report version (spec §7)
CREATE UNIQUE INDEX IF NOT EXISTS uq_case_drugs_pid_drug
    ON case_drugs (primaryid, COALESCE(drug_seq, drug_normalized));

-- ---------------------------------------------------------------------------
-- case_events
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS case_events (
    id              BIGSERIAL PRIMARY KEY,
    primaryid       TEXT NOT NULL REFERENCES cases(primaryid) ON DELETE CASCADE,
    case_id         TEXT NOT NULL,
    event_term      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_case_events_primaryid ON case_events (primaryid);
CREATE INDEX IF NOT EXISTS idx_case_events_case_id ON case_events (case_id);
CREATE INDEX IF NOT EXISTS idx_case_events_term ON case_events (event_term);
CREATE UNIQUE INDEX IF NOT EXISTS uq_case_events_pid_term ON case_events (primaryid, event_term);

-- ---------------------------------------------------------------------------
-- outcomes (OUTC file; parsed in a later pipeline extension)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outcomes (
    id              BIGSERIAL PRIMARY KEY,
    primaryid       TEXT NOT NULL REFERENCES cases(primaryid) ON DELETE CASCADE,
    case_id         TEXT NOT NULL,
    outcome_code    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outcomes_primaryid ON outcomes (primaryid);
CREATE UNIQUE INDEX IF NOT EXISTS uq_outcomes_pid_code ON outcomes (primaryid, outcome_code);

-- ---------------------------------------------------------------------------
-- signal_scores (spec §8): deterministic engine output, re-runs replace
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signal_scores (
    signal_id           BIGSERIAL PRIMARY KEY,
    drug                TEXT NOT NULL,
    event               TEXT NOT NULL,
    period              TEXT NOT NULL,   -- 'all' or e.g. '2024q1'
    a                   INTEGER NOT NULL CHECK (a >= 0),
    b                   INTEGER NOT NULL CHECK (b >= 0),
    c                   INTEGER NOT NULL CHECK (c >= 0),
    d                   INTEGER NOT NULL CHECK (d >= 0),
    case_count          INTEGER NOT NULL CHECK (case_count >= 0),
    prr                 DOUBLE PRECISION,
    prr_lower           DOUBLE PRECISION,
    prr_upper           DOUBLE PRECISION,
    ror                 DOUBLE PRECISION,
    ror_lower           DOUBLE PRECISION,
    ror_upper           DOUBLE PRECISION,
    chi_square          DOUBLE PRECISION,
    primary_suspect_mode BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (drug, event, period, primary_suspect_mode)
);
CREATE INDEX IF NOT EXISTS idx_signal_scores_drug ON signal_scores (drug);
CREATE INDEX IF NOT EXISTS idx_signal_scores_event ON signal_scores (event);

-- ---------------------------------------------------------------------------
-- doc_chunks: text + metadata now; embedding vector added in 002 (pgvector)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS doc_chunks (
    chunk_id        TEXT PRIMARY KEY,
    drug            TEXT,
    doc_type        TEXT NOT NULL,
    section         TEXT,
    subsection      TEXT,
    text            TEXT NOT NULL,
    source_url      TEXT,
    document_date   TEXT,
    chunk_index     INTEGER NOT NULL DEFAULT 0,
    chunking_method TEXT NOT NULL DEFAULT 'section',   -- 'section' | 'fixed'
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_doc_chunks_drug ON doc_chunks (drug);
CREATE INDEX IF NOT EXISTS idx_doc_chunks_doc_type ON doc_chunks (doc_type);
CREATE INDEX IF NOT EXISTS idx_doc_chunks_section ON doc_chunks (section);
CREATE INDEX IF NOT EXISTS idx_doc_chunks_metadata ON doc_chunks USING GIN (metadata);

-- ---------------------------------------------------------------------------
-- evaluation (spec §8, §35)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eval_questions (
    question_id         TEXT PRIMARY KEY,
    question            TEXT NOT NULL,
    category            TEXT NOT NULL,
    expected_answer     TEXT,
    expected_drug       TEXT,
    expected_event      TEXT,
    expected_sources    JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_eval_questions_category ON eval_questions (category);

CREATE TABLE IF NOT EXISTS eval_results (
    result_id           BIGSERIAL PRIMARY KEY,
    question_id         TEXT NOT NULL REFERENCES eval_questions(question_id),
    generated_answer    TEXT,
    retrieved_chunks    JSONB,
    claims              JSONB,
    verification_results JSONB,
    retrieval_metrics   JSONB,
    numeric_accuracy    DOUBLE PRECISION,
    citation_correctness DOUBLE PRECISION,
    verification_accuracy DOUBLE PRECISION,
    latency             DOUBLE PRECISION,
    token_cost          DOUBLE PRECISION,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_eval_results_question ON eval_results (question_id);
