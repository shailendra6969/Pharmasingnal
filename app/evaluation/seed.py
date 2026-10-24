"""Deterministic synthetic seed for the baseline evaluation set (spec §35).

The baseline cases need a real database: ``signal_scores`` rows for both
pairs under test, label chunks for the drug that must verify, and — just as
important — deliberately no chunks for the drug that must not. This module is
the single source of truth for that dataset: ``scripts/seed_evaluation.py``
(what CI runs before the evaluation) and ``tests/integration/test_evaluation.py``
both call it, so the CI evaluation run and the integration test can never
drift apart.
"""
from __future__ import annotations

from app.rag.spl_parser import DocChunk
from app.repositories import doc_repository

# DailyMed SPL set_id under which the seeded label chunks are filed.
EVALUATION_SET_ID = "test-eval-setid"

# Every seeded pair screens positive; case_count and the confidence limits
# are the values the evaluation's numeric claims are checked against.
SEED_SIGNALS_SQL = """
    INSERT INTO signal_scores (
        drug, event, period, a, b, c, d, case_count,
        prr, prr_lower, prr_upper, ror, ror_lower, ror_upper,
        chi_square, prr_screen, ror_screen, background_n,
        drug_case_count, event_case_count, primary_suspect_mode
    ) VALUES (%s, %s, 'all', 909, 65298, 4134, 2761434, %s,
              %s, %s, %s, %s, %s, %s,
              5437.8, TRUE, TRUE, 2831775,
              66207, 5043, FALSE)
"""

# (drug, event, case_count, prr, prr_lower, prr_upper, ror, ror_lower, ror_upper)
EVALUATION_SIGNALS: list[tuple] = [
    ("ATORVASTATIN", "RHABDOMYOLYSIS", 909, 9.18, 8.55, 9.86, 9.30, 8.65, 10.00),
    # Missing-evidence scenario: a real, screened signal whose drug has no
    # label text at all — numeric claims pass, the evidence gate must not.
    ("PREDNISONE", "FATIGUE", 9356, 2.58, 2.40, 2.77, 2.60, 2.42, 2.79),
]

# Label chunks for the positive control. PREDNISONE is deliberately absent:
# adding chunks for it would silently SKIP the missing-evidence case instead
# of exercising it (see app/evaluation/runner.py).
EVALUATION_CHUNKS: list[DocChunk] = [
    DocChunk(
        chunk_id="eval_atorv_001",
        drug="ATORVASTATIN",
        doc_type="SPL_LABEL",
        section="WARNINGS_AND_PRECAUTIONS",
        subsection="5.1 Myopathy and Rhabdomyolysis",
        text="LIPITOR may cause myopathy and rhabdomyolysis in treated patients.",
        source_url="https://dailymed.example.com/eval-set",
        document_date="2024-04-15",
        chunk_index=1,
        metadata={"set_id": EVALUATION_SET_ID},
    ),
    DocChunk(
        chunk_id="eval_atorv_002",
        drug="ATORVASTATIN",
        doc_type="SPL_LABEL",
        section="ADVERSE_REACTIONS",
        subsection="6.2 Postmarketing Experience",
        text="Cases of rhabdomyolysis have been reported with statin therapy.",
        source_url="https://dailymed.example.com/eval-set",
        document_date="2024-04-15",
        chunk_index=2,
        metadata={"set_id": EVALUATION_SET_ID},
    ),
]

_DRUGS = ("ATORVASTATIN", "PREDNISONE")

CLEANUP_SQL = [
    "DELETE FROM eval_results WHERE question_id LIKE 'eval_%';",
    "DELETE FROM eval_questions WHERE question_id LIKE 'eval_%';",
    f"DELETE FROM doc_chunks WHERE drug IN ({', '.join(repr(d) for d in _DRUGS)});",
    "DELETE FROM signal_scores WHERE (drug = 'ATORVASTATIN' AND event = 'RHABDOMYOLYSIS')"
    " OR (drug = 'PREDNISONE' AND event = 'FATIGUE');",
    "DELETE FROM signal_quarterly WHERE (drug = 'ATORVASTATIN' AND event = 'RHABDOMYOLYSIS')"
    " OR (drug = 'PREDNISONE' AND event = 'FATIGUE');",
]


def cleanup(conn) -> None:
    """Remove every row the evaluation seed owns (idempotent)."""
    with conn.cursor() as cur:
        for sql in CLEANUP_SQL:
            cur.execute(sql)
    conn.commit()


def seed_evaluation_data(conn, *, reset: bool = True) -> None:
    """Seed signals + label chunks for the baseline case set.

    With ``reset`` (the default) the owned rows are removed first, so the
    result is deterministic no matter what the database already held — which
    is exactly what CI needs before ``scripts/run_evaluation.py``.
    """
    if reset:
        cleanup(conn)

    with conn.cursor() as cur:
        for params in EVALUATION_SIGNALS:
            cur.execute(SEED_SIGNALS_SQL, params)
    conn.commit()

    # Commits internally (transaction()); chunks for the positive control only.
    doc_repository.replace_label_chunks(
        conn, "ATORVASTATIN", EVALUATION_SET_ID, EVALUATION_CHUNKS
    )