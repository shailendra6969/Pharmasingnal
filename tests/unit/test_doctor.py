"""Unit tests for the setup doctor (scripts/doctor.py).

The doctor answers what `python -m app.bootstrap --check` does not: are the
pinned requirements actually the pinned ones, is .env configured, do both
databases exist and carry every migration, and are the data directories
there? These tests pin:

1. CLI surface — only --network is a flag; a typo exits 2, never 0;
2. exit codes — 0 with passes/warnings only, 1 when any check fails, and a
   check that *raises* must surface as a FAIL line, never a traceback
   (a doctor that crashes mid-report is worse than no doctor);
3. every check's verdicts, through injected inputs so no test depends on
   the machine's real .env, database, or data state;
4. the secret rules — configuration is reported by key name, URLs without
   credentials, redacted exception text: a configured secret must never
   reach the output;
5. an end-to-end run of `python scripts/doctor.py` (exit 0 or 1 is fine —
   a traceback or exit 2 is not).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
import pytest

import scripts.doctor as doctor

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 1. CLI surface: --network is the only flag; a typo is a usage error.
# ---------------------------------------------------------------------------

def test_help_documents_usage_and_exit_codes(capsys):
    assert doctor.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "usage: python scripts/doctor.py [--network]" in out
    assert "1 = at least" in out          # exit-code contract is documented


def test_a_typo_is_a_usage_error_not_a_verdict(capsys):
    assert doctor.main(["--netwrk"]) == 2
    err = capsys.readouterr().err
    assert "[doctor] unknown arguments: --netwrk" in err
    assert "usage: python scripts/doctor.py" in err


def test_the_network_flag_is_forwarded_and_off_by_default(monkeypatch):
    seen: list[bool] = []

    def fake_collect(network: bool = False) -> list:
        seen.append(network)
        return []

    monkeypatch.setattr(doctor, "collect_checks", fake_collect)
    assert doctor.main([]) == 0
    assert doctor.main(["--network"]) == 0
    assert seen == [False, True]


# ---------------------------------------------------------------------------
# 2. Exit codes and the report: warnings pass, failures go red, crashes
#    become FAIL lines.
# ---------------------------------------------------------------------------

def test_warnings_do_not_fail_the_run_and_the_summary_is_greppable(monkeypatch,
                                                                 capsys):
    monkeypatch.setattr(doctor, "collect_checks", lambda network=False: [
        doctor.Result(doctor.PASS, "a", "fine"),
        doctor.Result(doctor.WARN, "b", "meh", "fix it somehow"),
    ])
    assert doctor.main([]) == 0
    out = capsys.readouterr().out
    assert "PharmaSignal doctor" in out
    assert out.count("→ fix it somehow") == 1       # hints print once, indented
    assert "checks: passed=1 warnings=1 failed=0" in out


def test_one_failing_check_turns_the_exit_code_red(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "collect_checks", lambda network=False: [
        doctor.Result(doctor.PASS, "a", "fine"),
        doctor.Result(doctor.FAIL, "b", "broken", "run the fixer"),
    ])
    assert doctor.main([]) == 1
    out = capsys.readouterr().out
    assert "FAIL  b" in out
    assert "checks: passed=1 warnings=0 failed=1" in out


def test_a_crashing_check_is_reported_as_a_failure_not_a_traceback(capsys):
    def boom() -> doctor.Result:
        raise RuntimeError("probe exploded")

    result = doctor._guard("boom", boom)
    assert result.status == doctor.FAIL
    assert "RuntimeError" in result.detail and "probe exploded" in result.detail
    assert doctor.report([result]) == 1
    assert "failed=1" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 3a. Interpreter + pinned dependencies.
# ---------------------------------------------------------------------------

def test_the_interpreter_check_reports_the_running_interpreter():
    result = doctor.check_interpreter()
    assert result.status in (doctor.PASS, doctor.WARN)
    assert sys.executable in result.detail
    assert "all pinned modules present" in result.detail


def test_the_interpreter_check_warns_on_pre_310_python(monkeypatch):
    """< 3.10 has no sys.orig_argv: replay works, but flags are not preserved."""
    monkeypatch.setattr(sys, "version_info", (3, 9, 16), raising=False)
    result = doctor.check_interpreter()
    assert result.status == doctor.WARN
    assert "sys.orig_argv" in result.detail


def test_the_interpreter_check_fails_when_the_bootstrap_is_not_satisfied(
        monkeypatch):
    plan = doctor.bootstrap.Plan(doctor.bootstrap.STATE_NO_VENV,
                                 ("psycopg2",), "", None)
    monkeypatch.setattr(doctor.bootstrap, "fallback_plan", lambda *a, **k: plan)
    result = doctor.check_interpreter()
    assert result.status == doctor.FAIL
    assert "no-venv" in result.detail


def test_pinned_requirements_parses_the_real_requirements_file():
    pins = dict(doctor.pinned_requirements())
    # The names whose importable module differs from the distribution name
    # are exactly the ones a naive parser gets wrong.
    assert pins["psycopg2-binary"] == "2.9.10"
    assert pins["python-dotenv"] == "1.0.1"
    assert "pandas" in pins


def test_pinned_deps_passes_when_every_pin_resolves(monkeypatch):
    # json: stdlib-importable, no distribution metadata — no drift possible.
    monkeypatch.setattr(doctor, "pinned_requirements", lambda: [("json", "9.9")])
    result = doctor.check_pinned_deps()
    assert result.status == doctor.PASS
    assert "1/1" in result.detail


def test_pinned_deps_fails_on_a_pin_that_is_not_importable(monkeypatch):
    monkeypatch.setattr(doctor, "pinned_requirements",
                        lambda: [("json", "9.9"), ("no-such-dist", "0.1")])
    result = doctor.check_pinned_deps()
    assert result.status == doctor.FAIL
    assert "no-such-dist" in result.detail
    assert "pip install -r requirements.txt" in result.hint


def test_pinned_deps_warns_on_version_drift_without_failing(monkeypatch):
    """Importable but not the pinned version: the runtime works, yet the
    environment no longer describes itself truthfully — a warning."""
    monkeypatch.setattr(doctor, "pinned_requirements", lambda: [("pytest", "0.0.1")])
    result = doctor.check_pinned_deps()
    assert result.status == doctor.WARN
    assert "pytest" in result.detail and "0.0.1" in result.detail


# ---------------------------------------------------------------------------
# 3b. Configuration: key names in, values never out.
# ---------------------------------------------------------------------------

def test_missing_env_file_warns_with_the_copy_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "ENV_FILE", tmp_path / ".env")   # absent
    result = doctor.check_env_file()
    assert result.status == doctor.WARN
    assert "cp .env.example .env" in result.hint


def test_env_file_checks_only_template_required_keys_by_name(tmp_path,
                                                           monkeypatch):
    monkeypatch.setattr(doctor, "ENV_FILE", tmp_path / ".env")
    (tmp_path / ".env").write_text("WHATEVER=1\n")
    monkeypatch.setattr(doctor, "ENV_EXAMPLE", tmp_path / ".env.example")
    (tmp_path / ".env.example").write_text(
        "REQUIRED_THING=template-value\nALSO_REQUIRED=another\nEMPTY_ONE=\n"
    )
    monkeypatch.delenv("REQUIRED_THING", raising=False)
    monkeypatch.setenv("ALSO_REQUIRED", "from-the-shell")
    result = doctor.check_env_file()
    assert result.status == doctor.WARN
    assert "REQUIRED_THING" in result.detail          # named
    assert "ALSO_REQUIRED" not in result.detail        # set via shell counts
    assert "EMPTY_ONE" not in result.detail            # template-empty = optional
    assert "template-value" not in result.detail       # values never shown


def test_llm_key_warnings_name_the_key_not_the_value(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-do-not-print-me")
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    result = doctor.check_llm_keys()
    assert result.status == doctor.WARN
    assert "EMBEDDING_API_KEY" in result.detail
    assert "LLM_API_KEY, EMBEDDING_API_KEY" not in result.detail
    assert "sk-do-not-print-me" not in result.detail + result.hint


def test_llm_keys_pass_when_both_are_set(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-one")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-two")
    result = doctor.check_llm_keys()
    assert result.status == doctor.PASS
    assert "sk-one" not in result.detail and "sk-two" not in result.detail


def test_admin_key_reports_both_states_as_pass(monkeypatch):
    """Empty is a documented, deliberate state — the doctor must not red it."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    disabled = doctor.check_admin_key()
    assert disabled.status == doctor.PASS
    assert "disabled" in disabled.detail

    monkeypatch.setenv("ADMIN_API_KEY", "hunter2-local-admin")
    enabled = doctor.check_admin_key()
    assert enabled.status == doctor.PASS
    assert "enabled" in enabled.detail
    assert "hunter2-local-admin" not in enabled.detail


# ---------------------------------------------------------------------------
# 3c. Databases: unreachable vs missing vs unmigrated vs pending, and the
#     test database's auto-provisioning downgrade from FAIL to WARN.
# ---------------------------------------------------------------------------

class _Cursor:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self._sql = ""

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, sql: str) -> None:
        self._sql = sql

    def fetchone(self):
        if "to_regclass" in self._sql:
            return (None,) if self._conn.applied is None else ("schema_migrations",)
        return None

    def fetchall(self):
        return [(name,) for name in self._conn.applied]


class _Conn:
    """Scripted connection: applied=None means no schema_migrations table."""

    def __init__(self, applied) -> None:
        self.applied = applied
        self.closed = False

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def close(self) -> None:
        self.closed = True


def _dead_connection(message: str):
    def _connect(url: str):
        raise psycopg2.OperationalError(message)
    return _connect


def test_dev_database_passes_when_every_migration_is_applied(monkeypatch):
    applied = doctor._migration_files()
    assert applied, "the repository must ship migrations for this to mean anything"
    conn = _Conn(applied)
    monkeypatch.setattr(doctor, "_connect", lambda url: conn)
    result = doctor.check_database("dev-db", "postgresql://localhost/db",
                                   auto_provision=False)
    assert result.status == doctor.PASS
    assert f"{len(applied)}/{len(applied)} migrations applied" in result.detail
    assert conn.closed, "the connection must be closed even on success"


def test_dev_database_fails_when_the_server_is_unreachable(monkeypatch):
    monkeypatch.setattr(doctor, "_connect",
                        _dead_connection('connection to server at "db" failed'))
    result = doctor.check_database("dev-db", "postgresql://localhost/db",
                                   auto_provision=False)
    assert result.status == doctor.FAIL
    assert "cannot reach" in result.detail
    assert "start PostgreSQL" in result.hint


def test_a_missing_database_names_the_command_that_creates_it(monkeypatch):
    monkeypatch.setattr(doctor, "_connect",
                        _dead_connection('FATAL: database "pharmasignal" does not exist'))
    result = doctor.check_database("dev-db",
                                   "postgresql://localhost/pharmasignal",
                                   auto_provision=False)
    assert result.status == doctor.FAIL
    assert "create_databases" in result.hint


def test_an_unmigrated_database_names_the_migrate_command(monkeypatch):
    monkeypatch.setattr(doctor, "_connect", lambda url: _Conn(None))
    result = doctor.check_database("dev-db", "postgresql://localhost/db",
                                   auto_provision=False)
    assert result.status == doctor.FAIL
    assert "schema_migrations" in result.detail
    assert "python scripts/migrate.py" in result.hint


def test_pending_migrations_are_listed_by_filename(monkeypatch):
    files = doctor._migration_files()
    monkeypatch.setattr(doctor, "_connect", lambda url: _Conn(files[:-1]))
    result = doctor.check_database("dev-db", "postgresql://localhost/db",
                                   auto_provision=False)
    assert result.status == doctor.FAIL
    assert files[-1] in result.detail
    assert f"1 of {len(files)}" in result.detail


def test_the_test_database_downgrades_missing_state_to_warnings(monkeypatch):
    """tests/conftest.py creates and migrates the test database itself."""
    monkeypatch.setattr(doctor, "_connect", lambda url: _Conn(None))
    result = doctor.check_database("test-db", "postgresql://localhost/db",
                                   auto_provision=True)
    assert result.status == doctor.WARN
    assert "automatically" in result.detail

    monkeypatch.setattr(doctor, "_connect",
                        _dead_connection("connection refused"))
    result = doctor.check_database("test-db", "postgresql://localhost/db",
                                   auto_provision=True)
    assert result.status == doctor.FAIL     # a dead server is never a warning
    assert "start PostgreSQL" in result.hint


# ---------------------------------------------------------------------------
# 3d. Data directories: fixtures are required, downloads are opt-in.
# ---------------------------------------------------------------------------

def test_the_fixture_check_passes_on_the_committed_tree():
    result = doctor.check_fixtures()
    assert result.status == doctor.PASS
    assert "fixture_2024q1" in result.detail


def test_the_fixture_check_fails_without_creating_anything(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "SAMPLES", tmp_path / "data")
    result = doctor.check_fixtures()
    assert result.status == doctor.FAIL
    assert "fixture_2024q1" in result.detail and "fixture_2024q2" in result.detail
    assert list(tmp_path.iterdir()) == []          # read-only: nothing created


def test_missing_raw_faers_data_is_a_warning_with_the_download_hint(tmp_path,
                                                                  monkeypatch):
    monkeypatch.setattr(doctor.settings, "faers_data_path",
                        lambda: tmp_path / "raw")
    result = doctor.check_faers_raw()
    assert result.status == doctor.WARN
    assert "download_faers" in result.hint
    assert not (tmp_path / "raw").exists()         # no directory invented


def test_raw_faers_data_passes_once_quarters_are_downloaded(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    (raw / "2024q1").mkdir(parents=True)
    monkeypatch.setattr(doctor.settings, "faers_data_path", lambda: raw)
    result = doctor.check_faers_raw()
    assert result.status == doctor.PASS
    assert "1 entry" in result.detail


def test_missing_processed_path_warns_without_creating_it(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.settings, "processed_data_path",
                        lambda: tmp_path / "processed")
    result = doctor.check_processed_path()
    assert result.status == doctor.WARN
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# 3e. Network (opt-in only): HTTP verdicts through the injectable seam.
# ---------------------------------------------------------------------------

def test_dailymed_check_uses_the_pinned_client_base(monkeypatch):
    seen: list[str] = []

    def fake_status(url: str, timeout: int) -> int:
        seen.append(url)
        return 200

    monkeypatch.setattr(doctor, "_http_status", fake_status)
    result = doctor.check_dailymed()
    assert result.status == doctor.PASS
    assert "HTTP 200" in result.detail
    assert "dailymed" in seen[0]


def test_dailymed_check_fails_on_http_errors_and_outages(monkeypatch):
    monkeypatch.setattr(doctor, "_http_status", lambda url, timeout: 404)
    assert doctor.check_dailymed().status == doctor.FAIL

    def offline(url: str, timeout: int):
        raise ConnectionError("no route to host")

    monkeypatch.setattr(doctor, "_http_status", offline)
    result = doctor.check_dailymed()
    assert result.status == doctor.FAIL
    assert "ConnectionError" in result.detail


# ---------------------------------------------------------------------------
# 4. Assembly and secret safety.
# ---------------------------------------------------------------------------

def test_collect_checks_covers_the_whole_setup_and_is_offline_by_default():
    names = [r.name for r in doctor.collect_checks(network=False)]
    assert names == ["interpreter", "pinned-deps", "env-file", "llm-keys",
                     "admin-key", "dev-db", "test-db", "fixtures",
                     "faers-raw", "processed"]
    assert "dailymed" not in names
    assert "dailymed" in [r.name for r in doctor.collect_checks(network=True)]
    assert all(r.status in (doctor.PASS, doctor.WARN, doctor.FAIL)
               for r in doctor.collect_checks())


def test_the_report_never_prints_configured_secret_values(monkeypatch, capsys):
    """URLs keep host/db but lose credentials; exception text is redacted."""
    monkeypatch.setenv("LLM_API_KEY", "sk-do-not-print-me")
    secret_url = "postgresql://appuser:SECRETPW@db.example.com:5432/pharmasignal"
    monkeypatch.setattr(doctor.settings, "database_url", lambda: secret_url)
    # Worst case: an error message that embeds the whole DSN verbatim.
    monkeypatch.setattr(
        doctor, "_connect",
        _dead_connection(f'connection to server at "db.example.com" failed: '
                         f'password authentication failed ({secret_url})')
    )
    results = [doctor.check_llm_keys(),
               doctor.check_database("dev-db", doctor.settings.database_url(),
                                     auto_provision=False)]
    capsys.readouterr()
    doctor.report(results)
    out = capsys.readouterr().out
    assert "SECRETPW" not in out
    assert "appuser" not in out
    assert "sk-do-not-print-me" not in out
    assert "db.example.com" in out            # host+db stay visible on purpose
    assert "/pharmasignal" in out


# ---------------------------------------------------------------------------
# 5. End to end: `python scripts/doctor.py` produces a report, never a
#    traceback. Exit 0 or 1 both count — the machine's real state decides.
# ---------------------------------------------------------------------------

def test_doctor_runs_end_to_end_and_reports_every_check():
    import subprocess

    proc = subprocess.run(
        [sys.executable, "scripts/doctor.py"],
        capture_output=True, text=True, cwd=PROJECT_ROOT,
    )
    assert proc.returncode in (0, 1), proc.stderr
    assert proc.stderr == "", f"doctor must not write to stderr: {proc.stderr!r}"
    assert "PharmaSignal doctor" in proc.stdout
    assert "checks: passed=" in proc.stdout

    # Whatever the machine has configured must not leak into the report.
    for key in ("LLM_API_KEY", "EMBEDDING_API_KEY", "ADMIN_API_KEY"):
        value = os.environ.get(key, "").strip()
        if value:
            assert value not in proc.stdout, f"{key} leaked into doctor output"
    password = urlparse(os.environ.get("DATABASE_URL", "")).password
    if password:
        assert password not in proc.stdout, "database password leaked"
