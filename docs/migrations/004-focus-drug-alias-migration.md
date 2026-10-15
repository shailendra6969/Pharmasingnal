# PharmaSignal Migration 004: Focus Drug Brand Alias Consolidation Specification

**Status:** PROPOSED & REVIEWABLE (NOT EXECUTED)  
**Migration ID:** `20261015_focus_drug_aliases`  
**Target Mappings:**
- `LIPITOR` $\to$ `ATORVASTATIN`
- `GLUCOPHAGE`, `GLUCOPHAGE XR` $\to$ `METFORMIN`
- `ADVIL`, `MOTRIN` $\to$ `IBUPROFEN`
- `PRILOSEC` $\to$ `OMEPRAZOLE`
- `ZOLOFT` $\to$ `SERTRALINE`

---

## 1. Executive Summary & Design Rationale

During initial quarterly ingestion, raw brand names were ingested into `case_drugs.drug_normalized` verbatim when not present in `drug_aliases.json`. While `app/faers/normalize.py` handles future quarterly ingests, existing database rows in `case_drugs` retain brand names, fragmenting case counts between brand and generic (e.g. 11,712 Lipitor rows vs. 66,207 Atorvastatin cases).

This migration package defines a **production-safe, fully auditable, and 100% reversible** forward and rollback procedure for consolidating these 7 brand names across historical reports in PostgreSQL.

### Critical Safety Guarantees
1. **Never Restore from `drug_original`:** A naive rollback of `drug_normalized = drug_original` is rejected because:
   - `case_drugs.drug_normalized` is `NOT NULL`, but `case_drugs.drug_original` is nullable.
   - `drug_original` retains unnormalized casing, whitespace, and dosage formulations.
   - Deriving a rollback value from another column rather than restoring the exact pre-migration state is an unsafe antipattern.
2. **Persistent Before-Image Snapshot:** Every affected row is recorded in `case_drugs_before_image` with its exact pre-migration `drug_normalized` value, `primaryid`, `drug_seq`, and `case_drug_id` prior to update.
3. **Transaction Atomicity:** Snapshot creation and forward update occur inside the same transaction block (`BEGIN; ... COMMIT;`). Any failure triggers immediate automatic rollback.
4. **Idempotency & Isolation:** Guard assertions prevent duplicate execution, partial snapshots, and rollback over modified state.
5. **Preservation of Raw Provenance:** `case_drugs.drug_original` is preserved strictly untouched.

---

## 2. Before-Image Audit Schema

```sql
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
```

---

## 3. Forward Migration (`forward.sql`)

The forward operation executes in a single transaction:

```sql
BEGIN;

-- Preflight Guard 1: Prevent duplicate execution
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
            RAISE EXCEPTION 'MIGRATION ABORTED: Before-image snapshot for 20261015_focus_drug_aliases already exists (% rows).', existing_snapshots;
        END IF;
    END IF;
END $$;

-- Preflight Guard 2: Unique index protection
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
        RAISE EXCEPTION 'MIGRATION ABORTED: % rows with NULL drug_seq would violate unique index upon normalization.', collision_count;
    END IF;
END $$;

-- Step 1: Ensure before-image table exists
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

-- Step 2: Capture exact before-image snapshot
INSERT INTO case_drugs_before_image (
    migration_id, case_drug_id, primaryid, drug_seq,
    pre_drug_normalized, post_drug_normalized, drug_original
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

-- Step 3: Forward update of drug_normalized
UPDATE case_drugs cd
SET drug_normalized = b.post_drug_normalized
FROM case_drugs_before_image b
WHERE cd.id = b.case_drug_id
  AND b.migration_id = '20261015_focus_drug_aliases';

-- Step 4: Postflight assertions
DO $$
DECLARE
    snap_count INTEGER;
    remaining_brands INTEGER;
    untouched_originals INTEGER;
BEGIN
    SELECT COUNT(*) INTO snap_count
    FROM case_drugs_before_image
    WHERE migration_id = '20261015_focus_drug_aliases';

    SELECT COUNT(*) INTO remaining_brands
    FROM case_drugs
    WHERE drug_normalized IN ('LIPITOR', 'GLUCOPHAGE', 'GLUCOPHAGE XR', 'ADVIL', 'MOTRIN', 'PRILOSEC', 'ZOLOFT');

    IF remaining_brands > 0 THEN
        RAISE EXCEPTION 'POSTFLIGHT ASSERTION FAILED: % targeted brand rows still remain in case_drugs.', remaining_brands;
    END IF;

    SELECT COUNT(*) INTO untouched_originals
    FROM case_drugs cd
    JOIN case_drugs_before_image b ON b.case_drug_id = cd.id AND b.migration_id = '20261015_focus_drug_aliases'
    WHERE cd.drug_original IS DISTINCT FROM b.drug_original;

    IF untouched_originals > 0 THEN
        RAISE EXCEPTION 'POSTFLIGHT ASSERTION FAILED: % rows had drug_original altered unexpectedly.', untouched_originals;
    END IF;

    RAISE NOTICE 'Migration 004 Postflight PASSED: Consolidated % rows. Zero brand rows remain. drug_original preserved.', snap_count;
END $$;

COMMIT;
```

---

## 4. Rollback Operation (`rollback.sql`)

```sql
BEGIN;

-- Rollback Preflight Guards
DO $$
DECLARE
    snap_count INTEGER;
    already_restored INTEGER;
    unexpected_state INTEGER;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables 
        WHERE table_schema = current_schema() AND table_name = 'case_drugs_before_image'
    ) THEN
        RAISE EXCEPTION 'ROLLBACK ABORTED: Table case_drugs_before_image does not exist.';
    END IF;

    SELECT COUNT(*) INTO snap_count
    FROM case_drugs_before_image
    WHERE migration_id = '20261015_focus_drug_aliases';

    IF snap_count = 0 THEN
        RAISE EXCEPTION 'ROLLBACK ABORTED: No snapshot found for migration_id = 20261015_focus_drug_aliases.';
    END IF;

    SELECT COUNT(*) INTO already_restored
    FROM case_drugs_before_image
    WHERE migration_id = '20261015_focus_drug_aliases'
      AND restored_at IS NOT NULL;

    IF already_restored > 0 THEN
        RAISE EXCEPTION 'ROLLBACK ABORTED: Migration was already restored at %.',
            (SELECT MIN(restored_at) FROM case_drugs_before_image WHERE migration_id = '20261015_focus_drug_aliases');
    END IF;

    -- Ensure target rows match expected post_drug_normalized
    SELECT COUNT(*) INTO unexpected_state
    FROM case_drugs cd
    JOIN case_drugs_before_image b ON b.case_drug_id = cd.id AND b.migration_id = '20261015_focus_drug_aliases'
    WHERE cd.drug_normalized != b.post_drug_normalized;

    IF unexpected_state > 0 THEN
        RAISE EXCEPTION 'ROLLBACK ABORTED: % rows no longer match expected post-migration value. Unrelated changes detected.', unexpected_state;
    END IF;
END $$;

-- Step 1: Restore exact pre-migration normalized values
UPDATE case_drugs cd
SET drug_normalized = b.pre_drug_normalized
FROM case_drugs_before_image b
WHERE cd.id = b.case_drug_id
  AND b.migration_id = '20261015_focus_drug_aliases';

-- Step 2: Mark restoration timestamp in audit log
UPDATE case_drugs_before_image
SET restored_at = now()
WHERE migration_id = '20261015_focus_drug_aliases';

-- Step 3: Rollback postflight verification assertions
DO $$
DECLARE
    mismatches INTEGER;
    snap_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO snap_count
    FROM case_drugs_before_image
    WHERE migration_id = '20261015_focus_drug_aliases';

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
```

---

## 5. Unique Index & Deduplication Protection

1. **Unique Index Invariant:**  
   `case_drugs` enforces the unique index `uq_case_drugs_pid_drug` on `(primaryid, COALESCE(drug_seq, drug_normalized))`.
   - **Verification in PostgreSQL:** We confirmed that across all 15,164,870 rows in `case_drugs`, `drug_seq IS NULL` count is exactly **0**.
   - Because `drug_seq` is always present on every report row, `COALESCE(drug_seq, drug_normalized)` evaluates to `drug_seq`. Updating `drug_normalized` does not alter the indexed value and cannot cause unique key collisions.
2. **Case-Level Deduplication Invariant:**  
   In `app/statistics/engine.py:58`, the disproportionality engine constructs the drug exposure staging table using:
   ```sql
   SELECT DISTINCT dr.primaryid, dr.drug_normalized AS drug, cc.source_quarter
   FROM case_drugs dr
   JOIN stg_cur cc ON cc.primaryid = dr.primaryid
   ```
   where `stg_cur` is built as `CREATE UNLOGGED TABLE stg_cur AS SELECT primaryid, source_quarter FROM cases WHERE is_current` (`engine.py:95`), so only current report versions enter the staging table.
   When a FAERS report lists both the brand and generic (e.g. `LIPITOR` on line 1 and `ATORVASTATIN` on line 2), consolidating both to `ATORVASTATIN` results in `SELECT DISTINCT` collapsing them into **exactly one case-exposure record**. Co-reported cases are never double-counted.

---

## 6. Downstream Recomputation Requirements & Recovery

If this migration is applied to `case_drugs`, downstream derived tables and artifacts become immediately stale.

### Affected Derived Artifacts
1. **`signal_scores`:** Table storing all pooled $2\times2$ contingency counts ($a, b, c, d$), PRR, ROR, 95% CIs, and $\chi^2$ statistics.
2. **`signal_quarterly`:** Table storing quarterly case counts and $a_q$ exposure history.
3. **Quarterly Regimes:** Reporting regime flags (bulk submission bursts, notoriety ramps) generated by `app/statistics/regime.py`.
4. **Quarterly Profiles:** Historical profile verdicts (`elevation_is_new`, etc.) generated by `app/statistics/qprofile.py`.
5. **Concordance Validation Report:** `data/processed/concordance_validation_latest.json`.
6. **Confounding Probe Report:** `data/processed/confounding_probe_latest.json`.
7. **Snapshot `signal_scores_q1only`:** Frozen baseline snapshot table. It is NOT automatically refreshed. If a consolidated Q1-only baseline is desired for drift evaluation, it must be explicitly regenerated.

### Exact Execution Order & CLI Commands

```bash
# 1. Recompute disproportionality signal scores for BOTH modes (all and primary-suspect)
.venv/bin/python scripts/compute_signals.py --mode both --min-cases 3

# 2. Re-run SIDER label concordance validation
.venv/bin/python scripts/validate_concordance.py

# 3. Re-run indication-bias / confounding probes
.venv/bin/python scripts/probe_confounding.py

# 4. Rebuild data quality summary
.venv/bin/python scripts/summarize_quality.py
```

### Recovery Procedure if Recomputation Fails
If `compute_signals.py` or subsequent scripts encounter an unhandled exception after the forward database migration has executed:
1. **Do not re-ingest FAERS files.**
2. Execute `docs/migrations/004_focus_drug_brand_aliases/rollback.sql` via `psql` within its atomic transaction.
3. Run postflight verification to confirm all rows returned to pre-migration brand names.
4. Re-run baseline `scripts/compute_signals.py --mode both` to restore `signal_scores` to the pre-migration baseline.

---

## 7. Reconciled Impact Reporting

Every metric below is explicitly labeled according to its methodology.

### 7.1 Row Counts in `case_drugs` Across All Report Versions
`[Measured from existing database rows]`

| Brand Name | Target Canonical Ingredient | Exact Row Count in `case_drugs` |
| :--- | :--- | :--- |
| `LIPITOR` | `ATORVASTATIN` | 11,712 |
| `GLUCOPHAGE` | `METFORMIN` | 1,745 |
| `GLUCOPHAGE XR` | `METFORMIN` | 428 |
| `ADVIL` | `IBUPROFEN` | 5,225 |
| `MOTRIN` | `IBUPROFEN` | 1,447 |
| `PRILOSEC` | `OMEPRAZOLE` | 3,890 |
| `ZOLOFT` | `SERTRALINE` | 6,651 |
| **Total Brand Rows** | | **31,098** |

*(Directly verified in PostgreSQL; matches Freebuff measurement exactly).*

---

### 7.2 Distinct Current Cases (`cases.is_current = TRUE`)
`[Measured from existing database rows]` for existing baseline and distinct unions; `[Projected after alias consolidation]` for post-consolidation denominator and percentage increase.

Multi-brand targets (`METFORMIN` and `IBUPROFEN`) use **distinct case unions** (`WHERE drug_normalized = ANY(...)`), completely eliminating double-counting of cases that report multiple brands.

| Focus Target | Generic Cases `[Measured]` | Brand Cases (Distinct Union) `[Measured]` | Co-reported with Generic `[Measured]` | Cases with Both Brands `[Measured]` | Net-New Brand Cases `[Measured]` | Post-Consolidation Unique Cases `[Projected]` | Denominator Increase `[Projected]` |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **ATORVASTATIN** (Lipitor) | 66,207 | 8,880 | 682 | N/A | 8,198 | 74,405 | **+12.38%** |
| **METFORMIN** (Glucophage, Glucophage XR) | 53,686 | 1,648 | 297 | 4 | 1,351 | 55,037 | **+2.52%** |
| **IBUPROFEN** (Advil, Motrin) | 29,207 | 4,907 | 348 | 63 | 4,559 | 33,766 | **+15.61%** |
| **OMEPRAZOLE** (Prilosec) | 44,491 | 3,078 | 243 | N/A | 2,835 | 47,326 | **+6.37%** |
| **SERTRALINE** (Zoloft) | 25,272 | 5,255 | 292 | N/A | 4,963 | 30,235 | **+19.64%** |

*Notes on Distinct Unions:*
- In Metformin, 4 cases reported **both** `GLUCOPHAGE` and `GLUCOPHAGE XR`. Taking the distinct union (`count(DISTINCT case_id)`) prevents double-counting these 4 cases.
- In Ibuprofen, 63 cases reported **both** `ADVIL` and `MOTRIN`. The distinct union prevents double-counting these 63 cases.

---

### 7.3 Disproportionality Signal Behavior: Existing Baseline vs. Projected
`[Independently recomputed from existing database]` for existing signals; `[Projected after alias consolidation]` for combined projections.

| Drug / Event Pair | Source / State | $a$ | $b$ | $c$ | $d$ | PRR [95% CI] | ROR [95% CI] | $\chi^2$ |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **ATORVASTATIN / RHABDOMYOLYSIS** | Existing Baseline `[Independently recomputed]` | 909 | 65,298 | 4,134 | 2,761,434 | 9.18 [8.55–9.86] | 9.30 [8.65–10.00] | 5,437.8 |
| **LIPITOR / RHABDOMYOLYSIS** | Existing Baseline `[Independently recomputed]` | 21 | 8,859 | 5,022 | 2,817,873 | 1.33 [0.87–2.04] | 1.33 [0.87–2.04] | 1.4 |
| **ATORVASTATIN (Consolidated) / RHABDOMYOLYSIS** | Projected `[Projected]` | 929 | 73,476 | 4,114 | 2,753,256 | 8.37 [7.80–8.98] | 8.46 [7.88–9.09] | 4,919.6 |
| **IBUPROFEN / GASTRIC ULCER** | Existing Baseline `[Independently recomputed]` | 166 | 29,041 | 1,870 | 2,800,698 | 8.52 [7.27–9.98] | 8.56 [7.30–10.04] | 1,005.4 |
| **ADVIL / GASTRIC ULCER** | Existing Baseline `[Independently recomputed]` | 28 | 3,878 | 2,008 | 2,825,861 | 10.10 [6.96–14.64] | 10.16 [6.99–14.77] | 217.6 |
| **IBUPROFEN (Consolidated) / GASTRIC ULCER** | Projected `[Projected]` | 192 | 33,574 | 1,844 | 2,796,165 | 8.63 [7.44–10.01] | 8.67 [7.47–10.07] | 1,166.6 |
| **METFORMIN / LACTIC ACIDOSIS** | Existing Baseline `[Independently recomputed]` | 3,490 | 50,196 | 1,870 | 2,776,219 | 96.58 [91.36–102.09] | 103.22 [97.52–109.26] | 115,352.0 |
| **GLUCOPHAGE, GLUCOPHAGE XR / LACTIC ACIDOSIS** | Existing Baseline `[Independently recomputed]` | 35 | 1,613 | 5,325 | 2,824,802 | 11.29 [8.12–15.68] | 11.51 [8.23–16.11] | 316.5 |
| **METFORMIN (Consolidated) / LACTIC ACIDOSIS** | Projected `[Projected]` | 3,522 | 51,515 | 1,838 | 2,774,900 | 96.68 [91.43–102.22] | 103.22 [97.49–109.28] | 114,540.4 |
| **OMEPRAZOLE / HYPOMAGNESAEMIA** | Existing Baseline `[Independently recomputed]` | 447 | 44,044 | 1,820 | 2,785,464 | 15.39 [13.88–17.06] | 15.53 [14.00–17.23] | 4,819.4 |
| **PRILOSEC / HYPOMAGNESAEMIA** | Existing Baseline `[Independently recomputed]` | 10 | 3,068 | 2,257 | 2,826,440 | 4.07 [2.19–7.57] | 4.08 [2.19–7.60] | 20.1 |
| **OMEPRAZOLE (Consolidated) / HYPOMAGNESAEMIA** | Projected `[Projected]` | 457 | 46,869 | 1,810 | 2,782,639 | 14.86 [13.41–16.45] | 14.99 [13.52–16.62] | 4,707.6 |
| **SERTRALINE / SEROTONIN SYNDROME** | Existing Baseline `[Independently recomputed]` | 450 | 24,822 | 2,841 | 2,803,662 | 17.59 [15.94–19.41] | 17.89 [16.18–19.78] | 6,070.9 |
| **ZOLOFT / SEROTONIN SYNDROME** | Existing Baseline `[Independently recomputed]` | 13 | 5,242 | 3,278 | 2,823,242 | 2.13 [1.24–3.68] | 2.14 [1.24–3.68] | 6.7 |
| **SERTRALINE (Consolidated) / SEROTONIN SYNDROME** | Projected `[Projected]` | 462 | 29,773 | 2,829 | 2,798,711 | 15.13 [13.72–16.68] | 15.35 [13.90–16.95] | 5,235.3 |

*Projected-cell methodology (reproducible):*
- The background $N = 2{,}831{,}775$ current cases (`cases.is_current = TRUE`) is invariant under this migration, so every row above — baseline and projected — satisfies $a + b + c + d = N$.
- Projected cells are counted on the current table with the drug set replaced by its post-migration union (e.g. `ATORVASTATIN` ∪ `LIPITOR`), which is exactly the post-migration state for current cases: $a + b$ equals the post-consolidation case count in §7.2 for every projected row (74,405 / 33,766 / 55,037 / 47,326 / 30,235), and the event marginal $a + c$ is unchanged (5,043 / 2,036 / 5,360 / 2,267 / 3,291) because consolidation moves cases between drug groups only.
- Representative events are label-canonical for each extended target — metformin → `LACTIC ACIDOSIS`, omeprazole → `HYPOMAGNESAEMIA`, sertraline → `SEROTONIN SYNDROME` — each with ample case support, so every group shows the same baseline-vs-projected comparison as the first two.
- PRR, ROR, 95% CIs, and $\chi^2$ are recomputed from those cells with `app/statistics/disproportionality.py` (`compute()`, Yates-corrected $\chi^2$); cells and statistics are therefore mutually reproducible.

---

### 7.4 Critical Scientific Boundary: Case Denominators vs. Complete Screened Sets

> [!IMPORTANT]
> **A change in drug case counts does NOT by itself determine how the complete set of screened signals will change.**
>
> In disproportionality analysis, consolidating 31,098 brand rows shifts:
> 1. **The numerator $a$:** Brand-only cases reporting event $E$ add to $a$.
> 2. **The target denominator $(a + b)$:** Brand-only cases add to $(a + b)$, which can dilute PRR/ROR if the brand reports were for general/unrelated events.
> 3. **The background $c$ and $d$:** Every case moved from "other drugs" into the focus drug alters the universal non-target contingency cells ($c$ and $d$) for all ~2.1 million signals across the entire database.
> 4. **Threshold boundaries ($a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$):** Rare events previously hovering near $a=2$ on generic may cross the floor into screened signals with brand consolidation, while weakly elevated signals with high background reporting may drop below $\text{PRR} \ge 2.0$.
>
> Therefore, no claim can be made about the exact complete set of screened signals until the full deterministic pipeline (`scripts/compute_signals.py --mode both`) is executed.
