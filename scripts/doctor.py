#!/usr/bin/env python3
"""Doctor: check the whole local setup, not just the interpreter.

`python -m app.bootstrap --check` answers one question — which interpreter
would this command run under? This answers the rest, in one pass, and prints
one PASS/WARN/FAIL line per check:

    interpreter   the bootstrap state actually in force, the interpreter,
                  its Python version (3.10+ matters for command-line replay)
    pinned-deps   every requirement.txt pin importable, at the pinned version
    env-file      .env present, and every setting .env.example marks as
                  required actually configured (key names only — never values)
    llm-keys      LLM_API_KEY / EMBEDDING_API_KEY set (values never printed)
    admin-key     whether /api/admin/* routes are enabled (documented: an
                  empty key is a valid, deliberate state)
    dev-db        DATABASE_URL reachable, schema_migrations up to date
    test-db       same for TEST_DATABASE_URL; the test suite creates and
                  migrates that database itself, so missing state there is a
                  warning, not a failure
    fixtures      data/samples fixtures the tests require
    faers-raw     raw FAERS data present (missing is a warning: downloading
                  is an explicit operator step)
    processed     data/processed present (ingestion creates it)
    dailymed      external reachability — only with --network, so the default
                  run is fully offline and fast

Guarantees, same as the bootstrap self-test:

  * Read-only. It connects to PostgreSQL for SELECTs only and creates,
    repairs, and downloads nothing — every problem is reported with the
    command that would fix it, never fixed inline.
  * Secrets stay unread-and-unprinted. Configuration is reported by key
    name; URLs are shown as scheme://host/db with credentials stripped,
    and exception text is redacted the same way.
  * It never crashes: a check that raises becomes a FAIL line naming the
    exception (and a red exit), so one broken probe still yields the full
    report.

Exit codes: 0 = no failing check (warnings are allowed and expected on a
fresh clone), 1 = at least one check failed, 2 = bad usage.

Self-test: tests/unit/test_doctor.py pins every check's verdicts, the exit
codes, and the secret-safety rules.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# app before psycopg2: importing app runs the venv bootstrap (app/bootstrap.py),
# so a system interpreter missing psycopg2 re-runs this script under .venv/
# instead of dying on the import below.
from app import bootstrap  # noqa: E402
from app.config import settings  # noqa: E402
import psycopg2  # noqa: E402

PROJECT_ROOT = settings.PROJECT_ROOT
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
ENV_FILE = PROJECT_ROOT / ".env"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
SAMPLES = PROJECT_ROOT / "data" / "samples"
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

USAGE = (
    "usage: python scripts/doctor.py [--network]\n"
    "\n"
    "  check the whole local setup: the interpreter/bootstrap state, the\n"
    "  pinned requirements, .env configuration, both database servers and\n"
    "  their migration state, and the data directories; --network also\n"
    "  pings DailyMed. Read-only — it never creates or repairs anything.\n"
    "  Exit codes: 0 = no failing check (warnings allowed), 1 = at least\n"
    "  one check failed, 2 = bad usage\n"
)

# requirements.txt distribution name -> importable module name (the rest
# follow the usual dash->underscore rule).
_IMPORT_NAME = {"psycopg2-binary": "psycopg2", "python-dotenv": "dotenv"}


class Result(NamedTuple):
    """One check's verdict: status, short name, human detail, fix-it hint."""

    status: str
    name: str
    detail: str
    hint: str = ""


# ---------------------------------------------------------------------------
# Checks — one function each; every one returns a Result, never raises for
# an unhealthy setup (collect_checks() turns an unexpected crash into FAIL).
# ---------------------------------------------------------------------------

def check_interpreter() -> Result:
    """The bootstrap state in force, the interpreter, and its version."""
    plan = bootstrap.fallback_plan()
    exe = sys.executable or "<embedded interpreter>"
    version = ".".join(str(v) for v in sys.version_info[:3])
    venv_python = bootstrap.VENV_DIR / "bin" / "python"

    if plan.state != bootstrap.STATE_COMPLETE:
        # Unreachable through `python scripts/doctor.py` (app/__init__ runs
        # ensure_venv() first), but a direct call must report, not guess.
        return Result(FAIL, "interpreter",
                      f"{exe} (Python {version}) — bootstrap state {plan.state!r}",
                      "run doctor through `python scripts/doctor.py` so the "
                      "bootstrap can re-run it under .venv/ if needed")

    status = PASS
    notes = [f"{exe} (Python {version}) — all pinned modules present"]
    in_venv = Path(exe).resolve() == venv_python.resolve()
    if not in_venv:
        notes.append("complete interpreter outside .venv (CI/Docker style)")
        if not venv_python.is_file():
            status = WARN
            notes.append(".venv/ is missing — create it so bare commands can "
                         "replay when modules go missing")
    if tuple(sys.version_info[:3]) < (3, 10):
        status = WARN
        notes.append("Python < 3.10 has no sys.orig_argv — replay works via "
                     "the constructed command line, but interpreter flags "
                     "(-i, -O, -X) are not preserved")
    return Result(status, "interpreter", " — ".join(notes))


def pinned_requirements(path: Path = REQUIREMENTS) -> list[tuple[str, str]]:
    """(distribution name, pinned version) for each pin in requirements.txt."""
    pins: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, _, version = line.partition("==")
        pins.append((name.strip(), version.strip()))
    return pins


def check_pinned_deps() -> Result:
    """Every requirement.txt pin importable, at the pinned version.

    Broader than the bootstrap's REQUIRED_MODULES probe on purpose: the
    bootstrap only needs enough to *reach* .venv/ (it fires before any
    third-party import), while this checks the whole pinned runtime —
    version drift included — using find_spec/metadata, never an import.
    """
    pins = pinned_requirements()
    missing, drifted = [], []
    for name, version in pins:
        module = _IMPORT_NAME.get(name, name.replace("-", "_").lower())
        if importlib.util.find_spec(module) is None:
            missing.append(name)
            continue
        try:
            installed = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            installed = None   # importable, but not installed as a distribution
        if installed is not None and installed != version:
            drifted.append(f"{name} {installed} != {version}")
    if missing:
        detail = f"not importable: {', '.join(missing)}"
        if drifted:
            detail += f"; also drifted: {', '.join(drifted)}"
        return Result(FAIL, "pinned-deps", detail,
                      "pip install -r requirements.txt (or recreate .venv/)")
    if drifted:
        return Result(WARN, "pinned-deps",
                      f"all {len(pins)} pins importable, but version drift: "
                      f"{', '.join(drifted)}",
                      "pip install -r requirements.txt to return to the pins")
    return Result(PASS, "pinned-deps",
                  f"{len(pins)}/{len(pins)} pinned requirements importable, "
                  "versions match")


def _env_pairs(path: Path) -> dict[str, str]:
    """KEY -> VALUE for a dotenv-style file. Values are never printed."""
    pairs: dict[str, str] = {}
    if not path.is_file():
        return pairs
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        pairs[key.strip()] = value.strip()
    return pairs


def check_env_file() -> Result:
    """`.env` present, and every setting .env.example marks required set.

    Required = the template carries a non-empty value for it. Checked
    against the runtime environment (shell exports count), key names only.
    """
    if not ENV_FILE.is_file():
        return Result(WARN, "env-file", "no .env — falling back to defaults",
                      "cp .env.example .env")
    required = [k for k, v in _env_pairs(ENV_EXAMPLE).items() if v]
    unset = [k for k in required if not os.environ.get(k, "").strip()]
    if unset:
        return Result(WARN, "env-file",
                      f"configured settings not set: {', '.join(unset)}",
                      "fill them in .env (see .env.example)")
    return Result(PASS, "env-file",
                  f".env present — {len(required)} required settings configured")


def check_llm_keys() -> Result:
    """Provider credentials for label RAG and the investigation workflow."""
    unset = [k for k in ("LLM_API_KEY", "EMBEDDING_API_KEY")
             if not os.environ.get(k, "").strip()]
    if unset:
        return Result(WARN, "llm-keys",
                      f"{', '.join(unset)} not set — label RAG and LLM-backed "
                      "investigation cannot call the provider",
                      "set them in .env (see .env.example)")
    return Result(PASS, "llm-keys",
                  "LLM_API_KEY and EMBEDDING_API_KEY set — provider calls enabled")


def check_admin_key() -> Result:
    """An empty admin key is a documented state, so this never warns."""
    if os.environ.get("ADMIN_API_KEY", "").strip():
        return Result(PASS, "admin-key", "set — /api/admin/* routes enabled")
    return Result(PASS, "admin-key",
                  "not set — /api/admin/* routes disabled (documented default)")


def _connect(url: str):
    """psycopg2.connect with a short timeout so a dead server cannot hang."""
    return psycopg2.connect(url, connect_timeout=5)


def _display_url(url: str) -> str:
    """scheme://host[:port]/db — credentials and query strings never shown."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return f"{parsed.scheme}://{host}{parsed.path}"


def _redact(text: str) -> str:
    """Strip anything that looks like credentials from an error message."""
    return re.sub(r"://[^@\s/]*@", "://***@", text)


def _migration_files() -> list[str]:
    return sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql")
                  if not p.name.endswith("_down.sql"))


def _connect_failure(name: str, url: str, message: str,
                     auto_provision: bool) -> Result:
    """Classify a connect failure: missing database vs unreachable server."""
    msg = (_redact(message).splitlines() or ["connection failed"])[0]
    database = urlparse(url).path.lstrip("/") or "(default)"
    if "does not exist" in message:
        if auto_provision:
            return Result(WARN, name,
                          f"database {database!r} does not exist at "
                          f"{_display_url(url)} — the test suite creates and "
                          "migrates it on first run")
        return Result(FAIL, name,
                      f"database {database!r} does not exist at {_display_url(url)}",
                      "python scripts/create_databases.py")
    return Result(FAIL, name, f"cannot reach {_display_url(url)}: {msg}",
                  "start PostgreSQL and check DATABASE_URL / TEST_DATABASE_URL "
                  "in .env")


def check_database(name: str, url: str, *, auto_provision: bool) -> Result:
    """Reachable, database exists, every migration on disk applied.

    `auto_provision` marks the test database: tests/conftest.py creates and
    migrates it itself, so missing pieces there are warnings; the dev
    database must already be ready for pipeline scripts.
    """
    try:
        conn = _connect(url)
    except psycopg2.OperationalError as exc:
        return _connect_failure(name, url, str(exc), auto_provision)
    except psycopg2.Error as exc:
        return Result(FAIL, name, f"database error: {_redact(str(exc))}",
                      "check PostgreSQL and the configured URL")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('schema_migrations')")
            row = cur.fetchone()
            applied: set[str] = set()
            if row and row[0] is not None:
                cur.execute("SELECT filename FROM schema_migrations")
                applied = {r[0] for r in cur.fetchall()}
    except psycopg2.Error as exc:
        return Result(FAIL, name, f"database error: {_redact(str(exc))}",
                      "check PostgreSQL and the configured URL")
    finally:
        conn.close()

    disk = _migration_files()
    exists = bool(row and row[0] is not None)
    pending = [f for f in disk if f not in applied]
    if not exists or pending:
        if not exists:
            detail = f"{_display_url(url)} reachable but not migrated " \
                     "(no schema_migrations table)"
        else:
            detail = f"{_display_url(url)} reachable — {len(pending)} of " \
                     f"{len(disk)} migration(s) not applied: {', '.join(pending)}"
        if auto_provision:
            return Result(WARN, name, detail + " — the test suite applies "
                          "them automatically (scripts/migrate.py --test)")
        return Result(FAIL, name, detail, "python scripts/migrate.py")
    return Result(PASS, name,
                  f"{_display_url(url)} reachable — {len(disk)}/{len(disk)} "
                  "migrations applied")


def check_fixtures() -> Result:
    """The committed sample fixtures tests/conftest.py requires."""
    missing = [name for name in ("fixture_2024q1", "fixture_2024q2")
               if not (SAMPLES / name).is_dir()
               or not any((SAMPLES / name).iterdir())]
    if missing:
        return Result(FAIL, "fixtures",
                      f"missing or empty: {', '.join(missing)} (data/samples)",
                      "restore data/samples/ — the tests need the committed fixtures")
    return Result(PASS, "fixtures",
                  "data/samples present (fixture_2024q1, fixture_2024q2)")


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def check_faers_raw() -> Result:
    """Raw FAERS quarters: absent is normal on a fresh clone (a warning)."""
    path = settings.faers_data_path()
    if path.is_dir() and any(path.iterdir()):
        count = sum(1 for _ in path.iterdir())
        return Result(PASS, "faers-raw", f"{_display(path)} — {count} entr{'ies' if count != 1 else 'y'}")
    return Result(WARN, "faers-raw",
                  f"{_display(path)} has no data",
                  "python scripts/download_faers.py before real ingestion")


def check_processed_path() -> Result:
    """data/processed: created by the first ingestion run, so absent warns."""
    path = settings.processed_data_path()
    if path.is_dir():
        return Result(PASS, "processed", f"{_display(path)} present")
    return Result(WARN, "processed",
                  f"{_display(path)} missing",
                  "created by the first ingestion run (scripts/run_ingestion.py)")


def _http_status(url: str, timeout: int) -> int:
    import requests
    return requests.get(url, timeout=timeout).status_code


def check_dailymed() -> Result:
    """External reachability (only with --network; the default run is offline)."""
    from app.rag.dailymed_client import DAILYMED_API_BASE
    try:
        status = _http_status(f"{DAILYMED_API_BASE}/drugs.json?pagesize=1", 8)
    except Exception as exc:  # a doctor reports outages, it never crashes
        return Result(FAIL, "dailymed",
                      f"unreachable: {type(exc).__name__}: {exc}",
                      "check network access to dailymed.nlm.nih.gov")
    if status >= 400:
        return Result(FAIL, "dailymed", f"HTTP {status} from DailyMed",
                      "check network access / the DailyMed API")
    return Result(PASS, "dailymed", f"reachable (HTTP {status})")


# ---------------------------------------------------------------------------
# Assembly, report, CLI
# ---------------------------------------------------------------------------

def _guard(name: str, check) -> Result:
    """Run one check; an unexpected crash becomes a visible FAIL, not a traceback."""
    try:
        return check()
    except Exception as exc:  # noqa: BLE001 — reported, never swallowed
        return Result(FAIL, name, f"check raised {type(exc).__name__}: {exc}",
                      "this probe failed unexpectedly — treat its area as unchecked")


def collect_checks(network: bool = False) -> list[Result]:
    """Every check in report order. Offline unless network=True."""
    checks: list[tuple[str, object]] = [
        ("interpreter", check_interpreter),
        ("pinned-deps", check_pinned_deps),
        ("env-file", check_env_file),
        ("llm-keys", check_llm_keys),
        ("admin-key", check_admin_key),
        ("dev-db", lambda: check_database("dev-db", settings.database_url(),
                                          auto_provision=False)),
        ("test-db", lambda: check_database("test-db", settings.test_database_url(),
                                           auto_provision=True)),
        ("fixtures", check_fixtures),
        ("faers-raw", check_faers_raw),
        ("processed", check_processed_path),
    ]
    if network:
        checks.append(("dailymed", check_dailymed))
    return [_guard(name, check) for name, check in checks]


def report(results: list[Result]) -> int:
    """Print the verdict lines and summary; exit 1 iff anything FAILed."""
    print("PharmaSignal doctor — local setup\n")
    width = max((len(r.name) for r in results), default=0)
    for r in results:
        print(f"{r.status:<4}  {r.name:<{width}}  {r.detail}")
        if r.hint:
            print(f"{'':<{4 + 2 + width + 2}}→ {r.hint}")
    counts = {s: sum(1 for r in results if r.status == s) for s in (PASS, WARN, FAIL)}
    print(f"\nchecks: passed={counts[PASS]} warnings={counts[WARN]} failed={counts[FAIL]}")
    return 1 if counts[FAIL] else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Only --network is a flag; a typo exits 2, not 0."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args in (["-h"], ["--help"]):
        print(USAGE, end="")
        return 0
    unknown = [a for a in args if a != "--network"]
    if unknown:
        print(f"[doctor] unknown arguments: {' '.join(unknown)}\n{USAGE}",
              file=sys.stderr, end="")
        return 2
    return report(collect_checks(network="--network" in args))


if __name__ == "__main__":
    raise SystemExit(main())
