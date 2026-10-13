-- Phase 3: Performance indexes for Signal Explorer, Dashboard & Profiles (spec §14, §51)
-- Speeds up queries on signal_scores, case_drugs, and case_events across millions of rows.

CREATE INDEX IF NOT EXISTS idx_signal_scores_filter_a
    ON signal_scores (period, primary_suspect_mode, a DESC);

CREATE INDEX IF NOT EXISTS idx_signal_scores_filter_prr
    ON signal_scores (period, primary_suspect_mode, prr DESC)
    WHERE prr IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_signal_scores_filter_chi2
    ON signal_scores (period, primary_suspect_mode, chi_square DESC)
    WHERE chi_square IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_signal_scores_drug_mode
    ON signal_scores (drug, period, primary_suspect_mode);

CREATE INDEX IF NOT EXISTS idx_signal_scores_event_mode
    ON signal_scores (event, period, primary_suspect_mode);

CREATE INDEX IF NOT EXISTS idx_signal_scores_pair_mode
    ON signal_scores (drug, event, period, primary_suspect_mode);

CREATE INDEX IF NOT EXISTS idx_case_drugs_drug_current
    ON case_drugs (drug_normalized, primaryid);

CREATE INDEX IF NOT EXISTS idx_case_events_event_current
    ON case_events (event_term, primaryid);
