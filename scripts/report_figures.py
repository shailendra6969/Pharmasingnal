#!/usr/bin/env python3
"""Regenerate or verify the figures in the migration 004 impact report.

The report (docs/migrations/004-focus-drug-alias-migration.md, §7.1–§7.3) states
measured and projected figures. Every one of them is derived here from the
database with read-only SQL, so the prose can never silently drift from the data:

    python scripts/report_figures.py              # --check: verify vs. database
    python scripts/report_figures.py --write      # rewrite drifted table rows
    python scripts/report_figures.py --json       # dump figures as JSON
    python scripts/report_figures.py --report path/to/other.md
    python scripts/report_figures.py --snapshot   # record baseline figures (before execution)
    python scripts/report_figures.py --diff       # prove only expected rows moved (after)

Snapshot workflow around a migration execution:

    1. --snapshot            immediately before forward.sql  (exit 0, writes JSON)
    2. execute forward.sql + recompute signals
    3. --diff                proves only the rows consolidation itself can move
                             actually moved; exit 1 on any unexpected movement

Take the snapshot with no ingestion in between: a rename must not change the
case population, so background N and every event marginal must hold exactly.

Statistics come from app.statistics.disproportionality.compute() — the module
the spec designates as the authoritative source of PRR/ROR numbers — and the
projected 2x2 tables must conserve the background N (a+b+c+d == current cases).

Exit codes: 0 report matches the database (or was written), 1 drift detected
in --check mode or unexpected movement in --diff, 2 execution error.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings                      # noqa: E402
from app.repositories import loader                  # noqa: E402
from app.statistics.disproportionality import (      # noqa: E402
    Table2x2,
    compute,
)

DEFAULT_REPORT = (
    Path(__file__).resolve().parents[1]
    / "docs" / "migrations" / "004-focus-drug-alias-migration.md"
)

# Baseline snapshot recorded with --snapshot and verified with --diff.
DEFAULT_SNAPSHOT = (
    Path(__file__).resolve().parents[1]
    / "docs" / "migrations" / "004_focus_drug_brand_aliases"
    / "figures_baseline_snapshot.json"
)
SNAPSHOT_SCHEMA = 1

# Brand aliases consolidated by migration 004 (report §7.1/§7.2 order).
@dataclass(frozen=True)
class FocusTarget:
    generic: str
    brands: tuple[str, ...]
    display: str          # parenthesised label used in §7.2


FOCUS: tuple[FocusTarget, ...] = (
    FocusTarget("ATORVASTATIN", ("LIPITOR",), "Lipitor"),
    FocusTarget("METFORMIN", ("GLUCOPHAGE", "GLUCOPHAGE XR"), "Glucophage, Glucophage XR"),
    FocusTarget("IBUPROFEN", ("ADVIL", "MOTRIN"), "Advil, Motrin"),
    FocusTarget("OMEPRAZOLE", ("PRILOSEC",), "Prilosec"),
    FocusTarget("SERTRALINE", ("ZOLOFT",), "Zoloft"),
)

ALL_BRANDS = tuple(b for t in FOCUS for b in t.brands)

# §7.3 rows: (first-column label, source cell, drug set, event term).
# Drug sets are pre-migration names; for projections that is exactly the
# post-migration union, since consolidation only renames rows.
BASELINE_SRC = "Existing Baseline `[Independently recomputed]`"
PROJECTED_SRC = "Projected `[Projected]`"
PAIR_ROWS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("**ATORVASTATIN / RHABDOMYOLYSIS**", BASELINE_SRC,
     ("ATORVASTATIN",), "RHABDOMYOLYSIS"),
    ("**LIPITOR / RHABDOMYOLYSIS**", BASELINE_SRC,
     ("LIPITOR",), "RHABDOMYOLYSIS"),
    ("**ATORVASTATIN (Consolidated) / RHABDOMYOLYSIS**", PROJECTED_SRC,
     ("ATORVASTATIN", "LIPITOR"), "RHABDOMYOLYSIS"),
    ("**IBUPROFEN / GASTRIC ULCER**", BASELINE_SRC,
     ("IBUPROFEN",), "GASTRIC ULCER"),
    ("**ADVIL / GASTRIC ULCER**", BASELINE_SRC,
     ("ADVIL",), "GASTRIC ULCER"),
    ("**IBUPROFEN (Consolidated) / GASTRIC ULCER**", PROJECTED_SRC,
     ("IBUPROFEN", "ADVIL", "MOTRIN"), "GASTRIC ULCER"),
    # --- extended targets: label-canonical representative events ---
    ("**METFORMIN / LACTIC ACIDOSIS**", BASELINE_SRC,
     ("METFORMIN",), "LACTIC ACIDOSIS"),
    ("**GLUCOPHAGE, GLUCOPHAGE XR / LACTIC ACIDOSIS**", BASELINE_SRC,
     ("GLUCOPHAGE", "GLUCOPHAGE XR"), "LACTIC ACIDOSIS"),
    ("**METFORMIN (Consolidated) / LACTIC ACIDOSIS**", PROJECTED_SRC,
     ("METFORMIN", "GLUCOPHAGE", "GLUCOPHAGE XR"), "LACTIC ACIDOSIS"),
    ("**OMEPRAZOLE / HYPOMAGNESAEMIA**", BASELINE_SRC,
     ("OMEPRAZOLE",), "HYPOMAGNESAEMIA"),
    ("**PRILOSEC / HYPOMAGNESAEMIA**", BASELINE_SRC,
     ("PRILOSEC",), "HYPOMAGNESAEMIA"),
    ("**OMEPRAZOLE (Consolidated) / HYPOMAGNESAEMIA**", PROJECTED_SRC,
     ("OMEPRAZOLE", "PRILOSEC"), "HYPOMAGNESAEMIA"),
    ("**SERTRALINE / SEROTONIN SYNDROME**", BASELINE_SRC,
     ("SERTRALINE",), "SEROTONIN SYNDROME"),
    ("**ZOLOFT / SEROTONIN SYNDROME**", BASELINE_SRC,
     ("ZOLOFT",), "SEROTONIN SYNDROME"),
    ("**SERTRALINE (Consolidated) / SEROTONIN SYNDROME**", PROJECTED_SRC,
     ("SERTRALINE", "ZOLOFT"), "SEROTONIN SYNDROME"),
)

HEADER_71 = ("| Brand Name | Target Canonical Ingredient | "
             "Exact Row Count in `case_drugs` |")
HEADER_72 = ("| Focus Target | Generic Cases `[Measured]` | "
             "Brand Cases (Distinct Union) `[Measured]` | "
             "Co-reported with Generic `[Measured]` | "
             "Cases with Both Brands `[Measured]` | "
             "Net-New Brand Cases `[Measured]` | "
             "Post-Consolidation Unique Cases `[Projected]` | "
             "Denominator Increase `[Projected]` |")
HEADER_73 = ("| Drug / Event Pair | Source / State | $a$ | $b$ | $c$ | $d$ | "
             "PRR [95% CI] | ROR [95% CI] | $\\chi^2$ |")


class ReportError(RuntimeError):
    """Figures could not be derived or are internally inconsistent."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TargetFigure:
    generic: int          # current cases with the generic name
    brand: int            # distinct union of current cases with any brand
    coreported: int       # cases carrying both generic and a brand
    both_brands: int      # cases carrying >= 2 distinct target brands


@dataclass(frozen=True)
class PairFigure:
    a: int
    b: int
    c: int
    d: int


# ---------------------------------------------------------------------------
# Read-only fetches
# ---------------------------------------------------------------------------

def fetch_brand_counts(conn) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT drug_normalized, count(*) FROM case_drugs "
            "WHERE drug_normalized = ANY(%s) GROUP BY drug_normalized",
            (list(ALL_BRANDS),),
        )
        return {name: int(n) for name, n in cur.fetchall()}


def _drug_map_values() -> tuple[str, list]:
    """VALUES clause mapping every focus drug name to its target generic.

    Keeps FOCUS as the single source of truth for the SQL itself.
    """
    placeholders: list[str] = []
    params: list = []
    for target in FOCUS:
        placeholders.append("(%s, %s, TRUE)")
        params += [target.generic, target.generic]
        for brand in target.brands:
            placeholders.append("(%s, %s, FALSE)")
            params += [brand, target.generic]
    return ", ".join(placeholders), params


def fetch_target_figures(conn) -> dict[str, TargetFigure]:
    """Distinct current cases per group for ALL focus targets in one query.

    brand_hits >= 2 means the case lists two or more of the target's brand
    names (e.g. GLUCOPHAGE and GLUCOPHAGE XR on the same report).
    """
    values_sql, params = _drug_map_values()
    sql = f"""
        WITH cur AS (SELECT primaryid FROM cases WHERE is_current),
             map AS (SELECT * FROM (VALUES {values_sql}) AS v(dn, tgt, is_generic)),
             hits AS (
                 SELECT cd.primaryid, m.tgt, m.is_generic,
                        cd.drug_normalized AS dn
                 FROM case_drugs cd
                 JOIN cur ON cur.primaryid = cd.primaryid
                 JOIN map m ON m.dn = cd.drug_normalized
             ),
             agg AS (
                 SELECT primaryid, tgt,
                        bool_or(is_generic) AS has_generic,
                        count(DISTINCT dn) FILTER (WHERE NOT is_generic) AS brand_hits
                 FROM hits
                 GROUP BY primaryid, tgt
             )
        SELECT tgt,
               count(*) FILTER (WHERE has_generic),
               count(*) FILTER (WHERE brand_hits > 0),
               count(*) FILTER (WHERE has_generic AND brand_hits > 0),
               count(*) FILTER (WHERE brand_hits >= 2)
        FROM agg
        GROUP BY tgt
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        figures = {
            tgt: TargetFigure(int(g), int(b), int(c), int(bb))
            for tgt, g, b, c, bb in cur.fetchall()
        }
    for target in FOCUS:
        figures.setdefault(target.generic, TargetFigure(0, 0, 0, 0))
    return figures


def fetch_background_n(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM cases WHERE is_current")
        return int(cur.fetchone()[0])


def fetch_pair_counts(conn) -> list[tuple[int, int, int]]:
    """(drug_cases, a, event_cases) for every PAIR_ROWS entry, one query.

    Counted as distinct current cases, matching the engine's DISTINCT staging
    (app/statistics/engine.py) — statistics are CASE counts, not row counts.
    The `cur` CTE is referenced by several child CTEs, so PostgreSQL
    materialises it once instead of re-scanning `cases` per row.
    """
    label_placeholders: list[str] = []
    label_params: list = []
    for idx, (_label, _source, drugs, event) in enumerate(PAIR_ROWS):
        label_placeholders.append("(%s, %s, %s::text[])")
        label_params += [idx, event, list(drugs)]
    all_drugs = sorted({d for _l, _s, drugs, _e in PAIR_ROWS for d in drugs})
    all_events = sorted({e for _l, _s, _d, e in PAIR_ROWS})

    sql = f"""
        WITH cur AS (SELECT primaryid FROM cases WHERE is_current),
             labels AS (
                 SELECT * FROM (VALUES {', '.join(label_placeholders)})
                 AS v(idx, evt, drugs)
             ),
             dset AS (
                 SELECT DISTINCT cd.primaryid, cd.drug_normalized AS dn
                 FROM case_drugs cd
                 JOIN cur ON cur.primaryid = cd.primaryid
                 WHERE cd.drug_normalized = ANY(%s)
             ),
             eset AS (
                 SELECT DISTINCT ev.primaryid, ev.event_term
                 FROM case_events ev
                 JOIN cur ON cur.primaryid = ev.primaryid
                 WHERE ev.event_term = ANY(%s)
             )
        SELECT l.idx,
               (SELECT count(DISTINCT d.primaryid)
                  FROM dset d WHERE d.dn = ANY(l.drugs)),
               (SELECT count(DISTINCT d.primaryid)
                  FROM dset d
                 WHERE d.dn = ANY(l.drugs)
                   AND EXISTS (SELECT 1 FROM eset e
                                WHERE e.primaryid = d.primaryid
                                  AND e.event_term = l.evt)),
               (SELECT count(DISTINCT e.primaryid)
                  FROM eset e WHERE e.event_term = l.evt)
        FROM labels l
        ORDER BY l.idx
    """
    params = label_params + [all_drugs, all_events]
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = [(int(dc), int(a), int(ec)) for _idx, dc, a, ec in cur.fetchall()]
    return rows


def fetch_figures(conn) -> dict:
    """All figures needed for §7.1–§7.3 in one read-only pass."""
    brand_counts = fetch_brand_counts(conn)
    targets = fetch_target_figures(conn)
    background_n = fetch_background_n(conn)

    counts = fetch_pair_counts(conn)
    if len(counts) != len(PAIR_ROWS):
        raise ReportError(
            f"expected {len(PAIR_ROWS)} pair rows from the database, got {len(counts)}"
        )
    pairs = []
    for (label, _source, _drugs, _event), (drug_cases, a_val, event_cases) in zip(
        PAIR_ROWS, counts, strict=True
    ):
        b = drug_cases - a_val
        c = event_cases - a_val
        d = background_n - a_val - b - c
        if min(a_val, b, c, d) < 0:
            raise ReportError(
                f"{label}: impossible 2x2 from database counts "
                f"(a={a_val}, b={b}, c={c}, d={d}, N={background_n})"
            )
        pairs.append(PairFigure(a_val, b, c, d))
    return {"background_n": background_n, "brand_counts": brand_counts,
            "targets": targets, "pairs": pairs}


# ---------------------------------------------------------------------------
# Rendering (must match the report byte-for-byte)
# ---------------------------------------------------------------------------

def render_71_rows(brand_counts: dict[str, int]) -> list[str]:
    rows: list[str] = []
    total = 0
    for target in FOCUS:
        for brand in target.brands:
            n = int(brand_counts.get(brand, 0))
            total += n
            rows.append(f"| `{brand}` | `{target.generic}` | {n:,} |")
    rows.append(f"| **Total Brand Rows** | | **{total:,}** |")
    return rows


def render_72_rows(targets: dict[str, TargetFigure]) -> list[str]:
    rows: list[str] = []
    for target in FOCUS:
        fig = targets[target.generic]
        net_new = fig.brand - fig.coreported
        post = fig.generic + net_new
        if fig.generic <= 0:
            raise ReportError(f"{target.generic}: no generic cases; cannot project")
        pct = net_new / fig.generic * 100.0
        both = f"{fig.both_brands:,}" if len(target.brands) > 1 else "N/A"
        rows.append(
            f"| **{target.generic}** ({target.display}) | {fig.generic:,} | "
            f"{fig.brand:,} | {fig.coreported:,} | {both} | {net_new:,} | "
            f"{post:,} | **+{pct:.2f}%** |"
        )
    return rows


def render_73_rows(background_n: int, pairs: list[PairFigure]) -> list[str]:
    if len(pairs) != len(PAIR_ROWS):
        raise ReportError(f"expected {len(PAIR_ROWS)} pair figures, got {len(pairs)}")
    rows: list[str] = []
    for (label, source, _drugs, _event), fig in zip(PAIR_ROWS, pairs, strict=True):
        if min(fig.a, fig.b, fig.c, fig.d) < 0:
            raise ReportError(f"{label}: negative cell in 2x2 {fig}")
        total = fig.a + fig.b + fig.c + fig.d
        if total != background_n:
            raise ReportError(
                f"{label}: background N not conserved "
                f"({total:,} != {background_n:,} current cases)"
            )
        stat = compute(Table2x2(fig.a, fig.b, fig.c, fig.d))
        if (stat.prr is None or stat.prr_lower is None or stat.ror is None
                or stat.ror_lower is None or stat.chi_square is None):
            raise ReportError(f"{label}: statistics undefined for {fig}")
        prr = f"{stat.prr:.2f} [{stat.prr_lower:.2f}–{stat.prr_upper:.2f}]"
        ror = f"{stat.ror:.2f} [{stat.ror_lower:.2f}–{stat.ror_upper:.2f}]"
        chi2 = f"{stat.chi_square:,.1f}"
        rows.append(
            f"| {label} | {source} | {fig.a:,} | {fig.b:,} | {fig.c:,} | "
            f"{fig.d:,} | {prr} | {ror} | {chi2} |"
        )
    return rows


def render_tables(figures: dict) -> dict[str, list[str]]:
    """header -> expected data rows, for every table we manage."""
    return {
        HEADER_71: render_71_rows(figures["brand_counts"]),
        HEADER_72: render_72_rows(figures["targets"]),
        HEADER_73: render_73_rows(figures["background_n"], figures["pairs"]),
    }


# ---------------------------------------------------------------------------
# Report locate / check / write
# ---------------------------------------------------------------------------

def table_data_span(text: str, header: str) -> tuple[int, int]:
    """Byte span of a table's data rows (after header + separator lines)."""
    if text.count(header) != 1:
        raise ReportError(
            f"expected exactly one table header {header[:50]!r}..., "
            f"found {text.count(header)}"
        )
    start = text.index(header)
    line_after_header = text.find("\n", start)
    if line_after_header == -1:
        raise ReportError(f"header {header[:50]!r} is not followed by any row")
    separator_end = text.find("\n", line_after_header + 1)
    if separator_end == -1:
        raise ReportError(f"header {header[:50]!r} is not followed by a separator")
    pos = separator_end + 1
    data_start = pos
    while True:
        nl = text.find("\n", pos)
        line_end = len(text) if nl == -1 else nl + 1
        if not text[pos:line_end].startswith("|"):
            return data_start, pos
        pos = line_end


def _block(rows: list[str]) -> str:
    return "".join(row + "\n" for row in rows)


def check_report(text: str, tables: dict[str, list[str]]) -> list[str]:
    """Return one message per differing row (empty == report matches DB)."""
    problems: list[str] = []
    for header, expected in tables.items():
        name = header.split("|")[1].strip()
        try:
            span = table_data_span(text, header)
        except ReportError as exc:
            problems.append(f"§table {name!r}: {exc}")
            continue
        actual = [line for line in text[span[0]:span[1]].splitlines() if line.strip()]
        for i in range(max(len(actual), len(expected))):
            got = actual[i] if i < len(actual) else "<missing row>"
            want = expected[i] if i < len(expected) else "<extra row in report>"
            if got != want:
                problems.append(
                    f"table {name!r} row {i + 1}:\n"
                    f"    report   : {got}\n"
                    f"    database : {want}"
                )
    return problems


def write_report(text: str, tables: dict[str, list[str]]) -> tuple[str, list[str]]:
    """Replace drifted data rows. Returns (new text, names of changed tables)."""
    changed: list[str] = []
    for header, expected in tables.items():
        name = header.split("|")[1].strip()
        span = table_data_span(text, header)
        if text[span[0]:span[1]] != _block(expected):
            text = text[:span[0]] + _block(expected) + text[span[1]:]
            changed.append(name)
    return text, changed


# ---------------------------------------------------------------------------
# Baseline snapshot: record before execution, diff after
# ---------------------------------------------------------------------------

def build_snapshot(figures: dict) -> dict:
    """JSON-serialisable baseline of every figure §7 depends on."""
    return {
        "schema_version": SNAPSHOT_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "background_n": figures["background_n"],
        "brand_counts": dict(figures["brand_counts"]),
        "targets": {
            name: {
                "generic": fig.generic,
                "brand": fig.brand,
                "coreported": fig.coreported,
                "both_brands": fig.both_brands,
            }
            for name, fig in figures["targets"].items()
        },
        "pairs": [
            {
                "label": label,
                "source": source,
                "drugs": list(drugs),
                "event": event,
                "a": fig.a,
                "b": fig.b,
                "c": fig.c,
                "d": fig.d,
            }
            for (label, source, drugs, event), fig in zip(
                PAIR_ROWS, figures["pairs"], strict=True
            )
        ],
    }


def load_snapshot(path: Path) -> dict:
    if not path.exists():
        raise ReportError(
            f"snapshot {path} does not exist — run --snapshot before execution"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"cannot read snapshot {path}: {exc}") from exc
    if data.get("schema_version") != SNAPSHOT_SCHEMA:
        raise ReportError(
            f"snapshot {path} has schema_version {data.get('schema_version')!r}, "
            f"expected {SNAPSHOT_SCHEMA}"
        )
    return data


def diff_snapshot(snap: dict, figures: dict) -> tuple[list[str], dict]:
    """Compare current figures against a recorded baseline.

    Returns (violations, stats). Every figure must sit at ONE of two coherent
    states — the recorded baseline, or the exact value consolidation produces —
    and the whole database must agree on which:

      * background N and every event marginal (a + c) never move;
      * projected rows are bit-identical in both states (the projection simply
        becomes reality);
      * post-execution: every brand falls to zero rows, each target's generic
        count equals its snapshot post-consolidation value with brand-side
        measures at zero, brand baseline rows end with zero drug cases, and
        generic baseline rows equal the projected row they collapse into;
      * any other value — and any mixture of baseline and post-execution
        elements, i.e. a partial execution — is a violation.
    """
    violations: list[str] = []
    at_baseline: list[str] = []
    at_post: list[str] = []
    held = 0
    moved = 0

    def place(name: str, base, post_val, now, post_desc: str) -> None:
        """Classify one figure as baseline / post-execution / unexpected."""
        nonlocal held, moved
        if now not in (base, post_val):
            violations.append(
                f"{name}: expected {base} (baseline) or {post_val} "
                f"({post_desc}), found {now}"
            )
            return
        if base == post_val:
            held += 1
        elif now == post_val:
            moved += 1
            at_post.append(name)
        else:
            held += 1
            at_baseline.append(name)

    # 1. The rename must not change the case population.
    if figures["background_n"] != snap["background_n"]:
        violations.append(
            f"background N moved {snap['background_n']:,} -> "
            f"{figures['background_n']:,} (ingestion between snapshot and diff?)"
        )
    else:
        held += 1

    # 2. Brand names: baseline row counts -> zero after consolidation.
    for brand, before in sorted(snap["brand_counts"].items()):
        place(f"brand `{brand}`", int(before), 0,
              int(figures["brand_counts"].get(brand, 0)),
              "post-consolidation")

    # 3. Target aggregates: snapshot value -> its post-consolidation value.
    for generic, before in sorted(snap["targets"].items()):
        after = figures["targets"].get(generic)
        if after is None:
            violations.append(f"target {generic}: missing from current figures")
            continue
        place(f"target {generic}.generic", before["generic"],
              before["generic"] + before["brand"] - before["coreported"],
              after.generic, "post-consolidation")
        for field in ("brand", "coreported", "both_brands"):
            place(f"target {generic}.{field}", before[field], 0,
                  getattr(after, field), "post-consolidation")

    # 4. Per-row expectations for section 7.3.
    snap_pairs = {p["label"]: p for p in snap.get("pairs", [])}
    brand_names = {b for t in FOCUS for b in t.brands}
    generics = {t.generic for t in FOCUS}
    projected_of = {
        drugs[0]: label
        for label, source, drugs, _event in PAIR_ROWS
        if source == PROJECTED_SRC
    }
    background_n = figures["background_n"]

    for (label, source, drugs, _event), cur in zip(
        PAIR_ROWS, figures["pairs"], strict=True
    ):
        before = snap_pairs.get(label)
        if before is None:
            violations.append(
                f"row {label}: absent from snapshot (row set changed since baseline)"
            )
            continue
        before_cells = (before["a"], before["b"], before["c"], before["d"])
        now_cells = (cur.a, cur.b, cur.c, cur.d)

        if sum(now_cells) != background_n:
            violations.append(
                f"row {label}: cells sum to {sum(now_cells):,}, expected "
                f"background N {background_n:,}"
            )
        margin_before = before["a"] + before["c"]
        if cur.a + cur.c != margin_before:
            violations.append(
                f"row {label}: event marginal (a+c) moved "
                f"{margin_before:,} -> {cur.a + cur.c:,}"
            )

        if source == PROJECTED_SRC:
            # Bit-identical in both states: the projection becomes reality.
            place(f"row {label}", before_cells, before_cells, now_cells,
                  "unchanged")
        elif set(drugs) <= brand_names:
            place(f"row {label}", before_cells,
                  (0, 0, margin_before, background_n - margin_before),
                  now_cells, "post-execution")
        elif len(drugs) == 1 and drugs[0] in generics:
            projected = snap_pairs.get(projected_of.get(drugs[0], ""))
            if projected is None:
                violations.append(
                    f"row {label}: no snapshot projected row for {drugs[0]}"
                )
                continue
            place(
                f"row {label}", before_cells,
                (projected["a"], projected["b"],
                 projected["c"], projected["d"]),
                now_cells, "post-execution",
            )
        else:
            violations.append(f"row {label}: unclassifiable drug set {drugs}")

    if at_baseline and at_post:
        violations.append(
            f"partial state: {len(at_post)} element(s) post-execution while "
            f"{len(at_baseline)} still at baseline (first: "
            f"{', '.join(at_baseline[:4])}) — forward.sql is transactional, "
            f"so a mixture means an unrelated change"
        )

    stats = {
        "held": held,
        "moved": moved,
        "state": "post-execution" if at_post else "baseline",
    }
    return violations, stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify or regenerate the migration 004 report figures"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="verify the report against the database (default)")
    mode.add_argument("--write", action="store_true",
                      help="rewrite drifted table rows in the report")
    mode.add_argument("--json", action="store_true",
                      help="print the derived figures as JSON and exit")
    mode.add_argument("--snapshot", action="store_true",
                      help="record baseline figures to the snapshot file "
                           "(run immediately before executing the migration)")
    mode.add_argument("--diff", action="store_true",
                      help="compare current figures against the recorded "
                           "baseline; exit 1 on any unexpected movement")
    parser.add_argument("--report", default=str(DEFAULT_REPORT),
                        help="report markdown path (default: migration 004 doc)")
    parser.add_argument("--snapshot-path", default=str(DEFAULT_SNAPSHOT),
                        help="snapshot JSON path (default: migration 004 package)")
    parser.add_argument("--force", action="store_true",
                        help="allow --snapshot to overwrite an existing snapshot")
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    report_path = Path(args.report)
    try:
        conn = loader.connect(args.database_url or settings.database_url())
    except Exception as exc:                      # noqa: BLE001
        print(f"[report-figures] cannot connect: {exc}", file=sys.stderr)
        return 2
    try:
        figures = fetch_figures(conn)
        tables = render_tables(figures)
    except ReportError as exc:
        print(f"[report-figures] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:                      # noqa: BLE001
        print(f"[report-figures] query failed: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    if args.json:
        payload = {
            "background_n": figures["background_n"],
            "brand_counts": figures["brand_counts"],
            "targets": {k: v.__dict__ for k, v in figures["targets"].items()},
            "pairs": [
                {"label": spec[0], **fig.__dict__}
                for spec, fig in zip(PAIR_ROWS, figures["pairs"], strict=True)
            ],
            "tables": tables,
        }
        print(json.dumps(payload, indent=2))
        return 0

    if args.snapshot:
        snapshot_path = Path(args.snapshot_path)
        if snapshot_path.exists() and not args.force:
            print(f"[report-figures] refusing to overwrite existing snapshot "
                  f"{snapshot_path}; re-take with --force", file=sys.stderr)
            return 2
        snapshot = build_snapshot(figures)
        snapshot_path.write_text(json.dumps(snapshot, indent=2) + "\n",
                                 encoding="utf-8")
        print(f"[report-figures] baseline snapshot written: {snapshot_path} "
              f"({snapshot['created_at']}, N={snapshot['background_n']:,}, "
              f"{len(snapshot['pairs'])} pair rows)")
        return 0

    if args.diff:
        snapshot_path = Path(args.snapshot_path)
        try:
            snapshot = load_snapshot(snapshot_path)
        except ReportError as exc:
            print(f"[report-figures] {exc}", file=sys.stderr)
            return 2
        violations, stats = diff_snapshot(snapshot, figures)
        print(f"[report-figures] diff vs {snapshot_path} "
              f"(baseline {snapshot['created_at']}): "
              f"held-as-required={stats['held']}, "
              f"moved-as-expected={stats['moved']}, state={stats['state']}")
        if violations:
            print(f"[report-figures] FAIL: {len(violations)} unexpected "
                  f"difference(s):", file=sys.stderr)
            for violation in violations:
                print(f"  {violation}", file=sys.stderr)
            return 1
        if stats["state"] == "baseline":
            print("[report-figures] PASS: figures identical to baseline "
                  "(no movement — migration not applied or fully rolled back)")
        else:
            print(f"[report-figures] PASS: only the expected rows moved "
                  f"({stats['moved']} moved, {stats['held']} held)")
        return 0

    if args.write:
        text = report_path.read_text(encoding="utf-8")
        new_text, changed = write_report(text, tables)
        if changed:
            report_path.write_text(new_text, encoding="utf-8")
            print(f"[report-figures] updated {', '.join(changed)} in {report_path}")
        else:
            print(f"[report-figures] {report_path} already matches the database")
        return 0

    text = report_path.read_text(encoding="utf-8")
    problems = check_report(text, tables)
    if problems:
        print(f"[report-figures] {len(problems)} mismatch(es) in {report_path}:",
              file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print("[report-figures] run with --write to regenerate", file=sys.stderr)
        return 1
    print(f"[report-figures] OK: {report_path} matches the database "
          f"(N={figures['background_n']:,} current cases, "
          f"{len(tables)} tables)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
