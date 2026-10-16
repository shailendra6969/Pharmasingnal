"""DB-free guards for scripts/verify_migration004.py (runbook §5 acceptance).

The live-database path is rehearsed end-to-end by
tests/integration/test_verify_migration004.py; these tests pin the pure
comparison logic: exactly the five §7.3 projected pairs are asserted, and each
failure mode (missing row, wrong cells, stale row, brand rows surviving) is
reported rather than tolerated.
"""
from __future__ import annotations

import pytest

from scripts.report_figures import PAIR_ROWS, PROJECTED_SRC, ReportError
from scripts.verify_migration004 import (
    PROJECTED_PAIRS,
    check_results,
    expected_projected_cells,
)

PROJECTED_SPECS = [spec for spec in PAIR_ROWS if spec[1] == PROJECTED_SRC]


def _snapshot(pairs_overrides: dict | None = None) -> dict:
    """Synthetic baseline snapshot shaped like build_snapshot's output."""
    pairs = []
    for label, source, drugs, event in PAIR_ROWS:
        cells = (9, 8, 7, 100) if source == PROJECTED_SRC else (1, 2, 3, 100)
        if pairs_overrides and label in pairs_overrides:
            cells = pairs_overrides[label]
        pairs.append({"label": label, "source": source, "drugs": list(drugs),
                      "event": event, "a": cells[0], "b": cells[1],
                      "c": cells[2], "d": cells[3]})
    return {"schema_version": 1, "background_n": 124, "pairs": pairs,
            "brand_counts": {}, "targets": {}}


def _expected(snapshot: dict | None = None) -> dict:
    return expected_projected_cells(snapshot or _snapshot())


def _rows(expected: dict, cells: dict | None = None) -> dict:
    """Stored signal_scores rows, matching expectations unless overridden."""
    out = {}
    for key, spec in expected.items():
        cell = (cells or {}).get(key, spec["cells"])
        out[key] = {"cells": cell, "case_count": cell[0]}
    return out


def _live(expected: dict) -> dict:
    return {key: spec["cells"] for key, spec in expected.items()}


# ---------------------------------------------------------------------------
# expected_projected_cells
# ---------------------------------------------------------------------------

def test_expected_covers_exactly_the_five_projected_pairs():
    expected = _expected()
    assert len(expected) == len(PROJECTED_PAIRS) == 5
    assert set(expected) == {(spec[2][0], spec[3]) for spec in PROJECTED_SPECS}
    assert expected[("SERTRALINE", "SEROTONIN SYNDROME")]["cells"] == (9, 8, 7, 100)


def test_expected_rejects_snapshot_without_the_projection():
    snap = _snapshot()
    snap["pairs"] = [p for p in snap["pairs"] if p["source"] != PROJECTED_SRC]
    with pytest.raises(ReportError, match="§7.3"):
        expected_projected_cells(snap)


# ---------------------------------------------------------------------------
# check_results
# ---------------------------------------------------------------------------

def test_check_passes_when_everything_matches():
    expected = _expected()
    assert check_results(expected, _rows(expected), _live(expected), 0) == []


def test_check_reports_a_missing_signal_row():
    expected = _expected()
    rows = _rows(expected)
    del rows[("METFORMIN", "LACTIC ACIDOSIS")]
    problems = check_results(expected, rows, _live(expected), 0)
    assert len(problems) == 1
    assert "no signal_scores row" in problems[0]
    assert "METFORMIN" in problems[0]


def test_check_reports_stored_cells_that_differ_from_the_projection():
    expected = _expected()
    key = ("ATORVASTATIN", "RHABDOMYOLYSIS")
    rows = _rows(expected, cells={key: (8, 8, 7, 100)})
    problems = check_results(expected, rows, _live(expected), 0)
    # A row that drifted trips both gates: it no longer matches the projection
    # *and* the live tables contradict it.
    assert len(problems) == 2
    assert any("do not equal the projected" in p for p in problems)
    assert any("stale" in p for p in problems)


def test_check_reports_a_stale_row_the_tables_contradict():
    expected = _expected()
    key = ("IBUPROFEN", "GASTRIC ULCER")
    live = dict(_live(expected))
    live[key] = (10, 8, 7, 99)               # live tables moved, row did not
    problems = check_results(expected, _rows(expected), live, 0)
    assert len(problems) == 1
    assert "stale" in problems[0]


def test_check_reports_surviving_brand_scores():
    expected = _expected()
    problems = check_results(expected, _rows(expected), _live(expected), 7398)
    assert problems == [
        "brand_scores = 7398: signal_scores still references retired brand "
        "names (expected 0)"
    ]


def test_check_accumulates_every_problem():
    expected = _expected()
    key = ("OMEPRAZOLE", "HYPOMAGNESAEMIA")
    rows = _rows(expected, cells={key: (1, 1, 1, 1)})
    rows.pop(("SERTRALINE", "SEROTONIN SYNDROME"))
    problems = check_results(expected, rows, _live(expected), 12)
    assert len(problems) == 4   # mismatch + stale + missing row + brand rows
