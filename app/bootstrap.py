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

Replay reads the real command line from sys.orig_argv, which only exists on
Python 3.10+. Interpreters older than that — notably macOS's
/usr/bin/python3 shim, still 3.9 — get the command line reconstructed from
what such an interpreter does keep: sys.argv, __main__.__spec__, and (for the
brief moment before runpy assigns the spec) runpy's own mod_name frame. See
constructed_argv(). Only invocations that leave nothing behind to reconstruct
(REPL, `python -c`, `python -`) still refuse on those interpreters.

Entry points call ensure_venv() *before* importing any third-party module:

    app/__init__.py   every `from app...` import (scripts, API, tests)
    conftest.py       bare `pytest` / `python -m pytest`, which import test
                      modules (and thus psycopg2) directly at collection time

Self-test: `python -m app.bootstrap --check` prints the interpreter this
command actually runs under and whether the venv fallback is armed, then
exits 0 when the configuration is valid and an interpreter is resolvable
(complete here, or armed to re-run under .venv/), 1 when it is not, and 2 on
bad usage.
Because app/__init__.py runs ensure_venv() at package import, a bare
interpreter re-runs this very `--check` under .venv/ first, so the interpreter
reported is always the one that executes the command — the same guarantee a
real command gets. The decision itself is not duplicated: `--check` and
ensure_venv() both read fallback_plan(), so the report cannot describe a
behavior the bootstrap does not perform.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

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

# fallback_plan() states — the five mutually exclusive outcomes, one per
# branch of ensure_venv(). `--check` maps each to a human-readable verdict.
STATE_COMPLETE = "complete"            # every pinned module present: no-op
STATE_ARMED = "armed"                  # would re-run the same command under .venv/
STATE_NO_VENV = "no-venv"              # missing modules and no .venv/ to fall back to
STATE_BROKEN_VENV = "broken-venv"      # missing modules *inside* .venv/ itself
STATE_UNREPLAYABLE = "unreplayable"    # missing modules, but argv cannot be replayed

USAGE = (
    "usage: python -m app.bootstrap --check\n"
    "\n"
    "  --check  print the repository root, the interpreter running this\n"
    "           process, whether the .venv/ fallback is armed, and which\n"
    "           interpreter the bootstrap would select; exit 0 = configuration\n"
    "           valid and an interpreter resolvable, 1 = invalid or\n"
    "           unresolvable, 2 = bad usage\n"
)


def missing_modules() -> list[str]:
    """Pinned modules the current interpreter cannot import."""
    return [m for m in REQUIRED_MODULES if importlib.util.find_spec(m) is None]


def reexec_command(venv_bin: Path, orig_argv: list[str] | None = None) -> list[str] | None:
    """Command that replays this invocation under `venv_bin`'s interpreter.

    Handles every way commands are launched in this repo (the argv is the
    original command line, before the interpreter consumed argv[0] — either
    sys.orig_argv itself, or the line constructed_argv() rebuilt when that
    attribute does not exist):

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


def _runpy_module_name() -> str | None:
    """The `-m` target, read from the runpy frame that is executing it.

    Under `python -m pkg.mod` the interpreter takes the module name *out* of
    sys.argv entirely (argv[0] is just "-m") and runpy does not assign
    __main__.__spec__ until after the target's package has been imported —
    which is exactly the moment app/__init__.py fires the bootstrap. The name
    survives in only one place: runpy's `mod_name` local.

    Reading a standard-library frame is deliberately narrow and safe by
    construction: this runs only when sys.orig_argv is missing, i.e. on the
    ≤3.9-era interpreters whose runpy is frozen, so no future Python can
    change underneath it. If no such frame is present (another
    implementation, an in-process runpy re-launch), returns None so the
    bootstrap refuses rather than replaying the wrong command.
    """
    frame = sys._getframe()
    while frame is not None:
        if Path(frame.f_code.co_filename).name == "runpy.py":
            name = frame.f_locals.get("mod_name")
            if isinstance(name, str) and name:
                return name
        frame = frame.f_back
    return None


def _replayable_module_name(name: str) -> str:
    """Module name in the form `-m` is canonically invoked with.

    Running a package (`python -m pkg`) records `pkg.__main__` on
    __main__.__spec__. Replaying that exact name works, but the canonical
    `pkg` is what the operator typed and works on every interpreter — and for
    the runpy-frame path (`python -m pkg` where the bootstrap fires during
    pkg's own import) it is the only faithful inverse.
    """
    return name[: -len(".__main__")] if name.endswith(".__main__") else name


def constructed_argv() -> list[str] | None:
    """Reconstruct this invocation's command line when sys.orig_argv is absent.

    Python only gained sys.orig_argv in 3.10; macOS's /usr/bin/python3 shim
    is still 3.9, so on it there is no real command line to replay. What such
    an interpreter does keep is sys.argv — the program it is running, plus
    its arguments — and __main__.__spec__, which runpy sets once a `-m`
    target is executing. This rebuilds the command line those imply, shaped
    exactly like an orig_argv, so reexec_command() replays it through the
    same vetted mapping instead of a second code path:

        `python -m mod args`  -> [python, -m, mod, args]
            (from __main__.__spec__, once the target module is running; also
            from runpy's mod_name frame in the earlier window where the
            spec is not assigned yet, which is where `python -m
            app.bootstrap` fires this bootstrap)
        `python script.py a`  -> [script.py, a]
        `./tool` / `pytest a` -> [./tool, a] / [pytest, a]
            (argv already IS the program plus its arguments — an
            interpreter-less console-script/shebang launch)
        REPL / `python -c` / `python -` -> None: the program text lives
            outside sys.argv (stdin or an interpreter flag), so there is
            nothing to reconstruct and the caller must refuse.

    Honest limits, all inherited from what a pre-3.10 interpreter discards:
    interpreter flags (`-i`, `-O`, `-X`) are gone with orig_argv, and an
    extensionless script invoked as `python3 tool` loses the interpreter name
    that identified it — that last case falls through to a refusal instead
    of a guess.
    """
    argv = list(sys.argv or [])
    if not argv:
        return None

    # `-m`, target already executing: runpy replaced sys.argv[0] with the
    # module file and recorded the module on __main__.__spec__. Checked first
    # because it identifies the module precisely; script launches leave the
    # spec None.
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    if spec is not None and getattr(spec, "name", None):
        head = sys.executable or "python"
        return [head, "-m", _replayable_module_name(spec.name), *argv[1:]]

    head = argv[0]
    if head == "-m":
        # Bootstrap fired before runpy assigned __spec__ (the `-m` target's
        # own package is being imported). The name left sys.argv entirely;
        # runpy's frame is the only remaining record of it.
        name = _runpy_module_name()
        if name is None:
            return None
        return [sys.executable or "python", "-m", _replayable_module_name(name),
                *argv[1:]]

    # REPL, `python -c`, `python -`: no recoverable command line.
    if head in ("", "-c", "-"):
        return None

    # A script or console script: sys.argv is already the command line with
    # the (shebang-executed) interpreter omitted — exactly the shape
    # reexec_command()'s file/console-script branches consume.
    return argv


def exit_code(result: int) -> int:
    """Translate a subprocess result into this process's exit code.

    On POSIX, subprocess reports a signal-terminated child as -N; shells use
    128 + N for the same event, and callers (CI, Makefiles) expect that.
    """
    return 128 - result if result < 0 else result


class Plan(NamedTuple):
    """What the bootstrap will do for the current interpreter and argv.

    The single decision point: ensure_venv() acts on it and `--check` reports
    it, so the self-test cannot describe a different behavior than the code
    performs.
    """
    state: str
    missing: tuple[str, ...]
    install_hint: str
    command: list[str] | None


def fallback_plan(orig_argv: list[str] | None = None) -> Plan:
    """Decide what ensure_venv() would do, without doing it.

    Defaults to sys.orig_argv — the real command line — so the reported
    command is exactly the command a re-run would spawn. When that attribute
    does not exist (Python < 3.10), the command line is reconstructed from
    sys.argv/__main__.__spec__ by constructed_argv() instead; an argv passed
    explicitly is always authoritative and never reconstructed. Every branch
    here corresponds one-to-one with a branch of ensure_venv().
    """
    missing = tuple(missing_modules())
    if not missing:
        return Plan(STATE_COMPLETE, missing, "", None)

    install_hint = (
        "create it with: python3.12 -m venv .venv && "
        ".venv/bin/pip install -r requirements.txt"
    )
    venv_bin = VENV_DIR / "bin"
    venv_python = venv_bin / "python"
    if not venv_python.is_file():
        return Plan(STATE_NO_VENV, missing, install_hint, None)
    if Path(sys.executable).resolve() == venv_python.resolve():
        return Plan(STATE_BROKEN_VENV, missing, install_hint, None)

    argv = list(orig_argv if orig_argv is not None
                else (getattr(sys, "orig_argv", None) or constructed_argv() or []))
    command = reexec_command(venv_bin, argv)
    if command is None:
        return Plan(STATE_UNREPLAYABLE, missing, install_hint, None)
    return Plan(STATE_ARMED, missing, install_hint, command)


def ensure_venv() -> None:
    """Re-run the current command under .venv/ if pinned modules are missing.

    No-op when the current interpreter is complete. Never loops: each path
    either runs the replay exactly once and exits with its code, or raises
    SystemExit with instructions.
    """
    plan = fallback_plan()
    if plan.state == STATE_COMPLETE:
        return

    missing = ", ".join(plan.missing)
    if plan.state == STATE_NO_VENV:
        raise SystemExit(
            f"[bootstrap] {missing} not available in "
            f"{sys.executable or '<embedded interpreter>'} and no .venv/ exists — "
            f"{plan.install_hint}"
        )
    if plan.state == STATE_BROKEN_VENV:
        raise SystemExit(
            f"[bootstrap] .venv/ itself is missing {missing}; "
            f"recreate it: rm -rf .venv && {plan.install_hint}"
        )
    if plan.state == STATE_UNREPLAYABLE:
        raise SystemExit(
            f"[bootstrap] {missing} not available in {sys.executable}; "
            f"{_unreplayable_reason()}"
        )

    # STATE_ARMED is the only state that carries a command.
    command = plan.command or []
    print(
        f"[bootstrap] {missing} not available in {sys.executable}; "
        f"re-running under {command[0]}",
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit(exit_code(subprocess.call(command)))


def _python_version() -> str:
    """Dotted version of the running interpreter, e.g. '3.9.6'."""
    return ".".join(str(v) for v in sys.version_info[:3])


def _unreplayable_reason() -> str:
    """Why the invocation cannot be replayed, for the one caller that has
    already refused to run. Shared by ensure_venv() and `--check` so the
    diagnosis the operator sees at the package-import stage is the same one
    the self-test reports.

    Pre-3.10 interpreters (notably macOS's /usr/bin/python3 shim) do not
    expose sys.orig_argv. The bootstrap reconstructs a command line for them
    from sys.argv, so reaching this state on such an interpreter means even
    the reconstruction came up empty — naming *that* is the difference
    between "your .venv is broken" and "your invocation leaves nothing to
    replay", which is the real, reachable case.
    """
    if getattr(sys, "orig_argv", None) is None:
        head = (list(sys.argv) or [""])[0]
        if head in ("", "-c", "-"):
            what = {"": "an interactive session",
                    "-c": "`python -c`",
                    "-": "a program read from stdin"}[head]
            detail = f"{what} leaves no command line to reconstruct"
        else:
            detail = "even the reconstructed command line cannot be replayed"
        return (
            f"this interpreter (Python {_python_version()}) predates "
            f"sys.orig_argv (3.10+) and {detail}; activate the project "
            f"environment (source .venv/bin/activate)"
        )
    return (
        "this invocation cannot be replayed; activate the project environment "
        "(source .venv/bin/activate)"
    )


def _fallback_summary(plan: Plan) -> str:
    """One-line verdict for `--check`, one branch per plan state."""
    if plan.state == STATE_COMPLETE:
        return "not armed — every pinned module is present, the bootstrap is a no-op"
    if plan.state == STATE_ARMED:
        return f"armed — the same command would re-run under {plan.command[0]}"
    if plan.state == STATE_NO_VENV:
        return f"cannot arm — no .venv/ exists ({plan.install_hint})"
    if plan.state == STATE_BROKEN_VENV:
        return (
            f"cannot arm — .venv/ itself is missing {', '.join(plan.missing)} "
            f"(rm -rf .venv && {plan.install_hint})"
        )
    return f"cannot arm — {_unreplayable_reason()}"


def selected_interpreter(plan: Plan) -> str:
    """Interpreter the bootstrap would actually end up running under.

    Deliberately separate from the interpreter running this process: when the
    fallback is armed they differ, and when the bootstrap refuses there is no
    selection at all.

        armed      -> the .venv/ interpreter the replay would spawn
        complete   -> this interpreter; nothing would change it
        cannot arm -> unresolved, because ensure_venv() refuses to guess
    """
    if plan.state == STATE_ARMED:
        return str(plan.command[0])
    if plan.state == STATE_COMPLETE:
        return sys.executable or "<embedded interpreter>"
    return "(unresolved — the bootstrap refuses to guess)"


def self_check() -> int:
    """`--check`: a read-only report of the interpreter situation.

    Prints the repository root, the interpreter running this process, the
    pinned-dependency status, the .venv/ target, the interpreter the bootstrap
    would select, and `venv_fallback_armed: true|false` together with the
    condition that decides it.

    Exit 0 = the bootstrap configuration is valid and an interpreter is
    resolvable (either complete here, or armed to re-run under .venv/); exit 1
    = invalid or unresolvable (no .venv/, a broken .venv/, or an invocation
    that cannot be replayed). Never writes, never imports a third-party
    module, never touches the network or the database.
    """
    plan = fallback_plan()
    interpreter = sys.executable or "<embedded interpreter>"
    venv_python = VENV_DIR / "bin" / "python"

    if plan.missing:
        deps = "missing " + ", ".join(plan.missing)
    else:
        deps = "complete"

    print(f"repository root      : {PROJECT_ROOT}")
    print(f"interpreter          : {interpreter} (Python {_python_version()})")
    print(f"pinned deps          : {deps}")
    print(
        f"venv interpreter     : {venv_python} "
        f"({'present' if venv_python.is_file() else 'absent'})"
    )
    print(f"selected interpreter : {selected_interpreter(plan)}")
    # Kept as a machine-readable token on its own line: greppable in CI logs.
    print(f"venv_fallback_armed: {'true' if plan.state == STATE_ARMED else 'false'}")
    print(f"fallback             : {_fallback_summary(plan)}")

    return 0 if plan.state in (STATE_COMPLETE, STATE_ARMED) else 1


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for `python -m app.bootstrap`.

    Only `--check` is a command; anything else is a usage error (exit 2), so a
    typo can never be mistaken for a bootstrap verdict.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--check"]:
        return self_check()
    if args in (["-h"], ["--help"]):
        print(USAGE, end="")
        return 0
    print(
        f"[bootstrap] unknown arguments: {' '.join(args) or '(none)'}\n{USAGE}",
        file=sys.stderr,
        end="",
    )
    return 2


if __name__ == "__main__":
    # `python -m app.bootstrap --check`. app/__init__.py has already run
    # ensure_venv() by the time this file executes as __main__, so a bare
    # interpreter re-runs this very --check under .venv/ first: the
    # interpreter printed above is always the one that executes the command,
    # which is the same guarantee a real command gets.
    raise SystemExit(main())
