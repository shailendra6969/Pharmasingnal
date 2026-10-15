-- ============================================================================
-- PharmaSignal Migration 004: Preflight Inspection & Diagnostic Queries
-- Read-only queries to run before applying the migration.
-- ============================================================================

-- 1. Total candidate brand rows across all report versions
SELECT drug_normalized, count(*) AS matching_rows
FROM case_drugs
WHERE drug_normalized IN ('LIPITOR', 'GLUCOPHAGE', 'GLUCOPHAGE XR', 'ADVIL', 'MOTRIN', 'PRILOSEC', 'ZOLOFT')
GROUP BY drug_normalized
ORDER BY count(*) DESC;

-- 2. Verify NULL drug_seq distribution
SELECT count(*) AS null_seq_brand_rows
FROM case_drugs
WHERE drug_seq IS NULL
  AND drug_normalized IN ('LIPITOR', 'GLUCOPHAGE', 'GLUCOPHAGE XR', 'ADVIL', 'MOTRIN', 'PRILOSEC', 'ZOLOFT');

-- 3. Verify drug_original integrity
SELECT count(*) AS rows_where_original_is_null
FROM case_drugs
WHERE drug_normalized IN ('LIPITOR', 'GLUCOPHAGE', 'GLUCOPHAGE XR', 'ADVIL', 'MOTRIN', 'PRILOSEC', 'ZOLOFT')
  AND drug_original IS NULL;

-- 4. Check for existing before-image records.
--    Guarded: case_drugs_before_image does not exist until forward.sql runs,
--    so an unguarded SELECT here would abort the whole preflight on a
--    pre-migration database.
DO $$
DECLARE
    r RECORD;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = current_schema() AND table_name = 'case_drugs_before_image'
    ) THEN
        RAISE NOTICE 'case_drugs_before_image does not exist yet: migration 004 has not been applied.';
        RETURN;
    END IF;

    FOR r IN
        SELECT migration_id,
               count(*)              AS snapshot_rows,
               min(captured_at)      AS first_capture,
               max(captured_at)      AS last_capture
        FROM case_drugs_before_image
        GROUP BY migration_id
    LOOP
        RAISE NOTICE 'migration_id=%, snapshot_rows=%, captured_at=[% .. %]',
            r.migration_id, r.snapshot_rows, r.first_capture, r.last_capture;
    END LOOP;
END $$;
