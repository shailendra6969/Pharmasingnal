#!/usr/bin/env python3
"""End-to-end quarterly refresh: download -> ingest -> recompute signals.

Adds one FAERS quarter to the database and rebuilds signal scores over the
enlarged background in a single command (spec §7 ingestion + §9 screening):

    python scripts/refresh_quarter.py --quarter 2026q1
    python scripts/refresh_quarter.py --quarter 2026q1 --mode all
    python scripts/refresh_quarter.py --quarter 2026q1 --skip-download
    python scripts/refresh_quarter.py --quarter 2026q1 \
        --quarter-dir /path/to/extracted   # skips download (dir supplied)

Every stage is verified before the next one starts; the exit code is 0
only when all stages ran and every check passed:

    download    extracted DEMO/DRUG/REAC files exist for the quarter
    ingest      cases landed for the quarter, and the cross-quarter dedup left
                exactly one current version per case (invariant)
    signals     signal_scores / signal_quarterly rebuilt over the new
                background; the recorded background_n must equal the live
                current-case count, and table row counts must match what the
                engine reports it wrote
    concordance SIDER label-concordance validation re-run on the refreshed
                screen, with the numeric delta vs the previous refresh's
                report (skipped when the reference files are absent)
    regimes     quarterly reporting-regime audit of the refreshed screen:
                flags bulk single-event submission bursts and notoriety
                ramps (app/statistics/regime.py) for the latest quarter
    profiles    quarterly-PRR profile verdicts for the top screened
                signals: is the pooled elevation historically supported, or
                new this quarter (app/statistics/qprofile.py)
    confounding the indication-bias probe re-run on the refreshed screen
                (same build_confounding path as probe_confounding.py),
                recording which top signals flag as possibly confounded

A machine-readable report is written to
data/processed/quarterly_refresh_latest.json (override with --report).

Note: FAERS publishes a new quarter with a lag of roughly two months after
quarter close; a 404 at download time usually means the export does not
exist yet, not that the pipeline is broken.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings                      # noqa: E402
from app.faers.download import download_quarter      # noqa: E402
from app.faers.parse import normalize_quarter        # noqa: E402
from app.repositories import loader                  # noqa: E402
from app.statistics.engine import compute_signals    # noqa: E402
from app.statistics.qprofile import quarterly_profiles  # noqa: E402
from app.statistics.regime import detect_regimes      # noqa: E402
from scripts.run_ingestion import main as ingestion_main  # noqa: E402
from scripts.probe_confounding import build_confounding   # noqa: E402
from scripts.validate_concordance import (           # noqa: E402
    REFERENCE_DIR, build_concordance, load_reference)

# Datasets the pipeline consumes; refresh refuses to run on a partial extract.
REQUIRED_DATASETS = ("DEMO", "DRUG", "REAC")


def _check_extracted(quarter: str, directory: Path) -> list[str]:
    """Fail loudly if the quarter's extracted files are missing or partial."""
    directory = Path(directory)
    if not directory.is_dir():
        raise RuntimeError(f"{directory} does not exist for {quarter}")
    names = [p.name.upper() for p in directory.glob("*.txt")]
    missing = [d for d in REQUIRED_DATASETS
               if not any(n.startswith(d) for n in names)]
    if missing:
        raise RuntimeError(
            f"{directory} has no {missing} file(s) for {quarter}; download "
            f"first (scripts/download_faers.py --quarters {quarter})")
    return sorted(names)


def _verify_ingest(conn, quarter: str) -> dict:
    """Check load + dedup invariants for the refreshed quarter."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*)                                    AS rows,
                   count(*) FILTER (WHERE is_current)          AS current,
                   count(DISTINCT case_id) FILTER (WHERE is_current)
                                                              AS distinct_current
            FROM cases WHERE source_quarter = %s
            """,
            (quarter,),
        )
        rows, current, distinct_current = cur.fetchone()
        cur.execute("SELECT count(*) FROM cases WHERE is_current")
        total_current = cur.fetchone()[0]
    if rows == 0:
        raise RuntimeError(
            f"ingestion reported success but no cases landed for {quarter}")
    if current == 0:
        raise RuntimeError(f"no current version for {quarter} after dedup")
    if current != distinct_current:
        raise RuntimeError(
            f"dedup invariant broken for {quarter}: {current} current rows "
            f"vs {distinct_current} distinct case_ids (must be equal)")
    return {
        "case_rows": int(rows),
        "current_cases": int(current),
        "background_current_cases": int(total_current),
    }


def _compute_signals(conn, mode: str, min_cases: int, quarter_series: bool,
                     expected_background: int) -> dict:
    """Recompute scores per mode; verify against live DB state."""
    modes = {"all": [False], "primary-suspect": [True],
             "both": [False, True]}[mode]
    out: dict[str, dict] = {}
    for ps_only in modes:
        res = compute_signals(
            conn,
            primary_suspect_only=ps_only,
            min_cases=min_cases,
            include_quarter_series=quarter_series,
            progress=True,
        )
        if res.background_n != expected_background:
            raise RuntimeError(
                f"mode={res.mode}: engine background N={res.background_n} "
                f"!= live current-case count {expected_background}")
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM signal_scores "
                        "WHERE primary_suspect_mode = %s", (ps_only,))
            score_rows = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM signal_quarterly "
                        "WHERE primary_suspect_mode = %s", (ps_only,))
            quarterly_rows = cur.fetchone()[0]
        if score_rows != len(res.scores):
            raise RuntimeError(
                f"mode={res.mode}: signal_scores holds {score_rows} rows, "
                f"engine wrote {len(res.scores)}")
        if quarterly_rows != res.quarterly_rows:
            raise RuntimeError(
                f"mode={res.mode}: signal_quarterly holds {quarterly_rows} "
                f"rows, engine wrote {res.quarterly_rows}")
        out[res.mode] = {
            "background_n": res.background_n,
            "pairs_considered": res.pairs_considered,
            "pairs_screened_prr": res.pairs_screened_prr,
            "pairs_screened_ror": res.pairs_screened_ror,
            "score_rows": score_rows,
            "quarterly_rows": quarterly_rows,
            "duration_seconds": round(res.duration_seconds, 1),
        }
    return out


def _summarize_concordance(report: dict) -> dict:
    """Compact per-mode metrics (recall + smallest top-N slice)."""
    out = {}
    for mode, m in report["modes"].items():
        top0 = m["top_signals"][0] if m["top_signals"] else None
        out[mode] = {
            "evaluable_pairs": m["evaluable_pairs"],
            "recall_prr": m["recall_prr"],
            "recall_ror": m["recall_ror"],
            "top": ({"n": top0["n"], "label_matches": top0["label_matches"],
                     "concordance_judgable": top0["concordance_judgable"],
                     "non_adverse_event_signals":
                         top0["non_adverse_event_signals"],
                     "disease_flare_signals": top0["disease_flare_signals"]}
                    if top0 else None),
        }
    return out


def _delta(previous: dict | None, current: dict) -> dict:
    """Numeric delta of the concordance summary vs the previous refresh.

    previous comes from the prior refresh report at the same path (loaded
    before this run overwrites it); None on the first ever run.
    """
    if not previous:
        return {"previous": None, "changes": None}
    changes = {}
    for mode, cur_m in current.items():
        prev_m = previous.get(mode) or {}
        d: dict = {}
        for key in ("evaluable_pairs", "recall_prr", "recall_ror"):
            pv, cv = prev_m.get(key), cur_m.get(key)
            if isinstance(pv, (int, float)) and isinstance(cv, (int, float)):
                d[key] = {"previous": pv, "current": cv,
                          "delta": round(cv - pv, 4)}
        pt, ct = prev_m.get("top") or {}, cur_m.get("top") or {}
        if pt and ct and pt.get("n") == ct.get("n"):
            for key in ("label_matches", "concordance_judgable",
                        "non_adverse_event_signals", "disease_flare_signals"):
                pv, cv = pt.get(key), ct.get(key)
                if isinstance(pv, (int, float)) and isinstance(cv, (int, float)):
                    d[f"top{ct['n']}_{key}"] = {"previous": pv, "current": cv,
                                                "delta": round(cv - pv, 4)}
        if d:
            changes[mode] = d
    return {"previous": previous.get("_source"), "changes": changes}


def _load_previous_concordance(out_path: Path) -> dict | None:
    """Prior refresh report's concordance summary, if one exists."""
    if not out_path.exists():
        return None
    try:
        prev = json.loads(out_path.read_text(encoding="utf-8"))
        modes = prev["stages"]["concordance"]["modes"]
        summary = dict(modes)
        summary["_source"] = (f"previous refresh report, "
                              f"finished {prev.get('finished_at', 'unknown')}")
        return summary
    except (KeyError, TypeError, json.JSONDecodeError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Refresh: download + ingest + recompute signals for one "
                    "new FAERS quarter")
    parser.add_argument("--quarter", required=True,
                        help="Quarter to add, e.g. 2026q1")
    parser.add_argument("--mode", choices=["all", "primary-suspect", "both"],
                        default="both")
    parser.add_argument("--min-cases", type=int, default=3)
    parser.add_argument("--no-quarter-series", action="store_true",
                        help="Skip signal_quarterly volume rows")
    parser.add_argument("--quarter-dir", default=None,
                        help="Directory with the quarter's extracted files; "
                             "skips the download stage")
    parser.add_argument("--skip-download", action="store_true",
                        help="Use already-extracted files under --raw-root/Q")
    parser.add_argument("--force-download", action="store_true",
                        help="Re-download and re-extract even if present")
    parser.add_argument("--raw-root", default=str(settings.faers_data_path()))
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--report", default=None,
                        help="Report path (default: "
                             "data/processed/quarterly_refresh_latest.json)")
    parser.add_argument("--skip-concordance", action="store_true",
                        help="Skip the SIDER concordance re-run stage")
    parser.add_argument("--reference-dir", default=str(REFERENCE_DIR),
                        help="SIDER files for the concordance stage")
    parser.add_argument("--concordance-top", default="100,500,1000",
                        help="Top-N slices for the concordance stage")
    parser.add_argument("--skip-regimes", action="store_true",
                        help="Skip the quarterly regime-detector stage")
    parser.add_argument("--regime-candidates", type=int, default=1000,
                        help="Screened pairs audited by the regime stage, "
                             "ranked by latest-quarter case depth")
    parser.add_argument("--skip-profiles", action="store_true",
                        help="Skip the quarterly-PRR profile stage")
    parser.add_argument("--profile-top", type=int, default=200,
                        help="Top screened signals profiled per refresh")
    parser.add_argument("--skip-confounding", action="store_true",
                        help="Skip the indication-bias probe stage")
    parser.add_argument("--confounding-top", type=int, default=200,
                        help="Top screened signals probed for confounding")
    args = parser.parse_args(argv)

    try:
        quarter = normalize_quarter(args.quarter)
    except ValueError as exc:
        print(f"[refresh] {exc}", file=sys.stderr)
        return 2

    concordance_top = sorted({int(x) for x in
                              args.concordance_top.split(",") if x.strip()})
    if not concordance_top or any(n <= 0 for n in concordance_top):
        print("[refresh] --concordance-top must be positive comma-separated "
              "integers", file=sys.stderr)
        return 2

    database_url = args.database_url or settings.database_url()
    raw_root = Path(args.raw_root)
    out_path = Path(args.report) if args.report else (
        Path(settings.processed_data_path()) /
        "quarterly_refresh_latest.json")
    # Delta baseline: the previous run's concordance summary, read BEFORE this
    # run overwrites the report.
    previous_concordance = _load_previous_concordance(out_path)
    extract_dir = Path(args.quarter_dir) if args.quarter_dir else raw_root / quarter
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report: dict = {
        "quarter": quarter,
        "started_at": started_at,
        "mode": args.mode,
        "min_cases": args.min_cases,
        "stages": {},
    }
    t_all = time.time()

    # ---- stage 1: download ------------------------------------------------
    t0 = time.time()
    try:
        if args.quarter_dir:
            print(f"[refresh] {quarter}: --quarter-dir given, skipping "
                  f"download; using {extract_dir}")
        elif args.skip_download:
            print(f"[refresh] {quarter}: --skip-download; using {extract_dir}")
        else:
            dl = download_quarter(quarter, raw_root, force=args.force_download)
            extract_dir = dl.extracted_dir
            report["stages"]["download"] = {
                "archive_sha256": dl.archive_sha256,
                "skipped_existing": dl.skipped_existing,
            }
            print(f"[refresh] {quarter}: "
                  f"{'already extracted' if dl.skipped_existing else 'downloaded'} "
                  f"({len(dl.extracted)} files)")
        names = _check_extracted(quarter, extract_dir)
    except Exception as exc:
        print(f"[refresh] download FAILED: {exc}", file=sys.stderr)
        return 1
    report["stages"]["download"]["extract_dir"] = str(extract_dir)
    report["stages"]["download"]["files"] = names
    report["stages"]["download"]["duration_seconds"] = round(time.time() - t0, 1)

    # ---- stage 2: ingest (parse -> validate -> load -> cross-quarter dedup)
    t0 = time.time()
    try:
        ingest_argv = ["--database-url", database_url]
        if args.quarter_dir:
            ingest_argv += ["--quarter-dirs", f"{quarter}={extract_dir}"]
        else:
            ingest_argv += ["--quarters", quarter, "--raw-root", str(raw_root)]
        rc = ingestion_main(ingest_argv)
        if rc != 0:
            raise RuntimeError(f"ingestion exited {rc}")
        conn = loader.connect(database_url)
        try:
            ingest_check = _verify_ingest(conn, quarter)
        finally:
            conn.close()
    except Exception as exc:
        print(f"[refresh] ingest FAILED: {exc}", file=sys.stderr)
        return 1
    report["stages"]["ingest"] = {**ingest_check,
                                  "duration_seconds": round(time.time() - t0, 1)}
    print(f"[refresh] {quarter}: ingested "
          f"{ingest_check['case_rows']} case rows "
          f"({ingest_check['current_cases']} current); background now "
          f"{ingest_check['background_current_cases']} current reports")

    # ---- stage 3: recompute signals over the enlarged background ----------
    t0 = time.time()
    try:
        conn = loader.connect(database_url)
        try:
            signal_stats = _compute_signals(
                conn, args.mode, args.min_cases,
                quarter_series=not args.no_quarter_series,
                expected_background=ingest_check["background_current_cases"],
            )
        finally:
            conn.close()
    except Exception as exc:
        print(f"[refresh] signals FAILED: {exc}", file=sys.stderr)
        return 1
    report["stages"]["signals"] = {"modes": signal_stats,
                                   "duration_seconds": round(time.time() - t0, 1)}
    for m, st in signal_stats.items():
        print(f"[refresh] {quarter}: mode={m}: N={st['background_n']}, "
              f"pairs>=min_cases={st['pairs_considered']}, "
              f"PRR-screened={st['pairs_screened_prr']}, "
              f"ROR-screened={st['pairs_screened_ror']}, "
              f"{st['duration_seconds']}s")

    # ---- stage 4: SIDER concordance re-run + delta vs previous refresh ----
    t0 = time.time()
    if args.skip_concordance:
        report["stages"]["concordance"] = {
            "status": "skipped", "reason": "--skip-concordance",
            "duration_seconds": round(time.time() - t0, 1)}
        print("[refresh] concordance skipped (--skip-concordance)")
    else:
        try:
            label_pairs, indication_pairs = load_reference(
                Path(args.reference_dir))
            if not label_pairs:
                raise RuntimeError("reference is empty; nothing to validate")
            conn = loader.connect(database_url)
            try:
                conc = build_concordance(conn, label_pairs, indication_pairs,
                                         None, concordance_top, "case_count")
            finally:
                conn.close()
        except FileNotFoundError as exc:
            # Reference files are not committed; absence is a known state,
            # not a pipeline failure (the curl commands are in the message).
            report["stages"]["concordance"] = {
                "status": "skipped",
                "reason": str(exc).splitlines()[0],
                "duration_seconds": round(time.time() - t0, 1)}
            print("[refresh] concordance skipped: SIDER reference files "
                  f"missing under {args.reference_dir} (run "
                  "scripts/validate_concordance.py once to see the curl "
                  "commands)", file=sys.stderr)
        except Exception as exc:
            print(f"[refresh] concordance FAILED: {exc}", file=sys.stderr)
            return 1
        else:
            summary = _summarize_concordance(conc)
            delta = _delta(previous_concordance, summary)
            report["stages"]["concordance"] = {
                "status": "succeeded",
                "reference": {
                    "name": conc["reference"]["name"],
                    "label_pairs": conc["reference"]["label_pairs"],
                    "reference_drugs": conc["reference"]["reference_drugs"],
                },
                "modes": summary,
                "delta_vs_previous": delta,
                "duration_seconds": round(time.time() - t0, 1)}
            if delta["changes"] is None:
                print(f"[refresh] concordance: "
                      f"{conc['reference']['label_pairs']} label pairs; "
                      "no previous refresh report to diff")
            for mode, dm in (delta.get("changes") or {}).items():
                bits = ", ".join(
                    f"{k} {v['previous']}->{v['current']}"
                    for k, v in dm.items())
                print(f"[refresh] concordance {mode}: {bits or 'unchanged'}")

    # ---- stage 5: quarterly regime detector (bulk / notoriety artifacts)
    t0 = time.time()
    if args.skip_regimes:
        report["stages"]["regimes"] = {
            "status": "skipped", "reason": "--skip-regimes",
            "duration_seconds": round(time.time() - t0, 1)}
        print("[refresh] regimes skipped (--skip-regimes)")
    else:
        try:
            conn = loader.connect(database_url)
            try:
                regimes = detect_regimes(
                    conn, min_cases=args.min_cases,
                    max_candidates=args.regime_candidates)
            finally:
                conn.close()
        except Exception as exc:
            print(f"[refresh] regimes FAILED: {exc}", file=sys.stderr)
            return 1
        report["stages"]["regimes"] = {
            **regimes, "status": "succeeded",
            "duration_seconds": round(time.time() - t0, 1)}
        counts = regimes["flag_counts"]
        summary = ", ".join(f"{k}={v}" for k, v in counts.items() if v) \
            or "none"
        print(f"[refresh] {quarter}: regime audit of "
              f"{regimes['candidates_evaluated']} screened pairs -> "
              f"{sum(counts.values())} flags ({summary})")
        for f in regimes["flags"]:
            print(f"[refresh]   {f['flag']}: {f['drug']} / {f['event']} — "
                  f"{f['reason']}")

    # ---- stage 6: quarterly-PRR profile verdicts -------------------------
    t0 = time.time()
    verdict_mode = ("primary-suspect" if args.mode == "primary-suspect"
                    else "all")
    if args.skip_profiles:
        report["stages"]["profiles"] = {
            "status": "skipped", "reason": "--skip-profiles",
            "duration_seconds": round(time.time() - t0, 1)}
        print("[refresh] profiles skipped (--skip-profiles)")
    else:
        try:
            conn = loader.connect(database_url)
            try:
                profiles = quarterly_profiles(
                    conn, top=args.profile_top, mode=verdict_mode,
                    min_cases=args.min_cases)
            finally:
                conn.close()
        except Exception as exc:
            print(f"[refresh] profiles FAILED: {exc}", file=sys.stderr)
            return 1
        report["stages"]["profiles"] = {
            **profiles, "status": "succeeded",
            "duration_seconds": round(time.time() - t0, 1)}
        vc = profiles["verdict_counts"]
        summary = ", ".join(f"{k}={v}" for k, v in sorted(vc.items())) \
            or "none"
        print(f"[refresh] {quarter}: profile verdicts over "
              f"{profiles['signals_evaluated']} signals: {summary}")
        for f in profiles["flagged"]:
            print(f"[refresh]   elevation_is_new: {f['drug']} / "
                  f"{f['event']} — {f['reason']}")

    # ---- stage 7: indication-bias (confounding) probe --------------------
    t0 = time.time()
    if args.skip_confounding:
        report["stages"]["confounding"] = {
            "status": "skipped", "reason": "--skip-confounding",
            "duration_seconds": round(time.time() - t0, 1)}
        print("[refresh] confounding skipped (--skip-confounding)")
    else:
        try:
            conn = loader.connect(database_url)
            try:
                conf = build_confounding(
                    conn, top=args.confounding_top, mode=verdict_mode,
                    reference_dir=args.reference_dir, progress=False)
            finally:
                conn.close()
        except Exception as exc:
            print(f"[refresh] confounding FAILED: {exc}", file=sys.stderr)
            return 1
        report["stages"]["confounding"] = {
            **conf, "status": "succeeded",
            "duration_seconds": round(time.time() - t0, 1)}
        cs = conf["summary"]
        print(f"[refresh] {quarter}: confounding probe "
              f"{cs['probed']} signals, {cs['flagged']} flagged")
        for f in conf["flagged"][:10]:
            print(f"[refresh]   FLAG {f['drug']} / {f['event']}: "
                  f"{'; '.join(f['reasons'])}")

    # ---- report -----------------------------------------------------------
    report["status"] = "succeeded"
    report["duration_seconds"] = round(time.time() - t_all, 1)
    report["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[refresh] {quarter}: report {out_path}")

    if quarter not in settings.faers_quarters():
        print(f"[refresh] note: {quarter} is not in FAERS_QUARTERS; add it "
              f"to .env so default pipeline commands include it")
    print(f"[refresh] {quarter}: done in {report['duration_seconds']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
