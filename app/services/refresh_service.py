"""Service layer for the admin quarterly-refresh endpoint (spec §46).

Long-running job model: POST creates a `refresh_jobs` row and starts a
daemon thread running the SAME orchestrator as scripts/refresh_quarter.py
(the runner is injectable for tests; the default imports the script lazily
inside the worker thread, so `app` never depends on `scripts` at import
time). Status is read back from the durable table, so it survives API
restarts; each job writes its refresh report to its own file
(data/processed/refresh_job_<id>.json), which is stored on the job row.

Single-flight: one refresh at a time. A 'running' row whose thread is not
alive is provably orphaned in this single-process deployment (the API
process restarted mid-job) and is marked failed before a new job starts —
the same recovery rule run_ingestion.py applies to ingest_runs.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from app.config import settings
from app.faers.parse import normalize_quarter
from app.repositories import refresh_repository as repo

ALLOWED_MODES = ("both", "all", "primary-suspect")

# job_id -> worker thread (in-process liveness only; durable state is in
# refresh_jobs). Guarded by _threads_lock.
#
# RLock, not Lock: start() holds the lock across the single-flight check and
# calls _thread_alive(), which acquires it again on the same thread — with a
# plain Lock that self-deadlocks the moment a second POST arrives while a job
# is running (found by a faulthandler dump of the single-flight test).
_threads: dict[int, threading.Thread] = {}
_threads_lock = threading.RLock()


def _thread_alive(job_id: int) -> bool:
    with _threads_lock:
        thread = _threads.get(job_id)
    return bool(thread and thread.is_alive())


def _default_runner():
    """The CLI orchestrator, imported lazily (composition-point choice)."""
    from scripts.refresh_quarter import main as refresh_main

    return refresh_main


class RefreshService:
    def __init__(self, database_url: str, runner=None):
        self.database_url = database_url
        self._runner = runner   # injectable for tests; None -> CLI default

    # -- public API ---------------------------------------------------------

    def start(self, params: dict) -> tuple[dict, int]:
        try:
            quarter = normalize_quarter(str(params.get("quarter", "")).strip())
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}, 400

        mode = params.get("mode", "both")
        if mode not in ALLOWED_MODES:
            return {"status": "error",
                    "message": f"mode must be one of {ALLOWED_MODES}"}, 400
        try:
            min_cases = int(params.get("min_cases", 3))
            if min_cases < 1:
                raise ValueError
        except (TypeError, ValueError):
            return {"status": "error",
                    "message": "min_cases must be a positive integer"}, 400
        quarter_dir = params.get("quarter_dir")
        if quarter_dir is not None and not isinstance(quarter_dir, str):
            return {"status": "error",
                    "message": "quarter_dir must be a string"}, 400

        opts = {
            "mode": mode,
            "min_cases": min_cases,
            "skip_download": bool(params.get("skip_download", False)),
            "force_download": bool(params.get("force_download", False)),
            "skip_concordance": bool(params.get("skip_concordance", False)),
            "quarter_dir": quarter_dir,
        }

        # Single-flight + orphan recovery, under the process lock so two
        # concurrent POSTs cannot both pass the check.
        with _threads_lock:
            active = repo.has_active_job(self.database_url)
            if active:
                if _thread_alive(active["job_id"]):
                    return {"status": "conflict",
                            "message": "a refresh job is already running",
                            "active_job": active}, 409
                repo.finish_job(
                    self.database_url, active["job_id"], "failed",
                    error="orphaned: API process died before completion "
                          "(recovered when a new job was requested)")
            job_id = repo.create_job(self.database_url, quarter, opts)

        report_path = (Path(settings.processed_data_path()) /
                       f"refresh_job_{job_id}.json")
        thread = threading.Thread(
            target=self._run, args=(job_id, self._argv(quarter, opts,
                                                       report_path)),
            name=f"refresh-job-{job_id}", daemon=True)
        with _threads_lock:
            _threads[job_id] = thread
        thread.start()

        job = repo.get_job(self.database_url, job_id)
        return {"status": "queued", "message": "refresh job started",
                "job": job,
                "status_url": f"/api/admin/refresh/{job_id}"}, 202

    def status(self, job_id: int) -> tuple[dict, int]:
        job = repo.get_job(self.database_url, job_id)
        if job is None:
            return {"status": "error",
                    "message": f"job {job_id} not found"}, 404
        return {"status": "ok", "job": job}, 200

    def list_jobs(self, limit) -> tuple[dict, int]:
        try:
            limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            limit = 20
        return {"status": "ok",
                "jobs": repo.list_jobs(self.database_url, limit)}, 200

    # -- worker -------------------------------------------------------------

    def _argv(self, quarter: str, opts: dict, report_path: Path) -> list[str]:
        argv = ["--quarter", quarter, "--mode", opts["mode"],
                "--min-cases", str(opts["min_cases"]),
                "--report", str(report_path)]
        if opts["skip_download"]:
            argv.append("--skip-download")
        if opts["force_download"]:
            argv.append("--force-download")
        if opts["skip_concordance"]:
            argv.append("--skip-concordance")
        if opts["quarter_dir"]:
            argv += ["--quarter-dir", opts["quarter_dir"]]
        return argv

    def _run(self, job_id: int, argv: list[str]) -> None:
        report_path = Path(argv[argv.index("--report") + 1])
        try:
            repo.mark_running(self.database_url, job_id)
            rc = (self._runner or _default_runner())(argv)
            if rc == 0:
                report = None
                try:
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    report = None   # job still succeeded; report not stored
                repo.finish_job(self.database_url, job_id, "succeeded",
                                report=report)
            else:
                repo.finish_job(
                    self.database_url, job_id, "failed",
                    error=f"refresh exited {rc}; re-run "
                          f"`scripts/refresh_quarter.py {' '.join(argv)}` "
                          f"locally for full stage output")
        except BaseException as exc:   # incl. SystemExit from the orchestrator
            try:
                repo.finish_job(self.database_url, job_id, "failed",
                                error=f"{type(exc).__name__}: {exc}"[:2000])
            except Exception:
                pass
        finally:
            with _threads_lock:
                _threads.pop(job_id, None)
