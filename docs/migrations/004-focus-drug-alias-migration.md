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

---

### 7.5 Focus-Drug Signals Crossing or Dropping the Screening Thresholds
`[Measured from existing database rows]` for the pre-consolidation state; `[Projected after alias consolidation]` for the post-consolidation state.

**Screen rule** (spec §9, `app/statistics/disproportionality.compute()`): a signal is screened when $a \ge 3$ **and** $\text{PRR} \ge 2.0$ **and** $\chi^2 \ge 4.0$ (Yates-corrected $\chi^2$) — byte-for-byte the predicate the engine stores in `signal_scores.prr_screen`. This section therefore resolves §7.4's boundary for the five focus targets:

- **Scope.** Every event co-occurring with a focus target's generic name or one of its 7 brand aliases — the *Candidate Events* column — in mode `all` (non-primary-suspect), `period = 'all'`, over the invariant background $N = 2{,}831{,}775$ current cases.
- **Exact for focus drugs, not sampled.** Consolidation only *renames* `case_drugs` rows, so the post-consolidation 2×2 for a focus target is today's table with the drug set replaced by generic ∪ brands, computed with the engine's own arithmetic ($b = \text{drug}_n - a$, $c = \text{event}_n - a$, $d = N - a - b - c$, distinct current cases). The *Screened Before* column is **measured**: it reproduces the 7,378 generic and 3,916 brand rows flagged `prr_screen` in `signal_scores` today exactly, so *Screened After* predicts the post-recompute flags for focus drugs exactly. §7.4's caveat still governs the other ~2.1 million signals, whose $c$/$d$ cells also move — only `compute_signals.py` can report those.
- **Transition semantics.** *Cross* = fails the screen before, passes after; *Drop* = passes before, fails after. A Cross row therefore lists the criteria the **baseline** signal failed and a Drop row the criteria the **consolidated** signal fails — never a mixture, because every crosser passes all three criteria after and every dropper passed all three before. One signal can flip more than one criterion, so the criterion counts are not a partition of the transition totals.
- **Brand signals** still have their own rows today; post-consolidation they no longer exist as signals at all — their cases fold into the target. The *Brand Signals Screened Today (Folded Away)* column quantifies those 3,916 rows.
- **Full cell-level record** — all four cells and both statistics for both states of every transition — comes from `python scripts/report_figures.py --json` (`screen_transitions`); this table keeps the movement readable.

| Focus Target | Candidate Events | Screened Before | Screened After | Crossed | Dropped | Net Change | Brand Signals Screened Today (Folded Away) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **ATORVASTATIN** (Lipitor) | 7,549 | 1,433 | 1,637 | 275 | 71 | +204 | 1,077 |
| **METFORMIN** (Glucophage, Glucophage XR) | 6,849 | 1,561 | 1,588 | 50 | 23 | +27 | 484 |
| **IBUPROFEN** (Advil, Motrin) | 6,022 | 1,598 | 1,704 | 178 | 72 | +106 | 1,040 |
| **OMEPRAZOLE** (Prilosec) | 7,052 | 1,596 | 1,699 | 145 | 42 | +103 | 627 |
| **SERTRALINE** (Zoloft) | 5,505 | 1,190 | 1,321 | 182 | 51 | +131 | 688 |
| **All five focus targets** | 32,977 | 7,378 | 7,949 | 830 | 259 | +571 | 3,916 |

The mechanism behind the movement — which of the three criteria does the flipping:

| Flipping Criterion | Crossers (was failing, now passes) | Droppers (was passing, now fails) |
| :--- | :--- | :--- |
| $a \ge 3$ | 256 | 0 |
| $\text{PRR} \ge 2.0$ | 484 | 130 |
| $\chi^2 \ge 4.0$ | 596 | 149 |

Every crossing and dropping signal, in focus-target order (all crossings before that target's drops):

| Focus Target | Event | Transition | $a$ | PRR | $\chi^2$ | Flipped Threshold(s) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **ATORVASTATIN** | `ALCOHOL ABUSE` | Cross | 9 → 11 | 2.08 → 2.28 | 3.8 → 6.2 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ALKALOSIS HYPOCHLORAEMIC` | Cross | 0 → 4 | 0.00 → 6.74 | 0.0 → 11.9 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ALLERGIC COUGH` | Cross | 6 → 8 | 1.93 → 2.32 | 1.7 → 4.4 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ALLERGY TO CHEMICALS` | Cross | 6 → 14 | 1.06 → 2.28 | 0.0 → 8.2 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ALTERED VISUAL DEPTH PERCEPTION` | Cross | 3 → 6 | 1.51 → 2.78 | 0.1 → 4.8 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `AMPUTATION` | Cross | 4 → 8 | 1.65 → 3.06 | 0.5 → 8.4 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ANAEMIA OF CHRONIC DISEASE` | Cross | 4 → 7 | 2.53 → 4.12 | 2.2 → 12.1 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ANAL SKIN TAGS` | Cross | 2 → 3 | 3.21 → 4.45 | 1.1 → 4.3 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ANGIOGRAM` | Cross | 2 → 4 | 3.98 → 7.80 | 1.8 → 14.2 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ANORECTAL DISCOMFORT` | Cross | 17 → 22 | 1.74 → 2.02 | 4.4 → 9.8 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `ANTINUCLEAR ANTIBODY POSITIVE` | Cross | 6 → 16 | 0.82 → 2.01 | 0.1 → 6.8 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ARACHNOID CYST` | Cross | 2 → 10 | 1.82 → 9.75 | 0.1 → 55.3 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ARTERIAL CATHETERISATION` | Cross | 1 → 3 | 3.21 → 10.11 | 0.1 → 12.7 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ARTERIAL DISORDER` | Cross | 6 → 11 | 1.52 → 2.55 | 0.6 → 8.2 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ARTERIAL INJURY` | Cross | 4 → 6 | 2.14 → 2.93 | 1.3 → 5.3 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ARTHROPOD BITE` | Cross | 47 → 61 | 1.85 → 2.16 | 16.8 → 34.8 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `ARTHROSCOPY` | Cross | 3 → 4 | 2.91 → 3.53 | 1.9 → 4.5 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ASTERIXIS` | Cross | 5 → 6 | 2.52 → 2.71 | 3.0 → 4.5 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ATELECTASIS` | Cross | 21 → 54 | 0.86 → 2.03 | 0.3 → 25.8 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `BILIARY DYSKINESIA` | Cross | 2 → 3 | 3.80 → 5.29 | 1.6 → 5.7 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `BILIARY TRACT DISORDER` | Cross | 7 → 8 | 2.23 → 2.28 | 3.4 → 4.3 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `BIOPSY BONE MARROW` | Cross | 3 → 5 | 1.96 → 2.99 | 0.6 → 4.4 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `BLADDER DISORDER` | Cross | 35 → 63 | 1.31 → 2.15 | 2.2 → 35.4 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `BLADDER IRRIGATION` | Cross | 2 → 4 | 7.59 → 16.47 | 4.8 → 30.0 | $a \ge 3$ |
| **ATORVASTATIN** | `BLOOD ALBUMIN DECREASED` | Cross | 36 → 48 | 1.69 → 2.03 | 9.1 → 22.7 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `BLOOD ELECTROLYTES DECREASED` | Cross | 10 → 18 | 1.68 → 2.77 | 2.0 → 17.3 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `BLOOD OSMOLARITY INCREASED` | Cross | 3 → 4 | 3.06 → 3.71 | 2.2 → 4.9 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `BONE DISORDER` | Cross | 59 → 77 | 1.78 → 2.08 | 18.5 → 40.1 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `BONE FRAGMENTATION` | Cross | 3 → 4 | 3.92 → 4.78 | 3.5 → 7.4 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `BRAIN NATRIURETIC PEPTIDE ABNORMAL` | Cross | 1 → 3 | 2.20 → 6.54 | 0.0 → 7.6 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `BREAST PAIN` | Cross | 41 → 60 | 1.77 → 2.35 | 12.5 → 42.4 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `BRONCHIAL HYPERREACTIVITY` | Cross | 3 → 8 | 1.04 → 2.56 | 0.0 → 5.7 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `BRONCHITIS` | Cross | 396 → 492 | 1.88 → 2.09 | 154.7 → 264.8 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `BUNION OPERATION` | Cross | 1 → 6 | 0.76 → 4.45 | 0.0 → 11.3 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `BURSA DISORDER` | Cross | 2 → 3 | 5.57 → 7.94 | 3.1 → 9.7 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `CALCULUS BLADDER` | Cross | 3 → 10 | 0.70 → 2.17 | 0.1 → 4.9 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `CANCER FATIGUE` | Cross | 4 → 6 | 2.09 → 2.85 | 1.2 → 5.0 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `CANCER PAIN` | Cross | 16 → 24 | 1.52 → 2.05 | 2.2 → 11.3 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `CARCINOID TUMOUR OF THE GASTROINTESTINAL TRACT` | Cross | 0 → 3 | 0.00 → 27.79 | 0.0 → 30.0 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `CARDIAC ABLATION` | Cross | 12 → 17 | 1.76 → 2.26 | 3.1 → 10.0 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `CARDIAC AMYLOIDOSIS` | Cross | 11 → 16 | 1.57 → 2.07 | 1.7 → 7.3 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `CARDIAC RESYNCHRONISATION THERAPY` | Cross | 2 → 4 | 8.35 → 18.53 | 5.4 → 33.0 | $a \ge 3$ |
| **ATORVASTATIN** | `CARDIAC VALVE FIBROELASTOMA` | Cross | 2 → 3 | 5.97 → 8.55 | 3.5 → 10.6 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `CARDIOVERSION` | Cross | 6 → 8 | 1.99 → 2.39 | 1.9 → 4.8 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `CAROTID ENDARTERECTOMY` | Cross | 2 → 3 | 3.63 → 5.05 | 1.5 → 5.3 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `CARPAL TUNNEL SYNDROME` | Cross | 84 → 101 | 1.94 → 2.09 | 35.7 → 53.3 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `CATARACT` | Cross | 419 → 588 | 1.81 → 2.30 | 146.1 → 405.7 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `CATHETER SITE ABSCESS` | Cross | 0 → 8 | 0.00 → 9.56 | 0.2 → 42.0 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `CATHETER SITE MASS` | Cross | 3 → 4 | 3.69 → 4.49 | 3.2 → 6.8 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `CENTRAL VENOUS PRESSURE DECREASED` | Cross | 2 → 3 | 41.77 → 111.18 | 21.7 → 56.0 | $a \ge 3$ |
| **ATORVASTATIN** | `CEREBRAL ARTERY STENOSIS` | Cross | 4 → 5 | 2.69 → 3.04 | 2.5 → 4.5 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `CEREBRAL MICROANGIOPATHY` | Cross | 2 → 3 | 3.63 → 5.05 | 1.5 → 5.3 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `CEREBRAL VASCULAR OCCLUSION` | Cross | 2 → 3 | 4.18 → 5.85 | 1.9 → 6.6 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `CHANGE OF BOWEL HABIT` | Cross | 5 → 20 | 1.04 → 4.01 | 0.0 → 38.0 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `CHEST DISCOMFORT` | Cross | 563 → 754 | 1.66 → 2.00 | 143.0 → 359.7 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `CHONDROPATHY` | Cross | 8 → 12 | 1.55 → 2.10 | 1.0 → 5.5 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `CHRONIC CHEEK BITING` | Cross | 0 → 3 | 0.00 → 22.24 | 0.0 → 25.6 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `CLAVICLE FRACTURE` | Cross | 21 → 33 | 1.86 → 2.66 | 7.2 → 30.4 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `COMMUNICATION DISORDER` | Cross | 12 → 30 | 1.09 → 2.52 | 0.0 → 24.2 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `COMPLEMENT FACTOR C3 DECREASED` | Cross | 2 → 3 | 3.48 → 4.83 | 1.3 → 5.0 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `COMPLEMENT FACTOR C4 INCREASED` | Cross | 1 → 4 | 3.48 → 16.47 | 0.1 → 30.0 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `CONJUNCTIVAL OEDEMA` | Cross | 6 → 8 | 1.93 → 2.32 | 1.7 → 4.4 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `COR PULMONALE` | Cross | 3 → 4 | 3.39 → 4.12 | 2.7 → 5.9 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `CREPITATIONS` | Cross | 8 → 16 | 1.32 → 2.42 | 0.3 → 11.2 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `DIABETES MELLITUS` | Cross | 369 → 452 | 1.98 → 2.18 | 170.6 → 270.9 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `DIABETIC RETINOPATHY` | Cross | 18 → 21 | 1.98 → 2.07 | 7.5 → 10.0 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `DIARRHOEA` | Cross | 4,351 → 5,064 | 1.96 → 2.04 | 2,027.5 → 2,638.4 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `DIVERTICULITIS` | Cross | 179 → 211 | 2.00 → 2.11 | 84.5 → 115.6 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `DOPAMINE TRANSPORTER SCINTIGRAPHY ABNORMAL` | Cross | 1 → 3 | 3.80 → 12.35 | 0.2 → 15.5 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `DYSPHAGIA` | Cross | 507 → 695 | 1.76 → 2.17 | 158.3 → 414.5 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `DYSPNOEA` | Cross | 3,352 → 3,932 | 1.98 → 2.08 | 1,594.0 → 2,137.2 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `DYSPNOEA PAROXYSMAL NOCTURNAL` | Cross | 3 → 6 | 1.79 → 3.32 | 0.4 → 6.9 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `EAR HAEMORRHAGE` | Cross | 12 → 16 | 1.86 → 2.23 | 3.7 → 9.1 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `EATING DISORDER` | Cross | 116 → 254 | 1.89 → 3.89 | 46.1 → 491.6 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `ECCHYMOSIS` | Cross | 30 → 38 | 1.92 → 2.19 | 11.8 → 22.0 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `ECG SIGNS OF MYOCARDIAL ISCHAEMIA` | Cross | 2 → 3 | 7.59 → 11.12 | 4.8 → 14.0 | $a \ge 3$ |
| **ATORVASTATIN** | `ELECTROCARDIOGRAM QT INTERVAL ABNORMAL` | Cross | 4 → 6 | 1.90 → 2.59 | 0.9 → 4.0 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ELECTROCARDIOGRAM QT SHORTENED` | Cross | 1 → 3 | 1.67 → 4.83 | 0.0 → 5.0 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ENDOMETRIAL THICKENING` | Cross | 5 → 7 | 1.90 → 2.40 | 1.2 → 4.1 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ENEMA ADMINISTRATION` | Cross | 2 → 3 | 5.97 → 8.55 | 3.5 → 10.6 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `EUSTACHIAN TUBE DYSFUNCTION` | Cross | 4 → 7 | 1.84 → 2.95 | 0.8 → 6.6 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `EXERCISE LACK OF` | Cross | 3 → 5 | 2.46 → 3.78 | 1.2 → 6.9 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `EXPOSURE TO SARS-COV-2` | Cross | 8 → 18 | 1.18 → 2.44 | 0.1 → 13.0 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `EYE INFECTION` | Cross | 87 → 120 | 1.78 → 2.22 | 27.9 → 74.4 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `EYELID DISORDER` | Cross | 2 → 16 | 0.28 → 2.11 | 2.9 → 7.8 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `EYELID FUNCTION DISORDER` | Cross | 4 → 7 | 1.78 → 2.85 | 0.7 → 6.1 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `EYELID INFECTION` | Cross | 5 → 6 | 2.61 → 2.81 | 3.3 → 4.9 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `FABRY'S DISEASE` | Cross | 3 → 6 | 2.32 → 4.36 | 1.0 → 11.0 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `FALLOPIAN TUBE CANCER` | Cross | 2 → 3 | 3.63 → 5.05 | 1.5 → 5.3 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `FATIGUE` | Cross | 5,017 → 5,919 | 1.90 → 2.00 | 2,126.6 → 2,941.6 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `FOOT DEFORMITY` | Cross | 69 → 97 | 1.65 → 2.09 | 16.4 → 51.4 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `FRACTURE PAIN` | Cross | 5 → 11 | 1.77 → 3.64 | 0.9 → 16.8 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `FRACTURED COCCYX` | Cross | 8 → 9 | 2.12 → 2.12 | 3.5 → 4.0 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `FRUSTRATION TOLERANCE DECREASED` | Cross | 50 → 75 | 1.48 → 2.01 | 7.1 → 35.0 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `FUNGAL FOOT INFECTION` | Cross | 8 → 14 | 1.73 → 2.77 | 1.7 → 13.1 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `FURUNCLE` | Cross | 26 → 48 | 1.22 → 2.06 | 0.8 → 23.7 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `GASTRIC CANCER STAGE IV` | Cross | 2 → 3 | 5.57 → 7.94 | 3.1 → 9.7 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `GASTROOESOPHAGEAL REFLUX DISEASE` | Cross | 491 → 620 | 1.94 → 2.19 | 212.0 → 380.9 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `GINGIVAL PRURITUS` | Cross | 1 → 3 | 3.80 → 12.35 | 0.2 → 15.5 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `GLUCOSE-6-PHOSPHATE DEHYDROGENASE DEFICIENCY` | Cross | 1 → 4 | 1.31 → 5.11 | 0.0 → 8.2 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `GRAVES' DISEASE` | Cross | 25 → 35 | 1.85 → 2.34 | 8.5 → 23.9 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `GROWTH HORMONE DEFICIENCY` | Cross | 3 → 6 | 3.30 → 6.35 | 2.5 → 18.6 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `HAEMANGIOMA OF LIVER` | Cross | 0 → 7 | 0.00 → 2.67 | 1.6 → 5.3 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `HAEMOGLOBIN DECREASED` | Cross | 537 → 654 | 1.91 → 2.08 | 222.1 → 349.0 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `HEART RATE ABNORMAL` | Cross | 40 → 50 | 1.97 → 2.21 | 17.3 → 30.1 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `HEPATIC ENZYME DECREASED` | Cross | 3 → 10 | 1.23 → 3.90 | 0.0 → 16.9 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `HEPATITIS A` | Cross | 4 → 7 | 1.56 → 2.49 | 0.3 → 4.5 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `HEPATITIS VIRAL` | Cross | 4 → 8 | 1.55 → 2.85 | 0.3 → 7.2 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `HERNIA PAIN` | Cross | 4 → 6 | 2.35 → 3.22 | 1.8 → 6.5 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `HIRSUTISM` | Cross | 7 → 9 | 1.86 → 2.15 | 1.9 → 4.2 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `HUMERUS FRACTURE` | Cross | 22 → 29 | 1.72 → 2.04 | 5.7 → 13.6 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `HYPERKERATOSIS` | Cross | 32 → 39 | 1.85 → 2.02 | 11.2 → 18.1 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `HYPERPLASIA OF THYMIC EPITHELIUM` | Cross | 0 → 3 | 0.00 → 111.18 | 0.0 → 56.0 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `HYPOVENTILATION` | Cross | 11 → 19 | 1.54 → 2.42 | 1.5 → 13.5 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `IMPAIRED FASTING GLUCOSE` | Cross | 2 → 6 | 2.98 → 9.26 | 0.9 → 28.9 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `INCISION SITE DISCHARGE` | Cross | 5 → 7 | 2.32 → 2.95 | 2.4 → 6.6 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `INCISIONAL HERNIA` | Cross | 7 → 10 | 1.98 → 2.56 | 2.3 → 7.4 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `INCREASED UPPER AIRWAY SECRETION` | Cross | 16 → 19 | 1.94 → 2.06 | 6.0 → 8.8 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `INFECTED DERMAL CYST` | Cross | 3 → 4 | 3.21 → 3.90 | 2.4 → 5.3 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `INFECTED SKIN ULCER` | Cross | 8 → 11 | 1.80 → 2.23 | 2.0 → 5.9 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `INFUSION SITE HAEMATOMA` | Cross | 5 → 7 | 2.68 → 3.41 | 3.5 → 8.8 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `INFUSION SITE HYPOAESTHESIA` | Cross | 2 → 3 | 3.48 → 4.83 | 1.3 → 5.0 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `INFUSION SITE LACERATION` | Cross | 1 → 3 | 4.64 → 15.88 | 0.3 → 19.6 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `INFUSION SITE REACTION` | Cross | 36 → 47 | 1.71 → 2.01 | 9.6 → 21.6 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `INFUSION SITE SCAB` | Cross | 3 → 4 | 4.18 → 5.11 | 4.0 → 8.2 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `INFUSION SITE SCAR` | Cross | 6 → 12 | 2.14 → 4.01 | 2.5 → 21.7 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `INTERNATIONAL NORMALISED RATIO DECREASED` | Cross | 6 → 9 | 2.51 → 3.44 | 3.8 → 12.0 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `INTERVERTEBRAL DISC PROTRUSION` | Cross | 113 → 132 | 1.99 → 2.08 | 52.5 → 69.4 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `INTESTINAL HAEMORRHAGE` | Cross | 28 → 45 | 1.80 → 2.64 | 8.9 → 41.3 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `INVESTIGATION ABNORMAL` | Cross | 14 → 20 | 1.70 → 2.19 | 3.2 → 11.1 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `JOINT ABSCESS` | Cross | 3 → 5 | 2.72 → 4.21 | 1.6 → 8.2 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `JOINT INSTABILITY` | Cross | 6 → 10 | 1.37 → 2.07 | 0.3 → 4.3 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `KYPHOSIS` | Cross | 5 → 10 | 1.39 → 2.56 | 0.2 → 7.4 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `LEFT ATRIAL VOLUME INCREASED` | Cross | 2 → 3 | 16.71 → 27.79 | 11.2 → 30.0 | $a \ge 3$ |
| **ATORVASTATIN** | `LIGAMENT SPRAIN` | Cross | 67 → 92 | 1.70 → 2.10 | 17.8 → 49.2 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `LIGAMENTUM FLAVUM HYPERTROPHY` | Cross | 1 → 7 | 2.09 → 18.53 | 0.0 → 65.9 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `LIPASE INCREASED` | Cross | 39 → 46 | 1.95 → 2.06 | 16.4 → 22.8 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `LOSS OF PERSONAL INDEPENDENCE IN DAILY ACTIVITIES` | Cross | 535 → 624 | 1.99 → 2.08 | 251.7 → 329.8 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `LOWER RESPIRATORY TRACT INFECTION BACTERIAL` | Cross | 2 → 7 | 1.33 → 4.47 | 0.0 → 13.8 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `LOWER URINARY TRACT SYMPTOMS` | Cross | 3 → 7 | 2.09 → 4.63 | 0.7 → 14.6 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `MALABSORPTION` | Cross | 29 → 41 | 1.76 → 2.24 | 8.3 → 25.4 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `MAMMOGRAM ABNORMAL` | Cross | 2 → 9 | 1.10 → 4.83 | 0.0 → 20.9 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `MEAN CELL VOLUME INCREASED` | Cross | 24 → 31 | 1.92 → 2.23 | 9.2 → 18.7 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `MEAN PLATELET VOLUME DECREASED` | Cross | 6 → 9 | 1.62 → 2.19 | 0.8 → 4.4 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `MEDICATION OVERUSE HEADACHE` | Cross | 0 → 16 | 0.00 → 3.71 | 3.3 → 26.3 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `MELANOMA RECURRENT` | Cross | 3 → 5 | 1.99 → 3.04 | 0.6 → 4.5 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `MERALGIA PARAESTHETICA` | Cross | 2 → 3 | 4.18 → 5.85 | 1.9 → 6.6 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `METABOLIC DYSFUNCTION-ASSOCIATED LIVER DISEASE` | Cross | 6 → 10 | 2.16 → 3.31 | 2.5 → 12.7 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `MICROALBUMINURIA` | Cross | 5 → 11 | 2.78 → 5.91 | 3.8 → 34.5 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `MICROANGIOPATHY` | Cross | 7 → 8 | 2.23 → 2.28 | 3.4 → 4.3 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `MICTURITION URGENCY` | Cross | 72 → 97 | 1.95 → 2.37 | 31.0 → 71.0 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `MONOCYTOSIS` | Cross | 1 → 11 | 0.36 → 3.88 | 0.6 → 18.7 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `MULTIMORBIDITY` | Cross | 5 → 7 | 2.09 → 2.65 | 1.7 → 5.2 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `MUSCLE SPASTICITY` | Cross | 46 → 162 | 1.63 → 5.65 | 10.2 → 533.8 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `MUSCULOSKELETAL FOREIGN BODY` | Cross | 0 → 4 | 0.00 → 24.71 | 0.0 → 41.0 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `MYOCARDIAL NECROSIS MARKER INCREASED` | Cross | 9 → 23 | 1.06 → 2.51 | 0.0 → 18.2 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `NASAL ULCER` | Cross | 9 → 22 | 1.94 → 4.50 | 3.0 → 50.3 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `NON-ALCOHOLIC FATTY LIVER` | Cross | 4 → 13 | 1.92 → 6.18 | 0.9 → 43.9 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `NON-CARDIAC CHEST PAIN` | Cross | 8 → 11 | 1.90 → 2.36 | 2.4 → 6.8 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `NUCHAL RIGIDITY` | Cross | 3 → 5 | 2.20 → 3.37 | 0.9 → 5.6 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `OPHTHALMIC ARTERY OCCLUSION` | Cross | 2 → 3 | 3.98 → 5.56 | 1.8 → 6.1 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `OPTIC ATROPHY` | Cross | 8 → 10 | 1.95 → 2.19 | 2.7 → 5.0 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `OSTEOARTHRITIS` | Cross | 274 → 358 | 1.74 → 2.04 | 81.5 → 178.8 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `OSTEOARTHROPATHY` | Cross | 0 → 6 | 0.00 → 22.24 | 0.0 → 63.0 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `OSTEOMALACIA` | Cross | 2 → 6 | 1.10 → 3.09 | 0.0 → 6.0 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `OSTEOMYELITIS ACUTE` | Cross | 1 → 3 | 1.49 → 4.28 | 0.0 → 4.1 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `PAIN IN EXTREMITY` | Cross | 1,529 → 1,795 | 1.97 → 2.07 | 710.2 → 955.8 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `PANCREATIC ENLARGEMENT` | Cross | 4 → 5 | 3.09 → 3.50 | 3.5 → 6.0 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `PANCREATIC PSEUDOCYST` | Cross | 1 → 9 | 0.54 → 4.76 | 0.1 → 20.4 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `PELVIC MASS` | Cross | 4 → 6 | 2.04 → 2.78 | 1.1 → 4.8 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `PENILE INFECTION` | Cross | 2 → 4 | 3.21 → 6.18 | 1.1 → 10.7 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `PENILE SIZE REDUCED` | Cross | 4 → 7 | 1.55 → 2.47 | 0.3 → 4.4 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `PERICARDIAL EFFUSION` | Cross | 124 → 209 | 1.62 → 2.49 | 27.9 → 173.7 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `PERIORBITAL INFLAMMATION` | Cross | 5 → 6 | 2.55 → 2.75 | 3.1 → 4.6 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `PERIPHERAL EMBOLISM` | Cross | 4 → 5 | 3.15 → 3.56 | 3.6 → 6.2 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `PERIUMBILICAL ABSCESS` | Cross | 0 → 8 | 0.00 → 19.76 | 0.0 → 80.8 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `PHARYNGEAL ERYTHEMA` | Cross | 6 → 11 | 1.30 → 2.17 | 0.2 → 5.5 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `PITUITARY TUMOUR BENIGN` | Cross | 6 → 9 | 1.66 → 2.25 | 0.9 → 4.8 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `PLANTAR FASCIITIS` | Cross | 21 → 29 | 1.95 → 2.43 | 8.4 → 21.6 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `PNEUMONIA HAEMOPHILUS` | Cross | 4 → 10 | 1.25 → 2.90 | 0.0 → 9.8 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `POLYPECTOMY` | Cross | 5 → 8 | 1.68 → 2.45 | 0.7 → 5.1 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `PORTAL TRACT INFLAMMATION` | Cross | 1 → 4 | 1.74 → 7.06 | 0.0 → 12.6 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `POST PROCEDURAL SEPSIS` | Cross | 6 → 8 | 2.30 → 2.77 | 3.0 → 6.8 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `POST PROCEDURAL SWELLING` | Cross | 9 → 12 | 2.07 → 2.48 | 3.7 → 8.6 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `POSTOPERATIVE WOUND INFECTION` | Cross | 33 → 61 | 1.30 → 2.20 | 2.0 → 36.4 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `PROCTALGIA` | Cross | 32 → 58 | 1.53 → 2.53 | 5.1 → 49.0 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `PRODUCT AFTER TASTE` | Cross | 15 → 25 | 1.63 → 2.48 | 2.9 → 19.2 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `PROPHYLAXIS OF NAUSEA AND VOMITING` | Cross | 1 → 5 | 4.64 → 37.06 | 0.3 → 70.2 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `PROSTATIC SPECIFIC ANTIGEN DECREASED` | Cross | 4 → 5 | 2.69 → 3.04 | 2.5 → 4.5 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `PROTEIN TOTAL DECREASED` | Cross | 19 → 30 | 1.71 → 2.46 | 4.8 → 23.0 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `PULMONARY ARTERIAL HYPERTENSION` | Cross | 61 → 74 | 1.97 → 2.14 | 26.8 → 41.4 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `PULMONARY ARTERIAL PRESSURE INCREASED` | Cross | 5 → 10 | 1.27 → 2.33 | 0.1 → 5.9 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `PULMONARY ARTERY DILATATION` | Cross | 3 → 5 | 2.28 → 3.50 | 1.0 → 6.0 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `PULMONARY VENOUS HYPERTENSION` | Cross | 1 → 3 | 6.96 → 27.79 | 0.7 → 30.0 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `PULSE ABNORMAL` | Cross | 13 → 23 | 1.71 → 2.78 | 3.0 → 22.7 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `PURULENT DISCHARGE` | Cross | 20 → 25 | 1.98 → 2.22 | 8.3 → 14.6 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `RECTAL POLYP` | Cross | 0 → 8 | 0.00 → 6.18 | 0.5 → 25.4 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `RED BLOOD CELL COUNT INCREASED` | Cross | 20 → 30 | 1.95 → 2.66 | 8.0 → 27.4 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `RED BLOOD CELL SEDIMENTATION RATE DECREASED` | Cross | 0 → 8 | 0.00 → 7.41 | 0.4 → 31.7 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `RED CELL DISTRIBUTION WIDTH INCREASED` | Cross | 30 → 38 | 1.84 → 2.10 | 10.3 → 19.6 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `RENAL CANCER RECURRENT` | Cross | 2 → 3 | 3.80 → 5.29 | 1.6 → 5.7 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `RESPIRATORY FUME INHALATION DISORDER` | Cross | 3 → 7 | 2.51 → 5.64 | 1.3 → 19.2 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `RESPIRATORY RATE INCREASED` | Cross | 54 → 73 | 1.77 → 2.15 | 16.5 → 41.5 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `RHINORRHOEA` | Cross | 488 → 655 | 1.98 → 2.39 | 224.5 → 498.7 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `RIB FRACTURE` | Cross | 122 → 153 | 1.95 → 2.20 | 53.2 → 93.2 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `RIGHT ATRIAL ENLARGEMENT` | Cross | 0 → 3 | 0.00 → 4.28 | 0.0 → 4.1 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `RIGHT VENTRICULAR SYSTOLIC PRESSURE INCREASED` | Cross | 3 → 5 | 2.56 → 3.94 | 1.4 → 7.4 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ROTATOR CUFF REPAIR` | Cross | 8 → 13 | 1.38 → 2.03 | 0.5 → 5.5 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ROTATOR CUFF SYNDROME` | Cross | 64 → 83 | 1.82 → 2.12 | 21.7 → 45.2 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `RUBBER SENSITIVITY` | Cross | 3 → 20 | 0.35 → 2.16 | 3.0 → 10.7 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `SCAR PAIN` | Cross | 4 → 5 | 3.09 → 3.50 | 3.5 → 6.0 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `SCIATICA` | Cross | 146 → 192 | 1.88 → 2.22 | 56.5 → 120.6 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `SCLERODERMA` | Cross | 15 → 20 | 1.86 → 2.23 | 4.9 → 11.7 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `SENSITISATION` | Cross | 2 → 16 | 0.72 → 5.81 | 0.0 → 50.9 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `SENSORY LOSS` | Cross | 46 → 166 | 2.00 → 7.31 | 21.0 → 750.6 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `SINONASAL OBSTRUCTION` | Cross | 6 → 8 | 1.99 → 2.39 | 1.9 → 4.8 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `SINUSITIS` | Cross | 593 → 852 | 1.61 → 2.08 | 131.4 → 456.7 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `SKIN ATROPHY` | Cross | 34 → 46 | 1.73 → 2.11 | 9.4 → 24.3 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `SKIN HAEMORRHAGE` | Cross | 143 → 263 | 1.78 → 3.01 | 46.1 → 325.0 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `SKIN SENSITISATION` | Cross | 5 → 11 | 2.05 → 4.25 | 1.6 → 21.6 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `SKIN ULCER` | Cross | 141 → 188 | 1.95 → 2.34 | 61.0 → 134.2 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `SNEEZING` | Cross | 146 → 276 | 1.65 → 2.88 | 35.7 → 312.0 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `SNORING` | Cross | 22 → 32 | 1.82 → 2.40 | 7.1 → 23.2 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `SPECIFIC GRAVITY URINE ABNORMAL` | Cross | 2 → 3 | 4.91 → 6.95 | 2.6 → 8.2 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `SPECIFIC GRAVITY URINE DECREASED` | Cross | 1 → 3 | 3.48 → 11.12 | 0.1 → 14.0 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `SPLEEN DISORDER` | Cross | 7 → 10 | 1.59 → 2.05 | 0.9 → 4.1 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `SPONDYLOLISTHESIS` | Cross | 6 → 9 | 2.02 → 2.76 | 2.0 → 7.8 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `SPUTUM DISCOLOURED` | Cross | 64 → 89 | 1.61 → 2.02 | 13.7 → 42.4 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `SPUTUM INCREASED` | Cross | 13 → 25 | 1.36 → 2.39 | 0.9 → 17.7 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `SQUAMOUS CELL CARCINOMA OF SKIN` | Cross | 50 → 58 | 1.98 → 2.05 | 22.1 → 28.5 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `STERNAL FRACTURE` | Cross | 6 → 11 | 1.34 → 2.24 | 0.2 → 6.0 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `STRESS FRACTURE` | Cross | 17 → 28 | 1.66 → 2.49 | 3.7 → 22.0 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `SUPERFICIAL INJURY OF EYE` | Cross | 6 → 10 | 1.42 → 2.15 | 0.4 → 4.8 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `SYMBLEPHARON` | Cross | 0 → 8 | 0.00 → 8.47 | 0.3 → 36.9 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `SYNOVIAL CYST` | Cross | 21 → 44 | 1.57 → 3.05 | 3.7 → 54.0 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `SYNOVIAL DISORDER` | Cross | 0 → 7 | 0.00 → 3.09 | 1.3 → 7.3 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `SYNOVIAL FLUID ANALYSIS` | Cross | 0 → 7 | 0.00 → 14.41 | 0.0 → 53.4 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `SYSTOLIC DYSFUNCTION` | Cross | 9 → 10 | 2.11 → 2.09 | 4.0 → 4.4 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `TENOSYNOVITIS STENOSANS` | Cross | 2 → 23 | 0.99 → 13.53 | 0.0 → 186.2 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `THORACIC OPERATION` | Cross | 3 → 4 | 3.92 → 4.78 | 3.5 → 7.4 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `THORACOTOMY` | Cross | 2 → 3 | 6.43 → 9.26 | 3.9 → 11.6 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `THYROID ATROPHY` | Cross | 1 → 10 | 2.09 → 33.69 | 0.0 → 149.0 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `THYROID DISORDER` | Cross | 85 → 165 | 1.63 → 2.92 | 19.4 → 191.1 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `TOE AMPUTATION` | Cross | 15 → 20 | 1.84 → 2.21 | 4.7 → 11.3 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `TOE OPERATION` | Cross | 1 → 8 | 0.39 → 2.94 | 0.4 → 7.7 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `TOXIC SHOCK SYNDROME` | Cross | 1 → 13 | 0.49 → 6.60 | 0.1 → 47.7 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `TRANSIENT GLOBAL AMNESIA` | Cross | 5 → 6 | 2.61 → 2.81 | 3.3 → 4.9 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `TRIGEMINAL NEURALGIA` | Cross | 23 → 31 | 1.93 → 2.34 | 9.0 → 21.2 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `TUMOUR COMPRESSION` | Cross | 2 → 5 | 2.26 → 5.45 | 0.4 → 12.1 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `URINARY BLADDER POLYP` | Cross | 2 → 4 | 4.91 → 9.88 | 2.6 → 18.5 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `URINE ABNORMALITY` | Cross | 32 → 52 | 1.98 → 2.94 | 13.8 → 59.8 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `URINE ALBUMIN/CREATININE RATIO ABNORMAL` | Cross | 3 → 4 | 3.69 → 4.49 | 3.2 → 6.8 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `URINE OUTPUT DECREASED` | Cross | 37 → 46 | 1.86 → 2.08 | 13.4 → 23.4 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `URINE PROTEIN/CREATININE RATIO ABNORMAL` | Cross | 3 → 4 | 3.48 → 4.24 | 2.8 → 6.1 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `URINE PROTEIN/CREATININE RATIO DECREASED` | Cross | 0 → 3 | 0.00 → 13.90 | 0.0 → 17.4 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `URINE PROTEIN/CREATININE RATIO INCREASED` | Cross | 12 → 21 | 1.75 → 2.80 | 3.0 → 20.9 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `UTEROVAGINAL PROLAPSE` | Cross | 0 → 4 | 0.00 → 37.06 | 0.0 → 52.9 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `VAGINAL LESION` | Cross | 0 → 8 | 0.00 → 12.89 | 0.1 → 56.4 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `VAGINAL PROLAPSE` | Cross | 4 → 8 | 2.93 → 5.59 | 3.1 → 22.3 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `VARICES OESOPHAGEAL` | Cross | 17 → 22 | 1.99 → 2.32 | 7.1 → 14.3 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `VASCULAR COMPRESSION` | Cross | 4 → 5 | 3.15 → 3.56 | 3.6 → 6.2 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `VASCULAR DEMENTIA` | Cross | 5 → 9 | 2.37 → 3.97 | 2.5 → 15.4 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `VASCULAR RUPTURE` | Cross | 7 → 9 | 2.04 → 2.37 | 2.6 → 5.4 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `VASCULITIS NECROTISING` | Cross | 3 → 5 | 1.99 → 3.04 | 0.6 → 4.5 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `VERTEBRAL LATERAL RECESS STENOSIS` | Cross | 0 → 6 | 0.00 → 44.47 | 0.0 → 96.5 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `VITAMIN B12 DECREASED` | Cross | 24 → 31 | 1.86 → 2.16 | 8.3 → 17.0 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `VITAMIN B12 DEFICIENCY` | Cross | 30 → 48 | 1.84 → 2.69 | 10.3 → 45.8 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `VITAMIN D DECREASED` | Cross | 39 → 58 | 1.58 → 2.13 | 7.5 → 31.6 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `VITAMIN D DEFICIENCY` | Cross | 61 → 84 | 1.95 → 2.42 | 26.0 → 64.6 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `WALKING DISTANCE TEST ABNORMAL` | Cross | 2 → 5 | 1.64 → 3.86 | 0.1 → 7.1 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `WEIGHT FLUCTUATION` | Cross | 87 → 113 | 1.91 → 2.24 | 35.5 → 71.8 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `WHITE BLOOD CELLS URINE ABNORMAL` | Cross | 1 → 3 | 2.98 → 9.26 | 0.1 → 11.6 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `WHITE MATTER LESION` | Cross | 7 → 17 | 1.38 → 3.12 | 0.4 → 20.6 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `WOUND CELLULITIS` | Cross | 2 → 4 | 3.21 → 6.18 | 1.1 → 10.7 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `WRIST FRACTURE` | Cross | 71 → 84 | 1.97 → 2.08 | 31.2 → 43.6 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `ACCIDENT AT HOME` | Drop | 5 → 5 | 2.90 → 2.57 | 4.1 → 3.1 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ALCOHOL INTERACTION` | Drop | 8 → 8 | 2.44 → 2.16 | 5.1 → 3.7 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ALLERGY TO IMMUNOGLOBULIN THERAPY` | Drop | 4 → 4 | 3.41 → 3.03 | 4.2 → 3.3 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ANAEMIA VITAMIN B12 DEFICIENCY` | Drop | 6 → 6 | 2.85 → 2.53 | 5.1 → 3.8 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ANTICHOLINERGIC EFFECT` | Drop | 3 → 3 | 4.32 → 3.83 | 4.2 → 3.4 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `BALANOPOSTHITIS` | Drop | 8 → 8 | 2.23 → 1.98 | 4.0 → 2.8 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `BENDOPNOEA` | Drop | 7 → 7 | 2.46 → 2.18 | 4.4 → 3.2 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `BLOOD IRON INCREASED` | Drop | 12 → 12 | 2.10 → 1.86 | 5.5 → 3.7 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `BLOOD URINE` | Drop | 6 → 6 | 2.69 → 2.39 | 4.5 → 3.3 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `BODY TEMPERATURE FLUCTUATION` | Drop | 11 → 12 | 2.01 → 1.95 | 4.4 → 4.4 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `BRAIN HERNIATION` | Drop | 15 → 15 | 2.04 → 1.81 | 6.6 → 4.4 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `BRASH SYNDROME` | Drop | 34 → 34 | 2.03 → 1.80 | 15.9 → 10.8 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `COLITIS ISCHAEMIC` | Drop | 38 → 41 | 2.06 → 1.98 | 18.8 → 18.0 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `COMA SCALE ABNORMAL` | Drop | 13 → 13 | 2.03 → 1.80 | 5.5 → 3.7 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `DERMAL CYST` | Drop | 14 → 14 | 2.14 → 1.90 | 7.0 → 4.8 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `DIABETIC WOUND` | Drop | 3 → 3 | 4.64 → 4.12 | 4.7 → 3.8 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ENDOTHELIAL DYSFUNCTION` | Drop | 3 → 3 | 4.48 → 3.97 | 4.5 → 3.6 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ESCHERICHIA TEST POSITIVE` | Drop | 8 → 8 | 2.35 → 2.09 | 4.7 → 3.3 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `EYE INFARCTION` | Drop | 3 → 3 | 4.48 → 3.97 | 4.5 → 3.6 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `FACIAL PAIN` | Drop | 58 → 63 | 2.00 → 1.94 | 26.9 → 26.3 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `FACIAL PARESIS` | Drop | 13 → 13 | 2.04 → 1.81 | 5.6 → 3.7 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `FEAR OF DEATH` | Drop | 13 → 13 | 2.08 → 1.85 | 5.9 → 4.0 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `FUSARIUM INFECTION` | Drop | 8 → 8 | 2.42 → 2.15 | 5.0 → 3.6 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `GAMMA-GLUTAMYLTRANSFERASE ABNORMAL` | Drop | 7 → 7 | 2.44 → 2.16 | 4.3 → 3.1 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `GASTROINTESTINAL ANGIODYSPLASIA` | Drop | 4 → 4 | 3.34 → 2.96 | 4.1 → 3.1 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `GENITAL SWELLING` | Drop | 6 → 6 | 2.61 → 2.32 | 4.2 → 3.0 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `HAEMODYNAMIC INSTABILITY` | Drop | 54 → 56 | 2.08 → 1.92 | 27.8 → 22.5 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `HENOCH-SCHONLEIN PURPURA` | Drop | 18 → 18 | 2.15 → 1.91 | 9.5 → 6.6 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `HEPATIC LESION` | Drop | 28 → 31 | 2.02 → 1.99 | 12.7 → 13.6 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `HOMICIDAL IDEATION` | Drop | 9 → 9 | 2.22 → 1.97 | 4.6 → 3.2 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `HOUSEBOUND` | Drop | 4 → 4 | 3.48 → 3.09 | 4.4 → 3.4 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `HUMIDITY INTOLERANCE` | Drop | 5 → 5 | 2.90 → 2.57 | 4.1 → 3.1 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `HYPERGASTRINAEMIA` | Drop | 3 → 3 | 4.32 → 3.83 | 4.2 → 3.4 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `HYPERLACTACIDAEMIA` | Drop | 23 → 23 | 2.02 → 1.79 | 10.3 → 6.9 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `HYPOGLYCAEMIC COMA` | Drop | 14 → 14 | 2.10 → 1.87 | 6.7 → 4.5 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `HYPOPHOSPHATAEMIA` | Drop | 52 → 55 | 2.01 → 1.89 | 24.2 → 21.1 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `HYPOTONIA` | Drop | 49 → 49 | 2.11 → 1.87 | 26.0 → 18.0 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `IMPLANTABLE CARDIAC MONITOR INSERTION` | Drop | 3 → 3 | 4.48 → 3.97 | 4.5 → 3.6 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `IMPULSIVE BEHAVIOUR` | Drop | 17 → 17 | 2.10 → 1.86 | 8.3 → 5.7 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `INAPPROPRIATE ANTIDIURETIC HORMONE SECRETION` | Drop | 68 → 71 | 2.11 → 1.96 | 36.6 → 30.6 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `INTERVERTEBRAL DISC COMPRESSION` | Drop | 7 → 7 | 2.54 → 2.26 | 4.8 → 3.5 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ISCHAEMIA` | Drop | 19 → 20 | 2.12 → 1.99 | 9.7 → 8.4 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `LABELLED DRUG-FOOD INTERACTION ISSUE` | Drop | 5 → 5 | 3.16 → 2.81 | 5.0 → 3.8 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `LENTIGO` | Drop | 4 → 4 | 3.71 → 3.29 | 5.0 → 3.9 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `LIP DISCOLOURATION` | Drop | 9 → 9 | 2.35 → 2.08 | 5.4 → 3.8 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `LOSS OF LIBIDO` | Drop | 68 → 70 | 2.18 → 2.00 | 40.2 → 32.0 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `LOWER RESPIRATORY TRACT INFECTION VIRAL` | Drop | 6 → 6 | 2.72 → 2.42 | 4.6 → 3.4 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `MYCOPLASMA INFECTION` | Drop | 12 → 12 | 2.21 → 1.96 | 6.4 → 4.5 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `MYOCARDIAL CALCIFICATION` | Drop | 3 → 3 | 4.64 → 4.12 | 4.7 → 3.8 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `OCULAR ICTERUS` | Drop | 21 → 21 | 2.22 → 1.97 | 12.2 → 8.6 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `OESOPHAGEAL DILATATION` | Drop | 4 → 4 | 3.41 → 3.03 | 4.2 → 3.3 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `OPTIC DISC HAEMORRHAGE` | Drop | 4 → 4 | 3.41 → 3.03 | 4.2 → 3.3 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `ORAL CONTUSION` | Drop | 4 → 4 | 3.34 → 2.96 | 4.1 → 3.1 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `PERIPHERAL SENSORIMOTOR NEUROPATHY` | Drop | 12 → 12 | 2.03 → 1.80 | 5.0 → 3.3 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `POLYCYSTIC OVARIES` | Drop | 5 → 5 | 2.94 → 2.61 | 4.3 → 3.2 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `PRESSURE OF SPEECH` | Drop | 4 → 4 | 3.41 → 3.03 | 4.2 → 3.3 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `PYELONEPHRITIS` | Drop | 64 → 66 | 2.18 → 2.00 | 37.8 → 30.4 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `RASH MACULO-PAPULAR` | Drop | 154 → 168 | 2.03 → 1.98 | 76.1 → 76.0 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `RESPIRATION ABNORMAL` | Drop | 54 → 58 | 2.03 → 1.94 | 26.0 → 24.3 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `SCHIZOAFFECTIVE DISORDER` | Drop | 14 → 14 | 2.24 → 1.99 | 8.0 → 5.6 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `SENSORY OVERLOAD` | Drop | 5 → 5 | 3.03 → 2.69 | 4.5 → 3.4 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `SHOCK HAEMORRHAGIC` | Drop | 55 → 57 | 2.03 → 1.87 | 26.3 → 21.0 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `SITTING DISABILITY` | Drop | 9 → 9 | 2.36 → 2.10 | 5.4 → 3.9 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `SKIN TURGOR DECREASED` | Drop | 3 → 3 | 4.48 → 3.97 | 4.5 → 3.6 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `STICKY SKIN` | Drop | 4 → 4 | 3.41 → 3.03 | 4.2 → 3.3 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `SUDDEN HEARING LOSS` | Drop | 12 → 12 | 2.23 → 1.98 | 6.6 → 4.6 | $\text{PRR} \ge 2.0$ |
| **ATORVASTATIN** | `SUPERFICIAL SIDEROSIS OF CENTRAL NERVOUS SYSTEM` | Drop | 4 → 4 | 3.56 → 3.15 | 4.6 → 3.6 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `SYMPATHOMIMETIC EFFECT` | Drop | 6 → 6 | 2.64 → 2.34 | 4.3 → 3.1 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `TONSIL CANCER` | Drop | 5 → 5 | 3.07 → 2.72 | 4.7 → 3.6 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `URETHRAL OBSTRUCTION` | Drop | 4 → 4 | 3.56 → 3.15 | 4.6 → 3.6 | $\chi^2 \ge 4.0$ |
| **ATORVASTATIN** | `VERY LOW DENSITY LIPOPROTEIN INCREASED` | Drop | 4 → 4 | 3.71 → 3.29 | 5.0 → 3.9 | $\chi^2 \ge 4.0$ |
| **METFORMIN** | `ADRENAL MASS` | Cross | 5 → 6 | 2.19 → 2.59 | 2.1 → 4.1 | $\chi^2 \ge 4.0$ |
| **METFORMIN** | `ANDROGEN DEFICIENCY` | Cross | 1 → 3 | 1.78 → 5.61 | 0.0 → 6.4 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **METFORMIN** | `ANORECTAL POLYP` | Cross | 3 → 4 | 2.50 → 3.31 | 1.3 → 4.0 | $\chi^2 \ge 4.0$ |
| **METFORMIN** | `ARTHROPOD STING` | Cross | 7 → 10 | 1.46 → 2.06 | 0.6 → 4.2 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **METFORMIN** | `ATELECTASIS` | Cross | 36 → 40 | 1.86 → 2.02 | 12.9 → 18.8 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `BACTERIAL PYELONEPHRITIS` | Cross | 2 → 4 | 2.11 → 4.29 | 0.3 → 6.5 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **METFORMIN** | `BLADDER CANCER` | Cross | 37 → 40 | 1.91 → 2.01 | 14.5 → 18.7 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `BLOOD THYROID STIMULATING HORMONE INCREASED` | Cross | 41 → 43 | 1.97 → 2.02 | 18.0 → 20.3 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `BLOOD URINE` | Cross | 5 → 6 | 2.75 → 3.25 | 3.7 → 6.8 | $\chi^2 \ge 4.0$ |
| **METFORMIN** | `BRONCHIAL HYPERREACTIVITY` | Cross | 4 → 6 | 1.72 → 2.57 | 0.6 → 4.0 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **METFORMIN** | `CARBON DIOXIDE DECREASED` | Cross | 7 → 9 | 1.79 → 2.27 | 1.7 → 4.9 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **METFORMIN** | `COLORECTAL ADENOCARCINOMA` | Cross | 0 → 4 | 0.00 → 4.20 | 0.2 → 6.3 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **METFORMIN** | `CORONARY ANGIOPLASTY` | Cross | 1 → 3 | 1.72 → 5.41 | 0.0 → 6.1 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **METFORMIN** | `CYSTITIS NONINFECTIVE` | Cross | 9 → 11 | 1.68 → 2.02 | 1.8 → 4.5 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **METFORMIN** | `DEEP BRAIN STIMULATION` | Cross | 7 → 8 | 2.17 → 2.43 | 3.2 → 5.1 | $\chi^2 \ge 4.0$ |
| **METFORMIN** | `EPICONDYLITIS` | Cross | 6 → 7 | 2.43 → 2.78 | 3.5 → 5.9 | $\chi^2 \ge 4.0$ |
| **METFORMIN** | `FACIAL PAIN` | Cross | 45 → 51 | 1.91 → 2.12 | 17.8 → 27.7 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `FOREIGN BODY IN EYE` | Cross | 5 → 8 | 2.05 → 3.28 | 1.7 → 9.8 | $\chi^2 \ge 4.0$ |
| **METFORMIN** | `FRUSTRATION TOLERANCE DECREASED` | Cross | 54 → 56 | 1.99 → 2.01 | 24.6 → 26.5 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `GASTROINTESTINAL ANGIODYSPLASIA` | Cross | 3 → 4 | 3.04 → 4.04 | 2.2 → 5.8 | $\chi^2 \ge 4.0$ |
| **METFORMIN** | `GASTROINTESTINAL FUNGAL INFECTION` | Cross | 4 → 5 | 2.69 → 3.32 | 2.6 → 5.5 | $\chi^2 \ge 4.0$ |
| **METFORMIN** | `ICHTHYOSIS` | Cross | 2 → 4 | 2.11 → 4.29 | 0.3 → 6.5 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **METFORMIN** | `INTERVERTEBRAL DISC PROTRUSION` | Cross | 92 → 95 | 1.99 → 2.01 | 42.9 → 45.4 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `LIP DISORDER` | Cross | 7 → 8 | 2.13 → 2.39 | 3.0 → 4.9 | $\chi^2 \ge 4.0$ |
| **METFORMIN** | `LYMPHADENITIS` | Cross | 14 → 15 | 1.96 → 2.05 | 5.4 → 6.8 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `LYMPHOEDEMA` | Cross | 39 → 50 | 1.93 → 2.44 | 16.1 → 39.3 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `MALABSORPTION` | Cross | 22 → 32 | 1.63 → 2.35 | 4.6 → 22.4 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `MUSCLE ATROPHY` | Cross | 81 → 85 | 1.99 → 2.04 | 37.5 → 42.3 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `NEOVASCULARISATION` | Cross | 1 → 4 | 1.08 → 4.48 | 0.0 → 7.0 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **METFORMIN** | `NEUTROPHIL COUNT INCREASED` | Cross | 46 → 50 | 1.96 → 2.08 | 19.8 → 25.9 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `OBSTRUCTIVE PANCREATITIS` | Cross | 9 → 10 | 2.07 → 2.25 | 3.8 → 5.5 | $\chi^2 \ge 4.0$ |
| **METFORMIN** | `OPTIC NERVE INJURY` | Cross | 5 → 8 | 1.68 → 2.67 | 0.7 → 6.4 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **METFORMIN** | `OSTEOPOROSIS` | Cross | 192 → 205 | 1.99 → 2.08 | 90.5 → 109.3 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `PERIARTHRITIS` | Cross | 10 → 12 | 1.84 → 2.17 | 2.9 → 6.2 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **METFORMIN** | `PERIPHERAL ARTERIAL OCCLUSIVE DISEASE` | Cross | 15 → 19 | 1.69 → 2.11 | 3.4 → 9.6 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **METFORMIN** | `PERIPHERAL ARTERY STENOSIS` | Cross | 2 → 3 | 2.80 → 4.20 | 0.8 → 4.1 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **METFORMIN** | `PHYSICAL DISABILITY` | Cross | 14 → 18 | 1.79 → 2.27 | 4.0 → 11.0 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **METFORMIN** | `PRECANCEROUS LESION OF DIGESTIVE TRACT` | Cross | 3 → 4 | 2.99 → 3.96 | 2.1 → 5.6 | $\chi^2 \ge 4.0$ |
| **METFORMIN** | `RASH VESICULAR` | Cross | 32 → 35 | 1.90 → 2.03 | 12.2 → 16.6 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `RED CELL DISTRIBUTION WIDTH INCREASED` | Cross | 26 → 29 | 1.97 → 2.15 | 11.0 → 16.0 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `SEPTIC SHOCK` | Cross | 251 → 266 | 1.99 → 2.06 | 118.8 → 139.4 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `SUDDEN DEATH` | Cross | 29 → 31 | 2.00 → 2.09 | 13.0 → 15.8 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `TACHYPNOEA` | Cross | 74 → 77 | 1.97 → 2.01 | 33.3 → 36.4 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `THYROID ATROPHY` | Cross | 0 → 10 | 0.00 → 45.87 | 0.0 → 206.5 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **METFORMIN** | `TONGUE OEDEMA` | Cross | 15 → 17 | 1.92 → 2.13 | 5.5 → 8.7 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `TROPONIN T INCREASED` | Cross | 7 → 8 | 2.29 → 2.57 | 3.7 → 5.9 | $\chi^2 \ge 4.0$ |
| **METFORMIN** | `URINARY TRACT INFECTION` | Cross | 981 → 1,011 | 2.00 → 2.01 | 475.3 → 498.1 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `USE OF ACCESSORY RESPIRATORY MUSCLES` | Cross | 3 → 5 | 2.54 → 4.28 | 1.4 → 8.7 | $\chi^2 \ge 4.0$ |
| **METFORMIN** | `VASCULAR GRAFT` | Cross | 9 → 10 | 2.02 → 2.20 | 3.5 → 5.2 | $\chi^2 \ge 4.0$ |
| **METFORMIN** | `VERTEBROBASILAR STROKE` | Cross | 2 → 3 | 4.50 → 6.88 | 2.3 → 8.5 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **METFORMIN** | `BLOOD PRESSURE DECREASED` | Drop | 321 → 328 | 2.01 → 2.00 | 155.3 → 157.2 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `BULLOUS HAEMORRHAGIC DERMATOSIS` | Drop | 5 → 5 | 2.87 → 2.80 | 4.1 → 3.9 | $\chi^2 \ge 4.0$ |
| **METFORMIN** | `CARCINOID SYNDROME` | Drop | 3 → 3 | 4.20 → 4.09 | 4.1 → 3.9 | $\chi^2 \ge 4.0$ |
| **METFORMIN** | `CARDIO-RESPIRATORY ARREST` | Drop | 132 → 132 | 2.01 → 1.96 | 64.0 → 59.3 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `DIFFERENTIAL WHITE BLOOD CELL COUNT ABNORMAL` | Drop | 10 → 10 | 2.03 → 1.98 | 4.1 → 3.7 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **METFORMIN** | `EMPHYSEMA` | Drop | 41 → 41 | 2.03 → 1.98 | 19.7 → 18.2 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `EXCESSIVE CERUMEN PRODUCTION` | Drop | 6 → 6 | 2.59 → 2.52 | 4.1 → 3.9 | $\chi^2 \ge 4.0$ |
| **METFORMIN** | `GOITRE` | Drop | 15 → 15 | 2.05 → 2.00 | 6.7 → 6.2 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `HEPATIC PAIN` | Drop | 21 → 21 | 2.02 → 1.97 | 9.5 → 8.8 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `HYDROURETER` | Drop | 4 → 4 | 3.34 → 3.25 | 4.1 → 3.9 | $\chi^2 \ge 4.0$ |
| **METFORMIN** | `HYPOPHYSITIS` | Drop | 12 → 12 | 2.05 → 2.00 | 5.2 → 4.8 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `LICHEN PLANUS` | Drop | 14 → 14 | 2.05 → 2.00 | 6.2 → 5.7 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `LUMBAR SPINAL STENOSIS` | Drop | 11 → 11 | 2.05 → 2.00 | 4.7 → 4.3 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `NAIL PSORIASIS` | Drop | 5 → 5 | 2.91 → 2.83 | 4.2 → 4.0 | $\chi^2 \ge 4.0$ |
| **METFORMIN** | `PAIN IN JAW` | Drop | 107 → 108 | 2.02 → 1.99 | 52.4 → 50.4 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `PRODUCT NAME CONFUSION` | Drop | 6 → 6 | 2.61 → 2.54 | 4.2 → 4.0 | $\chi^2 \ge 4.0$ |
| **METFORMIN** | `PROTEUS INFECTION` | Drop | 5 → 5 | 2.91 → 2.83 | 4.2 → 4.0 | $\chi^2 \ge 4.0$ |
| **METFORMIN** | `PULMONARY ARTERIAL PRESSURE ABNORMAL` | Drop | 3 → 3 | 4.20 → 4.09 | 4.1 → 3.9 | $\chi^2 \ge 4.0$ |
| **METFORMIN** | `REGURGITATION` | Drop | 18 → 18 | 2.03 → 1.98 | 8.1 → 7.5 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `RESPIRATORY DISTRESS` | Drop | 114 → 116 | 2.00 → 1.99 | 54.2 → 53.9 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `STENOSIS` | Drop | 18 → 18 | 2.01 → 1.96 | 7.9 → 7.2 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `THERAPEUTIC PRODUCT EFFECT INCOMPLETE` | Drop | 607 → 612 | 2.03 → 1.99 | 304.1 → 291.5 | $\text{PRR} \ge 2.0$ |
| **METFORMIN** | `UPPER-AIRWAY COUGH SYNDROME` | Drop | 53 → 53 | 2.03 → 1.97 | 25.5 → 23.6 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `ABSCESS RUPTURE` | Cross | 2 → 4 | 3.00 → 5.35 | 1.0 → 9.5 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `ACUTE CHEST SYNDROME` | Cross | 3 → 6 | 1.64 → 2.89 | 0.2 → 5.4 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `ADENOIDAL DISORDER` | Cross | 2 → 4 | 4.68 → 8.50 | 2.5 → 17.6 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `ADENOMYOSIS` | Cross | 3 → 4 | 2.97 → 3.45 | 2.1 → 4.5 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `AEROPHAGIA` | Cross | 1 → 3 | 3.31 → 9.21 | 0.1 → 13.0 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `ALLERGY TO CHEMICALS` | Cross | 6 → 7 | 2.44 → 2.47 | 3.7 → 4.6 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `ALLERGY TO METALS` | Cross | 3 → 5 | 2.50 → 3.67 | 1.4 → 6.9 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `AMNIOTIC CAVITY INFECTION` | Cross | 2 → 4 | 2.16 → 3.81 | 0.3 → 5.4 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `ARTHROPOD STING` | Cross | 6 → 9 | 2.31 → 3.03 | 3.2 → 9.9 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `ASPARTATE AMINOTRANSFERASE DECREASED` | Cross | 3 → 4 | 3.55 → 4.14 | 3.1 → 6.3 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `ATTENTION DEFICIT HYPERACTIVITY DISORDER` | Cross | 24 → 34 | 1.90 → 2.35 | 9.2 → 24.2 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `AUTOIMMUNE THYROIDITIS` | Cross | 12 → 19 | 1.81 → 2.51 | 3.5 → 15.3 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `AUTONOMIC NERVOUS SYSTEM IMBALANCE` | Cross | 9 → 12 | 1.91 → 2.22 | 3.0 → 6.7 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `BENIGN OVARIAN TUMOUR` | Cross | 2 → 8 | 6.62 → 28.82 | 4.4 → 139.2 | $a \ge 3$ |
| **IBUPROFEN** | `BILIARY TRACT DISORDER` | Cross | 4 → 5 | 2.86 → 3.12 | 3.1 → 5.0 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `BLADDER DISORDER` | Cross | 23 → 28 | 1.96 → 2.07 | 9.6 → 14.0 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `BLOOD BILIRUBIN DECREASED` | Cross | 4 → 5 | 3.20 → 3.48 | 3.9 → 6.2 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `BLOOD CHOLESTEROL ABNORMAL` | Cross | 11 → 16 | 1.59 → 2.01 | 1.8 → 7.0 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `BLOOD IMMUNOGLOBULIN G DECREASED` | Cross | 17 → 24 | 2.00 → 2.46 | 7.4 → 18.8 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `BODY TINEA` | Cross | 3 → 4 | 3.60 → 4.20 | 3.2 → 6.4 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `BONE CONTUSION` | Cross | 5 → 8 | 2.45 → 3.43 | 2.9 → 11.0 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `BONE DENSITY ABNORMAL` | Cross | 6 → 9 | 1.99 → 2.61 | 2.0 → 7.1 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `BRAIN NEOPLASM BENIGN` | Cross | 1 → 4 | 1.45 → 5.26 | 0.0 → 9.2 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `BREATH SOUNDS ABNORMAL` | Cross | 18 → 23 | 1.88 → 2.09 | 6.5 → 11.7 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `CARDIAC ABLATION` | Cross | 7 → 8 | 2.32 → 2.30 | 3.9 → 4.5 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `CARPAL TUNNEL DECOMPRESSION` | Cross | 2 → 5 | 1.30 → 2.86 | 0.0 → 4.2 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `CATARACT NUCLEAR` | Cross | 4 → 6 | 3.07 → 4.04 | 3.6 → 10.3 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `CATHETER SITE VESICLES` | Cross | 2 → 4 | 6.62 → 12.28 | 4.4 → 26.8 | $a \ge 3$ |
| **IBUPROFEN** | `CEREBRAL SMALL VESSEL ISCHAEMIC DISEASE` | Cross | 4 → 5 | 3.23 → 3.51 | 4.0 → 6.3 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `CHLAMYDIAL INFECTION` | Cross | 3 → 5 | 3.60 → 5.31 | 3.2 → 12.6 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `CHONDROMA` | Cross | 0 → 3 | 0.00 → 580.04 | 0.0 → 171.8 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `CHRONIC RESPIRATORY DISEASE` | Cross | 3 → 4 | 3.79 → 4.42 | 3.5 → 7.0 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `CHRONIC SPONTANEOUS URTICARIA` | Cross | 8 → 10 | 1.92 → 2.08 | 2.6 → 4.5 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `COLD SWEAT` | Cross | 40 → 50 | 1.90 → 2.07 | 16.0 → 25.8 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `COLORECTAL ADENOMA` | Cross | 3 → 8 | 1.79 → 4.25 | 0.4 → 15.9 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `COSTOCHONDRITIS` | Cross | 4 → 7 | 2.28 → 3.52 | 1.7 → 9.8 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `COUGH VARIANT ASTHMA` | Cross | 2 → 4 | 6.00 → 11.05 | 3.8 → 23.9 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `COVID-19` | Cross | 845 → 1,070 | 1.86 → 2.05 | 335.2 → 566.8 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `CYSTITIS` | Cross | 72 → 90 | 1.85 → 2.00 | 26.7 → 43.3 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `CYSTITIS INTERSTITIAL` | Cross | 4 → 7 | 1.99 → 3.05 | 1.1 → 7.4 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `DENGUE FEVER` | Cross | 4 → 14 | 0.67 → 2.05 | 0.4 → 6.4 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `DENTAL DISCOMFORT` | Cross | 4 → 5 | 2.70 → 2.94 | 2.7 → 4.4 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `DENTAL IMPLANTATION` | Cross | 4 → 8 | 1.48 → 2.59 | 0.2 → 6.1 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `DENTAL OPERATION` | Cross | 5 → 12 | 0.96 → 2.02 | 0.0 → 5.0 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `DIAPHRAGMATIC PARALYSIS` | Cross | 3 → 4 | 3.69 → 4.30 | 3.4 → 6.7 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `DIZZINESS POSTURAL` | Cross | 33 → 42 | 1.85 → 2.04 | 11.7 → 20.7 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `DYSARTHRIA` | Cross | 84 → 99 | 1.97 → 2.01 | 38.5 → 48.4 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `DYSLEXIA` | Cross | 4 → 6 | 2.31 → 3.03 | 1.8 → 6.0 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `DYSMENORRHOEA` | Cross | 22 → 41 | 1.46 → 2.39 | 2.7 → 30.8 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `EAR CONGESTION` | Cross | 7 → 10 | 1.68 → 2.08 | 1.3 → 4.5 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `EAR DISORDER` | Cross | 11 → 18 | 1.81 → 2.59 | 3.1 → 15.4 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `ECZEMA ASTEATOTIC` | Cross | 0 → 4 | 0.00 → 3.72 | 0.2 → 5.2 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `EHLERS-DANLOS SYNDROME` | Cross | 4 → 5 | 2.67 → 2.90 | 2.6 → 4.3 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `ENCEPHALOMALACIA` | Cross | 0 → 4 | 0.00 → 4.60 | 0.1 → 7.5 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `ENDOMETRIAL DISORDER` | Cross | 2 → 3 | 6.85 → 9.21 | 4.6 → 13.0 | $a \ge 3$ |
| **IBUPROFEN** | `EXCESSIVE CERUMEN PRODUCTION` | Cross | 3 → 5 | 2.34 → 3.42 | 1.1 → 6.1 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `EXCESSIVE EYE BLINKING` | Cross | 3 → 7 | 2.00 → 4.14 | 0.6 → 13.0 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `EYE PAIN` | Cross | 191 → 252 | 1.84 → 2.11 | 71.5 → 143.2 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `EYELID PAIN` | Cross | 11 → 16 | 1.82 → 2.31 | 3.2 → 10.3 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `FACIAL PAIN` | Cross | 23 → 35 | 1.77 → 2.35 | 6.9 → 25.2 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `FACTOR V LEIDEN MUTATION` | Cross | 0 → 4 | 0.00 → 5.26 | 0.1 → 9.2 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `FAILURE OF CHILD RESISTANT PRODUCT CLOSURE` | Cross | 1 → 5 | 1.65 → 7.67 | 0.0 → 20.7 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `FAT TISSUE INCREASED` | Cross | 4 → 8 | 1.61 → 2.82 | 0.4 → 7.4 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `FIBULA FRACTURE` | Cross | 6 → 9 | 1.81 → 2.37 | 1.4 → 5.6 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `FISTULA DISCHARGE` | Cross | 3 → 6 | 1.56 → 2.73 | 0.2 → 4.8 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `FOCAL DYSCOGNITIVE SEIZURES` | Cross | 2 → 5 | 1.31 → 2.90 | 0.0 → 4.3 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `FUNGAL FOOT INFECTION` | Cross | 5 → 6 | 2.45 → 2.55 | 2.9 → 4.1 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `FUNGAL INFECTION` | Cross | 89 → 120 | 1.85 → 2.17 | 33.4 → 72.7 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `GENITAL HERPES SIMPLEX` | Cross | 1 → 6 | 2.28 → 13.44 | 0.0 → 49.1 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `GENITAL SWELLING` | Cross | 2 → 4 | 1.92 → 3.38 | 0.2 → 4.3 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `HALLUCINATIONS, MIXED` | Cross | 14 → 17 | 1.91 → 2.01 | 5.1 → 7.5 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `HALO VISION` | Cross | 2 → 4 | 1.85 → 3.25 | 0.2 → 4.0 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `HAND-EYE COORDINATION IMPAIRED` | Cross | 2 → 3 | 17.45 → 24.86 | 14.1 → 35.9 | $a \ge 3$ |
| **IBUPROFEN** | `HEALTH ASSESSMENT QUESTIONNAIRE SCORE INCREASED` | Cross | 2 → 4 | 3.76 → 6.76 | 1.7 → 13.2 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `HEART RATE ABNORMAL` | Cross | 16 → 24 | 1.76 → 2.30 | 4.4 → 15.9 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `HEART RATE DECREASED` | Cross | 69 → 116 | 1.43 → 2.09 | 8.3 → 63.6 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `HYPERPYREXIA` | Cross | 18 → 24 | 1.93 → 2.24 | 7.1 → 14.9 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `ILLNESS ANXIETY DISORDER` | Cross | 2 → 3 | 5.82 → 7.77 | 3.6 → 10.5 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `IMMATURE GRANULOCYTE PERCENTAGE INCREASED` | Cross | 1 → 4 | 4.17 → 16.57 | 0.3 → 36.5 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `INCISION SITE COMPLICATION` | Cross | 1 → 3 | 1.96 → 5.29 | 0.0 → 6.2 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `INCISION SITE IMPAIRED HEALING` | Cross | 4 → 5 | 3.02 → 3.29 | 3.5 → 5.6 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `INCONTINENCE` | Cross | 24 → 33 | 1.86 → 2.23 | 8.6 → 20.6 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `INCREASED NEED FOR SLEEP` | Cross | 1 → 7 | 0.68 → 4.27 | 0.0 → 13.6 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `INFECTED BITE` | Cross | 5 → 6 | 2.59 → 2.70 | 3.3 → 4.7 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `INTERTRIGO` | Cross | 3 → 4 | 2.88 → 3.35 | 2.0 → 4.3 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `INTRAOSSEOUS ACCESS PLACEMENT` | Cross | 0 → 3 | 0.00 → 248.59 | 0.0 → 127.6 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `JAW DISORDER` | Cross | 6 → 9 | 1.60 → 2.09 | 0.8 → 4.0 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `LARYNGEAL DYSKINESIA` | Cross | 2 → 4 | 2.91 → 5.18 | 0.9 → 9.0 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `LICHEN SCLEROSUS` | Cross | 2 → 6 | 1.52 → 4.08 | 0.0 → 10.5 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `LIP INJURY` | Cross | 5 → 7 | 2.19 → 2.67 | 2.1 → 5.6 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `LOCALISED OEDEMA` | Cross | 10 → 15 | 1.95 → 2.55 | 3.6 → 12.3 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `LUMBOSACRAL RADICULOPATHY` | Cross | 1 → 3 | 3.31 → 9.21 | 0.1 → 13.0 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `MAJOR DEPRESSION` | Cross | 19 → 29 | 1.71 → 2.27 | 4.8 → 18.9 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `MALABSORPTION` | Cross | 13 → 17 | 1.77 → 2.01 | 3.5 → 7.4 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `MATERNAL EXPOSURE BEFORE PREGNANCY` | Cross | 30 → 43 | 1.90 → 2.37 | 11.7 → 31.9 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `MEDICAL DEVICE SITE HAEMORRHAGE` | Cross | 1 → 3 | 1.96 → 5.29 | 0.0 → 6.2 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `MENINGITIS` | Cross | 19 → 26 | 1.98 → 2.36 | 8.1 → 18.5 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `MIDDLE INSOMNIA` | Cross | 51 → 78 | 1.56 → 2.07 | 9.5 → 41.3 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `MILIARIA` | Cross | 5 → 8 | 1.62 → 2.25 | 0.6 → 4.3 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `MOTOR DEVELOPMENTAL DELAY` | Cross | 2 → 4 | 2.43 → 4.30 | 0.5 → 6.7 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `MUSCLE FATIGUE` | Cross | 12 → 17 | 1.75 → 2.16 | 3.1 → 9.2 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `MYALGIA` | Cross | 415 → 525 | 1.91 → 2.10 | 177.7 → 296.4 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `MYCOTIC ALLERGY` | Cross | 6 → 8 | 2.22 → 2.58 | 2.8 → 6.0 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `NARCOLEPSY` | Cross | 6 → 7 | 2.51 → 2.54 | 3.9 → 4.9 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `NEUROGENIC BOWEL` | Cross | 1 → 3 | 2.23 → 6.06 | 0.0 → 7.5 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `NIKOLSKY'S SIGN TEST` | Cross | 2 → 5 | 63.97 → 911.48 | 41.1 → 334.7 | $a \ge 3$ |
| **IBUPROFEN** | `NOCTURIA` | Cross | 44 → 53 | 1.97 → 2.06 | 19.6 → 27.0 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `NONREASSURING FOETAL HEART RATE PATTERN` | Cross | 0 → 3 | 0.00 → 6.72 | 0.0 → 8.7 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `OPHTHALMIC MIGRAINE` | Cross | 6 → 10 | 1.80 → 2.63 | 1.4 → 8.3 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `ORAL PUSTULE` | Cross | 0 → 3 | 0.00 → 11.30 | 0.0 → 16.5 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `ORAL SURGERY` | Cross | 7 → 10 | 1.79 → 2.22 | 1.7 → 5.4 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `OROPHARYNGEAL CANDIDIASIS` | Cross | 4 → 5 | 2.98 → 3.24 | 3.3 → 5.4 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `OVARIAN CYST RUPTURED` | Cross | 4 → 5 | 2.59 → 2.82 | 2.4 → 4.0 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `OXYGEN SATURATION ABNORMAL` | Cross | 10 → 14 | 1.70 → 2.07 | 2.2 → 6.5 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `PAEDIATRIC ACUTE-ONSET NEUROPSYCHIATRIC SYNDROME` | Cross | 2 → 3 | 8.72 → 11.84 | 6.4 → 17.3 | $a \ge 3$ |
| **IBUPROFEN** | `PARASOMNIA` | Cross | 3 → 4 | 3.10 → 3.60 | 2.3 → 4.9 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `PEAK EXPIRATORY FLOW RATE DECREASED` | Cross | 0 → 4 | 0.00 → 13.81 | 0.0 → 30.4 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `PERIODONTITIS` | Cross | 6 → 7 | 2.42 → 2.45 | 3.6 → 4.5 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `PERIORBITAL SWELLING` | Cross | 37 → 50 | 1.96 → 2.31 | 16.2 → 34.8 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `PERIPHERAL ARTERY THROMBOSIS` | Cross | 1 → 6 | 0.49 → 2.63 | 0.1 → 4.4 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `PERIPHERAL COLDNESS` | Cross | 32 → 41 | 1.84 → 2.05 | 11.3 → 20.5 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `PHOTOPHOBIA` | Cross | 58 → 78 | 1.87 → 2.19 | 22.3 → 48.1 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `PIGMENTATION DISORDER` | Cross | 14 → 32 | 1.49 → 3.00 | 1.7 → 39.2 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `PITUITARY-DEPENDENT CUSHING'S SYNDROME` | Cross | 1 → 3 | 2.28 → 6.21 | 0.0 → 7.8 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `PNEUMONIA MYCOPLASMAL` | Cross | 4 → 5 | 2.93 → 3.19 | 3.2 → 5.3 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `POST CONCUSSION SYNDROME` | Cross | 1 → 3 | 2.74 → 7.53 | 0.0 → 10.1 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `POST PROCEDURAL ERYTHEMA` | Cross | 1 → 3 | 2.82 → 7.77 | 0.1 → 10.5 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `POST-TRAUMATIC NECK SYNDROME` | Cross | 3 → 5 | 2.53 → 3.70 | 1.4 → 7.0 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `PRE-ECLAMPSIA` | Cross | 9 → 15 | 1.46 → 2.12 | 0.9 → 7.6 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `PRECANCEROUS SKIN LESION` | Cross | 3 → 6 | 1.47 → 2.58 | 0.1 → 4.2 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `PRESYNCOPE` | Cross | 59 → 77 | 1.78 → 2.02 | 19.1 → 37.7 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `PTERYGIUM` | Cross | 0 → 3 | 0.00 → 4.36 | 0.0 → 4.5 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `PULMONARY ARTERIAL PRESSURE ABNORMAL` | Cross | 2 → 3 | 5.05 → 6.72 | 2.9 → 8.7 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `PULMONARY PAIN` | Cross | 12 → 16 | 1.93 → 2.23 | 4.4 → 9.4 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `PURULENCE` | Cross | 3 → 5 | 2.36 → 3.45 | 1.1 → 6.2 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `PURULENT DISCHARGE` | Cross | 9 → 11 | 1.99 → 2.11 | 3.4 → 5.2 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `RADIUS FRACTURE` | Cross | 3 → 7 | 1.26 → 2.58 | 0.0 → 5.1 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `RESORPTION BONE INCREASED` | Cross | 2 → 3 | 3.84 → 5.07 | 1.7 → 5.8 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `RESPIRATORY TRACT INFECTION VIRAL` | Cross | 10 → 14 | 1.72 → 2.10 | 2.3 → 6.8 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `RHEUMATOID ARTHRITIS-ASSOCIATED INTERSTITIAL LUNG DISEASE` | Cross | 1 → 5 | 2.34 → 11.20 | 0.0 → 32.3 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `RHINITIS ALLERGIC` | Cross | 14 → 18 | 1.80 → 2.01 | 4.1 → 7.9 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `SCAPULA FRACTURE` | Cross | 0 → 3 | 0.00 → 4.60 | 0.0 → 4.9 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `SCLERITIS` | Cross | 6 → 8 | 1.91 → 2.22 | 1.7 → 4.1 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `SEBORRHOEIC DERMATITIS` | Cross | 5 → 9 | 1.65 → 2.61 | 0.7 → 7.1 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `SENSITIVITY TO WEATHER CHANGE` | Cross | 22 → 28 | 1.86 → 2.05 | 7.7 → 13.7 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `SINUS OPERATION` | Cross | 8 → 10 | 1.97 → 2.14 | 2.8 → 4.8 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `SKIN INJURY` | Cross | 9 → 11 | 2.05 → 2.17 | 3.7 → 5.7 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `SKIN PAPILLOMA` | Cross | 10 → 27 | 1.82 → 4.39 | 2.9 → 63.8 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `SLUGGISHNESS` | Cross | 24 → 29 | 1.95 → 2.04 | 9.9 → 14.0 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `SMOKE SENSITIVITY` | Cross | 3 → 4 | 3.23 → 3.77 | 2.6 → 5.3 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `SPONTANEOUS PENILE ERECTION` | Cross | 2 → 3 | 4.68 → 6.21 | 2.5 → 7.8 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `SPONTANEOUS RUPTURE OF MEMBRANES` | Cross | 0 → 5 | 0.00 → 41.43 | 0.0 → 105.7 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `STERNAL FRACTURE` | Cross | 4 → 6 | 2.03 → 2.66 | 1.2 → 4.5 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `STOMA SITE HAEMORRHAGE` | Cross | 7 → 8 | 2.28 → 2.26 | 3.8 → 4.3 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `SUSPECTED COVID-19` | Cross | 9 → 11 | 1.99 → 2.11 | 3.4 → 5.3 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `SUTURE INSERTION` | Cross | 3 → 4 | 2.91 → 3.38 | 2.0 → 4.3 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `SWELLING FACE` | Cross | 160 → 190 | 1.96 → 2.02 | 73.3 → 94.9 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `TATTOO ASSOCIATED SKIN REACTION` | Cross | 2 → 3 | 19.19 → 27.62 | 15.5 → 39.3 | $a \ge 3$ |
| **IBUPROFEN** | `TENDON CALCIFICATION` | Cross | 2 → 3 | 7.38 → 9.94 | 5.1 → 14.2 | $a \ge 3$ |
| **IBUPROFEN** | `THYROXINE INCREASED` | Cross | 3 → 4 | 3.23 → 3.77 | 2.6 → 5.3 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `TONSILLAR HYPERTROPHY` | Cross | 5 → 7 | 2.01 → 2.45 | 1.6 → 4.5 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `TOOTH DISORDER` | Cross | 50 → 60 | 1.97 → 2.05 | 22.4 → 30.4 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `TOOTH IMPACTED` | Cross | 1 → 3 | 1.78 → 4.78 | 0.0 → 5.2 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `TOURETTE'S DISORDER` | Cross | 3 → 4 | 3.13 → 3.64 | 2.4 → 5.0 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `TRACHEOSTOMY INFECTION` | Cross | 2 → 3 | 11.29 → 15.54 | 8.8 → 23.1 | $a \ge 3$ |
| **IBUPROFEN** | `TWIN PREGNANCY` | Cross | 0 → 3 | 0.00 → 4.52 | 0.0 → 4.8 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `UPPER RESPIRATORY TRACT INFECTION BACTERIAL` | Cross | 2 → 3 | 4.80 → 6.37 | 2.7 → 8.1 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `URTICARIA CHRONIC` | Cross | 3 → 8 | 2.23 → 5.35 | 1.0 → 22.6 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `UTERINE CONTRACTIONS ABNORMAL` | Cross | 1 → 3 | 4.80 → 13.81 | 0.4 → 20.5 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `UTERINE SPASM` | Cross | 4 → 5 | 2.95 → 3.21 | 3.3 → 5.3 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `VACCINATION SITE PAIN` | Cross | 3 → 5 | 2.32 → 3.40 | 1.1 → 6.0 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `VAGINAL DISORDER` | Cross | 0 → 4 | 0.00 → 4.14 | 0.2 → 6.3 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `VASCULAR ACCESS SITE DISCOMFORT` | Cross | 0 → 4 | 0.00 → 30.13 | 0.0 → 62.4 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `VEIN RUPTURE` | Cross | 7 → 9 | 1.92 → 2.14 | 2.2 → 4.3 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `VERTEBROPLASTY` | Cross | 2 → 3 | 3.49 → 4.60 | 1.4 → 4.9 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `VIRAL INFECTION` | Cross | 114 → 141 | 1.89 → 2.03 | 46.4 → 71.3 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `WEIGHT BEARING DIFFICULTY` | Cross | 6 → 10 | 1.80 → 2.62 | 1.4 → 8.2 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `WISDOM TEETH REMOVAL` | Cross | 5 → 8 | 1.85 → 2.59 | 1.2 → 6.1 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `ABDOMINAL LYMPHADENOPATHY` | Drop | 5 → 5 | 2.98 → 2.57 | 4.6 → 3.2 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `ABDOMINOPLASTY` | Drop | 3 → 3 | 4.11 → 3.55 | 4.1 → 3.1 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `ACINETOBACTER INFECTION` | Drop | 5 → 5 | 2.86 → 2.47 | 4.2 → 2.9 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `ADHESION` | Drop | 4 → 4 | 3.43 → 2.96 | 4.5 → 3.3 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `ADRENAL NEOPLASM` | Drop | 3 → 3 | 4.50 → 3.88 | 4.8 → 3.7 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `ANISOCORIA` | Drop | 5 → 5 | 3.08 → 2.66 | 4.9 → 3.5 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `APPENDICECTOMY` | Drop | 11 → 12 | 2.10 → 1.98 | 5.2 → 4.8 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `AUTOIMMUNE HEPATITIS` | Drop | 24 → 24 | 2.19 → 1.89 | 14.0 → 9.0 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `BACTERIAL SEPSIS` | Drop | 11 → 11 | 2.14 → 1.85 | 5.4 → 3.4 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `BLOOD LACTIC ACID INCREASED` | Drop | 18 → 19 | 2.01 → 1.84 | 8.0 → 6.3 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `BRAIN HERNIATION` | Drop | 8 → 8 | 2.44 → 2.11 | 5.3 → 3.5 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `BREAST CYST` | Drop | 5 → 5 | 2.98 → 2.57 | 4.6 → 3.2 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `BREAST DISCOMFORT` | Drop | 4 → 4 | 3.37 → 2.91 | 4.3 → 3.2 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `CARDIOMYOPATHY` | Drop | 38 → 39 | 2.24 → 1.99 | 24.4 → 17.8 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `CARDIOVASCULAR INSUFFICIENCY` | Drop | 5 → 5 | 2.98 → 2.57 | 4.6 → 3.2 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `CENTRAL VENOUS CATHETERISATION` | Drop | 9 → 9 | 2.14 → 1.85 | 4.3 → 2.7 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `CEREBRAL DISORDER` | Drop | 27 → 29 | 2.15 → 2.00 | 15.1 → 13.1 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `CHOLECYSTITIS` | Drop | 32 → 33 | 2.02 → 1.80 | 15.2 → 10.7 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `CORONAVIRUS INFECTION` | Drop | 26 → 26 | 2.11 → 1.82 | 13.7 → 8.6 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `CYANOSIS` | Drop | 38 → 41 | 2.11 → 1.97 | 20.7 → 18.2 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `DEATH OF RELATIVE` | Drop | 3 → 3 | 4.43 → 3.82 | 4.7 → 3.6 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `DEPRESSIVE SYMPTOM` | Drop | 15 → 15 | 2.26 → 1.95 | 9.1 → 5.9 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `DOCUMENTED HYPERSENSITIVITY TO ADMINISTERED PRODUCT` | Drop | 4 → 4 | 3.28 → 2.83 | 4.1 → 3.0 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `DUODENOGASTRIC REFLUX` | Drop | 3 → 3 | 4.23 → 3.66 | 4.3 → 3.3 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `ECCHYMOSIS` | Drop | 14 → 16 | 2.01 → 1.99 | 6.0 → 6.8 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `ENCEPHALOPATHY` | Drop | 65 → 65 | 2.05 → 1.77 | 33.1 → 20.5 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `END STAGE RENAL DISEASE` | Drop | 23 → 23 | 2.06 → 1.78 | 11.3 → 7.0 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `ENDOMETRITIS` | Drop | 3 → 3 | 4.50 → 3.88 | 4.8 → 3.7 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `FOETAL EXPOSURE DURING PREGNANCY` | Drop | 210 → 231 | 2.10 → 2.00 | 117.1 → 111.4 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `GASTROINTESTINAL TRACT IRRITATION` | Drop | 5 → 5 | 3.24 → 2.80 | 5.5 → 4.0 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `GINGIVAL ULCERATION` | Drop | 3 → 3 | 4.50 → 3.88 | 4.8 → 3.7 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `HILAR LYMPHADENOPATHY` | Drop | 5 → 5 | 3.10 → 2.67 | 5.0 → 3.6 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `HYPERKALAEMIA` | Drop | 102 → 102 | 2.27 → 1.96 | 69.5 → 45.9 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `HYPOTHERMIA` | Drop | 26 → 27 | 2.04 → 1.83 | 12.5 → 9.2 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `INFECTION SUSCEPTIBILITY INCREASED` | Drop | 9 → 9 | 2.11 → 1.82 | 4.1 → 2.5 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `INTRAOCULAR PRESSURE TEST` | Drop | 3 → 3 | 4.50 → 3.88 | 4.8 → 3.7 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `JOINT ARTHROPLASTY` | Drop | 6 → 6 | 2.63 → 2.27 | 4.4 → 3.0 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `LYMPHOCYTIC INFILTRATION` | Drop | 4 → 4 | 3.62 → 3.13 | 5.0 → 3.7 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `MUSCLE CONTRACTURE` | Drop | 6 → 6 | 2.68 → 2.31 | 4.6 → 3.2 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `NEONATAL RESPIRATORY DISTRESS` | Drop | 6 → 6 | 2.89 → 2.50 | 5.5 → 3.9 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `NEUROENDOCRINE CARCINOMA OF THE SKIN` | Drop | 5 → 5 | 3.22 → 2.78 | 5.4 → 3.9 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `PANCREATIC ATROPHY` | Drop | 3 → 3 | 4.36 → 3.77 | 4.5 → 3.5 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `PANCREATITIS ACUTE` | Drop | 66 → 68 | 2.11 → 1.87 | 36.4 → 26.3 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `PEPTIC ULCER HAEMORRHAGE` | Drop | 3 → 3 | 4.11 → 3.55 | 4.1 → 3.1 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `PERFUME SENSITIVITY` | Drop | 5 → 5 | 3.16 → 2.73 | 5.2 → 3.7 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `PERNICIOUS ANAEMIA` | Drop | 3 → 3 | 4.36 → 3.77 | 4.5 → 3.5 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `PETECHIAE` | Drop | 26 → 27 | 2.19 → 1.96 | 15.2 → 11.5 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `PNEUMOPERITONEUM` | Drop | 6 → 6 | 2.62 → 2.26 | 4.4 → 3.0 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `POST-TUSSIVE VOMITING` | Drop | 3 → 3 | 4.64 → 4.01 | 5.0 → 3.9 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `PRODUCT LABEL CONFUSION` | Drop | 28 → 29 | 2.07 → 1.85 | 14.1 → 10.3 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `PRODUCT SELECTION ERROR` | Drop | 5 → 5 | 2.84 → 2.45 | 4.1 → 2.9 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `PSOAS ABSCESS` | Drop | 4 → 4 | 3.52 → 3.04 | 4.7 → 3.5 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `PURPURA` | Drop | 20 → 20 | 2.06 → 1.78 | 9.6 → 5.9 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `PYELONEPHRITIS` | Drop | 28 → 29 | 2.13 → 1.91 | 15.3 → 11.4 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `RED CELL DISTRIBUTION WIDTH DECREASED` | Drop | 3 → 3 | 4.30 → 3.71 | 4.4 → 3.4 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `RHABDOMYOLYSIS` | Drop | 109 → 113 | 2.12 → 1.90 | 62.1 → 46.2 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `SALIVARY HYPERSECRETION` | Drop | 30 → 31 | 2.12 → 1.90 | 16.3 → 12.0 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `SEDATION COMPLICATION` | Drop | 12 → 12 | 2.24 → 1.93 | 6.8 → 4.4 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `SKIN DISCOMFORT` | Drop | 12 → 13 | 2.09 → 1.96 | 5.7 → 5.1 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `SKIN EROSION` | Drop | 13 → 13 | 2.26 → 1.96 | 7.8 → 5.0 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `SKIN LESION` | Drop | 81 → 88 | 2.02 → 1.90 | 40.0 → 35.8 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `SPINAL CORD NEOPLASM` | Drop | 4 → 4 | 3.25 → 2.81 | 4.0 → 2.9 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `SPINAL LAMINECTOMY` | Drop | 4 → 4 | 3.59 → 3.10 | 4.9 → 3.6 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `SPLENOMEGALY` | Drop | 30 → 33 | 2.07 → 1.97 | 15.2 → 14.5 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `TERMINAL INSOMNIA` | Drop | 11 → 11 | 2.02 → 1.74 | 4.6 → 2.7 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `THYROID CYST` | Drop | 3 → 3 | 4.23 → 3.66 | 4.3 → 3.3 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `TONGUE DISORDER` | Drop | 13 → 14 | 2.14 → 2.00 | 6.7 → 5.9 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `VARICELLA` | Drop | 8 → 8 | 2.48 → 2.14 | 5.5 → 3.7 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `VASCULAR DEVICE INFECTION` | Drop | 31 → 31 | 2.04 → 1.76 | 15.1 → 9.3 | $\text{PRR} \ge 2.0$ |
| **IBUPROFEN** | `VASCULAR DEVICE OCCLUSION` | Drop | 4 → 4 | 3.31 → 2.86 | 4.2 → 3.0 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `VIRAL PHARYNGITIS` | Drop | 3 → 3 | 4.23 → 3.66 | 4.3 → 3.3 | $\chi^2 \ge 4.0$ |
| **IBUPROFEN** | `WHITE COAT HYPERTENSION` | Drop | 6 → 6 | 2.81 → 2.43 | 5.1 → 3.6 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `ACQUIRED LEFT VENTRICLE OUTFLOW TRACT OBSTRUCTION` | Cross | 2 → 3 | 5.97 → 8.83 | 3.6 → 11.8 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `ACTIVATED PARTIAL THROMBOPLASTIN TIME SHORTENED` | Cross | 2 → 4 | 2.78 → 5.47 | 0.8 → 9.5 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `ACUTE CARDIAC EVENT` | Cross | 2 → 3 | 2.98 → 4.31 | 1.0 → 4.3 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `ADENOCARCINOMA PANCREAS` | Cross | 3 → 6 | 2.21 → 4.31 | 0.9 → 11.2 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `ALLERGIC RESPIRATORY SYMPTOM` | Cross | 2 → 3 | 3.13 → 4.53 | 1.1 → 4.7 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `ALLERGIC SINUSITIS` | Cross | 7 → 9 | 2.31 → 2.82 | 3.8 → 8.4 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `ALVEOLAR OSTEITIS` | Cross | 2 → 4 | 2.36 → 4.61 | 0.5 → 7.4 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `ANAEMIA OF MALIGNANT DISEASE` | Cross | 2 → 3 | 4.47 → 6.54 | 2.3 → 8.1 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `ANEMBRYONIC GESTATION` | Cross | 2 → 3 | 3.21 → 4.64 | 1.2 → 4.9 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `ANION GAP DECREASED` | Cross | 3 → 4 | 2.72 → 3.46 | 1.7 → 4.5 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `ANION GAP INCREASED` | Cross | 5 → 7 | 1.98 → 2.64 | 1.5 → 5.3 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `ANKLE FRACTURE` | Cross | 59 → 65 | 1.95 → 2.03 | 25.6 → 31.7 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `ANTINUCLEAR ANTIBODY INCREASED` | Cross | 4 → 5 | 3.25 → 3.87 | 4.0 → 7.4 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `ARTERIOSCLEROSIS CORONARY ARTERY` | Cross | 27 → 33 | 1.97 → 2.28 | 11.6 → 21.5 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `ASTHENOPIA` | Cross | 25 → 27 | 1.97 → 2.01 | 10.7 → 12.2 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `ASTHMA EXERCISE INDUCED` | Cross | 3 → 4 | 2.76 → 3.51 | 1.7 → 4.6 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `BIOPSY BONE MARROW` | Cross | 3 → 4 | 2.94 → 3.74 | 2.0 → 5.1 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `BLOOD ALBUMIN DECREASED` | Cross | 28 → 33 | 1.96 → 2.18 | 11.8 → 19.1 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `BLOOD ALBUMIN INCREASED` | Cross | 5 → 8 | 2.57 → 3.96 | 3.2 → 13.9 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `BLOOD CREATININE INCREASED` | Cross | 221 → 251 | 1.91 → 2.05 | 93.0 → 129.8 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `BLOOD IMMUNOGLOBULIN M INCREASED` | Cross | 3 → 5 | 2.54 → 4.09 | 1.4 → 8.2 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `BLOOD OSMOLARITY DECREASED` | Cross | 2 → 4 | 3.13 → 6.19 | 1.1 → 11.3 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `CARDIAC CIRRHOSIS` | Cross | 1 → 4 | 6.26 → 33.62 | 0.6 → 60.8 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `CENTRAL VENOUS CATHETER REMOVAL` | Cross | 3 → 5 | 2.32 → 3.72 | 1.1 → 6.9 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `CEREBRAL PALSY` | Cross | 5 → 6 | 2.41 → 2.74 | 2.7 → 4.7 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `CHEILITIS` | Cross | 18 → 22 | 1.96 → 2.27 | 7.3 → 13.8 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `CHOKING` | Cross | 88 → 97 | 1.99 → 2.07 | 41.2 → 50.7 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `CHRONIC SINUSITIS` | Cross | 24 → 29 | 1.81 → 2.07 | 7.7 → 14.5 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `COCCYDYNIA` | Cross | 8 → 9 | 2.00 → 2.13 | 3.0 → 4.1 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `COLD-STIMULUS HEADACHE` | Cross | 3 → 4 | 2.68 → 3.41 | 1.6 → 4.3 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `CONJUNCTIVITIS BACTERIAL` | Cross | 3 → 4 | 4.00 → 5.12 | 3.8 → 8.6 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `COSTOCHONDRITIS` | Cross | 6 → 7 | 2.26 → 2.50 | 2.9 → 4.7 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `DECREASED ACTIVITY` | Cross | 61 → 70 | 1.96 → 2.12 | 26.8 → 38.9 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `DENTAL DISCOMFORT` | Cross | 5 → 7 | 2.22 → 2.96 | 2.2 → 6.9 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `DIASTASIS RECTI ABDOMINIS` | Cross | 2 → 4 | 3.58 → 7.13 | 1.5 → 13.7 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `DIASTOLIC DYSFUNCTION` | Cross | 13 → 14 | 1.99 → 2.01 | 5.2 → 5.9 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `DILATATION ATRIAL` | Cross | 2 → 4 | 3.39 → 6.72 | 1.3 → 12.7 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `DRUG DETOXIFICATION` | Cross | 3 → 5 | 1.82 → 2.91 | 0.4 → 4.3 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `DYSPHONIA` | Cross | 258 → 281 | 1.98 → 2.03 | 120.1 → 140.9 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `ELECTROCARDIOGRAM ABNORMAL` | Cross | 23 → 28 | 1.94 → 2.23 | 9.2 → 17.2 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `EMOTIONAL DISTRESS` | Cross | 179 → 198 | 1.97 → 2.05 | 81.9 → 102.3 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `FACIAL NERVE DISORDER` | Cross | 2 → 3 | 3.06 → 4.41 | 1.0 → 4.5 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `FUNGAL INFECTION` | Cross | 135 → 158 | 1.85 → 2.05 | 50.6 → 80.7 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `GASTRIC BYPASS` | Cross | 6 → 10 | 1.54 → 2.45 | 0.6 → 6.9 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `GASTRIC MUCOSA ERYTHEMA` | Cross | 3 → 4 | 3.69 → 4.71 | 3.3 → 7.6 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `GASTROINTESTINAL INJURY` | Cross | 9 → 11 | 2.04 → 2.36 | 3.7 → 7.0 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `GASTROINTESTINAL SCARRING` | Cross | 8 → 10 | 2.10 → 2.48 | 3.4 → 7.1 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `GASTROINTESTINAL ULCER HAEMORRHAGE` | Cross | 4 → 5 | 2.53 → 3.00 | 2.2 → 4.6 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `GINGIVAL DISORDER` | Cross | 10 → 12 | 1.91 → 2.17 | 3.4 → 6.2 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `GLOMERULAR FILTRATION RATE DECREASED` | Cross | 75 → 85 | 1.94 → 2.08 | 32.4 → 44.8 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `HAEMANGIOMA OF SKIN` | Cross | 2 → 4 | 2.12 → 4.13 | 0.3 → 6.1 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `HAEMOGLOBIN ABNORMAL` | Cross | 30 → 36 | 1.76 → 2.00 | 8.9 → 16.4 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `HAEMORRHAGIC OCCLUSIVE RETINAL VASCULITIS` | Cross | 1 → 3 | 1.90 → 5.69 | 0.0 → 6.7 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `HAIR GROWTH ABNORMAL` | Cross | 25 → 28 | 1.99 → 2.10 | 11.0 → 14.6 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `HIGH DENSITY LIPOPROTEIN INCREASED` | Cross | 5 → 6 | 2.63 → 2.99 | 3.4 → 5.8 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `HYPERPARATHYROIDISM SECONDARY` | Cross | 4 → 5 | 2.48 → 2.94 | 2.1 → 4.4 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `HYPERPLASIA OF THYMIC EPITHELIUM` | Cross | 0 → 3 | 0.00 → 176.51 | 0.0 → 90.1 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `HYPERTONIC BLADDER` | Cross | 14 → 16 | 1.87 → 2.02 | 4.7 → 7.0 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `HYPOVOLAEMIA` | Cross | 17 → 24 | 1.55 → 2.08 | 2.8 → 12.0 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `ICHTHYOSIS` | Cross | 2 → 4 | 2.56 → 5.01 | 0.6 → 8.4 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `IMMUNODEFICIENCY COMMON VARIABLE` | Cross | 4 → 5 | 2.39 → 2.83 | 1.9 → 4.0 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `INCISION SITE SWELLING` | Cross | 3 → 4 | 3.30 → 4.20 | 2.6 → 6.3 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `INCREASED VISCOSITY OF BRONCHIAL SECRETION` | Cross | 4 → 5 | 2.41 → 2.86 | 1.9 → 4.1 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `INFUSION SITE ABSCESS` | Cross | 5 → 8 | 1.77 → 2.71 | 1.0 → 6.6 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `INSURANCE ISSUE` | Cross | 197 → 219 | 1.97 → 2.07 | 90.7 → 115.6 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `JOINT INJURY` | Cross | 98 → 109 | 2.00 → 2.10 | 46.5 → 59.4 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `JUGULAR VEIN DISTENSION` | Cross | 2 → 3 | 2.98 → 4.31 | 1.0 → 4.3 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `LACRIMATION DECREASED` | Cross | 3 → 4 | 3.48 → 4.44 | 2.9 → 6.9 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `LARGE GRANULAR LYMPHOCYTOSIS` | Cross | 3 → 5 | 2.89 → 4.67 | 1.9 → 10.1 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `LARYNGEAL CANCER` | Cross | 3 → 4 | 2.81 → 3.57 | 1.8 → 4.7 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `LICHEN SCLEROSUS` | Cross | 5 → 7 | 2.55 → 3.40 | 3.1 → 9.0 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `LIGHT CHAIN ANALYSIS ABNORMAL` | Cross | 1 → 3 | 1.79 → 5.35 | 0.0 → 6.1 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `LIGHT CHAIN ANALYSIS INCREASED` | Cross | 6 → 8 | 2.54 → 3.22 | 4.0 → 9.6 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `LIP PAIN` | Cross | 9 → 11 | 1.75 → 2.02 | 2.1 → 4.5 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `LIPASE INCREASED` | Cross | 24 → 29 | 1.77 → 2.02 | 7.1 → 13.5 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `LOWER RESPIRATORY TRACT INFECTION FUNGAL` | Cross | 2 → 3 | 10.44 → 16.05 | 7.6 → 22.3 | $a \ge 3$ |
| **OMEPRAZOLE** | `LOWER URINARY TRACT SYMPTOMS` | Cross | 2 → 6 | 2.05 → 6.19 | 0.3 → 19.1 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `LUNG DIFFUSION DISORDER` | Cross | 3 → 4 | 2.85 → 3.62 | 1.9 → 4.9 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `LUNG DIFFUSION TEST DECREASED` | Cross | 3 → 5 | 1.81 → 2.88 | 0.4 → 4.2 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `MALLORY-WEISS SYNDROME` | Cross | 2 → 5 | 1.31 → 3.16 | 0.0 → 5.1 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `MEDICAL DEVICE SITE INFECTION` | Cross | 5 → 6 | 2.80 → 3.18 | 3.9 → 6.5 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `MENISCUS INJURY` | Cross | 24 → 30 | 1.86 → 2.20 | 8.5 → 17.8 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `MONOCLONAL IMMUNOGLOBULIN INCREASED` | Cross | 4 → 5 | 2.91 → 3.46 | 3.1 → 6.1 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `MONOCYTE COUNT INCREASED` | Cross | 19 → 25 | 1.92 → 2.40 | 7.3 → 18.3 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `MYASTHENIC SYNDROME` | Cross | 6 → 8 | 2.26 → 2.87 | 2.9 → 7.6 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `MYOCLONUS` | Cross | 35 → 45 | 1.65 → 2.00 | 8.0 → 20.9 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `N-TERMINAL PROHORMONE BRAIN NATRIURETIC PEPTIDE INCREASED` | Cross | 9 → 14 | 1.62 → 2.40 | 1.5 → 9.7 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `NAIL DISCOMFORT` | Cross | 2 → 3 | 5.45 → 8.02 | 3.2 → 10.6 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `NASAL DRYNESS` | Cross | 25 → 29 | 1.85 → 2.02 | 8.7 → 13.5 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `NASAL INFLAMMATION` | Cross | 5 → 8 | 1.61 → 2.46 | 0.6 → 5.3 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `NEUTROPHIL COUNT INCREASED` | Cross | 39 → 49 | 2.00 → 2.38 | 17.9 → 36.2 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `NEUTROPHIL PERCENTAGE INCREASED` | Cross | 4 → 6 | 2.26 → 3.24 | 1.6 → 6.8 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `OCCULT BLOOD POSITIVE` | Cross | 2 → 7 | 1.49 → 5.21 | 0.0 → 18.1 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `OESOPHAGEAL OBSTRUCTION` | Cross | 5 → 8 | 2.49 → 3.83 | 2.9 → 13.1 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `OPEN ANGLE GLAUCOMA` | Cross | 3 → 4 | 2.72 → 3.46 | 1.7 → 4.5 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `OPHTHALMIC MIGRAINE` | Cross | 10 → 11 | 1.99 → 2.06 | 3.8 → 4.8 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `ORAL MUCOSAL ERYTHEMA` | Cross | 6 → 9 | 1.85 → 2.65 | 1.5 → 7.3 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `OSTEOMYELITIS` | Cross | 50 → 55 | 1.93 → 2.00 | 20.9 → 25.7 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `PANIC ATTACK` | Cross | 139 → 149 | 2.00 → 2.02 | 66.3 → 72.9 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `PAROTITIS` | Cross | 6 → 8 | 2.14 → 2.71 | 2.5 → 6.6 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `PATIENT DISSATISFACTION WITH TREATMENT` | Cross | 20 → 27 | 1.62 → 2.07 | 4.0 → 13.4 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `PELVIC MASS` | Cross | 3 → 5 | 2.26 → 3.63 | 1.0 → 6.6 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `PNEUMONIA INFLUENZAL` | Cross | 9 → 12 | 1.99 → 2.51 | 3.4 → 9.1 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `POOR QUALITY SLEEP` | Cross | 106 → 119 | 1.99 → 2.11 | 50.0 → 66.1 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `PORTAL FIBROSIS` | Cross | 1 → 4 | 2.41 → 10.23 | 0.0 → 20.9 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `PORTAL HYPERTENSIVE GASTROPATHY` | Cross | 4 → 7 | 2.69 → 4.58 | 2.6 → 14.9 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `PORTAL TRACT INFLAMMATION` | Cross | 1 → 4 | 2.61 → 11.21 | 0.0 → 23.1 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `PROCTALGIA` | Cross | 25 → 34 | 1.78 → 2.29 | 7.5 → 22.6 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `PROSTATIC OBSTRUCTION` | Cross | 2 → 3 | 8.35 → 12.61 | 5.8 → 17.6 | $a \ge 3$ |
| **OMEPRAZOLE** | `PROTEIN TOTAL ABNORMAL` | Cross | 5 → 6 | 2.68 → 3.04 | 3.5 → 6.0 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `PROTEIN TOTAL INCREASED` | Cross | 11 → 14 | 1.74 → 2.10 | 2.7 → 6.7 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `PROTHROMBIN TIME PROLONGED` | Cross | 10 → 11 | 1.99 → 2.06 | 3.8 → 4.8 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `PULMONARY THROMBOSIS` | Cross | 49 → 54 | 1.96 → 2.04 | 21.4 → 26.5 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `PULSE ABNORMAL` | Cross | 7 → 11 | 1.36 → 2.03 | 0.3 → 4.6 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `RADIUS FRACTURE` | Cross | 7 → 9 | 1.95 → 2.37 | 2.3 → 5.6 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `RED CELL DISTRIBUTION WIDTH INCREASED` | Cross | 20 → 24 | 1.82 → 2.06 | 6.3 → 11.6 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `RHINALGIA` | Cross | 18 → 22 | 1.87 → 2.16 | 6.2 → 12.1 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `RHONCHI` | Cross | 5 → 8 | 2.22 → 3.41 | 2.2 → 10.7 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `RIGHT VENTRICULAR DILATATION` | Cross | 4 → 5 | 2.91 → 3.46 | 3.1 → 6.1 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `RIGHT VENTRICULAR DYSFUNCTION` | Cross | 6 → 8 | 2.20 → 2.79 | 2.7 → 7.1 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `SALIVARY GLAND CALCULUS` | Cross | 3 → 4 | 3.61 → 4.61 | 3.1 → 7.4 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `SALIVARY GLAND MASS` | Cross | 2 → 3 | 6.26 → 9.29 | 3.9 → 12.6 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `SERUM FERRITIN DECREASED` | Cross | 10 → 12 | 1.98 → 2.24 | 3.8 → 6.8 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `SKIN HAEMORRHAGE` | Cross | 108 → 116 | 1.99 → 2.02 | 51.0 → 56.5 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `SLEEP DEFICIT` | Cross | 19 → 23 | 1.92 → 2.19 | 7.2 → 13.3 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `SPINAL DEFORMITY` | Cross | 6 → 7 | 2.51 → 2.76 | 3.9 → 5.9 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `SPINAL FRACTURE` | Cross | 94 → 103 | 2.00 → 2.06 | 44.4 → 53.4 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `STRESS URINARY INCONTINENCE` | Cross | 4 → 6 | 2.46 → 3.53 | 2.1 → 8.0 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `SYSTEMIC SCLERODERMA` | Cross | 6 → 10 | 1.63 → 2.60 | 0.9 → 8.0 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `TASTE DISORDER` | Cross | 185 → 201 | 1.97 → 2.01 | 84.6 → 98.4 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `TENDON PAIN` | Cross | 25 → 27 | 2.00 → 2.03 | 11.1 → 12.6 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `THYROID ATROPHY` | Cross | 0 → 9 | 0.00 → 44.13 | 0.0 → 192.4 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `THYROXINE INCREASED` | Cross | 4 → 5 | 2.85 → 3.38 | 3.0 → 5.8 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `TOOTHACHE` | Cross | 75 → 82 | 1.98 → 2.04 | 34.3 → 40.9 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `TRAUMATIC INTRACRANIAL HAEMORRHAGE` | Cross | 5 → 6 | 2.39 → 2.72 | 2.7 → 4.7 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `TROPONIN I INCREASED` | Cross | 7 → 8 | 2.10 → 2.26 | 2.9 → 4.3 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `TYMPANIC MEMBRANE PERFORATION` | Cross | 5 → 10 | 1.10 → 2.11 | 0.0 → 4.6 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `UNDERWEIGHT` | Cross | 9 → 11 | 1.82 → 2.11 | 2.5 → 5.1 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `UPPER RESPIRATORY TRACT INFECTION BACTERIAL` | Cross | 2 → 3 | 3.13 → 4.53 | 1.1 → 4.7 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `URINE KETONE BODY PRESENT` | Cross | 6 → 7 | 2.25 → 2.48 | 2.9 → 4.6 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `VARICES OESOPHAGEAL` | Cross | 11 → 15 | 1.90 → 2.47 | 3.7 → 11.1 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `VASCULAR INJURY` | Cross | 5 → 6 | 2.57 → 2.92 | 3.2 → 5.5 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `VERTEBRAL OSTEOPHYTE` | Cross | 2 → 4 | 1.87 → 3.62 | 0.2 → 4.9 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `WHITE BLOOD CELL COUNT ABNORMAL` | Cross | 28 → 31 | 1.95 → 2.04 | 11.7 → 14.8 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `WRIST SURGERY` | Cross | 6 → 7 | 2.49 → 2.75 | 3.8 → 5.8 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `ABDOMINAL RIGIDITY` | Drop | 11 → 11 | 2.06 → 1.93 | 4.8 → 3.9 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `ACUTE PHASE REACTION` | Drop | 3 → 3 | 4.37 → 4.10 | 4.4 → 4.0 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `ADMINISTRATION SITE PAIN` | Drop | 9 → 9 | 2.11 → 1.98 | 4.1 → 3.3 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `ALANINE AMINOTRANSFERASE INCREASED` | Drop | 218 → 227 | 2.04 → 1.99 | 110.6 → 108.0 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `AMMONIA INCREASED` | Drop | 14 → 14 | 2.07 → 1.94 | 6.5 → 5.3 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `ANGULAR CHEILITIS` | Drop | 6 → 6 | 2.70 → 2.54 | 4.6 → 4.0 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `AORTIC VALVE CALCIFICATION` | Drop | 4 → 4 | 3.48 → 3.27 | 4.5 → 4.0 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `ARRHYTHMIA` | Drop | 148 → 151 | 2.01 → 1.93 | 71.9 → 64.4 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `ARTHROSCOPY` | Drop | 3 → 3 | 4.37 → 4.10 | 4.4 → 4.0 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `ATRIOVENTRICULAR BLOCK` | Drop | 29 → 29 | 2.01 → 1.89 | 13.3 → 10.9 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `BITE` | Drop | 5 → 5 | 2.93 → 2.75 | 4.3 → 3.8 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `BODY TEMPERATURE ABNORMAL` | Drop | 12 → 12 | 2.00 → 1.88 | 4.9 → 4.0 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `BREAKTHROUGH PAIN` | Drop | 5 → 5 | 2.96 → 2.78 | 4.4 → 3.8 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `BUTTOCK INJURY` | Drop | 4 → 4 | 3.34 → 3.14 | 4.2 → 3.7 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `CARDIAC STRESS TEST ABNORMAL` | Drop | 3 → 3 | 4.37 → 4.10 | 4.4 → 4.0 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `CARPAL TUNNEL DECOMPRESSION` | Drop | 6 → 6 | 2.61 → 2.45 | 4.3 → 3.6 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `CELL DEATH` | Drop | 9 → 9 | 2.16 → 2.03 | 4.3 → 3.6 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `CHRONIC SPONTANEOUS URTICARIA` | Drop | 13 → 13 | 2.06 → 1.94 | 5.9 → 4.8 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `DIABETIC METABOLIC DECOMPENSATION` | Drop | 7 → 7 | 2.41 → 2.26 | 4.3 → 3.6 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `DISCHARGE` | Drop | 12 → 12 | 2.04 → 1.91 | 5.2 → 4.2 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `FOETAL HEART RATE DECELERATION ABNORMALITY` | Drop | 5 → 5 | 2.93 → 2.75 | 4.3 → 3.8 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `FULL BLOOD COUNT DECREASED` | Drop | 107 → 112 | 2.03 → 2.00 | 53.1 → 53.0 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `GAMMA-GLUTAMYLTRANSFERASE INCREASED` | Drop | 64 → 64 | 2.04 → 1.92 | 31.8 → 26.2 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `HEAD TITUBATION` | Drop | 7 → 7 | 2.37 → 2.23 | 4.1 → 3.4 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `HYPERMOBILITY SYNDROME` | Drop | 4 → 4 | 3.34 → 3.14 | 4.2 → 3.7 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `INTESTINAL DILATATION` | Drop | 7 → 7 | 2.40 → 2.25 | 4.2 → 3.5 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `INTESTINAL HAEMORRHAGE` | Drop | 22 → 22 | 2.11 → 1.98 | 11.3 → 9.4 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `LOW BIRTH WEIGHT BABY` | Drop | 34 → 34 | 2.01 → 1.88 | 15.6 → 12.8 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `LUMBAR VERTEBRAL FRACTURE` | Drop | 22 → 22 | 2.10 → 1.97 | 11.2 → 9.2 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `MIXED LIVER INJURY` | Drop | 24 → 24 | 2.00 → 1.88 | 10.7 → 8.7 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `MYDRIASIS` | Drop | 51 → 52 | 2.08 → 2.00 | 26.8 → 24.1 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `OBSTRUCTIVE PANCREATITIS` | Drop | 8 → 8 | 2.22 → 2.08 | 4.0 → 3.4 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `OSTEOLYSIS` | Drop | 8 → 8 | 2.22 → 2.08 | 4.0 → 3.4 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `PARADOXICAL DRUG REACTION` | Drop | 18 → 18 | 2.01 → 1.88 | 7.8 → 6.4 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `PENILE SIZE REDUCED` | Drop | 5 → 5 | 2.93 → 2.75 | 4.3 → 3.8 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `PERIPHERAL NERVE INJURY` | Drop | 6 → 6 | 2.57 → 2.42 | 4.1 → 3.5 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `PORPHYRIA` | Drop | 3 → 3 | 4.27 → 4.01 | 4.3 → 3.8 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `SCABIES` | Drop | 5 → 5 | 2.96 → 2.78 | 4.4 → 3.8 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `SLEEP DISORDER DUE TO A GENERAL MEDICAL CONDITION` | Drop | 215 → 221 | 2.02 → 1.95 | 106.2 → 98.3 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `THERAPEUTIC PRODUCT INEFFECTIVE` | Drop | 20 → 20 | 2.12 → 1.99 | 10.4 → 8.6 | $\text{PRR} \ge 2.0$ |
| **OMEPRAZOLE** | `TOOTH SOCKET HAEMORRHAGE` | Drop | 3 → 3 | 4.27 → 4.01 | 4.3 → 3.8 | $\chi^2 \ge 4.0$ |
| **OMEPRAZOLE** | `URINARY TRACT OBSTRUCTION` | Drop | 9 → 9 | 2.14 → 2.01 | 4.2 → 3.5 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `ABDOMINAL MASS` | Cross | 7 → 13 | 1.56 → 2.45 | 0.9 → 9.5 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `ACROCHORDON` | Cross | 5 → 6 | 2.66 → 2.67 | 3.5 → 4.6 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `ADNEXA UTERI CYST` | Cross | 2 → 3 | 8.88 → 11.58 | 6.6 → 17.2 | $a \ge 3$ |
| **SERTRALINE** | `ANEMBRYONIC GESTATION` | Cross | 2 → 3 | 5.69 → 7.32 | 3.5 → 9.8 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `ANOSOGNOSIA` | Cross | 3 → 4 | 3.17 → 3.56 | 2.5 → 4.8 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `ARTHRITIS INFECTIVE` | Cross | 9 → 19 | 1.26 → 2.25 | 0.3 → 11.7 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `ARTIFICIAL CROWN PROCEDURE` | Cross | 2 → 3 | 4.44 → 5.67 | 2.3 → 6.9 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `AUTOIMMUNE THYROIDITIS` | Cross | 9 → 16 | 1.57 → 2.35 | 1.3 → 10.8 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `AUTOPHOBIA` | Cross | 2 → 3 | 5.84 → 7.51 | 3.7 → 10.2 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `BEDRIDDEN` | Cross | 25 → 33 | 1.89 → 2.10 | 9.5 → 17.5 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `BENIGN NEOPLASM` | Cross | 4 → 6 | 2.94 → 3.73 | 3.3 → 9.0 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `BLADDER IRRITATION` | Cross | 2 → 4 | 2.24 → 3.82 | 0.4 → 5.5 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `BLOOD BICARBONATE DECREASED` | Cross | 1 → 7 | 0.64 → 3.86 | 0.0 → 11.6 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `BLOOD BLISTER` | Cross | 6 → 9 | 2.00 → 2.53 | 2.0 → 6.7 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `BLOOD CHLORIDE INCREASED` | Cross | 4 → 14 | 1.64 → 4.97 | 0.4 → 38.4 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `BLOOD FOLATE DECREASED` | Cross | 4 → 5 | 3.20 → 3.36 | 3.9 → 5.9 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `BLOOD IRON DECREASED` | Cross | 34 → 60 | 1.58 → 2.35 | 6.6 → 44.2 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `BLOOD LOSS ANAEMIA` | Cross | 10 → 19 | 1.99 → 3.20 | 3.9 → 25.7 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `BLOOD MAGNESIUM DECREASED` | Cross | 24 → 32 | 1.98 → 2.21 | 10.5 → 19.6 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `BLOOD SODIUM DECREASED` | Cross | 37 → 54 | 1.76 → 2.16 | 11.2 → 31.7 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `BODY HEIGHT DECREASED` | Cross | 10 → 23 | 1.36 → 2.64 | 0.6 → 21.3 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `BREAST RECONSTRUCTION` | Cross | 1 → 3 | 1.98 → 5.15 | 0.0 → 5.9 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `CANCER PAIN` | Cross | 3 → 10 | 0.73 → 2.07 | 0.1 → 4.4 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `CARBON DIOXIDE DECREASED` | Cross | 5 → 6 | 2.72 → 2.74 | 3.8 → 4.8 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `CARDIAC DISCOMFORT` | Cross | 3 → 8 | 1.09 → 2.46 | 0.0 → 5.4 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `CARTILAGE INJURY` | Cross | 5 → 9 | 2.07 → 3.16 | 1.8 → 10.8 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `CATHETER SITE CELLULITIS` | Cross | 2 → 3 | 5.17 → 6.62 | 3.0 → 8.6 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `CATHETER SITE HAEMORRHAGE` | Cross | 5 → 9 | 1.71 → 2.61 | 0.8 → 7.2 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `CENTRAL NERVOUS SYSTEM STIMULATION` | Cross | 2 → 3 | 11.69 → 15.44 | 9.3 → 23.3 | $a \ge 3$ |
| **SERTRALINE** | `CERUMEN IMPACTION` | Cross | 2 → 4 | 3.53 → 6.08 | 1.5 → 11.5 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `CHONDROPATHY` | Cross | 4 → 6 | 2.02 → 2.55 | 1.1 → 4.1 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `CHOROIDITIS` | Cross | 1 → 3 | 1.98 → 5.15 | 0.0 → 5.9 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `CHRONIC LEFT VENTRICULAR FAILURE` | Cross | 2 → 5 | 3.04 → 6.62 | 1.0 → 17.3 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `CLUBBING` | Cross | 1 → 3 | 2.18 → 5.67 | 0.0 → 6.9 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `COITAL BLEEDING` | Cross | 3 → 5 | 2.58 → 3.65 | 1.5 → 6.9 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `COLORECTAL ADENOMA` | Cross | 2 → 5 | 1.37 → 2.91 | 0.0 → 4.4 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `COMPLEX REGIONAL PAIN SYNDROME` | Cross | 2 → 5 | 1.50 → 3.20 | 0.0 → 5.3 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `COMPLICATION OF DEVICE INSERTION` | Cross | 27 → 60 | 1.19 → 2.24 | 0.7 → 39.1 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `CONSCIOUSNESS FLUCTUATING` | Cross | 2 → 4 | 2.44 → 4.16 | 0.5 → 6.4 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `CONTUSION` | Cross | 227 → 292 | 1.86 → 2.01 | 88.3 → 143.8 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `COVID-19 IMMUNISATION` | Cross | 2 → 5 | 1.49 → 3.17 | 0.0 → 5.2 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `CREST SYNDROME` | Cross | 1 → 3 | 5.84 → 16.35 | 0.6 → 24.7 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `CRYSTALLURIA` | Cross | 3 → 4 | 3.11 → 3.50 | 2.4 → 4.7 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `DEFAECATION URGENCY` | Cross | 33 → 40 | 1.97 → 2.00 | 14.7 → 18.7 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `DEMENTIA ALZHEIMER'S TYPE` | Cross | 22 → 27 | 1.97 → 2.02 | 9.3 → 12.6 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `DENTAL CARIES` | Cross | 20 → 41 | 1.70 → 2.96 | 5.1 → 49.8 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `DENTAL DISCOMFORT` | Cross | 4 → 5 | 3.13 → 3.29 | 3.7 → 5.6 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `DERMATITIS CONTACT` | Cross | 27 → 42 | 1.61 → 2.11 | 5.6 → 23.0 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `DEVICE PLACEMENT ISSUE` | Cross | 7 → 16 | 1.08 → 2.09 | 0.0 → 7.8 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `DIAPHRAGMATIC HERNIA` | Cross | 2 → 3 | 3.27 → 4.15 | 1.2 → 4.2 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `DISSOCIATION` | Cross | 45 → 79 | 1.80 → 2.67 | 15.1 → 78.9 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `DRUG DOSE TITRATION NOT PERFORMED` | Cross | 6 → 9 | 2.10 → 2.66 | 2.4 → 7.5 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `DRY MOUTH` | Cross | 197 → 249 | 1.91 → 2.02 | 83.4 → 125.6 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `DUODENAL POLYP` | Cross | 1 → 3 | 2.85 → 7.51 | 0.1 → 10.2 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `DYSPHORIA` | Cross | 7 → 19 | 1.63 → 3.78 | 1.1 → 34.6 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `DYSPLASTIC NAEVUS` | Cross | 2 → 5 | 2.78 → 6.02 | 0.8 → 15.2 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `EAR PAIN` | Cross | 38 → 63 | 1.46 → 2.04 | 5.1 → 31.8 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `EDUCATIONAL PROBLEM` | Cross | 0 → 5 | 0.00 → 4.98 | 0.2 → 11.5 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `EJACULATION DELAYED` | Cross | 2 → 3 | 4.04 → 5.15 | 1.9 → 5.9 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `ENDOMETRIAL ABLATION` | Cross | 0 → 3 | 0.00 → 6.46 | 0.0 → 8.3 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `ENDOMETRIOSIS` | Cross | 11 → 14 | 2.00 → 2.13 | 4.4 → 7.1 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `ENDOMETRITIS` | Cross | 0 → 3 | 0.00 → 4.34 | 0.0 → 4.5 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `EPISCLERITIS` | Cross | 3 → 4 | 3.03 → 3.40 | 2.2 → 4.4 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `EXERCISE TOLERANCE DECREASED` | Cross | 11 → 17 | 1.56 → 2.03 | 1.7 → 7.7 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `EYE INFARCTION` | Cross | 2 → 3 | 7.66 → 9.93 | 5.5 → 14.4 | $a \ge 3$ |
| **SERTRALINE** | `FEELING ABNORMAL` | Cross | 348 → 503 | 1.66 → 2.01 | 89.7 → 252.1 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `FOETAL ANAEMIA` | Cross | 2 → 3 | 6.73 → 8.69 | 4.6 → 12.2 | $a \ge 3$ |
| **SERTRALINE** | `FOETAL CARDIAC DISORDER` | Cross | 0 → 3 | 0.00 → 10.69 | 0.0 → 15.7 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `FOLATE DEFICIENCY` | Cross | 5 → 6 | 2.64 → 2.66 | 3.5 → 4.5 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `FOREIGN BODY IN UROGENITAL TRACT` | Cross | 3 → 8 | 1.23 → 2.79 | 0.0 → 7.2 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `GASTRIC ULCER` | Cross | 34 → 47 | 1.89 → 2.19 | 13.1 → 28.5 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `GASTROENTERITIS VIRAL` | Cross | 58 → 83 | 1.97 → 2.38 | 26.5 → 63.2 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `GASTROINTESTINAL CARCINOMA` | Cross | 5 → 10 | 1.64 → 2.77 | 0.7 → 9.3 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `GASTROINTESTINAL INFECTION` | Cross | 25 → 37 | 1.79 → 2.23 | 7.8 → 23.3 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `GASTROINTESTINAL STOMA OUTPUT ABNORMAL` | Cross | 2 → 4 | 3.27 → 5.62 | 1.2 → 10.2 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `HAEMORRHAGIC DIATHESIS` | Cross | 5 → 14 | 1.53 → 3.67 | 0.5 → 23.7 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `HAIR DISORDER` | Cross | 5 → 7 | 2.28 → 2.68 | 2.4 → 5.6 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `HALLUCINATION, TACTILE` | Cross | 3 → 4 | 3.27 → 3.67 | 2.6 → 5.1 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `HEART FAILURE WITH PRESERVED EJECTION FRACTION` | Cross | 0 → 6 | 0.00 → 2.74 | 1.0 → 4.8 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `HEPATIC PAIN` | Cross | 9 → 12 | 1.82 → 2.04 | 2.5 → 5.2 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `IATROGENIC INJURY` | Cross | 2 → 4 | 3.17 → 5.45 | 1.2 → 9.8 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `IMMUNODEFICIENCY COMMON VARIABLE` | Cross | 2 → 6 | 2.08 → 5.40 | 0.3 → 16.3 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `IMPAIRED WORK ABILITY` | Cross | 32 → 49 | 1.85 → 2.39 | 11.5 → 37.2 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `IMPERCEPTION` | Cross | 1 → 4 | 3.97 → 14.83 | 0.2 → 33.2 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `INADEQUATE LUBRICATION` | Cross | 2 → 3 | 22.21 → 30.89 | 18.3 → 44.4 | $a \ge 3$ |
| **SERTRALINE** | `INFUSION SITE BRUISING` | Cross | 13 → 18 | 1.98 → 2.31 | 5.3 → 11.7 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `INFUSION SITE INJURY` | Cross | 1 → 3 | 1.74 → 4.48 | 0.0 → 4.8 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `INFUSION SITE NODULE` | Cross | 4 → 8 | 1.54 → 2.61 | 0.3 → 6.2 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `INTRA-ABDOMINAL FLUID COLLECTION` | Cross | 6 → 10 | 2.01 → 2.83 | 2.1 → 9.8 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `INTRADUCTAL PROLIFERATIVE BREAST LESION` | Cross | 3 → 5 | 2.19 → 3.09 | 0.9 → 4.9 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `JARISCH-HERXHEIMER REACTION` | Cross | 1 → 4 | 1.21 → 4.16 | 0.0 → 6.4 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `JAW CLICKING` | Cross | 1 → 3 | 1.74 → 4.48 | 0.0 → 4.8 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `JC POLYOMAVIRUS TEST POSITIVE` | Cross | 4 → 7 | 1.91 → 2.82 | 0.9 → 6.3 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `JOINT CONTRACTURE` | Cross | 0 → 6 | 0.00 → 6.62 | 0.1 → 21.7 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `JOINT INJURY` | Cross | 52 → 68 | 1.85 → 2.03 | 19.3 → 33.9 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `KYPHOSIS` | Cross | 2 → 6 | 1.45 → 3.73 | 0.0 → 9.0 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `LAGOPHTHALMOS` | Cross | 2 → 3 | 5.17 → 6.62 | 3.0 → 8.6 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `LANGUAGE DISORDER` | Cross | 5 → 7 | 2.33 → 2.75 | 2.5 → 5.9 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `LEGAL PROBLEM` | Cross | 1 → 3 | 1.63 → 4.21 | 0.0 → 4.3 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `LIBIDO INCREASED` | Cross | 3 → 7 | 1.98 → 3.95 | 0.6 → 12.1 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `LOSS OF CONSCIOUSNESS` | Cross | 260 → 345 | 2.00 → 2.22 | 126.8 → 227.0 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `LUMBAR RADICULOPATHY` | Cross | 2 → 5 | 1.87 → 3.99 | 0.2 → 8.1 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `MACULAR DEGENERATION` | Cross | 33 → 43 | 1.89 → 2.06 | 12.7 → 22.1 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `MALNUTRITION` | Cross | 26 → 39 | 1.68 → 2.12 | 6.4 → 21.5 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `MANUFACTURING MATERIALS ISSUE` | Cross | 2 → 3 | 4.11 → 5.24 | 2.0 → 6.1 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `MENOMETRORRHAGIA` | Cross | 5 → 6 | 2.70 → 2.71 | 3.7 → 4.7 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `MERYCISM` | Cross | 2 → 3 | 5.84 → 7.51 | 3.7 → 10.2 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `MICROPENIS` | Cross | 2 → 3 | 24.68 → 34.75 | 20.2 → 48.9 | $a \ge 3$ |
| **SERTRALINE** | `MIGRAINE` | Cross | 258 → 326 | 1.94 → 2.06 | 115.5 → 173.0 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `MILIARIA` | Cross | 6 → 8 | 2.25 → 2.52 | 2.9 → 5.7 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `MORBID THOUGHTS` | Cross | 3 → 4 | 3.74 → 4.21 | 3.5 → 6.5 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `NASAL CAVITY CANCER` | Cross | 2 → 3 | 5.29 → 6.78 | 3.2 → 8.9 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `NASAL VESTIBULITIS` | Cross | 1 → 4 | 5.55 → 21.80 | 0.5 → 48.4 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `NORMAL PRESSURE HYDROCEPHALUS` | Cross | 2 → 3 | 6.35 → 8.18 | 4.2 → 11.3 | $a \ge 3$ |
| **SERTRALINE** | `NORMOCYTIC ANAEMIA` | Cross | 7 → 14 | 2.12 → 3.61 | 3.0 → 23.0 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `ORAL INFECTION` | Cross | 9 → 13 | 2.07 → 2.51 | 3.9 → 10.1 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `OTOLITHIASIS` | Cross | 0 → 3 | 0.00 → 18.53 | 0.0 → 28.0 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `OVARIAN CYST RUPTURED` | Cross | 2 → 6 | 1.48 → 3.81 | 0.0 → 9.4 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `PAEDIATRIC ACUTE-ONSET NEUROPSYCHIATRIC SYNDROME` | Cross | 2 → 3 | 10.10 → 13.24 | 7.8 → 19.9 | $a \ge 3$ |
| **SERTRALINE** | `PALLOR` | Cross | 46 → 65 | 1.75 → 2.08 | 13.9 → 34.6 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `PAROTITIS` | Cross | 4 → 7 | 2.50 → 3.71 | 2.2 → 10.8 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `PELVIC ADHESIONS` | Cross | 1 → 4 | 5.05 → 19.51 | 0.4 → 43.6 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `PEPTIC ULCER HAEMORRHAGE` | Cross | 0 → 7 | 0.00 → 9.83 | 0.0 → 42.4 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `PERSONAL RELATIONSHIP ISSUE` | Cross | 1 → 3 | 2.18 → 5.67 | 0.0 → 6.9 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `PHARYNGEAL ERYTHEMA` | Cross | 2 → 6 | 1.13 → 2.88 | 0.0 → 5.4 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `PHOBIA` | Cross | 1 → 4 | 1.82 → 6.39 | 0.0 → 12.3 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `PNEUMONIA RESPIRATORY SYNCYTIAL VIRAL` | Cross | 4 → 6 | 2.33 → 2.94 | 1.8 → 5.7 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `POOR VENOUS ACCESS` | Cross | 47 → 59 | 1.97 → 2.07 | 21.2 → 31.1 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `POST-ACUTE COVID-19 SYNDROME` | Cross | 10 → 13 | 1.97 → 2.15 | 3.8 → 6.7 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `POSTNATAL GROWTH RESTRICTION` | Cross | 1 → 5 | 1.46 → 6.43 | 0.0 → 16.6 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `POSTPARTUM HAEMORRHAGE` | Cross | 2 → 7 | 1.06 → 3.16 | 0.0 → 8.0 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `PREMATURE SEPARATION OF PLACENTA` | Cross | 4 → 7 | 2.66 → 3.95 | 2.6 → 12.1 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `PRODUCT COMPOUNDING QUALITY ISSUE` | Cross | 2 → 3 | 6.35 → 8.18 | 4.2 → 11.3 | $a \ge 3$ |
| **SERTRALINE** | `PULMONARY ARTERIAL PRESSURE ABNORMAL` | Cross | 2 → 3 | 5.84 → 7.51 | 3.7 → 10.2 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `PULMONARY HYPERTENSION` | Cross | 36 → 51 | 1.84 → 2.19 | 12.8 → 31.1 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `RETINAL INFARCTION` | Cross | 2 → 3 | 20.19 → 27.80 | 16.7 → 40.6 | $a \ge 3$ |
| **SERTRALINE** | `REVERSIBLE AIRWAYS OBSTRUCTION` | Cross | 2 → 4 | 3.53 → 6.08 | 1.5 → 11.5 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `ROTATOR CUFF TEAR ARTHROPATHY` | Cross | 0 → 6 | 0.00 → 111.19 | 0.0 → 249.3 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `SCHIZOAFFECTIVE DISORDER BIPOLAR TYPE` | Cross | 1 → 3 | 2.64 → 6.95 | 0.0 → 9.2 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `SEASONAL AFFECTIVE DISORDER` | Cross | 1 → 4 | 2.78 → 10.02 | 0.0 → 21.7 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `SINUS NODE DYSFUNCTION` | Cross | 2 → 8 | 0.74 → 2.51 | 0.0 → 5.7 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `SINUS PAIN` | Cross | 8 → 10 | 1.92 → 2.01 | 2.6 → 4.0 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `SJOGREN'S SYNDROME` | Cross | 6 → 19 | 0.81 → 2.16 | 0.1 → 10.5 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `SKIN WARM` | Cross | 12 → 30 | 1.75 → 3.75 | 3.1 → 55.4 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `SLEEP ATTACKS` | Cross | 2 → 3 | 4.44 → 5.67 | 2.3 → 6.9 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `SLEEP INERTIA` | Cross | 1 → 3 | 2.85 → 7.51 | 0.1 → 10.2 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `SLEEVE GASTRECTOMY` | Cross | 3 → 6 | 2.35 → 4.00 | 1.1 → 10.2 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `SLUGGISHNESS` | Cross | 19 → 31 | 1.77 → 2.44 | 5.6 → 24.3 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `SMALL AIRWAYS DISEASE` | Cross | 1 → 4 | 4.27 → 16.11 | 0.3 → 36.2 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `SOCIOECONOMIC PRECARITY` | Cross | 2 → 5 | 5.29 → 11.88 | 3.2 → 34.9 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `SOFT TISSUE SWELLING` | Cross | 2 → 4 | 2.34 → 3.99 | 0.5 → 5.9 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `SOLILOQUY` | Cross | 3 → 6 | 2.33 → 3.97 | 1.1 → 10.1 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `SPIDER VEIN` | Cross | 3 → 5 | 3.79 → 5.39 | 3.5 → 13.0 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `SPLINTER` | Cross | 2 → 3 | 7.40 → 9.59 | 5.2 → 13.8 | $a \ge 3$ |
| **SERTRALINE** | `STRESS` | Cross | 186 → 255 | 1.80 → 2.07 | 64.7 → 138.0 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `STRIDOR` | Cross | 4 → 8 | 1.33 → 2.25 | 0.1 → 4.3 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `SUBCHORIONIC HAEMORRHAGE` | Cross | 2 → 3 | 11.11 → 14.63 | 8.7 → 22.1 | $a \ge 3$ |
| **SERTRALINE** | `TENDERNESS` | Cross | 14 → 29 | 1.25 → 2.18 | 0.5 → 17.0 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `TENDINOUS CONTRACTURE` | Cross | 0 → 6 | 0.00 → 34.75 | 0.0 → 119.3 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `TESTICULAR PAIN` | Cross | 8 → 10 | 2.03 → 2.13 | 3.2 → 4.8 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `TETANY` | Cross | 3 → 7 | 1.51 → 3.00 | 0.1 → 7.2 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `THYROID CALCIFICATION` | Cross | 0 → 3 | 0.00 → 14.63 | 0.0 → 22.1 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `TOE AMPUTATION` | Cross | 4 → 12 | 1.26 → 3.23 | 0.0 → 15.8 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `TONGUE MOVEMENT DISTURBANCE` | Cross | 2 → 5 | 2.04 → 4.37 | 0.3 → 9.4 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `TOOTH ABSCESS` | Cross | 19 → 29 | 1.87 → 2.40 | 6.7 → 21.8 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `TOOTH INFECTION` | Cross | 31 → 44 | 1.77 → 2.11 | 9.5 → 24.1 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `TOOTH RESTORATION` | Cross | 2 → 3 | 6.73 → 8.69 | 4.6 → 12.2 | $a \ge 3$ |
| **SERTRALINE** | `TUBO-OVARIAN ABSCESS` | Cross | 1 → 3 | 2.31 → 6.04 | 0.0 → 7.6 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `TWIN PREGNANCY` | Cross | 2 → 4 | 3.97 → 6.86 | 1.9 → 13.5 | $a \ge 3$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `UPPER RESPIRATORY TRACT INFECTION` | Cross | 119 → 163 | 1.82 → 2.09 | 42.3 → 89.7 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `VASCULAR DEMENTIA` | Cross | 1 → 4 | 1.21 → 4.16 | 0.0 → 6.4 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `VASCULAR DEVICE INFECTION` | Cross | 23 → 33 | 1.74 → 2.10 | 6.4 → 17.5 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `VASCULAR DEVICE OCCLUSION` | Cross | 1 → 5 | 0.93 → 4.03 | 0.0 → 8.2 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `VENTRICULAR DYSKINESIA` | Cross | 0 → 3 | 0.00 → 18.53 | 0.0 → 28.0 | $a \ge 3$, $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `VITAMIN D DECREASED` | Cross | 18 → 26 | 1.90 → 2.31 | 6.7 → 17.6 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `VITAMIN D INCREASED` | Cross | 3 → 5 | 2.82 → 3.99 | 1.9 → 8.1 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `VITREOUS DETACHMENT` | Cross | 5 → 9 | 1.92 → 2.93 | 1.4 → 9.3 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `VOCAL CORD PARALYSIS` | Cross | 3 → 6 | 1.58 → 2.67 | 0.2 → 4.6 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `VULVOVAGINAL DISCOMFORT` | Cross | 8 → 10 | 1.93 → 2.02 | 2.6 → 4.1 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `VULVOVAGINAL MYCOTIC INFECTION` | Cross | 8 → 13 | 2.19 → 3.01 | 4.0 → 15.0 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `WEIGHT ABNORMAL` | Cross | 7 → 10 | 1.74 → 2.09 | 1.5 → 4.5 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `WRIST FRACTURE` | Cross | 23 → 34 | 1.64 → 2.04 | 5.1 → 16.6 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `ACUTE MYOCARDIAL INFARCTION` | Drop | 48 → 54 | 2.11 → 1.99 | 26.6 → 25.0 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `ALANINE AMINOTRANSFERASE INCREASED` | Drop | 123 → 137 | 2.01 → 1.87 | 60.3 → 53.6 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `ASPHYXIA` | Drop | 22 → 22 | 2.19 → 1.83 | 12.8 → 7.3 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `BENIGN PROSTATIC HYPERPLASIA` | Drop | 10 → 11 | 2.02 → 1.85 | 4.1 → 3.4 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `BEZOAR` | Drop | 7 → 7 | 2.48 → 2.07 | 4.7 → 2.8 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `BLOOD CREATINE PHOSPHOKINASE INCREASED` | Drop | 55 → 58 | 2.18 → 1.92 | 33.5 → 24.3 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `BLOOD IMMUNOGLOBULIN E INCREASED` | Drop | 9 → 9 | 2.23 → 1.86 | 4.8 → 2.7 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `BUNDLE BRANCH BLOCK RIGHT` | Drop | 9 → 9 | 2.49 → 2.08 | 6.5 → 3.9 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `CARDIAC DYSFUNCTION` | Drop | 18 → 18 | 2.06 → 1.72 | 8.6 → 4.6 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `CARDIAC FLUTTER` | Drop | 16 → 17 | 2.21 → 1.96 | 9.2 → 6.9 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `CARDIAC INFECTION` | Drop | 3 → 3 | 4.22 → 3.52 | 4.3 → 3.0 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `CEREBRAL MASS EFFECT` | Drop | 4 → 4 | 3.64 → 3.04 | 5.1 → 3.5 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `CIRCULATORY COLLAPSE` | Drop | 28 → 30 | 2.08 → 1.86 | 14.3 → 10.9 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `CLOSTRIDIAL INFECTION` | Drop | 4 → 4 | 3.31 → 2.77 | 4.2 → 2.8 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `CLOSTRIDIUM DIFFICILE COLITIS` | Drop | 28 → 30 | 2.17 → 1.94 | 16.1 → 12.4 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `COLITIS ISCHAEMIC` | Drop | 15 → 16 | 2.10 → 1.87 | 7.5 → 5.6 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `DERMATITIS` | Drop | 75 → 77 | 2.22 → 1.90 | 48.2 → 31.5 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `DYSGRAPHIA` | Drop | 14 → 16 | 2.07 → 1.98 | 6.6 → 6.7 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `EAR INFLAMMATION` | Drop | 4 → 4 | 3.42 → 2.85 | 4.5 → 3.0 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `EAR PRURITUS` | Drop | 16 → 16 | 2.10 → 1.75 | 8.0 → 4.3 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `EXCESSIVE CERUMEN PRODUCTION` | Drop | 4 → 4 | 3.64 → 3.04 | 5.1 → 3.5 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `FLUID IMBALANCE` | Drop | 3 → 3 | 4.69 → 3.92 | 5.2 → 3.7 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `GAMMA-GLUTAMYLTRANSFERASE INCREASED` | Drop | 36 → 39 | 2.00 → 1.82 | 16.8 → 13.2 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `GASTRITIS` | Drop | 58 → 66 | 2.09 → 1.99 | 31.4 → 30.9 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `GENERALISED TONIC-CLONIC SEIZURE` | Drop | 54 → 60 | 2.13 → 1.98 | 30.5 → 27.4 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `HAEMORRHAGE IN PREGNANCY` | Drop | 5 → 5 | 3.23 → 2.69 | 5.4 → 3.6 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `HEAT EXHAUSTION` | Drop | 4 → 4 | 3.80 → 3.17 | 5.5 → 3.8 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `HEPATITIS ACUTE` | Drop | 17 → 17 | 2.30 → 1.92 | 11.0 → 6.5 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `HERNIA PAIN` | Drop | 3 → 3 | 4.63 → 3.86 | 5.1 → 3.6 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `HORMONE LEVEL ABNORMAL` | Drop | 12 → 13 | 2.02 → 1.83 | 5.1 → 4.0 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `INTERVERTEBRAL DISC DEGENERATION` | Drop | 15 → 15 | 2.14 → 1.78 | 7.8 → 4.3 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `INTRACRANIAL PRESSURE INCREASED` | Drop | 14 → 14 | 2.12 → 1.77 | 7.1 → 3.9 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `LARYNGEAL DISORDER` | Drop | 3 → 3 | 4.76 → 3.97 | 5.3 → 3.8 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `LIGAMENT DISORDER` | Drop | 3 → 3 | 4.06 → 3.39 | 4.0 → 2.8 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `LUNG DISORDER` | Drop | 151 → 165 | 2.12 → 1.93 | 86.3 → 71.9 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `METABOLIC DYSFUNCTION-ASSOCIATED LIVER DISEASE` | Drop | 4 → 4 | 3.76 → 3.14 | 5.4 → 3.7 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `MOTOR DYSFUNCTION` | Drop | 26 → 29 | 2.09 → 1.95 | 13.5 → 12.3 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `OESOPHAGEAL DISORDER` | Drop | 8 → 8 | 2.25 → 1.88 | 4.3 → 2.4 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `PERIORBITAL OEDEMA` | Drop | 10 → 10 | 2.01 → 1.68 | 4.0 → 2.1 | $\text{PRR} \ge 2.0$, $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `PERIPHERAL SENSORY NEUROPATHY` | Drop | 20 → 20 | 2.22 → 1.86 | 12.0 → 6.9 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `PERITONSILLAR ABSCESS` | Drop | 4 → 4 | 3.37 → 2.81 | 4.3 → 2.9 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `PRESBYOPIA` | Drop | 3 → 3 | 4.44 → 3.71 | 4.7 → 3.4 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `SEPSIS` | Drop | 259 → 293 | 2.05 → 1.94 | 136.1 → 130.0 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `STUPOR` | Drop | 7 → 7 | 2.80 → 2.33 | 6.2 → 4.0 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `THROMBOSIS` | Drop | 174 → 182 | 2.14 → 1.87 | 103.0 → 71.6 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `TORSADE DE POINTES` | Drop | 15 → 16 | 2.06 → 1.83 | 7.0 → 5.2 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `UHTHOFF'S PHENOMENON` | Drop | 3 → 3 | 4.63 → 3.86 | 5.1 → 3.6 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `UNMASKING OF PREVIOUSLY UNIDENTIFIED DISEASE` | Drop | 6 → 6 | 2.87 → 2.40 | 5.4 → 3.5 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `URINE ODOUR ABNORMAL` | Drop | 13 → 14 | 2.14 → 1.93 | 6.7 → 5.2 | $\text{PRR} \ge 2.0$ |
| **SERTRALINE** | `VESTIBULAR MIGRAINE` | Drop | 3 → 3 | 4.63 → 3.86 | 5.1 → 3.6 | $\chi^2 \ge 4.0$ |
| **SERTRALINE** | `VITREOUS FLOATERS` | Drop | 31 → 35 | 2.03 → 1.92 | 14.9 → 14.1 | $\text{PRR} \ge 2.0$ |
