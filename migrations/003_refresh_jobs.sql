-- Admin-triggered quarterly refresh: durable job tracking (spec §39-style
-- run history for the refresh orchestrator, so status survives API restarts).
-- report: the refresh run's own JSON report (stages, concordance delta, ...).
CREATE TABLE IF NOT EXISTS refresh_jobs (
    job_id      BIGSERIAL PRIMARY KEY,
    quarter     TEXT NOT NULL,
    params      JSONB NOT NULL DEFAULT '{}'::jsonb,
    status      TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    error       TEXT,
    report      JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_refresh_jobs_status ON refresh_jobs (status);
