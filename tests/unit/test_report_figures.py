"""Unit tests for scripts/report_figures.py (migration 004 report figures).

Database-free by design: rendering formats, background-N conservation and the
locate/check/write logic against synthetic markdown. The live-database
verification is `python scripts/report_figures.py --check` (exit 1 on drift).
"""
from __future__ import annotations

import json

import pytest

from scripts.report_figures import (
    FOCUS,
    HEADER_71,
    PAIR_ROWS,
    PROJECTED_SRC,
    SNAPSHOT_SCHEMA,
    PairFigure,
    ReportError,
    TargetFigure,
    build_snapshot,
    check_report,
    diff_snapshot,
    load_snapshot,
    render_71_rows,
    render_72_rows,
    render_73_rows,
    table_data_span,
    write_report,
)

ALL_BRAND_COUNTS = {
    "LIPITOR": 11_712,
    "GLUCOPHAGE": 1_745,
    "GLUCOPHAGE XR": 428,
    "ADVIL": 5_225,
    "MOTRIN": 1_447,
    "PRILOSEC": 3_890,
    "ZOLOFT": 6_651,
}


# ---------------------------------------------------------------------------
# §7.1 / §7.2 rendering
# ---------------------------------------------------------------------------

def test_render_71_rows_exact_format():
    rows = render_71_rows(ALL_BRAND_COUNTS)
    assert rows[0] == "| `LIPITOR` | `ATORVASTATIN` | 11,712 |"
    assert rows[-1] == "| **Total Brand Rows** | | **31,098** |"
    assert len(rows) == 8  # 7 aliases + total


def test_render_71_rows_treats_missing_brand_as_zero():
    counts = {k: v for k, v in ALL_BRAND_COUNTS.items() if k != "ZOLOFT"}
    rows = render_71_rows(counts)
    assert "| `ZOLOFT` | `SERTRALINE` | 0 |" in rows
    assert "| **Total Brand Rows** | | **24,447** |" in rows


def test_render_72_rows_formats_percent_and_na():
    targets = {
        "ATORVASTATIN": TargetFigure(generic=100, brand=20, coreported=5,
                                     both_brands=0),
        "METFORMIN": TargetFigure(generic=50_000, brand=1_648, coreported=297,
                                  both_brands=4),
        "IBUPROFEN": TargetFigure(generic=29_207, brand=4_907, coreported=348,
                                  both_brands=63),
        "OMEPRAZOLE": TargetFigure(generic=44_491, brand=3_078, coreported=243,
                                   both_brands=0),
        "SERTRALINE": TargetFigure(generic=25_272, brand=5_255, coreported=292,
                                   both_brands=0),
    }
    rows = render_72_rows(targets)
    assert rows[0] == ("| **ATORVASTATIN** (Lipitor) | 100 | 20 | 5 | N/A | "
                       "15 | 115 | **+15.00%** |")
    # multi-brand targets report a real both-brands count, not N/A
    assert "| **METFORMIN** (Glucophage, Glucophage XR) | 50,000 | 1,648 | 297 | 4 | 1,351 | 51,351 | **+2.70%** |" in rows
    assert "| **IBUPROFEN** (Advil, Motrin) | 29,207 | 4,907 | 348 | 63 | 4,559 | 33,766 | **+15.61%** |" in rows
    # net-new = brand union - co-reported; post = generic + net-new
    assert rows[2].endswith("| 4,559 | 33,766 | **+15.61%** |")


def test_render_72_rows_rejects_zero_generic_denominator():
    with pytest.raises(ReportError):
        render_72_rows({t: TargetFigure(0, 0, 0, 0)
                        for t in ("ATORVASTATIN", "METFORMIN", "IBUPROFEN",
                                  "OMEPRAZOLE", "SERTRALINE")})


# ---------------------------------------------------------------------------
# §7.3 rendering: conservation + reproducible statistics
# ---------------------------------------------------------------------------

def test_render_73_rows_exact_format_and_statistics():
    """Cells and statistics must both come out in report format."""
    background_n = 1_000
    pairs = [PairFigure(50, 200, 30, 720)] * len(PAIR_ROWS)
    rows = render_73_rows(background_n, pairs)
    first = rows[0]
    label, source = PAIR_ROWS[0][0], PAIR_ROWS[0][1]
    assert first == (f"| {label} | {source} | 50 | 200 | 30 | 720 | "
                     "5.00 [3.25–7.68] | 6.00 [3.72–9.69] | 63.1 |")
    assert len(rows) == len(PAIR_ROWS)


def test_render_73_rows_rejects_background_drift():
    """F1: a 2x2 whose cells do not sum to the background N must never render."""
    pairs = [PairFigure(50, 200, 30, 720)] * len(PAIR_ROWS)
    pairs[2] = PairFigure(50, 200, 30, 721)          # N off by one
    with pytest.raises(ReportError, match="background N not conserved"):
        render_73_rows(1_000, pairs)


def test_render_73_rows_rejects_wrong_pair_count():
    with pytest.raises(ReportError, match="pair figures"):
        render_73_rows(1_000, [PairFigure(50, 200, 30, 720)] * 3)


def test_render_73_rows_rejects_negative_cells():
    """Impossible counts (e.g. drug_cases < a) must not be rendered."""
    pairs = [PairFigure(50, 200, 30, 720)] * len(PAIR_ROWS)
    pairs[0] = PairFigure(500, -100, 30, 570)        # b negative
    with pytest.raises(ReportError, match="negative cell"):
        render_73_rows(1_000, pairs)


# ---------------------------------------------------------------------------
# locate / check / write against markdown
# ---------------------------------------------------------------------------

def _doc(rows: list[str]) -> str:
    return (
        "# Report\n\n"
        "Intro text.\n\n"
        + HEADER_71 + "\n"
        "| :--- | :--- | :--- |\n"
        + "".join(row + "\n" for row in rows)
        + "\n---\n\nTrailing section.\n"
    )


def test_table_data_span_covers_only_data_rows():
    doc = _doc(["| `LIPITOR` | `ATORVASTATIN` | 999 |"])
    start, end = table_data_span(doc, HEADER_71)
    assert doc[start:end] == "| `LIPITOR` | `ATORVASTATIN` | 999 |\n"
    assert doc[:start].endswith("| :--- | :--- | :--- |\n")
    assert doc[end:].startswith("\n---")


def test_check_report_reports_drift():
    expected = render_71_rows(ALL_BRAND_COUNTS)
    stale = list(expected)
    stale[0] = "| `LIPITOR` | `ATORVASTATIN` | 999 |"
    problems = check_report(_doc(stale), {HEADER_71: expected})
    assert len(problems) == 1
    assert "report   : | `LIPITOR` | `ATORVASTATIN` | 999 |" in problems[0]
    assert "database : | `LIPITOR` | `ATORVASTATIN` | 11,712 |" in problems[0]


def test_check_report_passes_when_matching():
    expected = render_71_rows(ALL_BRAND_COUNTS)
    assert check_report(_doc(expected), {HEADER_71: expected}) == []


def test_check_report_flags_missing_header():
    problems = check_report("no tables here", {HEADER_71: ["| x | y | z |"]})
    assert len(problems) == 1
    assert "expected exactly one table header" in problems[0]


def test_write_report_repares_drift_and_is_idempotent():
    expected = render_71_rows(ALL_BRAND_COUNTS)
    stale = list(expected)
    stale[0] = "| `LIPITOR` | `ATORVASTATIN` | 999 |"
    doc = _doc(stale)
    fixed, changed = write_report(doc, {HEADER_71: expected})
    assert changed == [HEADER_71.split("|")[1].strip()]
    assert check_report(fixed, {HEADER_71: expected}) == []
    # surrounding content preserved
    assert fixed.startswith("# Report\n")
    assert fixed.endswith("\n---\n\nTrailing section.\n")
    # second write is a no-op
    again, changed_again = write_report(fixed, {HEADER_71: expected})
    assert again == fixed and changed_again == []


# ---------------------------------------------------------------------------
# Baseline snapshot / diff (pre-execution record, post-execution proof)
# ---------------------------------------------------------------------------

SNAPSHOT_N = 1_000_000
_BRANDS = tuple(b for t in FOCUS for b in t.brands)


def _synthetic_figures() -> dict:
    """Pre-execution figures consistent with FOCUS and PAIR_ROWS."""
    targets = {
        t.generic: TargetFigure(generic=10_000, brand=200, coreported=50,
                                both_brands=4 if len(t.brands) > 1 else 0)
        for t in FOCUS
    }
    pairs = []
    for _label, source, drugs, _event in PAIR_ROWS:
        if source == PROJECTED_SRC:
            a, drug_cases = 108, 11_950
        elif set(drugs) <= set(_BRANDS):
            a, drug_cases = 10, 2_000
        else:
            a, drug_cases = 100, 10_000
        b = drug_cases - a
        c = 3_000 - a
        d = SNAPSHOT_N - a - b - c
        pairs.append(PairFigure(a, b, c, d))
    return {"background_n": SNAPSHOT_N,
            "brand_counts": {b: 100 for b in _BRANDS},
            "targets": targets,
            "pairs": pairs}


def _post_execution_figures(figs: dict) -> dict:
    """What forward.sql does to the figures — written independently of diff_snapshot."""
    brand_counts = {b: 0 for b in figs["brand_counts"]}
    targets = {
        name: TargetFigure(t.generic + t.brand - t.coreported, 0, 0, 0)
        for name, t in figs["targets"].items()
    }
    projected = {
        spec[2][0]: fig
        for spec, fig in zip(PAIR_ROWS, figs["pairs"], strict=True)
        if spec[1] == PROJECTED_SRC
    }
    pairs = []
    for (_label, source, drugs, _event), cur in zip(
        PAIR_ROWS, figs["pairs"], strict=True
    ):
        if source == PROJECTED_SRC:
            pairs.append(cur)
        elif set(drugs) <= set(_BRANDS):
            margin = cur.a + cur.c
            pairs.append(PairFigure(0, 0, margin, SNAPSHOT_N - margin))
        else:
            pairs.append(projected[drugs[0]])
    return {"background_n": figs["background_n"],
            "brand_counts": brand_counts,
            "targets": targets,
            "pairs": pairs}


def test_snapshot_is_json_roundtrippable():
    snap = build_snapshot(_synthetic_figures())
    assert snap["schema_version"] == SNAPSHOT_SCHEMA
    assert snap["background_n"] == SNAPSHOT_N
    assert len(snap["pairs"]) == len(PAIR_ROWS)
    assert json.loads(json.dumps(snap))["pairs"][0]["label"] == PAIR_ROWS[0][0]


def test_load_snapshot_rejects_missing_file(tmp_path):
    with pytest.raises(ReportError, match="--snapshot"):
        load_snapshot(tmp_path / "missing.json")


def test_load_snapshot_rejects_unknown_schema(tmp_path):
    path = tmp_path / "snap.json"
    path.write_text(json.dumps({"schema_version": 999}), encoding="utf-8")
    with pytest.raises(ReportError, match="schema_version"):
        load_snapshot(path)


def test_diff_reports_untouched_baseline():
    figs = _synthetic_figures()
    violations, stats = diff_snapshot(build_snapshot(figs), figs)
    assert violations == []
    assert stats["moved"] == 0
    assert stats["state"] == "baseline"
    assert stats["held"] > 0


def test_diff_accepts_fully_executed_state():
    pre = _synthetic_figures()
    post = _post_execution_figures(pre)
    violations, stats = diff_snapshot(build_snapshot(pre), post)
    assert violations == []
    assert stats["moved"] > 0
    assert stats["state"] == "post-execution"


def test_diff_flags_background_movement():
    pre = _synthetic_figures()
    post = _post_execution_figures(pre)
    post["background_n"] += 1
    violations, _stats = diff_snapshot(build_snapshot(pre), post)
    assert any("background N moved" in v for v in violations)


def test_diff_flags_brand_rows_not_consolidated():
    pre = _synthetic_figures()
    post = _post_execution_figures(pre)
    post["brand_counts"]["LIPITOR"] = 5
    violations, _stats = diff_snapshot(build_snapshot(pre), post)
    assert any("brand `LIPITOR`" in v for v in violations)


def test_diff_flags_projected_row_movement():
    """The projection becoming reality must be bit-identical, not close."""
    pre = _synthetic_figures()
    post = _post_execution_figures(pre)
    idx = next(i for i, (label, src, *_rest) in enumerate(PAIR_ROWS)
               if src == PROJECTED_SRC)
    cur = post["pairs"][idx]
    # cells move while conserving both N and the event margin: still a violation
    post["pairs"][idx] = PairFigure(cur.a + 10, cur.b - 10,
                                     cur.c - 10, cur.d + 10)
    violations, _stats = diff_snapshot(build_snapshot(pre), post)
    label = PAIR_ROWS[idx][0]
    assert any("(unchanged)" in v and label in v for v in violations)


def test_diff_flags_generic_row_not_absorbed():
    pre = _synthetic_figures()
    post = _post_execution_figures(pre)
    idx = next(i for i, (label, src, drugs, _e) in enumerate(PAIR_ROWS)
               if src != PROJECTED_SRC and len(drugs) == 1
               and drugs[0] in {t.generic for t in FOCUS})
    post["pairs"][idx] = pre["pairs"][idx]      # generic row left un-absorbed
    violations, _stats = diff_snapshot(build_snapshot(pre), post)
    label = PAIR_ROWS[idx][0]
    assert any("partial state" in v and label in v for v in violations)


def test_diff_flags_brand_row_still_carrying_cases():
    pre = _synthetic_figures()
    post = _post_execution_figures(pre)
    idx = next(i for i, (_label, _src, drugs, _e) in enumerate(PAIR_ROWS)
               if set(drugs) <= set(_BRANDS))
    post["pairs"][idx] = pre["pairs"][idx]      # brand row still has cases
    violations, _stats = diff_snapshot(build_snapshot(pre), post)
    label = PAIR_ROWS[idx][0]
    assert any("partial state" in v and label in v for v in violations)
