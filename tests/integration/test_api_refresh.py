"""Admin refresh endpoint tests: auth, job lifecycle, single-flight.

The refresh runner is injected (create_app(refresh_runner=...)) so no real
pipeline runs; the stub mimics the CLI contract (reads --report from argv,
writes the refresh report JSON there, returns an exit code).
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.api import create_app  # noqa: E402
from app.config import settings  # noqa: E402

KEY = "test-admin-key-123"


def _wipe_jobs(test_db_url):
    conn = psycopg2.connect(test_db_url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM refresh_jobs")
    conn.close()


@pytest.fixture()
def jobs_db(test_db_url):
    _wipe_jobs(test_db_url)
    yield
    _wipe_jobs(test_db_url)


def _stub_runner(calls, release: threading.Event | None = None, rc: int = 0):
    def runner(argv):
        calls.append(argv)
        if release is not None:
            assert release.wait(10), "test never released the blocking runner"
        if rc != 0:
            return rc
        report_path = Path(argv[argv.index("--report") + 1])
        report_path.write_text(json.dumps(
            {"quarter": "2024q1", "status": "succeeded",
             "stages": {"signals": {"modes": {}}}}), encoding="utf-8")
        return 0
    return runner


def _client(test_db_url, runner=None, monkeypatch=None, api_key: str | None = KEY):
    if monkeypatch is not None:
        if api_key is None:
            monkeypatch.delenv("ADMIN_API_KEY", raising=False)
        else:
            monkeypatch.setenv("ADMIN_API_KEY", api_key)
        settings.admin_api_key.cache_clear()
    app = create_app(database_url=test_db_url, refresh_runner=runner)
    app.config["TESTING"] = True
    return app.test_client()


def _wait_terminal(client, job_id, timeout=10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = client.get(f"/api/admin/refresh/{job_id}",
                         headers={"X-Admin-Key": KEY})
        job = res.get_json()["job"]
        if job["status"] in ("succeeded", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached a terminal state")


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------

def test_admin_disabled_without_configured_key(test_db_url, jobs_db,
                                               monkeypatch):
    client = _client(test_db_url, monkeypatch=monkeypatch, api_key=None)
    res = client.post("/api/admin/refresh",
                      json={"quarter": "2024q1"},
                      headers={"X-Admin-Key": "anything"})
    assert res.status_code == 503
    assert "ADMIN_API_KEY" in res.get_json()["message"]


def test_admin_wrong_or_missing_key_is_401(test_db_url, jobs_db, monkeypatch):
    client = _client(test_db_url, runner=lambda argv: 0, monkeypatch=monkeypatch)
    assert client.post("/api/admin/refresh",
                       json={"quarter": "2024q1"}).status_code == 401
    assert client.post("/api/admin/refresh", json={"quarter": "2024q1"},
                       headers={"X-Admin-Key": "wrong"}).status_code == 401
    assert client.post("/api/admin/refresh", json={"quarter": "2024q1"},
                       headers={"Authorization": "Bearer wrong"}
                       ).status_code == 401
    # status/list are protected too
    assert client.get("/api/admin/refresh").status_code == 401
    assert client.get("/api/admin/refresh/1").status_code == 401


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------

def test_refresh_job_lifecycle(test_db_url, jobs_db, monkeypatch):
    calls: list[list[str]] = []
    client = _client(test_db_url, runner=_stub_runner(calls),
                     monkeypatch=monkeypatch)

    res = client.post("/api/admin/refresh",
                      json={"quarter": "2024Q1", "mode": "all",
                            "min_cases": 1, "skip_concordance": True},
                      headers={"Authorization": f"Bearer {KEY}"})
    assert res.status_code == 202
    body = res.get_json()
    job_id = body["job"]["job_id"]
    assert body["status"] == "queued"
    assert body["status_url"] == f"/api/admin/refresh/{job_id}"
    assert body["job"]["quarter"] == "2024q1"     # normalized
    assert body["job"]["params"]["mode"] == "all"

    job = _wait_terminal(client, job_id)
    assert job["status"] == "succeeded"
    assert job["error"] is None
    assert job["started_at"] and job["finished_at"]
    # the report the runner wrote is stored on the job row
    assert job["report"]["status"] == "succeeded"
    assert job["report"]["quarter"] == "2024q1"

    # the runner got exactly the CLI argv the service promised
    argv = calls[0]
    assert argv[:2] == ["--quarter", "2024q1"]
    assert "--mode" in argv and argv[argv.index("--mode") + 1] == "all"
    assert "--min-cases" in argv and argv[argv.index("--min-cases") + 1] == "1"
    assert "--skip-concordance" in argv
    assert "--skip-download" not in argv

    # list endpoint shows the job
    listing = client.get("/api/admin/refresh",
                         headers={"X-Admin-Key": KEY}).get_json()
    assert [j["job_id"] for j in listing["jobs"]] == [job_id]


def test_refresh_job_failure_recorded(test_db_url, jobs_db, monkeypatch):
    client = _client(test_db_url, runner=_stub_runner([], rc=1),
                     monkeypatch=monkeypatch)
    res = client.post("/api/admin/refresh", json={"quarter": "2024q1"},
                      headers={"X-Admin-Key": KEY})
    assert res.status_code == 202
    job = _wait_terminal(client, res.get_json()["job"]["job_id"])
    assert job["status"] == "failed"
    assert "exited 1" in job["error"]
    assert job["report"] is None


def test_refresh_unknown_job_is_404(test_db_url, jobs_db, monkeypatch):
    client = _client(test_db_url, monkeypatch=monkeypatch)
    res = client.get("/api/admin/refresh/999999",
                     headers={"X-Admin-Key": KEY})
    assert res.status_code == 404


def test_refresh_input_validation(test_db_url, jobs_db, monkeypatch):
    calls: list[list[str]] = []
    client = _client(test_db_url, runner=_stub_runner(calls),
                     monkeypatch=monkeypatch)
    h = {"X-Admin-Key": KEY}
    assert client.post("/api/admin/refresh", json={"quarter": "not-a-quarter"},
                       headers=h).status_code == 400
    assert client.post("/api/admin/refresh",
                       json={"quarter": "2024q1", "mode": "bogus"},
                       headers=h).status_code == 400
    assert client.post("/api/admin/refresh",
                       json={"quarter": "2024q1", "min_cases": 0},
                       headers=h).status_code == 400
    assert client.post("/api/admin/refresh", headers=h).status_code == 400
    assert calls == []   # nothing was ever started


# ---------------------------------------------------------------------------
# single-flight & orphan recovery
# ---------------------------------------------------------------------------

def test_single_flight_second_start_conflicts(test_db_url, jobs_db,
                                              monkeypatch):
    release = threading.Event()
    client = _client(test_db_url, runner=_stub_runner([], release=release),
                     monkeypatch=monkeypatch)
    h = {"X-Admin-Key": KEY}

    first = client.post("/api/admin/refresh", json={"quarter": "2024q1"},
                        headers=h)
    assert first.status_code == 202
    first_id = first.get_json()["job"]["job_id"]

    second = client.post("/api/admin/refresh", json={"quarter": "2024q2"},
                         headers=h)
    assert second.status_code == 409
    assert second.get_json()["active_job"]["job_id"] == first_id

    release.set()                       # let the first job finish
    assert _wait_terminal(client, first_id)["status"] == "succeeded"

    # slot is free again
    third = client.post("/api/admin/refresh", json={"quarter": "2024q2"},
                        headers=h)
    assert third.status_code == 202
    _wait_terminal(client, third.get_json()["job"]["job_id"])


def test_orphaned_running_job_recovered_on_next_start(test_db_url, jobs_db,
                                                      monkeypatch):
    """A 'running' row with no live thread is provably orphaned (the API
    process died) and must be failed before the new job starts."""
    from app.repositories import refresh_repository as repo

    orphan_id = repo.create_job(test_db_url, "2024q1", {})
    repo.mark_running(test_db_url, orphan_id)

    client = _client(test_db_url, runner=_stub_runner([]),
                     monkeypatch=monkeypatch)
    res = client.post("/api/admin/refresh", json={"quarter": "2025q1"},
                      headers={"X-Admin-Key": KEY})
    assert res.status_code == 202
    new_id = res.get_json()["job"]["job_id"]

    orphan = repo.get_job(test_db_url, orphan_id)
    assert orphan["status"] == "failed"
    assert "orphaned" in orphan["error"]
    assert _wait_terminal(client, new_id)["status"] == "succeeded"
