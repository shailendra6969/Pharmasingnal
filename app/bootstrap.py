"""Interpreter bootstrap: re-run the current command under the project venv.

The runtime is pinned in requirements.txt and a ready virtualenv ships in
.venv/. On a developer machine, though, a bare `pytest` or
`python scripts/report_figures.py` usually resolves to whatever interpreter
happens to be first on PATH (Anaconda base, Homebrew, system python3) — one
that has no psycopg2/pandas — and every command dies with ModuleNotFoundError
before doing anything useful.

This module fixes exactly that: when the current interpreter is missing a
pinned module, the *same command* is re-run under .venv/ in a child process
and this process exits with the child's exit code (signals map to the shell's
128+N). The child inherits this process's stdout/stderr, so everything the
command prints stays visible — os.execv was deliberately not used: a process
image replaced mid-pytest-startup loses its output in some sandboxes and
cannot be traced or timed.

It is a strict no-op — no fork, no output — when the current interpreter
already has every pinned module, so virtualenvs, CI runners and Docker images
are untouched. There is no retry loop: if .venv/ itself is broken, or the
invocation cannot be replayed (a REPL, a program piped to `python -`, or
`python -c` under an interpreter name the venv lacks), it raises SystemExit
with instructions instead of re-running anything.

Entry points call ensure_venv() *before* importing any third-party module:

    app/__init__.py   every `from app...` import (scripts, API, tests)
    conftest.py       bare `pytest` / `python -m pytest`, which import test
                      modules (and thus psycopg2) directly at collection time
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENV_DIR = PROJECT_ROOT / ".venv"

# The pinned runtime (requirements.txt), probed with find_spec so nothing is
# actually imported — a missing psycopg2 must be detected before anything
# imports it.
REQUIRED_MODULES = (
    "pandas",
    "psycopg2",
    "dotenv",       # python-dotenv
    "requests",
    "flask",
)


def missing_modules() -> list[str]:
    """Pinned modules the current interpreter cannot import."""
    return [m for m in REQUIRED_MODULES if importlib.util.find_spec(m) is None]


def reexec_command(venv_bin: Path, orig_argv: list[str] | None = None) -> list[str] | None:
    """Command that replays this invocation under `venv_bin`'s interpreter.

    Handles every way commands are launched in this repo (sys.orig_argv is the
    original command line, before the interpreter consumed argv[0]):

        pytest / py.test            -> .venv/bin/<same console script>
        python -m mod ...           -> .venv/bin/python -m mod ...
        python scripts/x.py ...     -> .venv/bin/python scripts/x.py ...
        ./scripts/x.py ...          -> .venv/bin/python ./scripts/x.py ...

    Returns None when the invocation cannot be replayed (empty argv, REPL,
    `python -c`, or a program piped to `python -`), so the caller can fail
    loudly instead of guessing.

    `python -` deserves its own note: the program text lives on stdin, which
    this interpreter has already consumed by the time the bootstrap fires. A
    replayed child would inherit the exhausted stdin, run an empty script and
    exit 0 — a silent wrong result. Refusing is the only honest answer.
    """
    argv = list(orig_argv or [])
    if not argv:
        return None
    head = argv[0]
    rest = argv[1:]

    # `python -`: the program comes from stdin, already consumed above — a
    # child would see EOF and exit 0 with no output. Must be rejected before
    # the console-script lookup below, since `python` IS a .venv/bin name.
    if rest[:1] == ["-"]:
        return None

    # Executed as a file (shebang): replay it with the venv interpreter.
    if head.endswith(".py") and Path(head).is_file():
        return [str(venv_bin / "python"), *argv]

    # A console script (pytest, py.test, python, python3, ...): the venv has
    # its own counterpart with a venv-pointing shebang.
    venv_names = {p.name for p in venv_bin.iterdir()} if venv_bin.is_dir() else set()
    if Path(head).name in venv_names:
        return [str(venv_bin / Path(head).name), *rest]

    # Interpreter forms the name lookup above could not match.
    if rest[:1] == ["-m"] and len(rest) > 1:
        return [str(venv_bin / "python"), *rest]
    if rest and Path(rest[0]).is_file():
        return [str(venv_bin / "python"), *rest]
    return None


def exit_code(result: int) -> int:
    """Translate a subprocess result into this process's exit code.

    On POSIX, subprocess reports a signal-terminated child as -N; shells use
    128 + N for the same event, and callers (CI, Makefiles) expect that.
    """
    return 128 - result if result < 0 else result


def ensure_venv() -> None:
    """Re-run the current command under .venv/ if pinned modules are missing.

    No-op when the current interpreter is complete. Never loops: each path
    either runs the replay exactly once and exits with its code, or raises
    SystemExit with instructions.
    """
    missing = missing_modules()
    if not missing:
        return

    install_hint = (
        "create it with: python3.12 -m venv .venv && "
        ".venv/bin/pip install -r requirements.txt"
    )
    venv_bin = VENV_DIR / "bin"
    venv_python = venv_bin / "python"
    if not venv_python.is_file():
        raise SystemExit(
            f"[bootstrap] {', '.join(missing)} not available in "
            f"{sys.executable or '<embedded interpreter>'} and no .venv/ exists — "
            f"{install_hint}"
        )
    if Path(sys.executable).resolve() == venv_python.resolve():
        raise SystemExit(
            f"[bootstrap] .venv/ itself is missing {', '.join(missing)}; "
            f"recreate it: rm -rf .venv && {install_hint}"
        )

    command = reexec_command(venv_bin, list(getattr(sys, "orig_argv", None) or []))
    if command is None:
        raise SystemExit(
            f"[bootstrap] {', '.join(missing)} not available in {sys.executable}; "
            f"activate the project environment (source .venv/bin/activate) and retry"
        )

    print(
        f"[bootstrap] {', '.join(missing)} not available in {sys.executable}; "
        f"re-running under {command[0]}",
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit(exit_code(subprocess.call(command)))
