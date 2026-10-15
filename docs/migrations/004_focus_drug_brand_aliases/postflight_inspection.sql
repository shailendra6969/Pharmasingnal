-- ============================================================================
-- PharmaSignal Migration 004: Postflight Inspection & Diagnostic Queries
-- Read-only queries to run after applying the forward migration.
-- ============================================================================

-- 1. Verify zero targeted brand rows remain in case_drugs
SELECT drug_normalized, count(*) AS remaining_count
FROM case_drugs
WHERE drug_normalized IN ('LIPITOR', 'GLUCOPHAGE', 'GLUCOPHAGE XR', 'ADVIL', 'MOTRIN', 'PRILOSEC', 'ZOLOFT')
GROUP BY drug_normalized;

-- 2. Verify snapshot count in case_drugs_before_image
SELECT migration_id, count(*) AS snapshot_rows, min(captured_at), max(captured_at)
FROM case_drugs_before_image
WHERE migration_id = '20261015_focus_drug_aliases'
GROUP BY migration_id;

-- 3. Verify breakdown of consolidated rows by pre_drug_normalized
SELECT pre_drug_normalized, post_drug_normalized, count(*) AS consolidated_rows
FROM case_drugs_before_image
WHERE migration_id = '20261015_focus_drug_aliases'
GROUP BY pre_drug_normalized, post_drug_normalized
ORDER BY count(*) DESC;

-- 4. Verify that drug_original was 100% preserved
SELECT count(*) AS corrupted_original_count
FROM case_drugs cd
JOIN case_drugs_before_image b ON b.case_drug_id = cd.id AND b.migration_id = '20261015_focus_drug_aliases'
WHERE cd.drug_original IS DISTINCT FROM b.drug_original;
