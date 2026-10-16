# Migration 004 — Operator Runbook

**Migration ID:** `20261015_focus_drug_aliases`  ·  **Package:** `docs/migrations/004_focus_drug_brand_aliases/`
**Effect:** renames 7 brand strings in `case_drugs.drug_normalized` to their canonical
ingredient (31,098 rows across all report versions), fully audited and reversible.
**Downstream impact:** every derived signal is stale the moment the forward transaction
commits — recomputation (§5) is part of the same window, not a follow-up task.

All commands run from the repository root with the project environment loaded:

```bash
. ./.env          # provides DATABASE_URL — never echo or paste its value
```

Client tools verified on this host: PostgreSQL 14.17 (`psql`, `pg_dump`, `pg_restore`).

> **Scope note:** migration 004 is *not* in `migrations/`, so `scripts/migrate.py` never
> applies it and `schema_migrations` stays at **4**. It is applied by an operator, once,
> from this runbook. Do not add it to `migrate.py`.

> **Measured window timings** (full-size rehearsal on a restored production copy,
> 2026-10-16, `pharmasignal_staging`): dump **58 s** (689 MB) · restore **~87 min** ·
> §3 snapshot **18 s** · forward **45 s** · `--diff` **24 s** · recompute
> `--mode both` **~20 min** · concordance **~20 s** · confounding **~2 min** ·
> quality **~20 s** · verifier **<5 s** · rollback **26 s** + baseline recompute
> **~19 min**. Budget the window at **25–30 min** (forward + recompute + verification);
> the ~87 min restore belongs to the §1 rehearsal and must never sit inside the window.

---

## 0. Go / no-go — every box must be checked before the window opens

- [ ] Package reviewed and approved; test suite green
      (`pytest -q` → exit 0 — includes the SQL-contract, snapshot/diff and runbook-coverage
      guards, plus a forward → postflight → rollback rehearsal executed on a scratch
      database, guards included, so §4/§6 are known to run end-to-end before you open the window).
- [ ] **Backup taken and restore rehearsal completed** (§1) — a backup that has never been
      restored is not a backup. Restore duration recorded and budgeted.
- [ ] Quiesced window scheduled with the API/ingestion owners (§2).
- [ ] Signal recomputation scheduled to start **immediately** after the forward run (§5).
- [ ] Database state sanity (read-only, values as of the review; refresh at execution time):

  ```bash
  psql "$DATABASE_URL" -X -c "SELECT count(*) AS brand_rows FROM case_drugs
        WHERE drug_normalized IN ('LIPITOR','GLUCOPHAGE','GLUCOPHAGE XR','ADVIL','MOTRIN','PRILOSEC','ZOLOFT');" \
       -c "SELECT count(*) AS null_seq FROM case_drugs WHERE drug_seq IS NULL" \
       -c "SELECT count(*) AS active_jobs FROM refresh_jobs WHERE status IN ('queued','running')" \
       -c "SELECT count(*) AS prior_snapshots FROM information_schema.tables
            WHERE table_name='case_drugs_before_image'"
  ```

  Expected: `brand_rows` = **31,098**, `null_seq` = **0**, `active_jobs` = **0**,
  `prior_snapshots` = **0** (table absent → guard 1 passes trivially).

---

## 1. Backup and restore rehearsal (before the window, not during it)

```bash
STAMP=$(date +%Y%m%dT%H%M%S)
mkdir -p backup
pg_dump --format=custom --no-owner --file="backup/pharmasignal-${STAMP}.dump" "$DATABASE_URL"
pg_restore --list "backup/pharmasignal-${STAMP}.dump" | head -5     # archive is readable
```

Restore rehearsal into a scratch database — this is the only proof the dump restores:

```bash
createdb pharmasignal_restore_rehearsal
time pg_restore --no-owner --dbname=pharmasignal_restore_rehearsal "backup/pharmasignal-${STAMP}.dump"

psql -X postgresql://localhost:5432/pharmasignal_restore_rehearsal \
     -c "SELECT count(*) AS brand_rows FROM case_drugs
          WHERE drug_normalized IN ('LIPITOR','GLUCOPHAGE','GLUCOPHAGE XR','ADVIL','MOTRIN','PRILOSEC','ZOLOFT');" \
     -c "SELECT count(*) AS current_cases FROM cases WHERE is_current" \
     -c "SELECT count(*) AS signal_scores FROM signal_scores"
```

Rehearsal acceptance (record all three numbers in the evidence log):

| Check | Value at review time |
| :--- | :--- |
| brand rows | 31,098 |
| current cases | 2,831,775 |
| `signal_scores` rows | 2,139,259 |

Use the rehearsal database to rehearse §4/§5 timing end-to-end (forward → diff →
recompute) and record how long the recompute takes; that measured duration sets the
window length. This was done at full size on 2026-10-16 — restore ~87 min, forward
45 s, `--diff` 24 s, recompute ~20 min (timings at the top of this runbook); re-measure
in every rehearsal rather than reusing the numbers blindly. Keep the dump until §7
passes; drop the scratch database only after the window closes.

---

## 2. Quiesce (window opens)

Migration 004 touches only `case_drugs` (row-level locks), so **readers are never
blocked**. Writers are the problem: nothing may ingest, refresh or recompute while the
forward transaction and the recomputation are in flight.

- [ ] Stop the API process (`scripts/run_api.py`). It is read-only, but leaving it up
      invites an admin-triggered refresh (`POST /api/admin/refresh`, guarded by
      `ADMIN_API_KEY`) and it will serve half-rebuilt `signal_scores` during §5.
- [ ] Stop any ingestion (`scripts/run_ingestion.py`) and quarterly refresh
      (`scripts/refresh_quarter.py`) processes.
- [ ] Confirm nothing is still writing:

  ```bash
  psql "$DATABASE_URL" -X -c "
    SELECT pid, state, wait_event_type, wait_event, left(query, 80) AS query
    FROM pg_stat_activity
    WHERE datname = current_database() AND pid <> pg_backend_pid()
      AND state <> 'idle' ORDER BY query_start;"
  ```

  Expected: no active queries against `case_drugs`, `cases`, `case_events`,
  `signal_scores` or `refresh_jobs` other than your own session.

---

## 3. Baseline snapshot (immediately before the forward run)

Records every figure §7 depends on; after execution it becomes the proof that only the
expected rows moved (§4).

```bash
.venv/bin/python scripts/report_figures.py --snapshot --force
# [report-figures] baseline snapshot written: docs/migrations/004_focus_drug_brand_aliases/figures_baseline_snapshot.json
#   (<timestamp>, N=2,831,775, 15 pair rows)
```

- [ ] File written, exit 0. Keep it — do not edit or delete it.
- **`--force` is required:** the package already ships a baseline snapshot, and without
  it `--snapshot` exits 2 having written nothing. That refusal is the guard against
  silently reusing a stale baseline — re-taking it inside the window is the correct
  action, not an error to work around. Never skip §3 just because a file exists; an
  outdated baseline turns the §4 proof into noise.
- Take it with no ingestion in flight, and do not re-run §3 after `forward.sql`: a
  rename must not change the case population, so background N and every event marginal
  must hold exactly for §4's proof to mean anything.
- [ ] Sanity: the fresh snapshot reports `N=2,831,775` and brand counts totalling
      31,098 (§0). If not, you are on the wrong database — stop.

---

## 4. Preflight → forward → postflight

```bash
MIG=docs/migrations/004_focus_drug_brand_aliases

psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -X -f "$MIG/preflight_inspection.sql"
```

- [ ] Candidate counts match §7.1 (LIPITOR 11,712 · ADVIL 5,225 · ZOLOFT 6,651 ·
      PRILOSEC 3,890 · GLUCOPHAGE 1,745 · MOTRIN 1,447 · GLUCOPHAGE XR 428; total 31,098),
      `null_seq_brand_rows` = 0, and the before-image notice says the table does not exist
      yet (nothing previously applied).

```bash
time psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -X -f "$MIG/forward.sql"
```

- [ ] Exit 0 and the closing notice:
      `Migration 004 Postflight PASSED: Consolidated 31098 rows. Zero brand-normalized rows remain. drug_original preserved.`

**What the transaction does and why it is safe:** one `BEGIN … COMMIT` containing both
the before-image snapshot and the `UPDATE`, so the audit trail and the change commit
together or not at all; three guards abort on double-execution, on unique-index
collision risk, and on any postflight mismatch. The `UPDATE` takes row-level locks on
31,098 rows only — readers continue, and an abort leaves the table byte-identical to
before. Expected duration: ~45 s at full size (measured in the 2026-10-16 rehearsal); if
it runs beyond a few minutes, treat it as a surprise and investigate rather than waiting
it out.

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -X -f "$MIG/postflight_inspection.sql"
.venv/bin/python scripts/report_figures.py --diff
```

- [ ] Postflight: zero remaining brand rows; snapshot rows = **31,098** with the
      per-brand breakdown matching §7.1; `corrupted_original_count` = **0**.
- [ ] `--diff` exits 0 with
      `PASS: only the expected rows moved (N moved, M held)`, `state=post-execution`.
      This is the load-bearing verification: brands → 0, each target's generic count at
      its projected value, every event marginal unchanged, every projected row
      bit-identical, and no mixture of moved and unmoved elements.

---

## 5. Immediate signal recomputation (same window — mandatory)

`signal_scores` still holds **2,139,259** pre-migration contingency tables; until this
step completes, every published signal uses the old denominators.

```bash
.venv/bin/python scripts/compute_signals.py --mode both --min-cases 3   # longest step: ~20 min measured (2026-10-16 full-size rehearsal)
.venv/bin/python scripts/validate_concordance.py
.venv/bin/python scripts/probe_confounding.py
.venv/bin/python scripts/summarize_quality.py
```

Post-recompute acceptance — the §7.3 projections must have become reality.
No hand-run SQL: the verifier reads the §3 baseline snapshot (the same artifact
`--diff` uses), asserts the five projected pairs' stored cells equal the
projection, that **no** `signal_scores` row of any period or mode names a
retired brand (`brand_scores = 0`), and that those five rows agree with cells
recomputed from the live tables, so a row left stale by a partial recompute
cannot pass. Exit 0 = acceptance met, 1 = failed (copy the stderr into the
evidence log), 2 = could not run.

```bash
.venv/bin/python scripts/verify_migration004.py
# [verify-004] PASS: 5/5 projected pairs match, brand_scores = 0, no stale rows — §5 acceptance met
```

- [ ] The five pairs equal the §7.3 *Projected* rows exactly
      (929/73,476/4,114/2,753,256 · 192/33,574/1,844/2,796,165 · 3,522/51,515/1,838/2,774,900 ·
      457/46,869/1,810/2,782,639 · 462/29,773/2,829/2,798,711).
- [ ] `brand_scores` = **0** (no score references a retired brand name). It is
      **7,398** today, so this check is a real before/after gate, not a formality.
- [ ] §7.5's projected screening totals became real: **7,949** focus-drug signals pass
      the PRR screen (it is **7,378** today — 830 crossings, 259 drops, enumerated per
      event in §7.5). A mismatch means a projection assumption failed: stop and
      investigate before publishing anything.

  ```bash
  psql "$DATABASE_URL" -X -c "
    SELECT count(*) AS focus_screened FROM signal_scores
    WHERE period='all' AND NOT primary_suspect_mode AND prr_screen
      AND drug IN ('ATORVASTATIN','METFORMIN','IBUPROFEN','OMEPRAZOLE','SERTRALINE');"
  ```
- [ ] Concordance, confounding and quality reports regenerated without error.

**Not refreshed by design:** the frozen `signal_scores_q1only` snapshot table. Regenerate
it deliberately only if a consolidated Q1-only drift baseline is wanted; otherwise leave
it as the pre-migration reference and say so in the ticket.

---

## 6. If anything fails — rollback

```bash
MIG=docs/migrations/004_focus_drug_brand_aliases   # set again: §6 is often run from a fresh shell
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -X -f "$MIG/rollback.sql"   # restores from the before-image only
.venv/bin/python scripts/compute_signals.py --mode both --min-cases 3   # restore baseline scores
.venv/bin/python scripts/report_figures.py --diff     # must return to: state=baseline, moved-as-expected=0
```

- Rollback restores the exact pre-migration strings from `case_drugs_before_image`
  (never from `drug_original`), stamps `restored_at`, and refuses to run twice or against
  rows altered since the forward run.
- Measured at full size (2026-10-16): rollback **~26 s**, baseline recompute **~19 min**,
  final `--diff` **~10 s** — a rollback window is about as long as the forward window.
- `scripts/verify_migration004.py` (§5) **fails by design after a rollback** — the pairs
  are back at baseline and `brand_scores` is 7,398 again. §6's proof that the window
  returned to baseline is `report_figures.py --diff` reporting `state=baseline`, not the
  §5 verifier.
- **Never re-ingest FAERS files to undo this migration** — ingestion cannot reproduce the
  pre-migration state of already-loaded quarters.
- If rollback aborts with "rows no longer match expected post-migration value", stop and
  escalate: something outside the window touched `case_drugs`. Do not force it; the
  before-image still holds the recovery values.
- **Re-attempting the forward run after a rollback:** `forward.sql`'s first guard keys on
  the *presence* of before-image rows, not on `restored_at`, so a second forward attempt
  aborts with `Before-image snapshot ... already exists` even though the data is back at
  baseline (verified — the scratch rehearsal asserts it). Clearing it means moving the
  spent snapshot out of the guard's way **without losing the audit trail**; only with
  operator approval, and only after re-passing §0:

  ```bash
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -X -c "
    CREATE TABLE case_drugs_before_image_spent AS
      SELECT * FROM case_drugs_before_image
      WHERE migration_id = '20261015_focus_drug_aliases';
    DELETE FROM case_drugs_before_image
      WHERE migration_id = '20261015_focus_drug_aliases';"
  ```

  Record both statements in the evidence log before running them. If you would rather not
  touch the audit table at all, restore the §1 dump instead — it is byte-equivalent to the
  rolled-back state — and end the window without retrying.

---

## 7. Close the window

- [ ] Resume the API and any ingestion/refresh processes.
- [ ] `--diff` last state recorded (`post-execution` after §5, or `baseline` after §6).
- [ ] Archive: the `pg_dump` file, `figures_baseline_snapshot.json`, and the evidence log
      below, in the ticket.
- [ ] Note for future runs: `report_figures.py --check` compares §7 against the *live*
      database and is a **pre-execution** tool. After execution its §7.1/§7.2 brand-side
      figures legitimately show zero and §7.5's *Before* columns describe a state that
      no longer exists; `--diff` (baseline vs current) is the post-execution
      verification tool from here on. `--snapshot` and `--diff` skip §7.5's expensive
      query on purpose, so the window stays fast.

---

## Evidence log (copy into the ticket)

| # | Step | Command / artifact | Expected | Observed | Operator | Time |
| :-- | :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | Backup | `pg_dump -Fc` file name + size | dump created | | | |
| 2 | Restore rehearsal | restore duration + 3 counts | counts match §1 table | | | |
| 3 | Quiesce | `pg_stat_activity` snapshot | no writers | | | |
| 4 | Snapshot | `figures_baseline_snapshot.json` + `created_at` | exit 0 | | | |
| 5 | Preflight | candidate rows | 31,098 / null_seq 0 | | | |
| 6 | Forward | duration + closing NOTICE | `Consolidated 31098 rows` | | | |
| 7 | Postflight | snapshot rows / corrupted originals | 31,098 / 0 | | | |
| 8 | Diff | `--diff` exit code + summary | exit 0, post-execution | | | |
| 9 | Recompute | `compute_signals --mode both` duration | completed | | | |
| 10 | Score check | `verify_migration004.py` exit code + summary | exit 0 | | | |
| 11 | Reports | concordance / confounding / quality | regenerated | | | |
