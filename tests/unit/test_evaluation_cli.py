"""Unit tests for the investigation and evaluation CLI entry points."""
from __future__ import annotations

import json

from app.evaluation import BASELINE_CASES, CaseResult, EvaluationReport
from scripts import run_evaluation as eval_cli
from scripts import run_investigation as inv_cli


class _FakeConn:
    def close(self):
        pass


def _report(status: str) -> dict:
    passed = status == "VERIFIED"
    return {
        "drug": "ATORVASTATIN",
        "event": "RHABDOMYOLYSIS",
        "mode": "all",
        "investigation_type": "comprehensive",
        "status": status,
        "metrics": {
            "case_count": 909, "prr": 9.18, "ror": 9.30,
            "chi_square": 5437.8, "screened": True,
        },
        "evidence": {
            "retrieved_chunks_count": 5,
            "citations_count": 2,
            "sections_searched": ["WARNINGS_AND_PRECAUTIONS"],
            "evidence_required": True,
            "evidence_passed": passed,
        },
        "verification": {
            "passed": passed,
            "claims": {"passed": 5 if passed else 4, "total": 5,
                       "details": []},
            "evidence": {"passed": passed,
                         "errors": [] if passed else ["No label evidence retrieved: retrieved_chunks is empty or unpopulated."]},
            "citations": {"passed": passed, "errors": []},
            "causal_guard": {"passed": True, "violations": []},
        },
        "citations": [{"chunk_id": "chunk_0001", "quoted_span": "LIPITOR may cause myopathy and rhabdomyolysis"}],
        "summary": "# report",
        "trace": ["plan", "gather_evidence", "synthesize", "verify", "finalize"],
    }


def _patch_investigation(monkeypatch, status: str) -> None:
    class _Workflow:
        def __init__(self, conn=None):
            self.conn = conn

        def run(self, **kwargs):
            return _report(status)

    monkeypatch.setattr(inv_cli, "InvestigationWorkflow", _Workflow)
    monkeypatch.setattr(inv_cli.loader, "connect", lambda url: _FakeConn())


# ------------------------------------------------ run_investigation ----------


def test_investigation_cli_exits_zero_when_verified(monkeypatch, capsys):
    _patch_investigation(monkeypatch, "VERIFIED")
    code = inv_cli.main(["--drug", "ATORVASTATIN", "--event", "RHABDOMYOLYSIS"])
    out = capsys.readouterr().out
    assert code == 0
    assert "status: VERIFIED" in out
    assert "citations=2" in out


def test_investigation_cli_exits_one_when_flagged(monkeypatch, capsys):
    _patch_investigation(monkeypatch, "FLAGGED_WITH_WARNINGS")
    code = inv_cli.main(["--drug", "ATORVASTATIN", "--event", "RHABDOMYOLYSIS"])
    out = capsys.readouterr().out
    assert code == 1
    assert "status: FLAGGED_WITH_WARNINGS" in out
    assert "evidence error" in out


def test_investigation_cli_exits_two_when_database_unreachable(monkeypatch, capsys):
    def _boom(url):
        raise OSError("connection refused")

    monkeypatch.setattr(inv_cli.loader, "connect", _boom)
    code = inv_cli.main(["--drug", "ATORVASTATIN", "--event", "RHABDOMYOLYSIS"])
    assert code == 2
    assert "cannot connect" in capsys.readouterr().err


def test_investigation_cli_writes_json_report(monkeypatch, tmp_path):
    _patch_investigation(monkeypatch, "VERIFIED")
    out_path = tmp_path / "report.json"
    code = inv_cli.main(
        ["--drug", "ATORVASTATIN", "--event", "RHABDOMYOLYSIS", "--json", str(out_path)]
    )
    assert code == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["status"] == "VERIFIED"
    assert payload["citations"]


# ------------------------------------------------ run_evaluation -------------


def _result(matched: bool, *, skip: bool = False) -> CaseResult:
    return CaseResult(
        case=BASELINE_CASES[0],
        status="VERIFIED" if matched else "FLAGGED_WITH_WARNINGS",
        matched_expectation=None if skip else matched,
        skip_reason="premise no longer holds" if skip else None,
        generated_answer="# report",
        verification={"evidence": {"errors": []}, "citations": {"errors": []},
                      "causal_guard": {"violations": []}},
        claims=[],
        numeric_accuracy=1.0 if matched else 0.8,
        citation_correctness=1.0,
        verification_accuracy=None if skip else (1.0 if matched else 0.0),
    )


def _patch_evaluation(monkeypatch, results: list[CaseResult], captured: dict) -> None:
    def _fake_run(conn, cases, *, persist=True):
        captured["persist"] = persist
        captured["cases"] = cases
        return EvaluationReport(results=results)

    monkeypatch.setattr(eval_cli, "run_evaluation", _fake_run)
    monkeypatch.setattr(eval_cli.loader, "connect", lambda url: _FakeConn())


def test_evaluation_cli_reports_pass_and_exits_zero(monkeypatch, capsys):
    _patch_evaluation(monkeypatch, [_result(True)], captured={})
    code = eval_cli.main([])
    out = capsys.readouterr().out
    assert code == 0
    assert "PASS" in out
    assert "passed=1 failed=0 skipped=0" in out


def test_evaluation_cli_exits_one_on_mismatch(monkeypatch, capsys):
    _patch_evaluation(monkeypatch, [_result(True), _result(False)], captured={})
    code = eval_cli.main([])
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL" in out
    assert "failed=1" in out


def test_evaluation_cli_reports_skips_without_failing(monkeypatch):
    _patch_evaluation(monkeypatch, [_result(True), _result(False, skip=True)], captured={})
    code = eval_cli.main([])
    assert code == 0  # skipped cases are reported, not failed


def test_evaluation_cli_no_persist_flag_reaches_runner(monkeypatch):
    captured: dict = {}
    _patch_evaluation(monkeypatch, [_result(True)], captured=captured)
    code = eval_cli.main(["--no-persist"])
    assert code == 0
    assert captured["persist"] is False


def test_evaluation_cli_rejects_unknown_case_before_connecting(monkeypatch, capsys):
    def _boom(url):
        raise AssertionError("must not connect for unknown case ids")

    monkeypatch.setattr(eval_cli.loader, "connect", _boom)
    code = eval_cli.main(["--case", "does_not_exist"])
    assert code == 2
    assert "unknown case" in capsys.readouterr().err
