#!/usr/bin/env python3
"""Automated indication-bias probe over top signals (screening diagnostic).

Usage:
    python scripts/probe_confounding.py                      # top 200, all mode
    python scripts/probe_confounding.py --top 500 --mode primary-suspect
    python scripts/probe_confounding.py --min-enrichment 2.0 --min-share 0.05

Reads the top PRR-screened signals from signal_scores and scores each one on
two reference-independent dimensions (app/statistics/confounding.py):

    co-event enrichment   pair cases also reporting treated-disease terms,
                          vs the same share across all cases of the drug
    treatment-failure     same over utilization/failure terms
                          (OFF LABEL USE, DRUG INEFFECTIVE, ...)

Drug-specific indication terms come from SIDER's indications file when the
reference is present; drugs SIDER does not cover fall back to a generic
disease-term list — this is the blind spot the DUPIXENT/CONDITION AGGRAVATED
investigation exposed, so the probe works for signals SIDER cannot judge.

Writes data/processed/confounding_probe_latest.json. Flagged means "review
as possibly confounded", never "false"; all numbers are screening
diagnostics, never causality or risk.

build_confounding() is the shared entry point: the CLI (this file) and the
quarterly refresh stage (scripts/refresh_quarter.py, stage `confounding`)
both execute it, so refresh numbers always match the standalone tool.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings                      # noqa: E402
from app.repositories import loader                  # noqa: E402
from app.statistics.confounding import (             # noqa: E402
    DISEASE_FLARE_EVENT_TERMS, GENERIC_DISEASE_TERMS,
    NON_ADVERSE_EVENT_TERMS, probe_signals,
)
from scripts.validate_concordance import (           # noqa: E402
    REFERENCE_DIR, load_reference,
)

_IDENT_MODE = {"all": False, "primary-suspect": True}


def build_confounding(conn, top: int = 200, mode: str = "all",
                      min_enrichment: float = 1.5, min_share: float = 0.10,
                      reference_dir: Path | str = REFERENCE_DIR,
                      progress: bool = False) -> dict:
    """Ranked screened signals -> confounding probe report (dict).

    One source of truth for the CLI and the refresh stage. An empty screen
    is a valid result here (summary.probed == 0); the CLI turns that into
    exit code 1 without writing a file, while the refresh records it as a
    stage with nothing to probe.
    """
    indication_pairs: set[tuple[str, str]] | None = None
    reference_note = "generic disease-term fallback for ALL drugs"
    try:
        _, indication_pairs = load_reference(Path(reference_dir))
        reference_note = (f"SIDER indications for covered drugs "
                          f"({len({d for d, _ in indication_pairs})} drugs); "
                          f"generic fallback for the rest")
    except FileNotFoundError as exc:
        print(f"[confounding] {exc}", file=sys.stderr)
        print("[confounding] continuing with generic disease terms only",
              file=sys.stderr)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT drug, event, case_count, prr
            FROM signal_scores
            WHERE period = 'all' AND primary_suspect_mode = %s
              AND prr_screen
            ORDER BY case_count DESC, chi_square DESC NULLS LAST,
                     drug, event
            LIMIT %s
        """, (_IDENT_MODE[mode], top))
        ranked = cur.fetchall()
    conn.rollback()

    report: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "parameters": {"top": top,
                       "min_enrichment": min_enrichment,
                       "min_share": min_share},
        "reference": {"indication_terms": reference_note},
        "terms": {
            "generic_disease_terms": sorted(GENERIC_DISEASE_TERMS),
            "treatment_failure_terms": sorted(NON_ADVERSE_EVENT_TERMS),
            "disease_flare_terms": sorted(DISEASE_FLARE_EVENT_TERMS),
        },
        "summary": {"probed": 0, "flagged": 0,
                    "flagged_coevent": 0,
                    "flagged_treatment_failure": 0},
        "flagged": [],
        "signals": [],
        "notes": [
            "Enrichment = share of pair cases carrying a probe term divided "
            "by the share across ALL current cases of the same drug; a "
            "signal's own event term never counts as its own co-event.",
            "Flag threshold: enrichment >= min_enrichment (or unbounded when "
            "the baseline share is 0) AND pair share >= min_share.",
            "Flagged = review as possibly confounded by indication / "
            "treatment failure; NOT a false positive and never a causality "
            "or risk statement.",
            "Indication terms per drug come from SIDER where available "
            "(drug-specific); drugs SIDER does not cover use "
            "GENERIC_DISEASE_TERMS — coverage-independent by design.",
            "Baseline uses current versions only (spec §7).",
        ],
    }
    if not ranked:
        return report

    scores = probe_signals(
        conn, ranked,
        indication_pairs=indication_pairs,
        min_enrichment=min_enrichment,
        min_share=min_share,
        progress=progress,
    )
    flagged = [asdict(s) for s in scores if s.flagged]
    report["summary"] = {
        "probed": len(scores),
        "flagged": len(flagged),
        "flagged_coevent": sum(1 for s in scores
                               if s.coevent.enrichment is not None
                               and s.coevent.enrichment >= min_enrichment
                               and s.coevent.share_pair >= min_share),
        "flagged_treatment_failure": sum(
            1 for s in scores
            if (s.treatment_failure.enrichment is not None
                and s.treatment_failure.enrichment >= min_enrichment
                or s.treatment_failure.enrichment is None
                and s.treatment_failure.pair_hits > 0)
            and s.treatment_failure.share_pair >= min_share),
    }
    report["flagged"] = flagged
    report["signals"] = [asdict(s) for s in scores]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score top signals for indication-bias enrichment")
    parser.add_argument("--top", type=int, default=200,
                        help="Number of top PRR-screened signals to probe")
    parser.add_argument("--mode", choices=["all", "primary-suspect"],
                        default="all")
    parser.add_argument("--min-enrichment", type=float, default=1.5,
                        help="Flag when share_pair/share_base reaches this")
    parser.add_argument("--min-share", type=float, default=0.10,
                        help="...and the pair-case share reaches this")
    parser.add_argument("--reference-dir", default=str(REFERENCE_DIR),
                        help="SIDER files for drug-specific indication terms "
                             "(absence falls back to generic disease terms)")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args(argv)

    conn = loader.connect(args.database_url or settings.database_url())
    try:
        report = build_confounding(
            conn, top=args.top, mode=args.mode,
            min_enrichment=args.min_enrichment, min_share=args.min_share,
            reference_dir=args.reference_dir, progress=True,
        )
    finally:
        conn.close()

    if report["summary"]["probed"] == 0:
        print("[confounding] no PRR-screened signals to probe",
              file=sys.stderr)
        return 1

    out_path = (Path(args.output) if args.output else
                Path(settings.processed_data_path()) /
                "confounding_probe_latest.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"[confounding] probed {report['summary']['probed']} "
          f"{args.mode} signals, flagged {report['summary']['flagged']} "
          f"-> {out_path}")
    for f in report["flagged"][:10]:
        print(f"  FLAG {f['drug']}/{f['event']} ({f['case_count']} cases): "
              f"{'; '.join(f['reasons'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
