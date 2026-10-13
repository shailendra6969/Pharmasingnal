"""Quarterly PRR profile checks (screening diagnostic, never causality).

Automates the quarterly-profile half of the manual artifact battery: every
quarter's own PRR for a screened drug-event pair, compared against the pooled
PRR that got it screened. The DAPAGLIFLOZIN/DEATH investigation (qPRR 0.3-0.9
below background for six quarters, then a pooled 5.17 from one 2025q3-q4
submission burst) and the DEPO-PROVERA/MENINGIOMA pattern (elevated in every
quarter — the pooled number is historically supported, its magnitude is what
notoriety inflates) are the two ends of this spectrum.

Verdicts per signal (profile_verdict, pure and hand-testable):

    elevation_is_new  FLAG: pooled PRR >= elevated bar, baseline quarterly
                      PRR median below background, and the latest quarter
                      elevated — the pooled statistic is a new-quarter
                      reporting regime, not history.
    consistent        elevated qPRR in EVERY evaluable quarter — the pooled
                      value is historically supported (systematic pattern).
    mixed             everything else with enough history.
    insufficient      fewer than min_baseline_quarters of evaluable history.

Quarterly PRR uses the standard 2x2 with a Haldane-Anscombe 0.5 continuity
correction (so quarters where the event never appears outside the drug give a
large finite PRR instead of infinity):

    a = pair cases, b = drug cases - a, c = event cases - a,
    d = population - drug - event + a
    qPRR = ((a+.5)/(a+b+1)) / ((c+.5)/(c+d+1))

A quarter is evaluable only when the drug itself has >= min_drug_cases that
quarter (a share built on 3 reports is noise, not a trend).
"""
from __future__ import annotations

from datetime import datetime, timezone

DEFAULT_THRESHOLDS: dict = {
    # candidate floor: latest-quarter co-occurrence (matches the refresh's
    # --min-cases) and the ranked-list cap (matches --profile-top)
    "min_cases": 3,
    "max_candidates": 200,
    # quarters of history required before the latest one
    "min_baseline_quarters": 2,
    # quarterly PRR at/above this counts as elevated (the screen's own bar)
    "elevated_prr": 2.0,
    # quarterly PRR below this is "below background"
    "background_prr": 1.0,
    # drug exposure floor per quarter for an evaluable qPRR
    "min_drug_cases": 10,
}


def quarter_prr(a: int, drug_cases: int, event_cases: int,
                population: int) -> float | None:
    """Continuity-corrected quarterly PRR; None when undefined."""
    if drug_cases <= 0 or population <= drug_cases:
        return None
    a2, b2 = a + 0.5, (drug_cases - a) + 0.5
    c2, d2 = (event_cases - a) + 0.5, \
        (population - drug_cases - event_cases + a) + 0.5
    return round((a2 / (a2 + b2)) / (c2 / (c2 + d2)), 4)


def profile_verdict(series: list[dict], pooled_prr: float | None,
                    thresholds: dict | None = None) -> dict:
    """Verdict for one pair from its quarter-ascending series.

    Each series row: quarter, a, drug_cases, event_cases, population_cases,
    prr (float | None from quarter_prr). `series` must end at the evaluated
    quarter. Returns verdict + metrics + reason (never raises on sparse
    history: that is `insufficient`).
    """
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    if not series:
        return {"verdict": "insufficient", "flagged": False,
                "quarters_evaluable": 0, "quarters_elevated": 0,
                "quarters_below_background": 0,
                "baseline_median_prr": None, "latest_prr": None,
                "pooled_prr": pooled_prr,
                "reason": "no quarterly data for this pair"}
    ev = [r for r in series
          if r.get("prr") is not None
          and r["drug_cases"] >= th["min_drug_cases"]]
    baseline = [r for r in ev if r is not series[-1]]
    latest = series[-1] if series else None
    latest_ev = (latest is not None and latest.get("prr") is not None
                 and latest["drug_cases"] >= th["min_drug_cases"])

    base = {
        "verdict": "insufficient",
        "flagged": False,
        "quarters_evaluable": len(ev),
        "quarters_elevated": 0,
        "quarters_below_background": 0,
        "baseline_median_prr": None,
        "latest_prr": None,
        "pooled_prr": pooled_prr,
    }
    if len(baseline) < th["min_baseline_quarters"] or not latest_ev:
        base["reason"] = (
            f"insufficient history: {len(baseline)} evaluable baseline "
            f"quarters (< {th['min_baseline_quarters']}) "
            f"{'or unevaluable latest quarter' if not latest_ev else ''}"
        ).strip()
        return base

    baseline_prrs = sorted(float(r["prr"]) for r in baseline)
    mid = len(baseline_prrs) // 2
    median = (baseline_prrs[mid] if len(baseline_prrs) % 2
              else (baseline_prrs[mid - 1] + baseline_prrs[mid]) / 2)
    latest_prr = float(latest["prr"])
    elevated = sum(1 for r in ev if r["prr"] >= th["elevated_prr"])
    below = sum(1 for r in ev if r["prr"] < th["background_prr"])

    base.update({
        "quarters_elevated": elevated,
        "quarters_below_background": below,
        "baseline_median_prr": round(median, 4),
        "latest_prr": round(latest_prr, 4),
    })

    pooled_ok = (pooled_prr is not None
                 and pooled_prr >= th["elevated_prr"])
    if (pooled_ok and median < th["background_prr"]
            and latest_prr >= th["elevated_prr"]):
        base["verdict"] = "elevation_is_new"
        base["flagged"] = True
        base["reason"] = (
            f"pooled PRR {pooled_prr:.2f} is elevated but the baseline "
            f"quarterly PRR median is {median:.2f} (< "
            f"{th['background_prr']:g}) across {len(baseline)} quarters; "
            f"elevation holds in only {elevated}/{len(ev)} quarters "
            f"(latest {latest_prr:.2f}) — pooled elevation is a new-quarter "
            "reporting regime, review before reading as risk")
    elif elevated == len(ev):
        base["verdict"] = "consistent"
        base["reason"] = (
            f"elevated quarterly PRR in all {len(ev)} quarters "
            f"(latest {latest_prr:.2f}, pooled "
            f"{pooled_prr if pooled_prr is not None else 'n/a'}) — the "
            "pooled statistic is historically supported (systematic "
            "pattern, not a new-quarter artifact)")
    else:
        base["verdict"] = "mixed"
        base["reason"] = (
            f"elevated in {elevated}/{len(ev)} quarters, baseline median "
            f"{median:.2f} (latest {latest_prr:.2f}) — mixed quarterly "
            "history; read the series before the pooled value")
    return base


def _ranked_signals(conn, top: int, ps_mode: bool) -> list[tuple]:
    """Top screened signals by pooled case depth (probe's own ranking)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT drug, event, case_count, prr
            FROM signal_scores
            WHERE period = 'all' AND primary_suspect_mode = %s
              AND prr_screen
            ORDER BY case_count DESC, chi_square DESC NULLS LAST,
                     drug, event
            LIMIT %s
        """, (ps_mode, top))
        rows = cur.fetchall()
    conn.rollback()
    return rows


def quarterly_profiles(conn, top: int = 200, mode: str = "all",
                       min_cases: int = 3,
                       thresholds: dict | None = None) -> dict:
    """Run the profile checks over the ranked screen; returns the report.

    Never raises on an empty database: zero signals is a valid result.
    """
    th = {**DEFAULT_THRESHOLDS, "max_candidates": int(top),
          "min_cases": int(min_cases), **(thresholds or {})}
    ps_mode = mode == "primary-suspect"
    ranked = _ranked_signals(conn, int(top), ps_mode)
    empty = {
        "status": "ok", "mode": mode, "evaluated_quarter": None,
        "signals_evaluated": 0, "parameters": th,
        "flagged": [], "verdict_counts": {},
        "generated_at":
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if not ranked:
        return empty
    signals = {(str(d), str(e)): (int(c), p) for d, e, c, p in ranked}
    drugs = sorted({d for d, _ in signals})
    events = sorted({e for _, e in signals})

    # ---- gather: pair volume, drug exposure, event volume, population ----
    from app.statistics.engine import PRIMARY_SUSPECT_ROLES

    role_filter = ("AND d.role IN "
                   f"({', '.join(repr(r) for r in PRIMARY_SUSPECT_ROLES)})"
                   if ps_mode else "")
    with conn.cursor() as cur:
        cur.execute("SET enable_nestloop = off")
        try:
            cur.execute("""
                SELECT sq.drug, sq.event, sq.quarter, sq.a
                FROM signal_quarterly sq
                WHERE sq.primary_suspect_mode = %s
                  AND sq.drug = ANY(%s) AND sq.event = ANY(%s)
            """, (ps_mode, drugs, events))
            pair_rows = cur.fetchall()
            cur.execute(f"""
                SELECT d.drug_normalized, c.source_quarter,
                       count(DISTINCT c.primaryid)
                FROM case_drugs d
                JOIN cases c ON c.primaryid = d.primaryid AND c.is_current
                WHERE c.is_current AND d.drug_normalized = ANY(%s)
                      {role_filter}
                GROUP BY 1, 2
            """, (drugs,))
            drug_rows = cur.fetchall()
            # event volume: the events table is large and the pair list is a
            # bare array, so force hash joins (same rationale as regime.py)
            cur.execute("""
                SELECT ev.event_term, c.source_quarter, count(*)
                FROM case_events ev
                JOIN cases c ON c.primaryid = ev.primaryid AND c.is_current
                WHERE ev.event_term = ANY(%s)
                GROUP BY 1, 2
            """, (events,))
            event_rows = cur.fetchall()
            cur.execute("""
                SELECT source_quarter, count(*) FROM cases
                WHERE is_current GROUP BY 1
            """)
            pop_rows = cur.fetchall()
        except BaseException:
            conn.rollback()   # reverts the transactional SET
            raise
        cur.execute("RESET enable_nestloop")

    population = {q: int(n) for q, n in pop_rows}
    if not population:
        return empty
    evaluated_quarter = max(population)
    pair_a: dict[tuple, dict] = {}
    for drug, event, quarter, a in pair_rows:
        pair_a.setdefault((drug, event), {})[quarter] = int(a)
    drug_cases: dict[tuple, dict] = {}
    for drug, quarter, n in drug_rows:
        drug_cases.setdefault(drug, {})[quarter] = int(n)
    event_cases: dict[str, dict] = {}
    for event, quarter, n in event_rows:
        event_cases.setdefault(event, {})[quarter] = int(n)

    # ---- verdicts ---------------------------------------------------------
    flagged: list[dict] = []
    counts: dict[str, int] = {}
    quarters = sorted(population)
    for (drug, event), (case_count, pooled_prr) in signals.items():
        series = []
        for q in quarters:
            a = pair_a.get((drug, event), {}).get(q, 0)
            dc = drug_cases.get(drug, {}).get(q, 0)
            ec = event_cases.get(event, {}).get(q, 0)
            if ec < a:          # mode mismatch guard; cannot happen in all-mode
                ec = a
            series.append({
                "quarter": q, "a": a, "drug_cases": dc,
                "event_cases": ec, "population_cases": population[q],
                "prr": quarter_prr(a, dc, ec, population[q]),
            })
        verdict = profile_verdict(series, pooled_prr, th)
        counts[verdict["verdict"]] = counts.get(verdict["verdict"], 0) + 1
        if verdict["flagged"]:
            flagged.append({
                "drug": drug, "event": event,
                "quarter": evaluated_quarter,
                "case_count": case_count,
                **{k: v for k, v in verdict.items() if k != "flagged"},
                "series": series,
            })

    return {
        "status": "ok",
        "mode": mode,
        "evaluated_quarter": evaluated_quarter,
        "signals_evaluated": len(signals),
        "parameters": th,
        "flagged": flagged,
        "verdict_counts": counts,
        "generated_at":
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
