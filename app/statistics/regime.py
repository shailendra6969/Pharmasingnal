"""Quarterly reporting-regime detector (spec: next-tier artifact screening).

Two data-collection regimes masquerade as drug-event signals and must be
flagged automatically on every quarterly refresh (stage 5 of
scripts/refresh_quarter.py), instead of by hand:

bulk_single_event  A one-quarter volume burst whose cases are almost all
                   single-event submissions (the case lists only that one
                   reaction) — the DAPAGLIFLOZIN/DEATH 2025q3–q4 pattern:
                   a bulk load of minimal reports, not new risk evidence.

notoriety_ramp     The event's share of the DRUG's own quarterly cases rises
                   steadily over several quarters (regulatory attention
                   driving reporting) — the DEPO-PROVERA/MENINGIOMA
                   within-drug-share 0% -> 73% pattern. Volume-normalized,
                   so a busy drug cannot trigger it.

Detection runs on candidates: pairs that pass the current screen
(signal_scores period='all', prr_screen OR ror_screen) AND have a >=
min_cases co-occurrences in the LATEST quarter of signal_quarterly (the
quarter just refreshed), ranked by that quarter's case depth and capped at
the top `max_candidates` — the same case-depth ranking the manual artifact
investigations used, bounded so a full-background screen (~400k active
pairs) stays a one-minute stage. Mode: all-reports (primary_suspect_mode =
FALSE), falling back to TRUE when only that mode was computed. Per-quarter
series come from signal_quarterly (a) plus live case-level joins
(single-event share, drug totals).

The two flags are deliberately mutually exclusive per pair: a one-quarter
bulk artifact cannot also claim to be a gradual ramp (evaluate_pair checks
bulk first and suppresses the ramp). Screening judgments only — never
causality or risk (spec §5).
"""
from __future__ import annotations

from datetime import datetime, timezone

# Thresholds are module constants so the report can record exactly what was
# applied; every one is overridable per call for tests and experimentation.
DEFAULT_THRESHOLDS: dict = {
    # minimum quarters before the evaluated one (bulk needs a baseline to
    # be a *spike*; ramp needs enough steps to be *gradual*)
    "min_baseline_quarters": 2,
    # bulk: latest-quarter volume vs median of prior quarters
    "volume_spike_ratio": 3.0,
    # bulk: share of pair cases whose whole report lists exactly one reaction
    "single_event_share_min": 0.70,
    # bulk: ...and how far above the pair's own baseline share
    "single_event_share_delta": 0.15,
    # ramp: latest within-drug share vs mean of prior quarters' shares
    "ramp_share_ratio": 3.0,
    # ramp: quarters where the drug itself has at least this many cases
    # (a share built on 3 reports is noise, not a trend)
    "ramp_min_drug_cases": 10,
    # ramp: required quarter-over-quarter increases among the last
    # transitions of the defined shares (2 of the last 3, or all when only
    # two transitions exist)
    "ramp_min_increases": 2,
    # ramp: a share step smaller than this is noise, not a trend step —
    # without it a flat baseline (1% -> 1.5%) plus one 20x burst reads as
    # "two consecutive increases"
    "ramp_min_step": 0.01,
    # candidate floor (defaults to the refresh's --min-cases)
    "min_cases": 3,
}


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n == 0:
        raise ValueError("median of empty sequence")
    mid = n // 2
    return float(s[mid]) if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _is_volume_spike(series: list[dict], thresholds: dict) -> bool:
    """Latest quarter's volume vs the median of all prior quarters.

    Shared by the bulk pre-filter (skip the expensive single-event fill for
    pairs that cannot possibly burst) and evaluate_pair itself.
    """
    if not series or len(series) - 1 < thresholds["min_baseline_quarters"]:
        return False
    a_last = int(series[-1]["a"])
    if a_last < thresholds["min_cases"]:
        return False
    med = _median([float(int(r["a"])) for r in series[:-1]])
    if med > 0:
        return a_last >= thresholds["volume_spike_ratio"] * med
    return a_last > 0   # never reported before -> any burst is one


def evaluate_pair(series: list[dict], thresholds: dict | None = None
                  ) -> list[dict]:
    """Pure detection logic for one drug-event pair.

    `series` is quarter-ascending dicts with keys:
        quarter, a (co-occurrences), single_event_cases (pair cases whose
        report lists exactly one reaction), drug_cases (drug's cases that
        quarter; 0/None = unknown).
    Returns the flags fired for the LATEST quarter in the series ([] = the
    pair's reporting regime looks ordinary).
    """
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    if not series:
        return []
    last = series[-1]
    baseline = series[:-1]
    if len(baseline) < th["min_baseline_quarters"]:
        return []   # not enough history to call anything a regime change

    quarter = last["quarter"]
    a_last = int(last["a"])
    single_last = int(last.get("single_event_cases") or 0)
    single_share_last = (single_last / a_last) if a_last else 0.0

    # ---- bulk single-event burst -----------------------------------------
    a_baseline = [int(r["a"]) for r in baseline]
    med = _median([float(x) for x in a_baseline])
    volume_spike = _is_volume_spike(series, th)
    # baseline single share only over quarters that actually reported the pair
    base_shares = [int(r.get("single_event_cases") or 0) / int(r["a"])
                   for r in baseline if int(r["a"]) > 0]
    base_single_mean = _mean(base_shares) if base_shares else 0.0
    bulk = (
        a_last >= th["min_cases"]
        and volume_spike
        and single_share_last >= th["single_event_share_min"]
        and single_share_last >= base_single_mean
        + th["single_event_share_delta"]
    )
    flags: list[dict] = []
    if bulk:
        flags.append({
            "flag": "bulk_single_event",
            "drug": last.get("drug"),
            "event": last.get("event"),
            "quarter": quarter,
            "metrics": {
                "cases": a_last,
                "baseline_median_cases": med,
                "volume_ratio": round(a_last / med, 2) if med > 0 else None,
                "single_event_cases": single_last,
                "single_event_share": round(single_share_last, 3),
                "baseline_single_event_share": round(base_single_mean, 3),
            },
            "reason": (
                f"{a_last} cases in {quarter} vs baseline median {med:g}, "
                f"{single_share_last:.0%} single-event reports "
                f"(baseline {base_single_mean:.0%}) — bulk submission "
                "burst, not new risk evidence"),
        })

    # ---- notoriety ramp ---------------------------------------------------
    # A bulk artifact is a jump, not a ramp: if bulk fired, suppress ramp so
    # the two detectors stay distinct.
    if not bulk:
        def share(r: dict) -> float | None:
            dc = int(r.get("drug_cases") or 0)
            if dc < th["ramp_min_drug_cases"]:
                return None
            return int(r["a"]) / dc

        defined = [(r["quarter"], share(r)) for r in series]
        defined = [(q, s) for q, s in defined if s is not None]
        # need the latest share + at least min_baseline_quarters of history
        if (len(defined) >= th["min_baseline_quarters"] + 1
                and defined[-1][0] == quarter):
            s_last = defined[-1][1]
            s_base = [s for _, s in defined[:-1]]
            base_mean = _mean(s_base)
            ratio_ok = (s_last >= th["ramp_share_ratio"] * base_mean
                        if base_mean > 0 else s_last > 0)
            tail = [s for _, s in defined[-(th["ramp_min_increases"] + 2):]]
            # tail spans increases+1 points -> at least increases transitions;
            # a transition only counts when the share moved by at least
            # ramp_min_step (noise steps are not a trend), and the last step
            # must qualify too (still ramping now)
            steps = [tail[i] - tail[i - 1] for i in range(1, len(tail))]
            counted = sum(1 for s in steps if s >= th["ramp_min_step"])
            trend_ok = (counted >= th["ramp_min_increases"]
                        and steps[-1] >= th["ramp_min_step"])
            peak_ok = s_last >= max(s for _, s in defined)   # peaks now
            if ratio_ok and trend_ok and peak_ok:
                flags.append({
                    "flag": "notoriety_ramp",
                    "drug": last.get("drug"),
                    "event": last.get("event"),
                    "quarter": quarter,
                    "metrics": {
                        "within_drug_share": round(s_last, 3),
                        "baseline_share_mean": round(base_mean, 3),
                        "share_ratio": round(s_last / base_mean, 2)
                        if base_mean > 0 else None,
                        "quarters_with_share": len(defined),
                        "cases": a_last,
                        "drug_cases": int(last.get("drug_cases") or 0),
                    },
                    "reason": (
                        f"event share of {last.get('drug')} cases rose to "
                        f"{s_last:.0%} in {quarter} (baseline mean "
                        f"{base_mean:.0%}) across {len(defined)} quarters — "
                        "reporting-regime shift (notoriety), review before "
                        "reading as risk"),
                })
    return flags


def _candidates(conn, min_cases: int, max_candidates: int
                ) -> tuple[list[tuple[str, str]], str | None, bool | None]:
    """Screened pairs active in the latest quarter, deepest case count first.

    Prefers the all-reports mode; falls back to primary-suspect rows when
    only that mode was computed (--mode primary-suspect refresh).
    """
    for mode in (False, True):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.drug, s.event, q.quarter
                FROM signal_quarterly q
                JOIN signal_scores s
                  ON s.drug = q.drug AND s.event = q.event
                 AND s.period = 'all'
                 AND s.primary_suspect_mode = q.primary_suspect_mode
                 AND (s.prr_screen OR s.ror_screen)
                WHERE q.primary_suspect_mode = %s
                  AND q.a >= %s
                  AND q.quarter = (
                      SELECT MAX(quarter) FROM signal_quarterly
                      WHERE primary_suspect_mode = %s)
                ORDER BY q.a DESC, s.drug, s.event
                LIMIT %s
                """,
                (mode, min_cases, mode, max_candidates),
            )
            rows = cur.fetchall()
        if rows:
            return [(r[0], r[1]) for r in rows], rows[0][2], mode
    return [], None, None


def _pair_series(conn, pairs: list[tuple[str, str]], mode: bool) -> dict:
    """{pair: [series rows]} from signal_quarterly + drug quarterly totals."""
    drugs = sorted({p[0] for p in pairs})
    drugs_sql = [p[0] for p in pairs]
    events_sql = [p[1] for p in pairs]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT sq.drug, sq.event, sq.quarter, sq.a, dq.drug_cases
            FROM signal_quarterly sq
            JOIN unnest(%s::text[], %s::text[]) AS c(drug, event)
              ON c.drug = sq.drug AND c.event = sq.event
            LEFT JOIN (
                SELECT cd.drug_normalized AS drug,
                       cs.source_quarter AS quarter,
                       COUNT(DISTINCT cs.primaryid) AS drug_cases
                FROM case_drugs cd
                JOIN cases cs ON cs.primaryid = cd.primaryid
                WHERE cs.is_current AND cd.drug_normalized = ANY(%s)
                GROUP BY 1, 2
            ) dq ON dq.drug = sq.drug AND dq.quarter = sq.quarter
            WHERE sq.primary_suspect_mode = %s
            ORDER BY sq.drug, sq.event, sq.quarter
            """,
            (drugs_sql, events_sql, drugs, mode),
        )
        rows = cur.fetchall()
    out: dict[tuple[str, str], list[dict]] = {}
    for drug, event, quarter, a, drug_cases in rows:
        out.setdefault((drug, event), []).append({
            "drug": drug, "event": event,   # so flags can carry identity
            "quarter": quarter, "a": int(a),
            "drug_cases": int(drug_cases or 0),
            "single_event_cases": 0,   # filled in _fill_single_event
        })
    return out


def _fill_single_event(conn, pairs: list[tuple[str, str]],
                       series: dict) -> None:
    """Annotate each series row with how many pair cases were single-event
    reports (the case's whole REAC listing is just this one event)."""
    if not pairs:
        return
    drugs = [p[0] for p in pairs]
    events = [p[1] for p in pairs]
    with conn.cursor() as cur:
        # The pair list is an unnest() array, so Postgres has no statistics
        # for it and under-estimates the join ~60x (200 rows est vs 95k
        # actual on the live DB) — it picks per-row index probes and burns
        # 141s. Forcing hash joins for THIS statement only makes the choice
        # data-driven: measured 141s -> 22s over the top spike pairs.
        # SET is transactional: a failure below rolls back with it.
        cur.execute("SET enable_nestloop = off")
        try:
            cur.execute(
                """
            WITH c2 AS (
                SELECT d.drug, d.event
                FROM unnest(%s::text[], %s::text[]) AS d(drug, event)
            ),
            sel AS (
                SELECT DISTINCT c2.drug, c2.event, cs.primaryid,
                                cs.source_quarter AS quarter
                FROM c2
                JOIN LATERAL (
                    SELECT cd.primaryid FROM case_drugs cd
                    WHERE cd.drug_normalized = c2.drug
                ) x ON true
                JOIN cases cs ON cs.primaryid = x.primaryid
                             AND cs.is_current
                JOIN case_events ce ON ce.primaryid = cs.primaryid
                                   AND ce.event_term = c2.event
            ),
            n_events AS (
                SELECT s.primaryid,
                       COUNT(DISTINCT ce.event_term) AS n
                FROM (SELECT DISTINCT primaryid FROM sel) s
                JOIN case_events ce ON ce.primaryid = s.primaryid
                GROUP BY s.primaryid
            )
            SELECT s.drug, s.event, s.quarter,
                   COUNT(*) FILTER (WHERE n.n = 1) AS single_event_cases
            FROM sel s
            JOIN n_events n ON n.primaryid = s.primaryid
            GROUP BY 1, 2, 3
            """,
                (drugs, events),
            )
            rows = cur.fetchall()
        except BaseException:
            conn.rollback()   # also undoes the SET (transactional)
            raise
        cur.execute("RESET enable_nestloop")
        for drug, event, quarter, single in rows:
            for row in series.get((drug, event), []):
                if row["quarter"] == quarter:
                    row["single_event_cases"] = int(single or 0)


def detect_regimes(conn, min_cases: int = 3, max_candidates: int = 1000,
                   thresholds: dict | None = None) -> dict:
    """Run the detector over the latest quarter; returns the stage report.

    Never raises on an empty database: zero candidates is a valid result.
    """
    th = {**DEFAULT_THRESHOLDS, "min_cases": int(min_cases),
          "max_candidates": int(max_candidates), **(thresholds or {})}
    pairs, evaluated_quarter, mode = _candidates(
        conn, int(min_cases), int(max_candidates))

    flags: list[dict] = []
    series_map = _pair_series(conn, pairs, mode) if pairs else {}
    # Only a volume spike can be a bulk submission burst, and only the bulk
    # flag reads single-event counts (the ramp is share-based) — so pay the
    # case-level fill for spike pairs only.
    spike_pairs = [p for p in pairs
                   if _is_volume_spike(series_map.get(p) or [], th)]
    _fill_single_event(conn, spike_pairs, series_map)
    if not pairs:
        return {
            "status": "ok",
            "evaluated_quarter": evaluated_quarter,
            "candidates_evaluated": 0,
            "parameters": th,
            "flags": [],
            "flag_counts": {"bulk_single_event": 0, "notoriety_ramp": 0},
            "generated_at":
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    for pair in pairs:
        series = series_map.get(pair)
        if not series:
            continue
        for flag in evaluate_pair(series, th):
            flag["drug"], flag["event"] = pair[0], pair[1]
            flags.append(flag)

    counts = {"bulk_single_event": 0, "notoriety_ramp": 0}
    for f in flags:
        counts[f["flag"]] = counts.get(f["flag"], 0) + 1
    return {
        "status": "ok",
        "evaluated_quarter": evaluated_quarter,
        "candidates_evaluated": len(pairs),
        "parameters": th,
        "flags": flags,
        "flag_counts": counts,
        "generated_at":
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
