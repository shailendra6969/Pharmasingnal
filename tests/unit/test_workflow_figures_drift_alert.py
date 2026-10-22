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
   once, refreshed with a weekly digest comment (how long the drift has
   persisted) plus the newest run link, closed when the guard is green with
   a comment stating how long the drift lasted;
   a repository with no completed runs must produce no issue at all;
6. escalation: once the open issue has survived four consecutive weekly
   failures (28 days open — the issue's own age, since any green week closes
   it), it is labeled `escalated` and retitled — once, on that same issue,
   with no second issue and no new permission;
7. queue triage: every failing issue carries a severity label (made on
   demand) and an assignee, asserted at creation and re-asserted on each
   refresh (never on green) — so it lands in a person's queue, not only in
   the tracker;
8. weekly digest: each failing refresh states how long the drift has
   persisted — measured from the open issue's own createdAt, the same age
   handed to escalation, so the two comments can never disagree — and only
   when a failing issue already exists (never at creation, never on green);
9. green close: the closing comment states how long the drift lasted —
   measured the same way as the digest (the issue's own age), pluralized,
   and only ever on green, only when an open issue exists to close.
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


def test_gh_knows_the_repo_without_a_checkout():
    """The job deliberately has no checkout step, so gh cannot discover the
    repository from a git remote — GH_REPO must name it explicitly.

    Regression: the first real run (2026-10-18) died with
    "failed to run git: fatal: not a git repository" before touching any
    issue, because gh fell back to git-remote discovery with nothing to find.
    """
    text = _text()
    assert "GH_REPO: ${{ github.repository }}" in text
    assert "actions/checkout" not in text, \
        "the job's design is no checkout; GH_REPO is the substitute"


# ---------------------------------------------------------------------------
# 6. Escalation: four consecutive weekly failures -> label + retitle, once.
# ---------------------------------------------------------------------------

def _escalation_body() -> str:
    """The escalate() function: its definition through its closing brace."""
    text = _text()
    assert "escalate() {" in text, "the alert has no escalation step"
    return text.split("escalate() {", 1)[1].split("\n          }", 1)[0]


def test_the_escalation_threshold_is_four_weekly_checks():
    """28 days: the issue is created on the first failing check and any
    green week closes it, so the open issue's age IS the run of consecutive
    weekly failures — 28 days is the fourth."""
    text = _text()
    assert 'ESCALATE_AFTER_DAYS: "28"' in text
    assert "--json createdAt" in text, \
        "the streak must be measured from the open issue's own age, not " \
        "re-derived from run history"
    assert "ESCALATE_AFTER_DAYS" in _escalation_body(), \
        "the threshold must actually gate the escalation"


def test_escalation_marks_the_same_issue_and_never_creates_a_second():
    body = _escalation_body()
    assert "gh label create" in body
    assert "--force" in body, "label creation must be idempotent"
    assert "--add-label" in body
    assert "gh issue create" not in body, \
        "escalation must reuse the one open issue, not open a second"


def test_escalation_retitles_without_losing_the_dedupe_marker():
    body = _escalation_body()
    assert '--title "[ESCALATED] ${TITLE}"' in body
    # the marker survives the prefix, so dedupe still finds this issue
    assert 'TITLE="[${ISSUE_MARKER}]' in _text()


def test_escalation_is_gated_by_the_label_so_it_happens_once():
    """The label is both the signal and the guard: checked before it is
    applied, so week five refreshes normally instead of re-escalating."""
    body = _escalation_body()
    guard = body.index('index("escalated")')
    applied = body.index("gh label create")
    assert guard < applied, "the already-escalated check must precede the mutation"
    assert "4+ consecutive weekly failures" in body, \
        "the escalation comment must name the streak"


def test_escalation_runs_only_on_a_failing_conclusion():
    text = _text()
    failure = text.split("failure|timed_out|cancelled)", 1)[1].split(";;", 1)[0]
    assert 'escalate "$open_issue"' in failure, \
        "escalation belongs to the failing branch of the case statement"
    success = text.split("success)", 1)[1].split(";;", 1)[0]
    assert "escalate" not in success, "a green week must never escalate"


def test_escalation_gained_no_new_permission():
    """Labels ride the issues API surface: the job still declares exactly
    the two scopes it was built with, and no labels scope."""
    text = _text()
    block = text.split("permissions:", 1)[1].split("\n\n", 1)[0]
    assert re.findall(r"^  ([a-z-]+):", block, re.M) == ["actions", "issues"]
    assert "labels: write" not in text


# ---------------------------------------------------------------------------
# 7. Queue triage: severity label + assignee on every failing issue.
# ---------------------------------------------------------------------------

def _triage_body() -> str:
    """The triage() function: its definition through its closing brace."""
    text = _text()
    assert "triage() {" in text, "the alert has no triage step"
    return text.split("triage() {", 1)[1].split("\n          }", 1)[0]


def test_the_assignee_and_severity_are_configuration():
    """Both come from env, so retargeting the queue is a two-line edit and
    neither is buried in a command line."""
    text = _text()
    assert "ALERT_ASSIGNEE: shailendra6969" in text, \
        "the repository's sole assignable user must be named"
    assert 'SEVERITY_LABEL: "severity: high"' in text


def test_triage_makes_the_severity_label_on_demand():
    body = _triage_body()
    assert 'gh label create "$SEVERITY_LABEL" --force' in body, \
        "the label may not exist yet; creation must be idempotent"


def test_triage_asserts_label_and_assignee_in_one_mutation():
    """One `gh issue edit` carries both, so an issue can never end up
    labeled-but-unassigned (or the reverse) from a partial failure."""
    body = _triage_body()
    assert body.count("gh issue edit") == 1
    assert '--add-label "$SEVERITY_LABEL"' in body
    assert '--add-assignee "$ALERT_ASSIGNEE"' in body
    assert body.index('--add-label "$SEVERITY_LABEL"') < \
        body.index('--add-assignee "$ALERT_ASSIGNEE"')


def test_triage_runs_on_create_and_on_every_refresh_but_never_on_green():
    text = _text()
    failure = text.split("failure|timed_out|cancelled)", 1)[1].split(";;", 1)[0]
    assert 'triage "$open_issue"' in failure, \
        "the weekly refresh must re-assert the queue"
    assert 'triage "${new_issue##*/}"' in failure, \
        "a freshly created issue must be triaged from its own number"
    success = text.split("success)", 1)[1].split(";;", 1)[0]
    assert "triage" not in success, "a green week must never touch triage"


# ---------------------------------------------------------------------------
# 8. Weekly digest: each failing refresh says how long the drift persisted.
# ---------------------------------------------------------------------------

def _failure_branch() -> str:
    """The failure|timed_out|cancelled case body, through its `;;`."""
    text = _text()
    return text.split("failure|timed_out|cancelled)", 1)[1].split(";;", 1)[0]


def test_the_weekly_refresh_comment_carries_a_persistence_digest():
    branch = _failure_branch()
    assert "Weekly digest" in branch, "the weekly comment must summarize the drift"
    assert "persisted ${days} (${span})" in branch, "duration must be explicit"


def test_the_digest_pluralizes_its_durations():
    """Manual dispatches can hit any age, including 0 and 1 days — the
    comment must read grammatically at every one of them."""
    branch = _failure_branch()
    assert 'days="${age_days} days"' in branch
    assert 'days="1 day"' in branch
    assert 'span="1 week"' in branch


def test_the_digest_duration_comes_from_the_open_issue_itself():
    """Not re-derived from run history: the issue was created on the first
    failing check and any green week closes it, so its own age IS the run of
    consecutive red weekly checks."""
    branch = _failure_branch()
    assert 'gh issue view "$open_issue" --json createdAt' in branch
    assert 'date -u -d "$created"' in branch


def test_the_digest_runs_only_when_a_failing_issue_already_exists():
    """Creation states the facts in the issue body; only the weekly refresh
    digests a duration — and a green week closes, it does not digest."""
    branch = _failure_branch()
    assert "Weekly digest" in branch
    assert branch.index('if [ -n "$open_issue" ]; then') \
        < branch.index("Weekly digest") \
        < branch.index("gh issue create"), \
        "digest belongs to the refresh path, not the creation path"
    success = _text().split("success)", 1)[1].split(";;", 1)[0]
    assert "Weekly digest" not in success, "a green week must never digest"


def test_the_escalation_reuses_the_digests_age_instead_of_refetching():
    """One measurement per run: the digest comment and the escalation
    comment can never report different durations for the same issue."""
    branch = _failure_branch()
    assert 'escalate "$open_issue" "$created" "$age_days"' in branch
    assert "createdAt" not in _escalation_body(), \
        "escalate() must use the age it is given, not re-read the issue"


# ---------------------------------------------------------------------------
# 9. Green close: the comment says how long the drift lasted before clearing.
# ---------------------------------------------------------------------------

def _success_branch() -> str:
    """The success case body, through its `;;`."""
    text = _text()
    return text.split("success)", 1)[1].split(";;", 1)[0]


def test_the_green_close_comment_states_how_long_the_drift_lasted():
    branch = _success_branch()
    assert "The drift lasted ${days} (${span})" in branch, \
        "closing must record the duration the tracker just lived through"


def test_the_close_duration_measures_the_issue_the_same_way_as_the_digest():
    """Same source (the issue's own createdAt), same arithmetic, same
    pluralization — a close and a digest for one issue can't disagree."""
    branch = _success_branch()
    assert 'gh issue view "$open_issue" --json createdAt' in branch
    assert 'date -u -d "$created"' in branch
    assert 'days="${age_days} days"' in branch
    assert 'days="1 day"' in branch
    assert 'span="1 week"' in branch


def test_the_close_runs_only_on_green_and_only_for_an_open_issue():
    branch = _success_branch()
    assert 'if [ -n "$open_issue" ]; then' in branch, \
        "no open issue, nothing to close and nothing to say"
    assert "gh issue create" not in branch, "a green week never creates an issue"
    assert "gh label" not in branch and "gh issue edit" not in branch, \
        "closing is a comment, never a mutation of labels or title"
