#!/usr/bin/env python3
"""Label-concordance validation of the disproportionality screen (spec §9, §11).

Reference standard: SIDER 4.1 label-derived MedDRA drug/side-effect pairs
(downloaded to data/reference/ — see SIDER_README.txt, licensed CC BY-SA 4.0:
Creative Commons Attribution-ShareAlike, https://sideeffects.embl.de/download/).
Attribution: SIDER, EMBL-EBI (Kuhn et al., Nucleic Acids Research 2016). Because
SIDER is
built from product labels, it is independent of FAERS report counts, which
makes it usable as an external check on our screen. Limitations are stated in
the output notes: SIDER covers only 1,344 drugs, so concordance is a LOWER
BOUND; absence from SIDER is "unknown", never a proven false positive.

Metrics per mode (all / primary-suspect):
  * recall      — of SIDER label pairs evaluable in signal_scores, fraction
                  passing the PRR and ROR screens
  * top-N       — of the top N PRR-screened signals ranked by case_count
                  (evidence depth; chi2 alone surfaces rare-event artifacts,
                  use --rank chi2), fraction matching a SIDER label pair
                  (judgable = drug in SIDER vocab)
  * indication  — top-N signals whose event is the drug's labeled INDICATION
                  (classic FAERS confounder, flagged not discarded)
  * disease-flare — top-N signals whose event is a non-specific disease-flare/
                  progression term (CONDITION AGGRAVATED etc.): confounded by
                  indication by construction and unjudgeable by SIDER when the
                  drug is outside its vocabulary — counted regardless of
                  reference coverage
  * baseline    — same recall/concordance on a q1-only snapshot table, when
                  present, to show what the 8-quarter background changed

Usage:
    python scripts/validate_concordance.py
    python scripts/validate_concordance.py --top 100,1000
    python scripts/validate_concordance.py --pairs my_pairs.tsv --top 50
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings                      # noqa: E402
from app.repositories import loader                  # noqa: E402
from app.statistics.confounding import (             # noqa: E402
    DISEASE_FLARE_EVENT_TERMS, NON_ADVERSE_EVENT_TERMS)
from psycopg2.extras import execute_values           # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_DIR = PROJECT_ROOT / "data" / "reference"

SIDER_SOURCE = "SIDER 4.1 (sideeffects.embl.de, CC BY-SA 4.0)"
SIDER_DOWNLOAD = "https://sideeffects.embl.de/download/"
SIDER_ATTRIBUTION = ("SIDER, EMBL-EBI (Kuhn et al., Nucleic Acids Research "
                     "2016), licensed CC BY-SA 4.0")

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Term sets are shared with the indication-bias probe
# (app/statistics/confounding.py): NON_ADVERSE_EVENT_TERMS = utilization/
# administration outcomes that can never match SIDER labels;
# DISEASE_FLARE_EVENT_TERMS = disease-flare/progression terms that carry
# drugs SIDER often does not cover at all (see that module for grounding).


def _missing_reference_msg(paths: list[Path]) -> str:
    cmds = "\n".join(
        f"  curl -sL -o {REFERENCE_DIR / f.name} "
        f"{SIDER_DOWNLOAD}media/download/{f.name}"
        for f in paths
    )
    return ("Reference files missing:\n" +
            "\n".join(f"  {p}" for p in paths) +
            f"\nDownload them with:\n{cmds}")


def load_reference(reference_dir: Path = REFERENCE_DIR
                   ) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Parse SIDER files -> ({label pairs}, {indication pairs}).

    PT-level terms only (README recommendation: filter LLT -> PT). Drug names
    and events are uppercased to match drug_normalized / event_term.
    """
    names_path = reference_dir / "drug_names.tsv"
    se_path = reference_dir / "meddra_all_se.tsv.gz"
    ind_path = reference_dir / "meddra_all_indications.tsv.gz"
    missing = [p for p in (names_path, se_path, ind_path) if not p.exists()]
    if missing:
        raise FileNotFoundError(_missing_reference_msg(missing))

    names: dict[str, str] = {}
    with names_path.open(encoding="utf-8") as f:
        for line in f:
            cid, _, name = line.rstrip("\n").partition("\t")
            if cid and name:
                names[cid] = name.upper()

    def pairs_from(path: Path, kind_idx: int, name_idx: int) -> set[tuple[str, str]]:
        out: set[tuple[str, str]] = set()
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) <= max(kind_idx, name_idx):
                    continue
                if parts[kind_idx] != "PT":
                    continue
                drug = names.get(parts[0])
                if drug:
                    out.add((drug, parts[name_idx].upper()))
        return out

    label_pairs = pairs_from(se_path, kind_idx=3, name_idx=5)
    indication_pairs = pairs_from(ind_path, kind_idx=4, name_idx=6)
    return label_pairs, indication_pairs


def load_pairs_file(path: Path) -> set[tuple[str, str]]:
    """Alternate reference: TSV with one drug\\tevent pair per line."""
    out: set[tuple[str, str]] = set()
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#") or "\t" not in line:
                continue
            # Only the first two columns are read; anything after the second
            # tab is ignored so an extra column cannot silently become part of
            # the event name (which would fabricate a never-matching pair).
            fields = line.split("\t")
            if len(fields) < 2:
                continue
            drug, event = fields[0].strip().upper(), fields[1].strip().upper()
            if drug and event:
                out.add((drug, event))
    return out


def topn_stats(rows: list[tuple[str, str]],
               label_pairs: set[tuple[str, str]],
               reference_drugs: set[str],
               indication_pairs: set[tuple[str, str]],
               ns: list[int]) -> list[dict]:
    """Concordance of the first N rows (already ranked) at each N in ns.

    judgable = the drug exists in the reference vocabulary at all; pairs whose
    drug SIDER does not cover cannot be judged and are excluded from
    concordance_judgable (they still count in concordance_all).
    """
    out = []
    for n in ns:
        head = rows[:n]
        if not head:
            continue
        matches = sum(1 for d, e in ((r[0], r[1]) for r in head)
                      if (d, e) in label_pairs)
        judgable = sum(1 for r in head if r[0] in reference_drugs)
        indication = sum(1 for r in head if (r[0], r[1]) in indication_pairs)
        util = sum(1 for r in head if r[1] in NON_ADVERSE_EVENT_TERMS)
        # Reference-independent: fires even when the drug is absent from SIDER.
        flare = sum(1 for r in head if r[1] in DISEASE_FLARE_EVENT_TERMS)
        out.append({
            "n": n,
            "signals_returned": len(head),
            "label_matches": matches,
            "judgable_signals": judgable,
            "concordance_judgable": round(matches / judgable, 4) if judgable else None,
            "concordance_all": round(matches / len(head), 4),
            "indication_events": indication,
            "indication_share": round(indication / len(head), 4),
            "non_adverse_event_signals": util,
            "non_adverse_event_share": round(util / len(head), 4),
            "disease_flare_signals": flare,
            "disease_flare_share": round(flare / len(head), 4),
        })
    return out


def _vocab(conn) -> tuple[set[str], set[str]]:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT drug_normalized FROM case_drugs")
        drugs = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT DISTINCT event_term FROM case_events")
        events = {r[0] for r in cur.fetchall()}
    return drugs, events


def _table_exists(conn, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
        return cur.fetchone()[0]


def _mode_metrics(conn, table: str, ps_only: bool, label_pairs, reference_drugs,
                  indication_pairs, db_drugs, db_events, top_ns: list[int],
                  rank: str) -> dict:
    if not _IDENT.match(table):
        raise ValueError(f"unsafe table name: {table!r}")
    both_in_vocab = sum(1 for d, e in label_pairs
                        if d in db_drugs and e in db_events)
    drug_unknown = sum(1 for d, _ in label_pairs if d not in db_drugs)
    event_unknown = sum(1 for _, e in label_pairs if e not in db_events)

    # Ranking matters: chi-square alone is dominated by rare-event artifacts
    # (a handful of cases against c~0 inflate chi2 into the millions), so the
    # default ranks by evidence depth (case_count). See notes in the report.
    order = ("s.case_count DESC, s.chi_square DESC NULLS LAST, s.drug, s.event"
             if rank == "case_count" else
             "s.chi_square DESC NULLS LAST, s.case_count DESC, s.drug, s.event")

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT count(*) FILTER (WHERE s.prr_screen),
                   count(*) FILTER (WHERE s.ror_screen),
                   count(*)
            FROM ref_pairs r
            JOIN {table} s ON s.drug = r.drug AND s.event = r.event
            WHERE s.period = 'all' AND s.primary_suspect_mode = %s
        """, (ps_only,))
        prr_screened, ror_screened, evaluable = cur.fetchone()

        # SIDER pairs whose drug+event both exist in our vocabulary but that
        # never reached signal_scores: co-occurrence below min_cases (or zero).
        # Distinguishing 0 from 1-2 cases would need a full pairwise scan.
        in_vocab_not_scored = max(0, both_in_vocab - evaluable)

        cur.execute(f"""
            SELECT s.drug, s.event, s.case_count, s.prr, s.chi_square
            FROM {table} s
            WHERE s.period = 'all' AND s.primary_suspect_mode = %s
              AND s.prr_screen
            ORDER BY {order}
            LIMIT %s
        """, (ps_only, max(top_ns)))
        ranked = cur.fetchall()

    return {
        "label_pairs_total": len(label_pairs),
        "pairs_both_sides_in_db_vocab": both_in_vocab,
        "pairs_drug_not_in_db": drug_unknown,
        "pairs_event_not_in_db": event_unknown,
        "evaluable_pairs": evaluable,
        "in_vocab_not_scored": in_vocab_not_scored,
        "prr_screened": prr_screened,
        "recall_prr": round(prr_screened / evaluable, 4) if evaluable else None,
        "ror_screened": ror_screened,
        "recall_ror": round(ror_screened / evaluable, 4) if evaluable else None,
        "top_signals": topn_stats(ranked, label_pairs, reference_drugs,
                                  indication_pairs, top_ns),
        # Audit trail: concrete examples from the ranked list so a reader can
        # inspect what matched, what SIDER cannot judge, and what looks like
        # confounding by indication — aggregates alone are not verifiable.
        "examples": {
            "matched_label": _example_rows(
                ranked, label_pairs, matched=True),
            "judgable_unmatched": _example_rows(
                ranked, label_pairs, reference_drugs, matched=False),
            "indication_events": _example_rows_indication(
                ranked, indication_pairs),
            "disease_flare": _example_rows_disease_flare(ranked),
        },
    }


def _row_dict(row) -> dict:
    d, e, cases, prr, chi2 = row
    return {"drug": d, "event": e, "case_count": cases,
            "prr": round(prr, 3) if prr is not None else None,
            "chi_square": round(chi2, 1) if chi2 is not None else None}


def _example_rows(ranked, pairs, reference_drugs=None, *, matched,
                  limit: int = 15) -> list[dict]:
    out = []
    for row in ranked:
        d, e = row[0], row[1]
        is_match = (d, e) in pairs
        if is_match != matched:
            continue
        if not matched and reference_drugs is not None and d not in reference_drugs:
            continue  # unmatched because the drug is outside SIDER's vocab
        out.append(_row_dict(row))
        if len(out) >= limit:
            break
    return out


def _example_rows_indication(ranked, indication_pairs, limit: int = 15) -> list[dict]:
    out = []
    for row in ranked:
        if (row[0], row[1]) in indication_pairs:
            out.append(_row_dict(row))
            if len(out) >= limit:
                break
    return out


def _example_rows_disease_flare(ranked, limit: int = 15) -> list[dict]:
    """Flare-term signals regardless of reference coverage (SIDER blind spot)."""
    out = []
    for row in ranked:
        if row[1] in DISEASE_FLARE_EVENT_TERMS:
            out.append(_row_dict(row))
            if len(out) >= limit:
                break
    return out


def _baseline_common_pairs(conn, baseline_table: str) -> dict:
    """Recall delta on pairs evaluable in BOTH runs (apples-to-apples).

    Overall recall can move just because the larger background made more
    pairs evaluable; restricting to the intersection shows what actually
    changed for the pairs the q1-only run could see.
    """
    if not _IDENT.match(baseline_table):
        raise ValueError(f"unsafe table name: {baseline_table!r}")
    if not _table_exists(conn, baseline_table):
        return {"skipped": f"table {baseline_table} not found"}
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT count(*) FILTER (WHERE b.prr_screen),
                   count(*) FILTER (WHERE n.prr_screen),
                   count(*),
                   count(*) FILTER (WHERE b.prr_screen AND n.prr_screen),
                   count(*) FILTER (WHERE b.prr_screen AND NOT n.prr_screen),
                   count(*) FILTER (WHERE NOT b.prr_screen AND n.prr_screen)
            FROM {baseline_table} b
            JOIN signal_scores n
              ON n.drug = b.drug AND n.event = b.event
             AND n.period = 'all' AND n.primary_suspect_mode = b.primary_suspect_mode
            JOIN ref_pairs r ON r.drug = b.drug AND r.event = b.event
            WHERE b.period = 'all'
        """)
        (b_scr, n_scr, common, kept, dropped, gained) = cur.fetchone()
    return {
        "common_evaluable_pairs": common,
        "baseline_prr_screened": b_scr,
        "current_prr_screened": n_scr,
        "baseline_recall": round(b_scr / common, 4) if common else None,
        "current_recall": round(n_scr / common, 4) if common else None,
        "kept_screened": kept,
        "dropped_screened": dropped,
        "newly_screened": gained,
    }


def build_report(conn, label_pairs: set[tuple[str, str]],
                 indication_pairs: set[tuple[str, str]],
                 baseline_table: str | None, top_ns: list[int],
                 rank: str) -> dict:
    reference_drugs = {d for d, _ in label_pairs}
    db_drugs, db_events = _vocab(conn)

    modes = {}
    for key, ps_only in (("all", False), ("primary_suspect", True)):
        modes[key] = _mode_metrics(conn, "signal_scores", ps_only, label_pairs,
                                   reference_drugs, indication_pairs,
                                   db_drugs, db_events, top_ns, rank)

    baseline: dict = {"table": baseline_table or "(disabled)",
                      "ranking": rank, "modes": {}}
    if baseline_table:
        if _table_exists(conn, baseline_table):
            for key, ps_only in (("all", False), ("primary_suspect", True)):
                baseline["modes"][key] = _mode_metrics(
                    conn, baseline_table, ps_only, label_pairs,
                    reference_drugs, indication_pairs,
                    db_drugs, db_events, top_ns, rank)
            baseline["common_pairs_all_mode"] = _baseline_common_pairs(
                conn, baseline_table)
        else:
            baseline["skipped"] = f"table {baseline_table} not found"

    notes = [
        f"Reference: {SIDER_SOURCE} ({SIDER_DOWNLOAD}), PT-level terms only.",
        "Recall denominator = SIDER pairs evaluable in signal_scores "
        "(case_count >= min_cases); pairs below threshold are in_vocab_not_scored.",
        "in_vocab_not_scored combines 0, 1 and 2 co-occurring cases; separating "
        "them would require a full pairwise scan.",
        "SIDER covers 1,344 drugs only; absence from SIDER is UNKNOWN, never a "
        "proven false positive — concordance_all is a lower bound.",
        "indication_events = top signals whose event is the drug's own labeled "
        "indication (confounding by indication, flagged not discarded).",
        "Matching is exact after uppercasing; brand-name rows in FAERS that "
        "SIDER records under the generic name count as unmatched.",
        "Top-N ranking = case_count (evidence depth). Ranking by chi-square "
        "alone surfaces rare-event artifacts (a few cases against c~0 give "
        "chi2 in the millions); use --rank chi2 to reproduce that ordering.",
        "non_adverse_event_* = signals whose event is a utilization/admin "
        "term (e.g. OFF LABEL USE, DRUG INEFFECTIVE, PRODUCT USE IN "
        "UNAPPROVED INDICATION), not an adverse event: labels list no side "
        "effect for them, so they can never match SIDER and are not false "
        "positives.",
        "disease_flare_* = signals whose event is a non-specific "
        "disease-flare/progression term (CONDITION AGGRAVATED, DISEASE "
        "PROGRESSION, ...): the treated condition worsening, not a drug "
        "reaction. Counted WITHOUT requiring the drug to be in SIDER's "
        "vocabulary — the SIDER-based indication flag cannot see these when "
        "the drug is missing from the reference (e.g. DUPIXENT).",
    ]
    if baseline.get("skipped"):
        notes.append(f"Baseline comparison unavailable: {baseline['skipped']}.")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reference": {
            "name": SIDER_SOURCE,
            "url": SIDER_DOWNLOAD,
            "license": "CC BY-SA 4.0 (Creative Commons Attribution-ShareAlike)",
            "attribution": SIDER_ATTRIBUTION,
            "label_pairs": len(label_pairs),
            "indication_pairs": len(indication_pairs),
            "reference_drugs": len({d for d, _ in label_pairs}),
            "reference_events": len({e for _, e in label_pairs}),
        },
        "database": {
            "distinct_drugs": len(db_drugs),
            "distinct_events": len(db_events),
        },
        "top_n": top_ns,
        "ranking": rank,
        "modes": modes,
        "baseline": baseline,
        "notes": notes,
    }


def build_concordance(conn, label_pairs: set[tuple[str, str]],
                      indication_pairs: set[tuple[str, str]],
                      baseline_table: str | None, top_ns: list[int],
                      rank: str) -> dict:
    """Reference pairs -> temp table -> full concordance report.

    Shared entry point for the standalone CLI and the quarterly refresh
    orchestrator (scripts/refresh_quarter.py), so both produce identical
    metrics from the same code path (Rule 4: one source of truth).
    """
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE ref_pairs "
                    "(drug TEXT NOT NULL, event TEXT NOT NULL)")
        execute_values(cur,
                       "INSERT INTO ref_pairs (drug, event) VALUES %s "
                       "ON CONFLICT DO NOTHING",
                       list(label_pairs), page_size=1000)
        cur.execute("CREATE INDEX ON ref_pairs (event, drug)")
    conn.commit()
    try:
        return build_report(conn, label_pairs, indication_pairs,
                            baseline_table, top_ns, rank)
    finally:
        conn.rollback()  # drop the session temp table


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate screen against SIDER label pairs")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--reference-dir", default=str(REFERENCE_DIR),
                        help="Directory with SIDER files (data/reference)")
    parser.add_argument("--pairs", default=None,
                        help="Use this TSV (drug<TAB>event) instead of SIDER")
    parser.add_argument("--indications", default=None,
                        help="Optional TSV of indication pairs (with --pairs)")
    parser.add_argument("--top", default="100,500,1000",
                        help="Comma-separated N values for top-N concordance")
    parser.add_argument("--rank", choices=["case_count", "chi2"],
                        default="case_count",
                        help="Top-N ranking (default: case_count; chi2 alone is "
                             "dominated by rare-event artifacts)")
    parser.add_argument("--baseline-table", default="signal_scores_q1only",
                        help="Snapshot table for baseline comparison "
                             "(skipped if absent); '' disables")
    parser.add_argument("--output", default=None,
                        help="Output JSON (default: "
                             "data/processed/concordance_validation_latest.json)")
    args = parser.parse_args(argv)

    top_ns = sorted({int(x) for x in args.top.split(",") if x.strip()})
    if not top_ns or any(n <= 0 for n in top_ns):
        parser.error("--top must be positive comma-separated integers")

    if args.pairs:
        label_pairs = load_pairs_file(Path(args.pairs))
        indication_pairs = (load_pairs_file(Path(args.indications))
                            if args.indications else set())
    else:
        label_pairs, indication_pairs = load_reference(Path(args.reference_dir))
    if not label_pairs:
        print("[concordance] reference is empty; nothing to validate",
              file=sys.stderr)
        return 1

    conn = loader.connect(args.database_url or settings.database_url())
    try:
        report = build_concordance(conn, label_pairs, indication_pairs,
                                   args.baseline_table or None, top_ns,
                                   args.rank)
    finally:
        conn.close()

    out_path = (Path(args.output) if args.output else
                Path(settings.processed_data_path()) /
                "concordance_validation_latest.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"[concordance] reference pairs={report['reference']['label_pairs']} "
          f"({report['reference']['reference_drugs']} drugs, "
          f"{report['reference']['reference_events']} events)")
    print(f"[concordance] wrote {out_path}")
    for mode, m in report["modes"].items():
        top0 = m["top_signals"][0] if m["top_signals"] else None
        print(f"[concordance] mode={mode}: evaluable={m['evaluable_pairs']} "
              f"PRR-recall={m['recall_prr']} ROR-recall={m['recall_ror']}")
        if top0:
            print(f"  top-{top0['n']}: label_matches={top0['label_matches']}/"
                  f"{top0['signals_returned']} "
                  f"(judgable {top0['concordance_judgable']}), "
                  f"indication={top0['indication_events']}, "
                  f"disease_flare={top0['disease_flare_signals']}")
    if report["baseline"].get("modes"):
        b = report["baseline"]["modes"].get("all", {})
        print(f"[concordance] baseline {report['baseline']['table']} "
              f"all-mode: evaluable={b.get('evaluable_pairs')} "
              f"PRR-recall={b.get('recall_prr')}")
        common = report["baseline"].get("common_pairs_all_mode", {})
        if common.get("common_evaluable_pairs"):
            print(f"[concordance] common pairs: "
                  f"{common['common_evaluable_pairs']} "
                  f"recall {common['baseline_recall']} -> "
                  f"{common['current_recall']} "
                  f"(kept={common['kept_screened']} "
                  f"dropped={common['dropped_screened']} "
                  f"new={common['newly_screened']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
