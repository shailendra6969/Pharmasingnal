"""API integration tests (spec §47): /api/health and /api/data-quality."""
from __future__ import annotations

import pytest

from app.api import create_app


@pytest.fixture()
def client(test_db_url):
    """Flask test client against the test DB with fixtures ingested."""
    from scripts.run_ingestion import main as ingest_main

    ingest_main(["--fixtures", "--database-url", test_db_url])
    app = create_app(database_url=test_db_url)
    app.config["TESTING"] = True
    return app.test_client()


def test_health_ok(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] == "ok"
    assert data["database"]["ok"] is True
    assert data["service"] == "pharmasignal-api"
    assert "server_time" in data
    # fixture ingestion ran -> a pipeline run must be visible (spec §39)
    assert data["last_ingest"] is not None
    assert data["last_ingest"]["status"] in ("succeeded", "running", "failed")
    assert data["last_ingest"]["run_id"] >= 1


def test_health_db_unreachable_returns_503():
    app = create_app(database_url="postgresql://localhost:59999/does_not_exist")
    app.config["TESTING"] = True
    res = app.test_client().get("/api/health")
    assert res.status_code == 503
    data = res.get_json()
    assert data["status"] == "degraded"
    assert data["database"]["ok"] is False
    # error surfaced as a message, never a stack trace (spec §48)
    assert isinstance(data["database"]["error"], str)
    assert "Traceback" not in data["database"]["error"]


def test_data_quality_returns_measured_report(client):
    res = client.get("/api/data-quality")
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] == "ok"
    assert data["report"] is not None
    # measured fixture numbers, not placeholders
    totals = data["stage_totals"]
    assert totals["demo_raw_rows"] == 9          # 7 + 2 fixture rows
    assert totals["drug_raw_rows"] == 11         # 9 + 2
    assert totals["reac_raw_rows"] == 11         # 9 + 2
    assert data["database_counts"]["cases_current"] == 5
    assert data["quarters"] == ["2024q1", "2024q2"]
    # completed run -> report_status complete; killed runs persist 'partial'
    assert data["report_status"] == "complete"
    # full report preserved alongside convenience slices
    assert data["report"]["stage_totals"] == totals
    assert isinstance(data["per_quarter"], list) and data["per_quarter"]


def test_data_quality_empty_database_returns_null_report():
    """Fresh database: absence of a report is a valid 200 state."""
    from scripts.create_databases import ensure_database
    from scripts.migrate import apply_migrations

    url = "postgresql://localhost:5432/pharmasignal_test_empty"
    ensure_database(url)
    apply_migrations(url)
    app = create_app(database_url=url)
    app.config["TESTING"] = True
    res = app.test_client().get("/api/data-quality")
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] == "ok"
    assert data["report"] is None
    assert "No ingestion" in data["message"]


def test_unknown_route_is_json_404(client):
    res = client.get("/api/nope")
    assert res.status_code == 404
