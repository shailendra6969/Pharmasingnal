"""Regression guards for the migration 004 impact report (spec 004).

Second-review fixes these tests pin down:

F1  Every 2x2 table in report section 7.3 must conserve the background total
    (a + b + c + d identical across baseline and projected rows), and every
    reported PRR / ROR / chi-square string must be exactly reproducible from
    the cells in the same row via app.statistics.disproportionality.compute().
F2/F3  The report and forward.sql must cite the index name the schema actually
    creates (uq_case_drugs_pid_drug), never the invented
    uq_case_drugs_primaryid_seq.

No database access: figures are checked for internal consistency only.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.statistics.disproportionality import Table2x2, compute

REPO_ROOT = Path(__file__).resolve().parents[2]
REPORT = REPO_ROOT / "docs" / "migrations" / "004-focus-drug-alias-migration.md"
FORWARD_SQL = REPO_ROOT / "docs" / "migrations" / "004_focus_drug_brand_aliases" / "forward.sql"
SCHEMA_SQL = REPO_ROOT / "migrations" / "001_initial_schema.sql"


def _section(text: str, heading: str, next_heading: str) -> str:
    match = re.search(
        rf"{re.escape(heading)}.*?(?={re.escape(next_heading)})", text, re.S
    )
    assert match, f"section {heading!r} not found in report"
    return match.group(0)


def _rows_73() -> list[tuple[str, int, int, int, int, str, str, str]]:
    """Parse the section 7.3 table into (label, a, b, c, d, prr, ror, chi2).

    Cells must be plain integers: a '~'-prefixed (approximate) cell fails the
    int() conversion, which is exactly what F1 forbids on projections.
    """
    text = REPORT.read_text(encoding="utf-8")
    section = _section(text, "### 7.3 Disproportionality Signal Behavior",
                       "### 7.4 Critical Scientific Boundary")
    rows = []
    for line in section.splitlines():
        if not line.startswith("| **"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        assert len(cells) == 9, f"unexpected table shape in 7.3: {line}"
        a, b, c, d = (int(cells[i].replace(",", "")) for i in range(2, 6))
        rows.append((cells[0], a, b, c, d, cells[6], cells[7], cells[8]))
    assert len(rows) == 15, f"expected 15 data rows in section 7.3, got {len(rows)}"
    return rows


def test_report_rows_conserve_background_n():
    """F1a: a+b+c+d must be the same (invariant background) for every row."""
    totals = {label: a + b + c + d for label, a, b, c, d, *_ in _rows_73()}
    assert len(set(totals.values())) == 1, f"background N not conserved: {totals}"


@pytest.mark.parametrize("row_index", range(15))
def test_report_statistics_reproduce_from_cells(row_index):
    """F1b: each reported PRR/ROR/chi2 string recomputes from its own cells."""
    label, a, b, c, d, prr_cell, ror_cell, chi2_cell = _rows_73()[row_index]
    stat = compute(Table2x2(a, b, c, d))

    assert stat.prr is not None and stat.prr_lower is not None
    assert stat.ror is not None and stat.ror_lower is not None
    assert stat.chi_square is not None

    expected_prr = f"{stat.prr:.2f} [{stat.prr_lower:.2f}–{stat.prr_upper:.2f}]"
    expected_ror = f"{stat.ror:.2f} [{stat.ror_lower:.2f}–{stat.ror_upper:.2f}]"
    expected_chi2 = f"{stat.chi_square:,.1f}"

    assert prr_cell == expected_prr, f"{label}: PRR cell not reproducible"
    assert ror_cell == expected_ror, f"{label}: ROR cell not reproducible"
    assert chi2_cell == expected_chi2, f"{label}: chi2 cell not reproducible"


def test_report_cites_the_real_unique_index():
    """F3: report and forward.sql name the index the schema actually creates."""
    schema = SCHEMA_SQL.read_text(encoding="utf-8")
    match = re.search(
        r"CREATE UNIQUE INDEX IF NOT EXISTS (\w+)\s*\n\s*ON case_drugs "
        r"\(primaryid, COALESCE\(drug_seq, drug_normalized\)\)",
        schema,
    )
    assert match, "unique index on case_drugs not found in 001_initial_schema.sql"
    real_index = match.group(1)

    report = REPORT.read_text(encoding="utf-8")
    forward = FORWARD_SQL.read_text(encoding="utf-8")

    assert real_index in report, f"report must cite {real_index}"
    assert real_index in forward, f"forward.sql must cite {real_index}"
    assert "uq_case_drugs_primaryid_seq" not in report
    assert "uq_case_drugs_primaryid_seq" not in forward


def test_report_cites_actual_case_drugs_row_count():
    """F2: the row-count figure in section 5 must match today's schema claim.

    The report asserts a measured total; it must be a plain, non-fabricated
    number and must not be the superseded 3,365,490 figure.
    """
    report = REPORT.read_text(encoding="utf-8")
    assert "3,365,490" not in report
    match = re.search(r"across all ([\d,]+) rows in `case_drugs`", report)
    assert match, "section 5 row-count claim not found"
    counted = int(match.group(1).replace(",", ""))
    assert counted > 3_365_490, "row-count claim was not updated"


# ---------------------------------------------------------------------------
# F4: section 7.5 — screen-threshold crossings must be internally consistent
# ---------------------------------------------------------------------------

CRITERION_LABELS = ["$a \\ge 3$", "$\\text{PRR} \\ge 2.0$", "$\\chi^2 \\ge 4.0$"]


def _num(cell: str) -> int:
    return int(cell.replace(",", "").replace("+", ""))


def _tables_75() -> tuple[list[list[str]], list[list[str]], list[list[str]]]:
    """Parse §7.5 into (summary, criteria, detail) rows of cells.

    §7.5 is the last section of the report, so it runs to end of file.
    """
    text = REPORT.read_text(encoding="utf-8")
    match = re.search(r"### 7\.5 Focus-Drug Signals.*$", text, re.S)
    assert match, "section 7.5 not found in report"
    summary: list[list[str]] = []
    criteria: list[list[str]] = []
    detail: list[list[str]] = []
    for line in match.group(0).splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells[0].startswith(":---") or cells[0] in ("Focus Target",
                                                        "Flipping Criterion"):
            continue
        {8: summary, 3: criteria, 7: detail}[len(cells)].append(cells)
    assert summary and criteria and detail, "§7.5 tables are empty"
    return summary, criteria, detail


def test_section_75_summary_rows_close_arithmetically():
    """F4a: screened_after == screened_before - dropped + crossed, per target."""
    summary, _criteria, _detail = _tables_75()
    assert len(summary) == 6, "expected 5 targets + one total row"
    for cells in summary[:5]:
        before, after, crossed, dropped, net = map(_num, cells[2:7])
        assert after == before - dropped + crossed, cells[0]
        assert net == crossed - dropped == after - before, cells[0]


def test_section_75_total_row_equals_the_sum_of_targets():
    summary, _criteria, _detail = _tables_75()
    total = summary[5]
    assert total[0] == "**All five focus targets**"
    for column in range(2, 8):
        assert _num(total[column]) == sum(_num(r[column]) for r in summary[:5]), \
            f"total row column {column} is not the sum of the target rows"


def test_section_75_detail_rows_match_the_summary_tallies():
    """F4b: every crossing/dropping signal is listed exactly once."""
    summary, _criteria, detail = _tables_75()
    for cells in summary[:5]:
        target = cells[0].split("**")[1]
        crossed, dropped = _num(cells[4]), _num(cells[5])
        mine = [d for d in detail if d[0] == f"**{target}**"]
        assert sum(1 for d in mine if d[2] == "Cross") == crossed, target
        assert sum(1 for d in mine if d[2] == "Drop") == dropped, target
    for cells in detail:
        assert cells[2] in ("Cross", "Drop"), cells
        assert cells[6], f"empty flipped-threshold cell: {cells}"
        for label in cells[6].split(", "):
            assert label in CRITERION_LABELS, label


def test_section_75_criteria_counts_are_bounded_by_transition_totals():
    summary, criteria, _detail = _tables_75()
    assert [c[0] for c in criteria] == CRITERION_LABELS
    total_cross = sum(_num(r[4]) for r in summary[:5])
    total_drop = sum(_num(r[5]) for r in summary[:5])
    for label, cross_cell, drop_cell in criteria:
        assert 0 <= _num(cross_cell) <= total_cross, label
        assert 0 <= _num(drop_cell) <= total_drop, label
