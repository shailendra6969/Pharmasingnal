"""Regression guards for the evaluation CI workflow (.github/workflows/evaluation.yml).

That workflow is the only thing standing between a broken evidence gate and a
green build: it seeds a deterministic dataset and runs
``scripts/run_evaluation.py``, whose exit code decides the job. These tests
pin the contract that makes it a real gate:

1. it is credential-free — figures-drift.yml remains the only CI job allowed
   to hold a production credential;
2. it seeds the dataset (scripts/seed_evaluation.py) before evaluating, so
   cases are never scored against an empty database;
3. it runs the evaluation CLI and lets that command's own exit code decide —
   no ``continue-on-error``, no ``|| true``, nothing that can turn a failing
   case green;
4. a misconfigured runner and a dead PostgreSQL service both fail before the
   evaluation runs, loudly, instead of silently skipping;
5. it triggers on push/pull_request (safe precisely because it holds no
   credential) and can always be dispatched manually.
"""
from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "evaluation.yml"

EVAL_STEP = "python scripts/run_evaluation.py --json eval-report.json"
SEED_STEP = "python scripts/seed_evaluation.py"
BOOTSTRAP = "python -m app.bootstrap --check"


def _text() -> str:
    assert WORKFLOW.is_file(), "evaluation workflow is missing"
    return WORKFLOW.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Separation of duties: the evaluation job holds no credentials at all.
# ---------------------------------------------------------------------------

def test_the_evaluation_workflow_holds_no_credentials():
    text = _text()
    assert "secrets." not in text, "the evaluation DB is ephemeral — no secrets"
    assert "PRODUCTION_REPLICA" not in text, \
        "only figures-drift.yml may hold a production credential"


def test_the_database_is_this_runs_own_ephemeral_service():
    """The DSN must name the job's own service, never a shared/host database."""
    text = _text()
    assert "DATABASE_URL: postgresql://postgres:postgres@localhost:5432/pharmasignal_eval" in text
    assert "POSTGRES_DB: pharmasignal_eval" in text, \
        "the service must create the database the reachability check opens"


# ---------------------------------------------------------------------------
# 2. Deterministic setup: seed before evaluate, migrate before seed.
# ---------------------------------------------------------------------------

def test_the_dataset_is_seeded_before_the_evaluation_runs():
    text = _text()
    assert SEED_STEP in text, "evaluation must run against the seeded dataset"
    assert text.index(SEED_STEP) < text.index(EVAL_STEP), \
        "seed must precede the evaluation run"


def test_the_database_is_migrated_before_it_is_seeded():
    text = _text()
    migrate = text.index("python scripts/migrate.py")
    assert migrate < text.index(SEED_STEP), "migrations must precede seeding"


def test_the_seed_comes_from_the_shared_module():
    """CI and the integration test must seed the same dataset."""
    assert (PROJECT_ROOT / "scripts" / "seed_evaluation.py").is_file()
    assert (PROJECT_ROOT / "app" / "evaluation" / "seed.py").is_file()
    seed = (PROJECT_ROOT / "scripts" / "seed_evaluation.py").read_text(encoding="utf-8")
    assert "seed_evaluation_data" in seed


# ---------------------------------------------------------------------------
# 3. The gate: the evaluation CLI's exit code decides the job.
# ---------------------------------------------------------------------------

def test_the_evaluation_cli_is_the_gated_command():
    text = _text()
    assert EVAL_STEP in text, "the evaluation run must be a named step"


def test_nothing_can_swallow_a_failing_evaluation():
    text = _text()
    assert "continue-on-error" not in text
    assert "|| true" not in text
    # The gated step must actually be a step with a bare `run:` — no `if:`
    # guard that could quietly keep it from executing.
    eval_block = text.split(EVAL_STEP)[0].rsplit("- name:", 1)[1]
    assert "if:" not in eval_block, "the gated step must not be conditional"


def test_the_report_is_uploaded_even_when_the_run_fails():
    """A red job must leave the per-case evidence behind."""
    text = _text()
    assert "eval-report.json" in text
    assert "actions/upload-artifact" in text
    assert "if: ${{ always() }}" in text


# ---------------------------------------------------------------------------
# 4. Fail fast: misconfiguration and dead services are red before evaluating.
# ---------------------------------------------------------------------------

def test_the_bootstrap_self_check_runs_before_the_evaluation():
    text = _text()
    assert BOOTSTRAP in text
    assert text.index(BOOTSTRAP) < text.index(EVAL_STEP), \
        "self-check must come before the evaluation run"


def test_a_dead_service_fails_before_the_evaluation_runs():
    text = _text()
    reachability = text.index("PostgreSQL is reachable")
    assert reachability < text.index(EVAL_STEP)
    # and it must fail loudly rather than skip
    block = text[reachability:text.index(SEED_STEP)]
    assert "exit 1" in block or "psycopg2.connect" in block


# ---------------------------------------------------------------------------
# 5. Triggers: it runs on push/PR (safe — no credential) and on dispatch.
# ---------------------------------------------------------------------------

def test_the_workflow_runs_on_push_and_pull_request():
    text = _text()
    assert "workflow_dispatch" in text
    assert "push:" in text and "pull_request:" in text


def test_the_workflow_triggers_on_its_own_and_on_the_evaluation_code():
    text = _text()
    assert '.github/workflows/evaluation.yml' in text
    assert '"app/**"' in text and '"scripts/**"' in text and '"migrations/**"' in text


def test_the_job_has_a_timeout():
    text = _text()
    assert "timeout-minutes:" in text, "an evaluation run must never hang a runner"