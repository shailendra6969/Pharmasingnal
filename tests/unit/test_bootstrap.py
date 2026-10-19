"""Unit tests for the interpreter bootstrap (app/bootstrap.py).

The bootstrap is what makes a bare `pytest` / `python scripts/...` work on a
machine whose default interpreter lacks the pinned dependencies: the same
command is re-executed under .venv/ exactly once, with the exit code
preserved. These tests pin the command mapping for every launch form this
repo uses, the command-line construction used when sys.orig_argv does not
exist (Python < 3.10), the no-op case, and the failure modes that must raise
SystemExit instead of looping.

The re-run itself is never executed here: subprocess.call is replaced by a
sentinel that records the command it was asked to run. Where a real
pre-3.10 interpreter exists (macOS's /usr/bin/python3 shim), the suite does
prove the end-to-end replay against it; on machines without one, that proof
skips.

The self-test (`python -m app.bootstrap --check`) is pinned here too: every
plan state must produce the verdict it claims, must agree with what
ensure_venv() actually does in that same state (so the report cannot drift
from the behavior), and the documented command must really run and exit 0.
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
# Command-line construction: sys.orig_argv does not exist before Python
# 3.10 (macOS's /usr/bin/python3 shim is 3.9), so the line is rebuilt from
# what such an interpreter does keep: sys.argv and __main__.__spec__.
# ---------------------------------------------------------------------------

def _simulate_pre_310(monkeypatch, argv, *, spec_name=None,
                      executable="/usr/bin/python3"):
    """Pin everything constructed_argv() reads, as a pre-3.10 interpreter.

    Every input is pinned because the real ones depend on how the suite
    itself was launched (pytest vs python -m pytest), and sys.orig_argv is
    removed — the state this whole section is about.
    """
    import importlib.machinery

    monkeypatch.delattr(sys, "orig_argv", raising=False)
    monkeypatch.setattr(sys, "argv", list(argv))
    monkeypatch.setattr(
        sys.modules["__main__"], "__spec__",
        None if spec_name is None else importlib.machinery.ModuleSpec(spec_name, None),
        raising=False,
    )
    monkeypatch.setattr(sys, "executable", executable)


def _inside_fake_runpy(func, mod_name):
    """Run func() with a runpy.py frame holding mod_name on the stack.

    The exact stack shape a real `python -m pkg.mod` presents when the
    bootstrap fires during pkg's import: argv[0] is still '-m', __main__ has
    no spec yet, and the module name exists only as runpy's local.
    """
    namespace = {"_call": func}
    exec(compile("def _run_module_as_main(mod_name):\n    return _call()\n",
                 "runpy.py", "exec"), namespace)
    return namespace["_run_module_as_main"](mod_name)


def test_a_script_invocation_is_reconstructed_without_orig_argv(tmp_path, monkeypatch):
    """The macOS system-python3 case: no sys.orig_argv, but sys.argv still
    names the script — a command line is built, not refused."""
    venv_bin = _broken_venv(tmp_path) / "bin"
    monkeypatch.setattr(bootstrap, "VENV_DIR", venv_bin.parent)
    monkeypatch.setattr(bootstrap, "missing_modules", lambda: ["psycopg2"])
    _simulate_pre_310(monkeypatch, ["scripts/report_figures.py", "--check"])

    assert bootstrap.constructed_argv() == ["scripts/report_figures.py", "--check"]
    plan = bootstrap.fallback_plan()
    assert plan.state == bootstrap.STATE_ARMED
    assert plan.command == [str(venv_bin / "python"), "scripts/report_figures.py",
                             "--check"]


def test_a_console_script_lands_on_its_venv_counterpart_without_orig_argv(tmp_path,
                                                                        monkeypatch):
    """argv[0] of a shebang launch is the program itself — the console-script
    mapping applies exactly as it does with a real orig_argv."""
    venv_bin = _broken_venv(tmp_path) / "bin"
    (venv_bin / "pytest").write_text("#!/bin/sh\n")
    monkeypatch.setattr(bootstrap, "VENV_DIR", venv_bin.parent)
    monkeypatch.setattr(bootstrap, "missing_modules", lambda: ["psycopg2"])
    _simulate_pre_310(monkeypatch, ["/opt/anaconda3/bin/pytest", "-q", "-k", "x"])

    plan = bootstrap.fallback_plan()
    assert plan.state == bootstrap.STATE_ARMED
    assert plan.command == [str(venv_bin / "pytest"), "-q", "-k", "x"]


def test_a_running_dash_m_target_is_reconstructed_from_main_spec(tmp_path, monkeypatch):
    """Once a `-m` target is executing, runpy has replaced argv[0] with the
    module file and recorded the module on __main__.__spec__ — enough to
    replay `-m` itself, canonically."""
    venv_bin = _broken_venv(tmp_path) / "bin"
    monkeypatch.setattr(bootstrap, "VENV_DIR", venv_bin.parent)
    monkeypatch.setattr(bootstrap, "missing_modules", lambda: ["psycopg2"])
    _simulate_pre_310(
        monkeypatch,
        ["/opt/homebrew/lib/python3.12/site-packages/pytest/__main__.py", "-q"],
        spec_name="pytest.__main__",
    )

    plan = bootstrap.fallback_plan()
    assert plan.state == bootstrap.STATE_ARMED
    # '.__main__' stripped: the canonical `python -m pytest` round-trips.
    assert plan.command == [str(venv_bin / "python"), "-m", "pytest", "-q"]


def test_the_earliest_dash_m_window_reconstructs_from_runpy(tmp_path, monkeypatch):
    """`python -m app.bootstrap` fires the bootstrap while app's own package
    is being imported: before runpy assigns __main__.__spec__, and after the
    module name left sys.argv (argv[0] is just '-m'). runpy's frame is the
    only record left — the plan built from it must still replay."""
    venv_bin = _broken_venv(tmp_path) / "bin"
    monkeypatch.setattr(bootstrap, "VENV_DIR", venv_bin.parent)
    monkeypatch.setattr(bootstrap, "missing_modules", lambda: ["psycopg2"])
    _simulate_pre_310(monkeypatch, ["-m", "--check"])

    plan = _inside_fake_runpy(bootstrap.fallback_plan, "app.bootstrap")
    assert plan.state == bootstrap.STATE_ARMED
    assert plan.command == [str(venv_bin / "python"), "-m", "app.bootstrap", "--check"]


def test_dash_m_reconstruction_refuses_without_a_runpy_frame(tmp_path, monkeypatch):
    """argv[0] == '-m' but no runpy frame: the module name cannot be
    recovered, so the bootstrap refuses instead of guessing."""
    venv_bin = _broken_venv(tmp_path) / "bin"
    monkeypatch.setattr(bootstrap, "VENV_DIR", venv_bin.parent)
    monkeypatch.setattr(bootstrap, "missing_modules", lambda: ["psycopg2"])
    _simulate_pre_310(monkeypatch, ["-m", "--check"])

    assert bootstrap.constructed_argv() is None
    assert bootstrap.fallback_plan().state == bootstrap.STATE_UNREPLAYABLE


@pytest.mark.parametrize("argv", [[], [""], ["-c"], ["-c", "import app"], ["-"]])
def test_nothing_is_reconstructed_where_the_program_text_is_gone(monkeypatch, argv):
    """REPL, `python -c` and `python -` keep no record of the program itself."""
    _simulate_pre_310(monkeypatch, argv)
    assert bootstrap.constructed_argv() is None


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


# ---------------------------------------------------------------------------
# Self-test: python -m app.bootstrap --check
# ---------------------------------------------------------------------------

def _raise_sentinel(command) -> int:
    """Stands in for the child run in the STATE_ARMED consistency check."""
    raise _ExecSentinel


def _field(out: str, label: str) -> str:
    """Value of the `label : value` line --check prints, looked up by label.

    Returns the value by exact equality rather than a padded substring, so a
    re-alignment cannot quietly turn these assertions into no-ops.
    """
    for line in out.splitlines():
        if line.startswith(label + " ") or line.startswith(label + ":"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"no line labelled {label!r} in:\n{out}")


def test_check_under_the_project_interpreter_reports_complete(capsys):
    """The interpreter this suite runs under is complete: --check says so,
    names that interpreter, and exits 0."""
    assert bootstrap.self_check() == 0
    out = capsys.readouterr().out
    assert _field(out, "repository root") == str(bootstrap.PROJECT_ROOT)
    assert _field(out, "interpreter").startswith(f"{sys.executable} (Python ")
    assert _field(out, "pinned deps") == "complete"
    # Complete means the fallback is neither needed nor enabled.
    assert _field(out, "venv_fallback_armed") == "false"
    # With nothing to fix, the bootstrap would leave this interpreter alone.
    assert _field(out, "selected interpreter") == sys.executable


def test_check_names_the_venv_target_and_whether_it_exists(capsys):
    bootstrap.self_check()
    out = capsys.readouterr().out
    assert _field(out, "venv interpreter") == (
        f"{bootstrap.VENV_DIR / 'bin' / 'python'} (present)"
    )


# The five plan states, each asserted through the public self-check.

def test_check_reports_armed_when_a_module_is_missing(tmp_path, monkeypatch, capsys):
    venv_bin = _broken_venv(tmp_path) / "bin"
    monkeypatch.setattr(bootstrap, "VENV_DIR", venv_bin.parent)
    monkeypatch.setattr(bootstrap, "missing_modules", lambda: ["psycopg2"])
    monkeypatch.setattr(sys, "executable", "/opt/anaconda3/bin/python3.13")
    monkeypatch.setattr(
        sys, "orig_argv",
        ["/opt/anaconda3/bin/python3.13", "-m", "app.bootstrap", "--check"],
        raising=False,
    )
    # Armed means the configuration is valid and an interpreter IS
    # resolvable -- the bootstrap would re-run under .venv/ -- so this is a
    # passing probe, not a failure. It is also where "available" and "armed"
    # part company: the venv exists AND the bootstrap is wired to use it.
    assert bootstrap.self_check() == 0
    out = capsys.readouterr().out
    assert _field(out, "pinned deps") == "missing psycopg2"
    assert _field(out, "venv interpreter") == f"{venv_bin / 'python'} (present)"
    assert _field(out, "selected interpreter") == str(venv_bin / "python")
    assert _field(out, "venv_fallback_armed") == "true"
    assert _field(out, "fallback").startswith("armed — ")


def test_check_reports_cannot_arm_when_no_venv_exists(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, "VENV_DIR", tmp_path / ".venv")   # does not exist
    monkeypatch.setattr(bootstrap, "missing_modules", lambda: ["psycopg2"])
    # A venv that is absent is not a fallback, however hopeful the path.
    assert bootstrap.self_check() == 1
    out = capsys.readouterr().out
    assert _field(out, "venv interpreter").endswith("(absent)")
    assert _field(out, "venv_fallback_armed") == "false"
    assert _field(out, "selected interpreter") == (
        "(unresolved — the bootstrap refuses to guess)"
    )
    assert "cannot arm — no .venv/ exists" in out
    assert "pip install -r requirements.txt" in out


def test_check_reports_cannot_arm_when_the_venv_python_is_missing(tmp_path, monkeypatch,
                                                                 capsys):
    """A .venv/ directory that exists but has no bin/python is exactly as
    unusable as no .venv/ at all; bootstrap collapses them into one state,
    and --check must report that state, not a false 'present'."""
    (tmp_path / ".venv" / "bin").mkdir(parents=True)   # directory exists, no python
    monkeypatch.setattr(bootstrap, "VENV_DIR", tmp_path / ".venv")
    monkeypatch.setattr(bootstrap, "missing_modules", lambda: ["psycopg2"])
    assert bootstrap.self_check() == 1
    out = capsys.readouterr().out
    assert _field(out, "venv interpreter").endswith("(absent)")
    assert _field(out, "venv_fallback_armed") == "false"
    assert "cannot arm — no .venv/ exists" in out


def test_an_available_venv_is_not_the_same_as_an_armed_fallback(capsys):
    """The distinction that matters most: .venv/ being present does NOT mean
    the fallback is enabled. Here the venv exists and the interpreter is
    already complete, so the bootstrap would do nothing."""
    bootstrap.self_check()
    out = capsys.readouterr().out
    assert _field(out, "venv interpreter").endswith("(present)")
    assert _field(out, "venv_fallback_armed") == "false"
    assert _field(out, "fallback").startswith("not armed")


def test_check_reports_cannot_arm_when_the_venv_itself_is_broken(tmp_path, monkeypatch,
                                                                  capsys):
    venv_python = _broken_venv(tmp_path) / "bin" / "python"
    monkeypatch.setattr(bootstrap, "VENV_DIR", venv_python.parent.parent)
    monkeypatch.setattr(bootstrap, "missing_modules", lambda: ["psycopg2"])
    monkeypatch.setattr(sys, "executable", str(venv_python))
    assert bootstrap.self_check() == 1
    out = capsys.readouterr().out
    assert "cannot arm — .venv/ itself is missing psycopg2" in out
    # The directory exists, but it cannot actually supply an interpreter.
    assert _field(out, "venv_fallback_armed") == "false"


def test_check_reports_cannot_arm_for_an_unreplayable_invocation(tmp_path, monkeypatch,
                                                                 capsys):
    venv_bin = _broken_venv(tmp_path) / "bin"
    monkeypatch.setattr(bootstrap, "VENV_DIR", venv_bin.parent)
    monkeypatch.setattr(bootstrap, "missing_modules", lambda: ["psycopg2"])
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3.13")
    monkeypatch.setattr(sys, "orig_argv", ["/usr/bin/python3.13", "-c", "import app"],
                        raising=False)
    assert bootstrap.self_check() == 1
    out = capsys.readouterr().out
    assert "cannot arm — this invocation cannot be replayed" in out
    assert "activate the project environment" in out
    assert _field(out, "venv_fallback_armed") == "false"


@pytest.mark.parametrize("argv,kind", [
    ([""], "an interactive session"),
    (["-c", "import app"], "`python -c`"),
    (["-"], "a program read from stdin"),
])
def test_check_names_the_real_cause_when_even_reconstruction_is_impossible(
        tmp_path, monkeypatch, capsys, argv, kind):
    """On a pre-3.10 interpreter even the reconstruction can come up empty
    (REPL, `python -c`, `python -`). The report must name that as the cause
    rather than blaming .venv/ — a live, reachable state, not a hypothetical."""
    venv_bin = _broken_venv(tmp_path) / "bin"
    monkeypatch.setattr(bootstrap, "VENV_DIR", venv_bin.parent)
    monkeypatch.setattr(bootstrap, "missing_modules", lambda: ["psycopg2"])
    _simulate_pre_310(monkeypatch, argv)
    assert bootstrap.self_check() == 1
    out = capsys.readouterr().out
    assert "predates sys.orig_argv (3.10+)" in out
    assert kind in out
    assert "activate the project environment" in out
    assert _field(out, "venv_fallback_armed") == "false"


def test_check_replays_from_a_constructed_line_when_orig_argv_is_absent(tmp_path,
                                                                      monkeypatch, capsys):
    """Pre-3.10 (macOS's /usr/bin/python3 shim), script invocation: the
    command line is reconstructed from sys.argv, so the report must show it
    ARMED — a resolvable interpreter, exit 0 — not refuse."""
    venv_bin = _broken_venv(tmp_path) / "bin"
    monkeypatch.setattr(bootstrap, "VENV_DIR", venv_bin.parent)
    monkeypatch.setattr(bootstrap, "missing_modules", lambda: ["psycopg2"])
    _simulate_pre_310(monkeypatch, ["scripts/report_figures.py", "--check"])
    assert bootstrap.self_check() == 0
    out = capsys.readouterr().out
    assert _field(out, "pinned deps") == "missing psycopg2"
    assert _field(out, "venv interpreter") == f"{venv_bin / 'python'} (present)"
    assert _field(out, "selected interpreter") == str(venv_bin / "python")
    assert _field(out, "venv_fallback_armed") == "true"
    assert _field(out, "fallback").startswith("armed — ")


def test_ensure_venv_names_the_same_cause_the_self_test_reports(tmp_path, monkeypatch):
    """On a pre-3.10 interpreter ensure_venv() refuses at package-import time,
    before --check can print anything. The operator therefore sees
    ensure_venv()'s message, so it must carry the same diagnosis."""
    venv_bin = _broken_venv(tmp_path) / "bin"
    monkeypatch.setattr(bootstrap, "VENV_DIR", venv_bin.parent)
    monkeypatch.setattr(bootstrap, "missing_modules", lambda: ["psycopg2"])
    _simulate_pre_310(monkeypatch, ["-c", "import app"])
    with pytest.raises(SystemExit, match="predates sys.orig_argv"):
        bootstrap.ensure_venv()


# ---------------------------------------------------------------------------
# The self-test must not drift from the behavior it reports.
# ---------------------------------------------------------------------------

def test_fallback_plan_and_ensure_venv_agree_in_every_state(tmp_path, monkeypatch):
    """For each state, the plan --check reports must predict exactly what
    ensure_venv() does: return None, raise SystemExit, or run the child."""
    # complete -> no-op
    monkeypatch.setattr(bootstrap, "missing_modules", lambda: [])
    assert bootstrap.fallback_plan().state == bootstrap.STATE_COMPLETE
    assert bootstrap.ensure_venv() is None

    # no-venv -> SystemExit with the install hint
    monkeypatch.setattr(bootstrap, "VENV_DIR", tmp_path / ".venv")
    monkeypatch.setattr(bootstrap, "missing_modules", lambda: ["psycopg2"])
    assert bootstrap.fallback_plan().state == bootstrap.STATE_NO_VENV
    with pytest.raises(SystemExit, match=r"pip install -r requirements\.txt"):
        bootstrap.ensure_venv()

    # broken-venv -> SystemExit with the recreate hint
    venv_python = _broken_venv(tmp_path) / "bin" / "python"
    monkeypatch.setattr(bootstrap, "VENV_DIR", venv_python.parent.parent)
    monkeypatch.setattr(sys, "executable", str(venv_python))
    assert bootstrap.fallback_plan().state == bootstrap.STATE_BROKEN_VENV
    with pytest.raises(SystemExit, match="recreate it"):
        bootstrap.ensure_venv()

    # unreplayable -> SystemExit with the activate hint
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3.13")
    monkeypatch.setattr(sys, "orig_argv", ["/usr/bin/python3.13", "-c", "pass"],
                        raising=False)
    assert bootstrap.fallback_plan().state == bootstrap.STATE_UNREPLAYABLE
    with pytest.raises(SystemExit, match="activate"):
        bootstrap.ensure_venv()

    # armed -> the child actually runs, carrying the planned command
    monkeypatch.setattr(sys, "orig_argv",
                        ["/usr/bin/python3.13", "-m", "pytest", "-q"], raising=False)
    monkeypatch.setattr(bootstrap.subprocess, "call", _raise_sentinel)
    assert bootstrap.fallback_plan().state == bootstrap.STATE_ARMED
    with pytest.raises(_ExecSentinel):
        bootstrap.ensure_venv()


# ---------------------------------------------------------------------------
# CLI surface: only --check is a command; a typo must not read as a verdict.
# ---------------------------------------------------------------------------

def test_main_routes_check_help_and_typos(capsys):
    assert bootstrap.main(["--check"]) == 0
    capsys.readouterr()

    assert bootstrap.main(["--help"]) == 0
    assert "usage: python -m app.bootstrap --check" in capsys.readouterr().out

    assert bootstrap.main([]) == 2
    assert bootstrap.main(["--chek"]) == 2
    err = capsys.readouterr().err
    assert "unknown arguments: --chek" in err
    assert "usage: python -m app.bootstrap --check" in err


def test_main_defaults_to_sys_argv_when_none_is_passed(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["app.bootstrap", "--help"])
    assert bootstrap.main() == 0


def test_the_documented_command_really_runs_and_exits_zero():
    """End-to-end proof of the interface the README documents."""
    import subprocess

    proc = subprocess.run(
        [str(VENV_BIN / "python"), "-m", "app.bootstrap", "--check"],
        capture_output=True, text=True, cwd=PROJECT_ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
    assert _field(proc.stdout, "repository root") == str(PROJECT_ROOT)
    assert _field(proc.stdout, "interpreter").startswith(f"{venv_python} (Python ")
    assert _field(proc.stdout, "pinned deps") == "complete"
    assert _field(proc.stdout, "venv interpreter") == f"{venv_python} (present)"
    assert _field(proc.stdout, "venv_fallback_armed") == "false"
    assert proc.stderr == ""


def _pre_310_python() -> str | None:
    """A real interpreter older than 3.10 — the only one on which
    constructed_argv() runs for real (macOS ships /usr/bin/python3 as 3.9)."""
    import subprocess

    for candidate in ("/usr/bin/python3", "/usr/local/bin/python3"):
        if not Path(candidate).exists():
            continue
        probe = subprocess.run(
            [candidate, "-c",
             "import sys; sys.exit(0 if sys.version_info < (3, 10) else 1)"],
            capture_output=True,
        )
        if probe.returncode == 0:
            return candidate
    return None


def test_the_documented_command_works_on_a_pre_310_interpreter():
    """End-to-end on the interpreter the construction exists for: a system
    python3 without sys.orig_argv must replay, not refuse. Skips where no
    such interpreter exists (Linux CI ships 3.10+)."""
    import subprocess

    old = _pre_310_python()
    if old is None:
        pytest.skip("needs a pre-3.10 interpreter (macOS's system python3)")
    if not (PROJECT_ROOT / ".venv" / "bin" / "python").is_file():
        pytest.skip("no .venv/ to replay into")

    # `-m`: fires while app's own package is being imported — the runpy-frame
    # window where neither sys.orig_argv nor __main__.__spec__ has the name.
    proc = subprocess.run(
        [old, "-m", "app.bootstrap", "--check"],
        capture_output=True, text=True, cwd=PROJECT_ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    assert _field(proc.stdout, "repository root") == str(PROJECT_ROOT)
    assert _field(proc.stdout, "pinned deps") == "complete"
    if "[bootstrap]" in proc.stderr:            # replayed (the shim lacks the pins)
        assert "re-running under" in proc.stderr
        assert _field(proc.stdout, "interpreter").startswith(
            str(PROJECT_ROOT / ".venv" / "bin")
        )

    # A script: fires with sys.argv naming the file — the argv branch.
    proc = subprocess.run(
        [old, "scripts/report_figures.py", "--help"],
        capture_output=True, text=True, cwd=PROJECT_ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    if "[bootstrap]" in proc.stderr:
        assert "re-running under" in proc.stderr


# ---------------------------------------------------------------------------
# Read-only guarantee: --check reports, it never repairs.
# ---------------------------------------------------------------------------

def test_check_is_read_only_even_when_the_venv_is_missing(tmp_path, monkeypatch):
    """--check must create nothing, spawn nothing, and reach neither the
    database nor the network — including in the state most likely to tempt a
    "helpful" repair, a missing .venv/."""
    import socket
    import subprocess as _subprocess

    def _forbidden(*args, **kwargs):
        raise AssertionError("--check must not do that")

    venv_root = tmp_path / ".venv"          # absent: the worst case
    monkeypatch.setattr(bootstrap, "VENV_DIR", venv_root)
    monkeypatch.setattr(bootstrap, "missing_modules", lambda: ["psycopg2"])
    monkeypatch.setattr(bootstrap.subprocess, "call", _forbidden)
    monkeypatch.setattr(_subprocess, "Popen", _forbidden)   # no pip, no re-run
    monkeypatch.setattr(socket, "socket", _forbidden)      # no external calls

    assert sorted(p.name for p in tmp_path.iterdir()) == []
    assert bootstrap.self_check() == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == []   # nothing created
    assert not venv_root.exists()                              # no venv made


def test_check_never_reads_or_prints_the_env_file(tmp_path, monkeypatch, capsys):
    """Secrets stay unread: the report must not contain .env contents."""
    (tmp_path / ".env").write_text("DATABASE_URL=postgres://secret:pw@host/db\n")
    monkeypatch.setattr(bootstrap, "VENV_DIR", tmp_path / ".venv")
    bootstrap.self_check()
    out = capsys.readouterr().out
    assert "secret" not in out and "postgres://" not in out
