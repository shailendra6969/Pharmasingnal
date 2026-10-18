"""Unit tests for the interpreter bootstrap (app/bootstrap.py).

The bootstrap is what makes a bare `pytest` / `python scripts/...` work on a
machine whose default interpreter lacks the pinned dependencies: the same
command is re-executed under .venv/ exactly once, with the exit code
preserved. These tests pin the command mapping for every launch form this
repo uses, the no-op case, and the failure modes that must raise SystemExit
instead of looping.

The re-run itself is never executed here: subprocess.call is replaced by a
sentinel that records the command it was asked to run. The real re-run is
exercised manually (a system interpreter without psycopg2 re-runs the command
and reports green under .venv/), since it requires an interpreter this suite
does not have.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app import bootstrap

PROJECT_ROOT = Path(__file__).resolve().parents[2]
VENV_BIN = PROJECT_ROOT / ".venv" / "bin"


# ---------------------------------------------------------------------------
# The interpreter this suite runs under must be complete: that is precisely
# what makes ensure_venv() a no-op for tests, CI and Docker.
# ---------------------------------------------------------------------------

def test_the_project_interpreter_has_every_pinned_module():
    assert bootstrap.missing_modules() == []


def test_ensure_venv_is_a_silent_noop_with_a_complete_interpreter(capsys):
    assert bootstrap.ensure_venv() is None
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_real_venv_counterparts_exist():
    """Every command the mapping targets (.venv/bin/<name>) must exist."""
    for name in ("pytest", "py.test", "python", "python3"):
        assert (VENV_BIN / name).exists(), f".venv/bin/{name} is missing"


# ---------------------------------------------------------------------------
# Command mapping: replay any launch form the README/runbooks document.
# ---------------------------------------------------------------------------

def test_console_script_maps_to_the_venv_counterpart(tmp_path):
    (tmp_path / "pytest").write_text("#!/bin/sh\n")
    cmd = bootstrap.reexec_command(
        tmp_path, ["/opt/anaconda3/bin/pytest", "-q", "-k", "x"]
    )
    assert cmd == [str(tmp_path / "pytest"), "-q", "-k", "x"]


def test_py_test_console_script_also_maps(tmp_path):
    (tmp_path / "py.test").write_text("#!/bin/sh\n")
    cmd = bootstrap.reexec_command(tmp_path, ["py.test", "-q"])
    assert cmd == [str(tmp_path / "py.test"), "-q"]


@pytest.mark.parametrize("head", ["/opt/anaconda3/bin/python",
                                  "/usr/local/bin/python3.13"])
def test_python_dash_m_module_maps_to_the_venv_interpreter(tmp_path, head):
    # `python3.13` is not a .venv/bin name, so this also covers the fallback
    # branch; `python` covers the name-lookup branch. Both must land the same.
    (tmp_path / "python").write_text("")
    cmd = bootstrap.reexec_command(tmp_path, [head, "-m", "pytest", "-q"])
    assert cmd == [str(tmp_path / "python"), "-m", "pytest", "-q"]


def test_python_with_a_script_path_maps_to_the_venv_interpreter(tmp_path):
    (tmp_path / "python").write_text("")
    script = str(PROJECT_ROOT / "scripts" / "report_figures.py")
    cmd = bootstrap.reexec_command(tmp_path, ["/usr/local/bin/python3.13", script, "--check"])
    assert cmd == [str(tmp_path / "python"), script, "--check"]


def test_shebang_executed_script_is_replayed_with_the_venv_interpreter(tmp_path):
    script = tmp_path / "tool.py"
    script.write_text("print('hi')\n")
    cmd = bootstrap.reexec_command(tmp_path, [str(script), "--flag"])
    assert cmd == [str(tmp_path / "python"), str(script), "--flag"]


def test_documented_launch_forms_all_replay_onto_existing_interpreters():
    """The commands README/runbook tell operators to run must all be replayable."""
    for argv in (
        ["pytest", "-q"],
        [str(VENV_BIN / "python"), "-m", "pytest", "-q"],
        ["python3", "scripts/report_figures.py", "--check"],
        ["/opt/homebrew/bin/python3.13", str(PROJECT_ROOT / "scripts" / "report_figures.py"),
         "--check"],
        [str(PROJECT_ROOT / "scripts" / "migrate.py"), "--test"],
    ):
        cmd = bootstrap.reexec_command(VENV_BIN, argv)
        assert cmd is not None, argv
        assert Path(cmd[0]).exists(), f"{argv} -> {cmd[0]} does not exist"


@pytest.mark.parametrize("argv", [[], [""], ["-"], ["-c"], ["-c", "import sys"],
                                  ["/usr/bin/python3.13", "-c", "pass"],
                                  ["/opt/anaconda3/bin/python", "-"],
                                  ["python3", "-"]])
def test_repl_and_dash_c_invocations_cannot_be_replayed(tmp_path, argv):
    (tmp_path / "python").write_text("")
    assert bootstrap.reexec_command(tmp_path, argv) is None


def test_python_dash_cannot_be_replayed_even_though_python_is_a_venv_name(tmp_path):
    """Regression: `python -` reads the program from stdin, which the parent
    has already consumed. The old mapping hit the console-script branch ("python"
    IS a .venv/bin name) and produced a child that ran an empty script and
    exited 0 — a silent wrong result, the worst failure mode for a guard."""
    (tmp_path / "python").write_text("")
    cmd = bootstrap.reexec_command(tmp_path, ["/opt/anaconda3/bin/python", "-"])
    assert cmd is None


# ---------------------------------------------------------------------------
# ensure_venv failure modes: never a loop, never a silent wrong-interpreter run
# ---------------------------------------------------------------------------

class _ExecSentinel(Exception):
    """Stands in for the child run, which must never actually spawn here."""


def _broken_venv(tmp_path: Path) -> Path:
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("")
    return tmp_path / ".venv"


def test_missing_module_reruns_the_same_command_under_the_venv(tmp_path, monkeypatch):
    venv_bin = _broken_venv(tmp_path) / "bin"
    monkeypatch.setattr(bootstrap, "VENV_DIR", venv_bin.parent)
    monkeypatch.setattr(bootstrap, "missing_modules", lambda: ["psycopg2"])
    monkeypatch.setattr(sys, "executable", "/opt/anaconda3/bin/python3.13")
    monkeypatch.setattr(
        sys, "orig_argv",
        ["/opt/anaconda3/bin/python3.13", str(PROJECT_ROOT / "scripts" / "report_figures.py"),
         "--check"],
        raising=False,
    )
    ran: list[list[str]] = []

    def fake_call(command) -> int:
        ran.append(list(command))
        raise _ExecSentinel

    monkeypatch.setattr(bootstrap.subprocess, "call", fake_call)
    with pytest.raises(_ExecSentinel):
        bootstrap.ensure_venv()
    venv_python = str(venv_bin / "python")
    assert ran == [[venv_python, str(PROJECT_ROOT / "scripts" / "report_figures.py"),
                    "--check"]]


def test_a_broken_venv_itself_raises_instead_of_looping(tmp_path, monkeypatch):
    """The only self-referential path: deps missing *inside* .venv/."""
    venv_python = _broken_venv(tmp_path) / "bin" / "python"
    monkeypatch.setattr(bootstrap, "VENV_DIR", venv_python.parent.parent)
    monkeypatch.setattr(bootstrap, "missing_modules", lambda: ["psycopg2"])
    monkeypatch.setattr(sys, "executable", str(venv_python))
    with pytest.raises(SystemExit, match="recreate it"):
        bootstrap.ensure_venv()


def test_missing_venv_raises_with_install_instructions(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "VENV_DIR", tmp_path / ".venv")   # does not exist
    monkeypatch.setattr(bootstrap, "missing_modules", lambda: ["psycopg2"])
    with pytest.raises(SystemExit, match=r"pip install -r requirements\.txt"):
        bootstrap.ensure_venv()


def test_unreplayable_invocation_raises_instead_of_guessing(tmp_path, monkeypatch):
    venv_python = _broken_venv(tmp_path) / "bin" / "python"
    monkeypatch.setattr(bootstrap, "VENV_DIR", venv_python.parent.parent)
    monkeypatch.setattr(bootstrap, "missing_modules", lambda: ["psycopg2"])
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3.13")
    monkeypatch.setattr(sys, "orig_argv", ["/usr/bin/python3.13", "-c", "import app"],
                        raising=False)
    with pytest.raises(SystemExit, match="activate"):
        bootstrap.ensure_venv()


def test_a_program_piped_to_python_dash_raises_instead_of_running_empty(tmp_path, monkeypatch):
    """`python -` + missing modules must raise, never spawn a child that reads
    the exhausted stdin and exits 0 with no output."""
    venv_python = _broken_venv(tmp_path) / "bin" / "python"
    monkeypatch.setattr(bootstrap, "VENV_DIR", venv_python.parent.parent)
    monkeypatch.setattr(bootstrap, "missing_modules", lambda: ["psycopg2"])
    monkeypatch.setattr(sys, "executable", "/opt/anaconda3/bin/python")
    monkeypatch.setattr(sys, "orig_argv", ["/opt/anaconda3/bin/python", "-"],
                        raising=False)
    with pytest.raises(SystemExit, match="activate"):
        bootstrap.ensure_venv()


# ---------------------------------------------------------------------------
# Exit-code forwarding: a bare `pytest` must end with the suite's own code.
# ---------------------------------------------------------------------------

def test_signal_terminated_children_become_the_shells_128_plus_n():
    assert bootstrap.exit_code(-15) == 143      # SIGTERM
    assert bootstrap.exit_code(-9) == 137       # SIGKILL
    assert bootstrap.exit_code(0) == 0
    assert bootstrap.exit_code(3) == 3


def test_a_real_child_exit_code_passes_through_unchanged():
    """Spawn exactly the way ensure_venv() does; the code must survive."""
    import subprocess

    ret = subprocess.call(
        [str(VENV_BIN / "python"), "-c", "import sys; sys.exit(3)"]
    )
    assert bootstrap.exit_code(ret) == 3
