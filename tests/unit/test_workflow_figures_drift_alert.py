"""Regression guards for the weekly figures-drift alert workflow.

.github/workflows/figures-drift-alert.yml is the only workflow in this
repository that can *write* to the issue tracker, so its contract is pinned
here the same way the drift guard's is pinned in
test_workflow_figures_drift.py:

1. weekly schedule + manual dispatch, never pull_request/push — an
   issues:write token must not be reachable from fork PRs;
2. permissions are exactly what the job needs (actions:read to inspect the
   guard's runs, issues:write to maintain the alert) and nothing more;
3. the token is the built-in GITHUB_TOKEN — never a PAT or repository
   secret;
4. it OBSERVES the guard: it reads figures-drift.yml's completed-run history
   and never runs the check itself (no database access from this job);
5. issue lifecycle: exactly one open issue per ongoing failure — created
   once, refreshed with the newest run link, closed when the guard is green;
   a repository with no completed runs must produce no issue at all.
"""
from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "figures-drift-alert.yml"
DRIFT_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "figures-drift.yml"


def _text() -> str:
    assert WORKFLOW.is_file(), "figures-drift-alert workflow is missing"
    return WORKFLOW.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Triggers: weekly + manual only.
# ---------------------------------------------------------------------------

def test_the_alert_runs_weekly():
    text = _text()
    cron = re.search(r'-\s+cron:\s*"([^"]+)"', text)
    assert cron, "no cron trigger"
    fields = cron.group(1).split()
    assert len(fields) == 5, f"not a 5-field cron: {cron.group(1)!r}"
    assert fields[4] != "*", f"cron must pin a weekday for weekly cadence: {fields}"


def test_manual_dispatch_is_available():
    assert "workflow_dispatch" in _text()


def test_never_triggered_by_pull_request_or_push():
    """An issues:write token must not be reachable from fork PRs."""
    text = _text()
    assert "pull_request:" not in text
    assert "push:" not in text


# ---------------------------------------------------------------------------
# 2. Least-privilege permissions.
# ---------------------------------------------------------------------------

def test_permissions_are_scoped_to_actions_read_and_issues_write():
    text = _text()
    assert "actions: read" in text, "job needs actions:read to list guard runs"
    assert "issues: write" in text, "job needs issues:write to maintain the issue"
    assert "contents: write" not in text
    assert "permissions: write-all" not in text


def test_the_job_is_bounded_by_a_timeout():
    assert "timeout-minutes:" in _text()


# ---------------------------------------------------------------------------
# 3. Built-in token only.
# ---------------------------------------------------------------------------

def test_uses_only_the_builtin_github_token():
    text = _text()
    assert "GH_TOKEN: ${{ github.token }}" in text
    assert "secrets." not in text, "alert needs no repository secrets"
    assert "ghp_" not in text, "no personal access token may appear"


# ---------------------------------------------------------------------------
# 4. It observes the drift guard; it never runs the check itself.
# ---------------------------------------------------------------------------

def test_it_reads_the_drift_workflows_completed_runs():
    text = _text()
    assert "DRIFT_WORKFLOW: figures-drift.yml" in text
    assert "runs?status=completed&per_page=1" in text, \
        "must read the latest COMPLETED run — in-progress runs must not alert"
    assert DRIFT_WORKFLOW.is_file(), "the guarded workflow must exist"


def test_it_never_runs_the_drift_check_itself():
    """No database access from the alert job: it only inspects run history."""
    assert "--check" not in _text()


# ---------------------------------------------------------------------------
# 5. Issue lifecycle: one open issue per ongoing failure, closed on green.
# ---------------------------------------------------------------------------

def test_issue_dedupe_uses_the_marker_in_the_title():
    text = _text()
    assert "ISSUE_MARKER: figures-drift" in text
    assert re.search(r"gh issue list .*--search .*in:title", text, re.S), \
        "must look up an existing open issue before creating one"
    assert "[${ISSUE_MARKER}]" in text, "created title must carry the marker"


def test_all_three_issue_mutations_are_present():
    """create (first failure), comment (still failing), close (green)."""
    text = _text()
    assert "gh issue create" in text
    assert "gh issue comment" in text
    assert "gh issue close" in text


def test_failure_conclusions_are_filtered_explicitly():
    text = _text()
    assert "failure|timed_out|cancelled" in text, \
        "only completed-with-a-problem runs may open an issue"
    assert "not alerting" in text, "unhandled conclusions must be a no-op"


def test_no_completed_runs_means_no_issue():
    text = _text()
    assert "No completed" in text
    # the empty-history branch must exit before any issue mutation
    empty_branch = text.split("No completed", 1)[1][:200]
    assert "exit 0" in empty_branch


def test_the_step_shell_is_strict():
    assert "set -euo pipefail" in _text()
