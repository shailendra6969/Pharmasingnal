"""Service layer for API operations (spec §46: routes → services → repositories)."""
from __future__ import annotations

from datetime import datetime, timezone

from app.repositories import system_repository as repo


class SystemService:
    def __init__(self, database_url: str):
        self.database_url = database_url

    def health(self) -> tuple[dict, int]:
        """Liveness/readiness payload. 200 when DB reachable, else 503."""
        db_ok, db_error = repo.check_database(self.database_url)
        payload = {
            "status": "ok" if db_ok else "degraded",
            "service": "pharmasignal-api",
            "database": {"ok": db_ok, "error": db_error},
            "server_time": datetime.now(timezone.utc).isoformat(),
            "last_ingest": None,
        }
        if db_ok:
            try:
                payload["last_ingest"] = repo.last_ingest_run(self.database_url)
            except Exception as exc:
                payload["last_ingest"] = {"error": f"{type(exc).__name__}"[:120]}
            return payload, 200
        return payload, 503

    def data_quality(self) -> tuple[dict, int]:
        """Latest measured data-quality report (spec §7, §38).

        No report yet is a valid state (fresh database) -> 200 with
        report: null, not an error. DB unreachable -> 503.
        """
        db_ok, db_error = repo.check_database(self.database_url)
        if not db_ok:
            return {"status": "degraded",
                    "database": {"ok": False, "error": db_error},
                    "report": None}, 503
        entry = repo.latest_quality_report(self.database_url)
        if entry is None:
            return {
                "status": "ok",
                "report": None,
                "message": "No ingestion has completed yet; no quality report exists.",
            }, 200
        report = entry["report"]
        return {
            "status": "ok",
            "report_id": entry["report_id"],
            "run_id": entry["run_id"],
            "generated_at": entry["generated_at"],
            "quarters": entry["quarters"],
            # 'partial' while its run was killed mid-flight; consumers must
            # not treat it as a complete picture.
            "report_status": report.get("status", "complete"),
            # Convenience slices for dashboards; full report stays intact.
            "stage_totals": report.get("stage_totals", {}),
            "database_counts": report.get("database_counts", {}),
            "per_quarter": report.get("per_quarter", []),
            "report": report,
        }, 200
