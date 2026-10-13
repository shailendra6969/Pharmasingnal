"""Automated indication-bias probe (screening diagnostic, never causality).

Motivated by the DUPIXENT/CONDITION AGGRAVATED investigation
(data/processed/signal_investigation_orgovyx_dupixent.json): disproportion-
ality cannot correct confounding by indication, and SIDER-based flagging is
blind when the drug is missing from SIDER's vocabulary. This module scores
every top signal on two reference-independent enrichment dimensions:

  co-event enrichment   share of pair cases that ALSO report a treated-
                        disease term, vs the same share across ALL current
                        cases of the drug. Terms = the drug's SIDER labeled
                        indications when the reference covers it, else a
                        generic disease-term fallback; both are unioned with
                        the disease-flare terms.
  treatment-failure     same construction over utilization/failure terms
  enrichment            (OFF LABEL USE, DRUG INEFFECTIVE, ...).

A signal is FLAGGED when either dimension shows enrichment >= min_enrichment
with pair-share >= min_share: the pair's case set is systematically sicker
or more treatment-failing than the drug's own baseline, so part of its
reporting volume is the disease, not the drug. Flagged means "review as
possibly confounded", never "false" — all computed from current versions
only (spec §7), and a signal's own event term never counts as its own
co-event.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

# MedDRA terms that are utilization/administration outcomes, not adverse
# events: product labels list no "side effect" for them, so SIDER (label-
# derived) can never contain these pairs. A large share of top signals here
# would otherwise be miscounted as discordant "false positives".
NON_ADVERSE_EVENT_TERMS = frozenset({
    "OFF LABEL USE", "DRUG INEFFECTIVE", "DRUG DISTRIBUTION",
    "PRODUCT QUALITY ISSUE", "MEDICATION ERROR", "DRUG ABUSE",
    "DRUG DEPENDENCE", "THERAPY NON-RESPONDER", "OVERDOSE",
    "DRUG INTERACTION", "WRONG DRUG ADMINISTERED",
    "UNLABELLED DRUG PRODUCT USE", "DRUG THERAPY PROBLEM",
    "SELF MEDICATION", "DRUG WITHDRAWAL", "TREATMENT FAILURE",
    "PRODUCT COMPROMISE", "EXPIRED DRUG PRODUCT",
    "PRODUCT USE IN UNAPPROVED INDICATION",
})

# MedDRA terms that report the underlying disease getting worse, not a
# pharmacologic adverse reaction of the drug. Confounded by indication by
# construction (the drug is prescribed BECAUSE the disease is severe or
# flaring); SIDER-derived pairs cannot judge them, and the drugs carrying
# them are often newer than SIDER's vocabulary entirely. Every term here was
# verified present in the DB vocabulary (CONDITION AGGRAVATED 59k current
# cases, DISEASE PROGRESSION 18k, ...).
DISEASE_FLARE_EVENT_TERMS = frozenset({
    "CONDITION AGGRAVATED", "DISEASE PROGRESSION", "DISEASE COMPLICATION",
    "GENERAL PHYSICAL HEALTH DETERIORATION", "DRUG RESISTANCE",
})

# Fallback treated-disease terms for drugs SIDER does not cover (e.g.
# DUPIXENT): common indication PTs that FAERS reporters also enter as event
# terms. Every entry verified present in the DB vocabulary; symptoms that
# are not diseases (PRURITUS, PAIN) are deliberately excluded. Drugs SIDER
# DOES cover use their own SIDER indications instead — this list is only the
# blind-spot fallback.
GENERIC_DISEASE_TERMS = frozenset({
    "DERMATITIS ATOPIC", "ECZEMA", "ASTHMA", "NASAL POLYPS",
    "RHINITIS ALLERGIC", "URTICARIA", "CHRONIC SPONTANEOUS URTICARIA",
    "RHEUMATOID ARTHRITIS", "PSORIASIS", "PSORIATIC ARTHROPATHY",
    "PROSTATE CANCER", "PROSTATE CANCER METASTATIC", "MYELOFIBROSIS",
    "TYPE 2 DIABETES MELLITUS", "MAJOR DEPRESSION", "MIGRAINE",
    "HYPERTENSION", "CHRONIC OBSTRUCTIVE PULMONARY DISEASE",
    "INFLAMMATORY BOWEL DISEASE", "CROHN'S DISEASE", "COLITIS ULCERATIVE",
    "MULTIPLE SCLEROSIS", "LUPUS NEPHRITIS", "SYSTEMIC LUPUS ERYTHEMATOSUS",
})

PROBE_TABLE = "probe_pairs"          # (drug, event) signals under probe


@dataclass
class Enrichment:
    """Share of cases carrying a probe term, pair vs drug baseline."""
    share_pair: float
    share_base: float
    enrichment: float | None         # None when the baseline share is 0
    pair_hits: int
    base_hits: int


@dataclass
class ProbeScore:
    drug: str
    event: str
    case_count: int
    prr: float | None
    pair_cases: int
    drug_cases: int
    coevent: Enrichment
    treatment_failure: Enrichment
    flagged: bool = False
    reasons: list[str] = field(default_factory=list)
    top_coevents: list[dict] = field(default_factory=list)


def _enrichment(share_pair: float, share_base: float,
                pair_hits: int, base_hits: int) -> Enrichment:
    ratio = (share_pair / share_base) if share_base > 0 else None
    return Enrichment(share_pair=round(share_pair, 4),
                      share_base=round(share_base, 4),
                      enrichment=round(ratio, 3) if ratio is not None else None,
                      pair_hits=pair_hits, base_hits=base_hits)


def _indication_terms_by_drug(
        indication_pairs: set[tuple[str, str]] | None) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for drug, event in indication_pairs or ():
        out.setdefault(drug, set()).add(event)
    return out


def _probe_term_arrays(signals, ind_by_drug) -> list[tuple]:
    """One (drug, event, ind_terms[], tf_terms[]) row per signal.

    Terms are aggregated per signal so the SQL join is per-signal, not
    per-signal-per-term: with ~30 terms per signal the per-term join form
    multiplies the drug-case scan by ~30 and takes >5 min on blockbuster
    drugs (measured), while the array form stays in seconds.

    The signal's own event is always excluded so a pair can never report
    100% co-occurrence with itself.
    """
    out = []
    for drug, event, _, _ in signals:
        base = ind_by_drug.get(drug) or GENERIC_DISEASE_TERMS
        out.append((drug, event,
                    sorted((base | DISEASE_FLARE_EVENT_TERMS) - {event}),
                    sorted(NON_ADVERSE_EVENT_TERMS - {event})))
    return out


def probe_signals(conn, signals: list[tuple],
                  indication_pairs: set[tuple[str, str]] | None = None,
                  min_enrichment: float = 1.5,
                  min_share: float = 0.10,
                  top_coevents_limit: int = 5,
                  progress: bool = False) -> list[ProbeScore]:
    """Score each (drug, event, case_count, prr) signal for indication bias.

    Batched, and now structured around **one-time per-drug indexes** (the
    per-drug base dedup): instead of five full drug-case scans per batch
    (measured 250s+ per query on the live 8-quarter DB), two heavy temp
    tables are built once — `probe_pair_cases` (which cases carry each
    probed pair) and `probe_case_terms` (which probe vocabulary terms each
    probed drug's cases carry) — and every numerator/denominator aggregates
    over those. Heavy builds run with hash joins forced (`enable_nestloop
    = off`): temp tables carry no planner statistics, so the default plan
    under-estimates ~60x and picks per-row index probes (measured 250s ->
    22s for one build). Numbers are identical to the per-signal form — the
    signal's own event never appears in its term arrays, so array membership
    implies the old `<> event` exclusion.
    """
    started = time.time()
    signals = [(str(d), str(e), int(c), float(p) if p is not None else None)
               for d, e, c, p in signals]
    if not signals:
        return []

    cur = conn.cursor()
    ind_by_drug = _indication_terms_by_drug(indication_pairs)
    term_rows = _probe_term_arrays(signals, ind_by_drug)
    drugs = sorted({d for d, _, _, _ in signals})
    vocab = sorted({t for _, _, ind, tf in term_rows for t in ind}
                   | {t for _, _, _, tf in term_rows for t in tf})

    def run(sql: str, params: tuple = ()) -> list[tuple]:
        cur.execute(sql, params)
        if cur.description is None:   # DDL/DML (DROP/CREATE) returns no rows
            return []
        return cur.fetchall()

    def keyed(rows) -> dict:
        """{(drug, event): count} from 3-column aggregate results."""
        return {(r[0], r[1]): int(r[2]) for r in rows}

    try:
        run("SET enable_nestloop = off")
        from psycopg2.extras import execute_values
        run(f"DROP TABLE IF EXISTS {PROBE_TABLE}")
        run("DROP TABLE IF EXISTS probe_terms")
        run("DROP TABLE IF EXISTS probe_pair_cases")
        run("DROP TABLE IF EXISTS probe_case_terms")
        run("DROP TABLE IF EXISTS probe_flagged")
        run(f"CREATE TEMP TABLE {PROBE_TABLE} (drug TEXT, event TEXT)")
        execute_values(cur, f"INSERT INTO {PROBE_TABLE} VALUES %s",
                       [(d, e) for d, e, _, _ in signals])
        run(f"CREATE INDEX ON {PROBE_TABLE} (drug, event)")
        run("CREATE TEMP TABLE probe_terms (drug TEXT, event TEXT, "
            "ind_terms TEXT[], tf_terms TEXT[])")
        execute_values(cur, "INSERT INTO probe_terms VALUES %s", term_rows)

        # One-time heavy builds (scale with DRUGS, not signals) -----------
        run(f"""
            CREATE TEMP TABLE probe_pair_cases AS
            SELECT DISTINCT p.drug, p.event, cc.primaryid
            FROM {PROBE_TABLE} p
            JOIN case_drugs dr ON dr.drug_normalized = p.drug
            JOIN cases cc ON cc.primaryid = dr.primaryid AND cc.is_current
            JOIN case_events ev ON ev.primaryid = cc.primaryid
                               AND ev.event_term = p.event
        """)
        run("CREATE INDEX ON probe_pair_cases (primaryid)")
        run("""
            CREATE TEMP TABLE probe_case_terms AS
            SELECT dr.drug_normalized AS drug, cc.primaryid,
                   array_agg(DISTINCT ev.event_term) AS terms
            FROM case_drugs dr
            JOIN cases cc ON cc.primaryid = dr.primaryid AND cc.is_current
            JOIN case_events ev ON ev.primaryid = cc.primaryid
            WHERE dr.drug_normalized = ANY(%s)
              AND ev.event_term = ANY(%s)
            GROUP BY 1, 2
        """, (drugs, vocab))
        run("CREATE INDEX ON probe_case_terms (primaryid)")

        # Denominators -----------------------------------------------------
        drug_cases = {r[0]: int(r[1]) for r in run("""
            SELECT dr.drug_normalized, count(DISTINCT cc.primaryid)
            FROM case_drugs dr JOIN cases cc ON cc.primaryid = dr.primaryid
            WHERE cc.is_current AND dr.drug_normalized = ANY(%s)
            GROUP BY 1
        """, (drugs,))}
        pair_cases = keyed(run(f"""
            SELECT drug, event, count(*) FROM probe_pair_cases GROUP BY 1, 2
        """))

        # Numerators: pair-case and drug-baseline co-report shares.
        # Pair = cases carrying BOTH the signal event and a probe term;
        # base = any case of the drug carrying a probe term (drug-wide).
        def hits(term_col: str) -> tuple[dict, dict]:
            pair = keyed(run(f"""
                SELECT p.drug, p.event, count(DISTINCT pc.primaryid)
                FROM {PROBE_TABLE} p
                JOIN probe_pair_cases pc
                  ON pc.drug = p.drug AND pc.event = p.event
                JOIN probe_case_terms ct
                  ON ct.drug = p.drug AND ct.primaryid = pc.primaryid
                JOIN probe_terms t ON t.drug = p.drug AND t.event = p.event
                WHERE ct.terms && t.{term_col}
                GROUP BY 1, 2
            """))
            base = keyed(run(f"""
                SELECT p.drug, p.event, count(DISTINCT ct.primaryid)
                FROM {PROBE_TABLE} p
                JOIN probe_case_terms ct ON ct.drug = p.drug
                JOIN probe_terms t ON t.drug = p.drug AND t.event = p.event
                WHERE ct.terms && t.{term_col}
                GROUP BY 1, 2
            """))
            return pair, base

        ind_pair, ind_base = hits("ind_terms")
        tf_pair, tf_base = hits("tf_terms")

        scores: list[ProbeScore] = []
        flagged_rows: list[tuple] = []
        for drug, event, case_count, prr in signals:
            n_pair = pair_cases.get((drug, event), 0)
            n_drug = drug_cases.get(drug, 0)
            ind_e = _enrichment(ind_pair.get((drug, event), 0) / n_pair if n_pair else 0.0,
                                ind_base.get((drug, event), 0) / n_drug if n_drug else 0.0,
                                ind_pair.get((drug, event), 0),
                                ind_base.get((drug, event), 0))
            tf_e = _enrichment(tf_pair.get((drug, event), 0) / n_pair if n_pair else 0.0,
                               tf_base.get((drug, event), 0) / n_drug if n_drug else 0.0,
                               tf_pair.get((drug, event), 0),
                               tf_base.get((drug, event), 0))
            score = ProbeScore(drug=drug, event=event, case_count=case_count,
                               prr=prr, pair_cases=n_pair, drug_cases=n_drug,
                               coevent=ind_e, treatment_failure=tf_e)
            for label, e in (("co-event", ind_e), ("treatment-failure", tf_e)):
                strong = e.enrichment is not None and e.enrichment >= min_enrichment
                # baseline 0 with nonzero pair hits is unbounded enrichment
                unbounded = e.enrichment is None and e.pair_hits > 0
                if (strong or unbounded) and e.share_pair >= min_share:
                    score.flagged = True
                    score.reasons.append(
                        f"{label} enrichment "
                        f"{'unbounded (base share 0)' if unbounded else f'{e.enrichment}x'}: "
                        f"{e.share_pair:.1%} of pair cases vs {e.share_base:.1%} "
                        f"of all {drug} cases")
            if score.flagged:
                flagged_rows.append((drug, event))
            scores.append(score)

        # Top co-events for flagged signals only (audit trail, like
        # concordance), computed while the index tables still exist.
        if flagged_rows and top_coevents_limit:
            run(f"CREATE TEMP TABLE probe_flagged (drug TEXT, event TEXT)")
            execute_values(cur, "INSERT INTO probe_flagged VALUES %s",
                           flagged_rows)
            cur.execute(f"""
                SELECT f.drug, f.event, ev.event_term,
                       count(DISTINCT ev.primaryid)
                FROM probe_flagged f
                JOIN probe_pair_cases pc
                  ON pc.drug = f.drug AND pc.event = f.event
                JOIN case_events ev ON ev.primaryid = pc.primaryid
                                  AND ev.event_term <> f.event
                GROUP BY 1, 2, 3 ORDER BY 1, 2, 4 DESC
            """)
            by_pair: dict[tuple, list[dict]] = {}
            for drug, event, term, n in cur.fetchall():
                bucket = by_pair.setdefault((drug, event), [])
                if len(bucket) < top_coevents_limit:
                    bucket.append({"event": term, "cases": int(n)})
            for score in scores:
                score.top_coevents = by_pair.get((score.drug, score.event), [])

        conn.rollback()  # temp tables are session-scoped; drop staged DDL/DML
    finally:
        try:
            cur.execute("RESET enable_nestloop")
        except Exception:
            # aborted transaction: rollback also reverts the transactional SET
            conn.rollback()
        cur.close()

    if progress:
        print(f"[confounding] probed {len(signals)} signals, "
              f"{sum(1 for s in scores if s.flagged)} flagged "
              f"({time.time() - started:.1f}s)", flush=True)
    return scores
