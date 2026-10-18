"""Regression guards for the scheduled figures-drift workflow.

.github/workflows/figures-drift.yml is the only CI job that holds a
production credential and the only one that opens a production database.
These tests pin the contract that makes that safe, so a well-meaning edit
cannot silently weaken it:

1. it runs on a schedule (plus manual dispatch) and NEVER on pull_request or
   push — a production DSN must not be reachable from fork PRs;
2. the guarded command is `report_figures.py --check` against an explicitly
   passed DSN — drift exits 1 and turns the job red; an unreachable database
   exits 2, which must also be red (an unverifiable guard is not a pass);
3. every connection in that step is forced read-only server-side via
   PGOPTIONS default_transaction_read_only=on, so no future script regression
   can write to production from CI even if the DSN points at a writable host;
4. the DSN comes only from the repository secret — no literal DSN in the
   file, and a missing secret fails loudly instead of skipping;
5. a second gate runs `--diff` against the committed baseline snapshot, so
   unexecuted-migration movement turns the job red even when the report was
   regenerated to match — same read-only/secret/exit-code contract, run even
   after a failing check but never without the secret;
6. migration-004.yml keeps its documented promise of holding no production
   credentials.
"""
from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "figures-drift.yml"
REHEARSAL_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "migration-004.yml"

SECRET_REF = "${{ secrets.PRODUCTION_REPLICA_DATABASE_URL }}"


def _text() -> str:
    assert WORKFLOW.is_file(), "figures-drift workflow is missing"
    return WORKFLOW.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Triggers: scheduled + manual only. Production credentials must never be
#    reachable from pull_request (fork PRs) or push builds.
# ---------------------------------------------------------------------------

def test_the_guard_runs_on_a_schedule():
    text = _text()
    # Comment lines between the key and the entry are allowed; the cron entry
    # must still be the one attached to `schedule:`.
    assert re.search(
        r"schedule:\s*(?:\s*#[^\n]*\n\s*)*-\s+cron:", text
    ), "no schedule trigger"
    cron = re.search(r'-\s+cron:\s*"([^"]+)"', text)
    assert cron, "cron expression not found"
    assert len(cron.group(1).split()) == 5, f"not a 5-field cron: {cron.group(1)!r}"


def test_manual_dispatch_is_available():
    assert "workflow_dispatch" in _text()


def test_never_triggered_by_pull_request_or_push():
    """A production DSN must not be reachable from PR/push builds."""
    text = _text()
    assert "pull_request:" not in text
    assert "push:" not in text


# ---------------------------------------------------------------------------
# 2. The guarded command: --check with an explicitly passed DSN.
# ---------------------------------------------------------------------------

def test_the_guard_command_is_report_figures_check():
    text = _text()
    assert "scripts/report_figures.py --check" in text
    assert '--database-url "$PRODUCTION_REPLICA_DATABASE_URL"' in text, \
        "the DSN must be passed explicitly — never fall back to the default"


def test_the_check_step_cannot_swallow_failure():
    text = _text()
    assert "continue-on-error" not in text


def test_the_job_is_bounded_by_a_timeout():
    assert "timeout-minutes:" in _text()


# ---------------------------------------------------------------------------
# 3. Read-only enforcement, server-side, independent of the DSN's role.
# ---------------------------------------------------------------------------

def test_every_connection_in_the_check_step_is_forced_read_only():
    text = _text()
    assert re.search(
        r'PGOPTIONS:\s*"-c default_transaction_read_only=on"', text
    ), "check step must force read-only transactions via PGOPTIONS"


# ---------------------------------------------------------------------------
# 4. The DSN is secret-sourced and nothing is hardcoded.
# ---------------------------------------------------------------------------

def test_the_dsn_comes_from_the_secret():
    assert SECRET_REF in _text()


def test_no_literal_dsn_anywhere_in_the_workflow():
    """No connection string may be committed — not even an example one."""
    assert "postgresql://" not in _text()


def test_a_missing_secret_fails_loudly_instead_of_skipping():
    text = _text()
    assert 'if [ -z "$PRODUCTION_REPLICA_DATABASE_URL" ]' in text, \
        "missing-secret guard must test the variable"
    assert "exit 1" in text
    assert "::error::" in text, "failures must surface as GitHub annotations"


def test_the_job_runs_with_least_privilege():
    text = _text()
    assert "permissions:" in text
    assert "contents: read" in text


# ---------------------------------------------------------------------------
# 5. Second gate: --diff against the committed baseline snapshot.
# ---------------------------------------------------------------------------

SNAPSHOT_PATH = (
    "docs/migrations/004_focus_drug_brand_aliases/figures_baseline_snapshot.json"
)


def test_the_second_gate_is_the_diff_command():
    text = _text()
    assert "scripts/report_figures.py --diff" in text
    assert f"--snapshot-path {SNAPSHOT_PATH}" in text, \
        "the diff must name the committed baseline snapshot explicitly"
    assert (PROJECT_ROOT / SNAPSHOT_PATH).is_file(), \
        "the committed baseline snapshot the gate diffs against must exist"


def test_the_diff_gate_shares_the_read_only_and_secret_contract():
    """Both gated steps force read-only server-side and use the secret DSN."""
    text = _text()
    assert text.count(SECRET_REF) >= 2, "both steps must pass the secret DSN"
    assert text.count('PGOPTIONS: "-c default_transaction_read_only=on"') >= 2, \
        "both steps must force read-only transactions via PGOPTIONS"
    # CI only ever diffs: --snapshot (which writes the baseline) must appear
    # only as --snapshot-path, never as its own invocation.
    assert not re.search(r"--snapshot(?!-path)", text), \
        "CI must never re-take the baseline snapshot"


def test_the_diff_gate_runs_even_after_a_failing_check_but_never_without_the_secret():
    """One red run should show which gate fired — but an empty DSN must not
    let the script fall back to its default connection."""
    text = _text()
    assert ("if: ${{ !cancelled() && steps.replica-secret.outcome == 'success' }}"
            in text), "diff gate must run after a failed check, skip a missing secret"
    assert "id: replica-secret" in text


def test_the_diff_gate_cannot_swallow_failure():
    text = _text()
    diff_block = text.split("name: Figures match the committed baseline snapshot")[1]
    assert "continue-on-error" not in diff_block


# ---------------------------------------------------------------------------
# 6. Separation of duties: the rehearsal workflow stays credential-free.
# ---------------------------------------------------------------------------

def test_the_rehearsal_workflow_holds_no_production_credentials():
    other = REHEARSAL_WORKFLOW.read_text(encoding="utf-8")
    assert "PRODUCTION_REPLICA" not in other, \
        "migration-004.yml documents that CI holds no production credentials"
