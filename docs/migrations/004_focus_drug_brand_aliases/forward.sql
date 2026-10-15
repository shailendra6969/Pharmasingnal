-- ============================================================================
-- PharmaSignal Migration 004: Focus Drug Brand Alias Consolidation (FORWARD)
-- Migration ID: 20261015_focus_drug_aliases
-- Target mappings:
--   LIPITOR                 -> ATORVASTATIN
--   GLUCOPHAGE              -> METFORMIN
--   GLUCOPHAGE XR           -> METFORMIN
--   ADVIL                   -> IBUPROFEN
--   MOTRIN                  -> IBUPROFEN
--   PRILOSEC                -> OMEPRAZOLE
--   ZOLOFT                  -> SERTRALINE
--
-- Safety Contract:
-- 1. Must run within an explicit transaction (atomic all-or-nothing).
-- 2. Preserves exact pre-migration drug_normalized values in case_drugs_before_image.
-- 3. Does NOT overwrite or derive rollback values from drug_original.
-- 4. Preserves drug_original strictly untouched.
-- 5. Dynamic preflight assertions: calculates candidate rows dynamically.
-- 6. Dynamic postflight assertions: verifies 0 brand rows remain in case_drugs
--    and snapshot count strictly equals updated row count.
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- STEP 1: PREFLIGHT CHECKS & SAFETY ASSERTIONS
-- ----------------------------------------------------------------------------

-- Guard 1: Abort if snapshot for this migration_id already exists (prevents double execution)
DO $$
DECLARE
    existing_snapshots INTEGER;
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables 
        WHERE table_schema = current_schema() AND table_name = 'case_drugs_before_image'
    ) THEN
        SELECT COUNT(*) INTO existing_snapshots
        FROM case_drugs_before_image
        WHERE migration_id = '20261015_focus_drug_aliases';

        IF existing_snapshots > 0 THEN
            RAISE EXCEPTION 'MIGRATION ABORTED: Before-image snapshot for 20261015_focus_drug_aliases already exists (% rows). Run rollback or check migration state.', existing_snapshots;
        END IF;
    END IF;
END $$;

-- Guard 2: Abort if unique index collision could occur on rows with NULL drug_seq
DO $$
DECLARE
    collision_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO collision_count
    FROM case_drugs cd1
    WHERE cd1.drug_seq IS NULL
      AND cd1.drug_normalized IN ('LIPITOR', 'GLUCOPHAGE', 'GLUCOPHAGE XR', 'ADVIL', 'MOTRIN', 'PRILOSEC', 'ZOLOFT')
      AND EXISTS (
          SELECT 1 FROM case_drugs cd2
          WHERE cd2.primaryid = cd1.primaryid
            AND cd2.drug_seq IS NULL
            AND cd2.drug_normalized = CASE
                WHEN cd1.drug_normalized = 'LIPITOR' THEN 'ATORVASTATIN'
                WHEN cd1.drug_normalized IN ('GLUCOPHAGE', 'GLUCOPHAGE XR') THEN 'METFORMIN'
                WHEN cd1.drug_normalized IN ('ADVIL', 'MOTRIN') THEN 'IBUPROFEN'
                WHEN cd1.drug_normalized = 'PRILOSEC' THEN 'OMEPRAZOLE'
                WHEN cd1.drug_normalized = 'ZOLOFT' THEN 'SERTRALINE'
            END
      );

    IF collision_count > 0 THEN
        RAISE EXCEPTION 'MIGRATION ABORTED: % rows with NULL drug_seq would violate uq_case_drugs_pid_drug upon normalization.', collision_count;
    END IF;
END $$;

-- ----------------------------------------------------------------------------
-- STEP 2: ENSURE PERSISTENT BEFORE-IMAGE TABLE EXISTS
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS case_drugs_before_image (
    snapshot_id          BIGSERIAL PRIMARY KEY,
    migration_id         TEXT NOT NULL,
    case_drug_id         BIGINT NOT NULL,
    primaryid            TEXT NOT NULL,
    drug_seq             TEXT,
    pre_drug_normalized  TEXT NOT NULL,
    post_drug_normalized TEXT NOT NULL,
    drug_original        TEXT,
    captured_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    restored_at          TIMESTAMPTZ,
    UNIQUE (migration_id, case_drug_id)
);

CREATE INDEX IF NOT EXISTS idx_cd_before_image_mig_drug 
    ON case_drugs_before_image (migration_id, case_drug_id);

-- ----------------------------------------------------------------------------
-- STEP 3: CAPTURE EXACT BEFORE-IMAGE SNAPSHOT
-- ----------------------------------------------------------------------------
INSERT INTO case_drugs_before_image (
    migration_id,
    case_drug_id,
    primaryid,
    drug_seq,
    pre_drug_normalized,
    post_drug_normalized,
    drug_original
)
SELECT 
    '20261015_focus_drug_aliases',
    cd.id,
    cd.primaryid,
    cd.drug_seq,
    cd.drug_normalized,
    CASE
        WHEN cd.drug_normalized = 'LIPITOR' THEN 'ATORVASTATIN'
        WHEN cd.drug_normalized IN ('GLUCOPHAGE', 'GLUCOPHAGE XR') THEN 'METFORMIN'
        WHEN cd.drug_normalized IN ('ADVIL', 'MOTRIN') THEN 'IBUPROFEN'
        WHEN cd.drug_normalized = 'PRILOSEC' THEN 'OMEPRAZOLE'
        WHEN cd.drug_normalized = 'ZOLOFT' THEN 'SERTRALINE'
    END,
    cd.drug_original
FROM case_drugs cd
WHERE cd.drug_normalized IN ('LIPITOR', 'GLUCOPHAGE', 'GLUCOPHAGE XR', 'ADVIL', 'MOTRIN', 'PRILOSEC', 'ZOLOFT');

-- ----------------------------------------------------------------------------
-- STEP 4: PERFORM FORWARD UPDATE OF drug_normalized
-- ----------------------------------------------------------------------------
UPDATE case_drugs cd
SET drug_normalized = b.post_drug_normalized
FROM case_drugs_before_image b
WHERE cd.id = b.case_drug_id
  AND b.migration_id = '20261015_focus_drug_aliases';

-- ----------------------------------------------------------------------------
-- STEP 5: POSTFLIGHT VERIFICATION ASSERTIONS
-- ----------------------------------------------------------------------------
DO $$
DECLARE
    snap_count INTEGER;
    remaining_brands INTEGER;
    untouched_originals INTEGER;
BEGIN
    -- 1. Verify snapshot count
    SELECT COUNT(*) INTO snap_count
    FROM case_drugs_before_image
    WHERE migration_id = '20261015_focus_drug_aliases';

    -- 2. Verify zero targeted brands remain in case_drugs
    SELECT COUNT(*) INTO remaining_brands
    FROM case_drugs
    WHERE drug_normalized IN ('LIPITOR', 'GLUCOPHAGE', 'GLUCOPHAGE XR', 'ADVIL', 'MOTRIN', 'PRILOSEC', 'ZOLOFT');

    IF remaining_brands > 0 THEN
        RAISE EXCEPTION 'POSTFLIGHT ASSERTION FAILED: % targeted brand rows still remain in case_drugs.', remaining_brands;
    END IF;

    -- 3. Verify drug_original was preserved 100% untouched
    SELECT COUNT(*) INTO untouched_originals
    FROM case_drugs cd
    JOIN case_drugs_before_image b ON b.case_drug_id = cd.id AND b.migration_id = '20261015_focus_drug_aliases'
    WHERE cd.drug_original IS DISTINCT FROM b.drug_original;

    IF untouched_originals > 0 THEN
        RAISE EXCEPTION 'POSTFLIGHT ASSERTION FAILED: % rows had drug_original altered unexpectedly.', untouched_originals;
    END IF;

    RAISE NOTICE 'Migration 004 Postflight PASSED: Consolidated % rows. Zero brand-normalized rows remain. drug_original preserved.', snap_count;
END $$;

COMMIT;
