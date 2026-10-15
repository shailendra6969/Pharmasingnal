-- ============================================================================
-- PharmaSignal Migration 004: Focus Drug Brand Alias Consolidation (ROLLBACK)
-- Migration ID: 20261015_focus_drug_aliases
--
-- Safety Contract:
-- 1. Must run within an explicit transaction (atomic all-or-nothing).
-- 2. Restores drug_normalized strictly from case_drugs_before_image.pre_drug_normalized.
--    NEVER derives restoration from drug_original.
-- 3. Guards against reverting rows modified by unrelated downstream operations.
-- 4. Guards against repeated rollback execution.
-- 5. Verifies 100% bit-for-bit restoration of original normalized strings.
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- STEP 1: ROLLBACK PREFLIGHT CHECKS & SAFETY ASSERTIONS
-- ----------------------------------------------------------------------------
DO $$
DECLARE
    snap_count INTEGER;
    already_restored INTEGER;
    unexpected_state INTEGER;
BEGIN
    -- 1. Ensure before-image table and snapshot exist
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables 
        WHERE table_schema = current_schema() AND table_name = 'case_drugs_before_image'
    ) THEN
        RAISE EXCEPTION 'ROLLBACK ABORTED: Table case_drugs_before_image does not exist. Cannot perform safe rollback.';
    END IF;

    SELECT COUNT(*) INTO snap_count
    FROM case_drugs_before_image
    WHERE migration_id = '20261015_focus_drug_aliases';

    IF snap_count = 0 THEN
        RAISE EXCEPTION 'ROLLBACK ABORTED: No before-image snapshot found for migration_id = 20261015_focus_drug_aliases.';
    END IF;

    -- 2. Guard against repeated execution
    SELECT COUNT(*) INTO already_restored
    FROM case_drugs_before_image
    WHERE migration_id = '20261015_focus_drug_aliases'
      AND restored_at IS NOT NULL;

    IF already_restored > 0 THEN
        RAISE EXCEPTION 'ROLLBACK ABORTED: Migration 20261015_focus_drug_aliases was already restored at %.',
            (SELECT MIN(restored_at) FROM case_drugs_before_image WHERE migration_id = '20261015_focus_drug_aliases');
    END IF;

    -- 3. Guard against reverting rows that were altered by unrelated operations
    -- Every targeted row must currently equal post_drug_normalized
    SELECT COUNT(*) INTO unexpected_state
    FROM case_drugs cd
    JOIN case_drugs_before_image b ON b.case_drug_id = cd.id AND b.migration_id = '20261015_focus_drug_aliases'
    WHERE cd.drug_normalized != b.post_drug_normalized;

    IF unexpected_state > 0 THEN
        RAISE EXCEPTION 'ROLLBACK ABORTED: % rows no longer match expected post-migration drug_normalized value. Unrelated changes detected.', unexpected_state;
    END IF;

    RAISE NOTICE 'Rollback Preflight PASSED: % rows verified ready for restoration.', snap_count;
END $$;

-- ----------------------------------------------------------------------------
-- STEP 2: RESTORE EXACT PRE-MIGRATION drug_normalized VALUES
-- ----------------------------------------------------------------------------
UPDATE case_drugs cd
SET drug_normalized = b.pre_drug_normalized
FROM case_drugs_before_image b
WHERE cd.id = b.case_drug_id
  AND b.migration_id = '20261015_focus_drug_aliases';

-- ----------------------------------------------------------------------------
-- STEP 3: RECORD RESTORATION TIMESTAMP IN BEFORE-IMAGE AUDIT
-- ----------------------------------------------------------------------------
UPDATE case_drugs_before_image
SET restored_at = now()
WHERE migration_id = '20261015_focus_drug_aliases';

-- ----------------------------------------------------------------------------
-- STEP 4: ROLLBACK POSTFLIGHT VERIFICATION ASSERTIONS
-- ----------------------------------------------------------------------------
DO $$
DECLARE
    mismatches INTEGER;
    snap_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO snap_count
    FROM case_drugs_before_image
    WHERE migration_id = '20261015_focus_drug_aliases';

    -- Verify that every row now strictly matches pre_drug_normalized
    SELECT COUNT(*) INTO mismatches
    FROM case_drugs cd
    JOIN case_drugs_before_image b ON b.case_drug_id = cd.id AND b.migration_id = '20261015_focus_drug_aliases'
    WHERE cd.drug_normalized != b.pre_drug_normalized;

    IF mismatches > 0 THEN
        RAISE EXCEPTION 'ROLLBACK POSTFLIGHT FAILED: % rows failed to match original pre_drug_normalized value.', mismatches;
    END IF;

    RAISE NOTICE 'Rollback Postflight PASSED: All % rows restored to exact pre-migration normalized values.', snap_count;
END $$;

COMMIT;
