-- Phase 2: persistence/analysis columns for signal_scores (spec §9, §11, §14)
-- quarters_observed:     quarters in which the pair co-occurred at least once
-- quarters_screening:    quarters in which the pair met the a >= 3 case floor
-- Both are only meaningful on 'all' period rows; quarterly rows carry 0 defaults.

ALTER TABLE signal_scores
    ADD COLUMN IF NOT EXISTS quarters_observed INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS quarters_screening INTEGER NOT NULL DEFAULT 0;

ALTER TABLE signal_scores
    ADD COLUMN IF NOT EXISTS prr_screen BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS ror_screen BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS insufficient BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS continuity_corrected BOOLEAN NOT NULL DEFAULT FALSE;

-- Totals snapshot: N (background current cases) and mode totals used for the
-- 2x2, recorded so the contingency table is auditable after the fact.
ALTER TABLE signal_scores
    ADD COLUMN IF NOT EXISTS background_n BIGINT,
    ADD COLUMN IF NOT EXISTS drug_case_count BIGINT,   -- a + b in this mode
    ADD COLUMN IF NOT EXISTS event_case_count BIGINT;  -- a + c

CREATE INDEX IF NOT EXISTS idx_signal_scores_screen
    ON signal_scores (drug, event) WHERE prr_screen;

-- Quarterly co-occurrence history lives OUTSIDE signal_scores because it is
-- reporting VOLUME, not disproportionality (spec §12: never mix the two).
CREATE TABLE IF NOT EXISTS signal_quarterly (
    drug            TEXT NOT NULL,
    event           TEXT NOT NULL,
    quarter         TEXT NOT NULL,
    a               INTEGER NOT NULL CHECK (a >= 0),
    background_n    BIGINT,
    primary_suspect_mode BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (drug, event, quarter, primary_suspect_mode)
);
CREATE INDEX IF NOT EXISTS idx_signal_quarterly_pair
    ON signal_quarterly (drug, event);
