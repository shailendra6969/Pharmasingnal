"""PharmaSignal Flask application & REST API (spec §40, §46, §47).

Layered architecture: routes -> services -> repositories.
Routes only parse, call, and serialize.
"""
from __future__ import annotations

import hmac
from functools import wraps
from pathlib import Path
from flask import Flask, Blueprint, current_app, jsonify, request, render_template, redirect, abort

from app.config import settings
from app.services.system_service import SystemService
from app.services.signal_service import SignalService
from app.services.refresh_service import RefreshService

api_bp = Blueprint("api", __name__, url_prefix="/api")
pages_bp = Blueprint("pages", __name__)


def _system_service() -> SystemService:
    return SystemService(current_app.config["DATABASE_URL"])


def _signal_service() -> SignalService:
    return SignalService(current_app.config["DATABASE_URL"])


def _refresh_service() -> RefreshService:
    return RefreshService(current_app.config["DATABASE_URL"],
                          runner=current_app.config.get("REFRESH_RUNNER"))


def _require_admin(view):
    """Admin guard for /api/admin/*: shared secret, constant-time compare.

    No key configured -> 503 (endpoint disabled, misconfiguration visible
    instead of silently open or silently locked). Wrong/missing -> 401.
    """
    @wraps(view)
    def wrapper(*args, **kwargs):
        configured = settings.admin_api_key()
        if not configured:
            return jsonify({
                "status": "error",
                "message": "Admin API disabled: ADMIN_API_KEY is not "
                           "configured (see .env.example)",
            }), 503
        supplied = ""
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            supplied = auth[len("Bearer "):].strip()
        supplied = supplied or request.headers.get("X-Admin-Key", "").strip()
        if not supplied or not hmac.compare_digest(supplied, configured):
            return jsonify({"status": "error",
                            "message": "Unauthorized"}), 401
        return view(*args, **kwargs)

    return wrapper


# ============================================================================
# API Endpoints (spec §47)
# ============================================================================

@api_bp.get("/health")
def health():
    payload, status = _system_service().health()
    return jsonify(payload), status


@api_bp.get("/data-quality")
def data_quality():
    payload, status = _system_service().data_quality()
    return jsonify(payload), status


@api_bp.get("/dashboard/stats")
def dashboard_stats():
    payload = _signal_service().get_dashboard_stats()
    return jsonify(payload), 200


@api_bp.get("/signals")
def list_signals():
    screened_only = request.args.get("screened_only", "").lower() in ("true", "1", "yes")
    prr_screen = None
    if "prr_screen" in request.args:
        prr_screen = request.args.get("prr_screen", "").lower() in ("true", "1", "yes")
    ror_screen = None
    if "ror_screen" in request.args:
        ror_screen = request.args.get("ror_screen", "").lower() in ("true", "1", "yes")

    res = _signal_service().list_signals(
        drug=request.args.get("drug"),
        event=request.args.get("event"),
        mode=request.args.get("mode", "all"),
        screened_only=screened_only,
        prr_screen=prr_screen,
        ror_screen=ror_screen,
        min_cases=request.args.get("min_cases", 3),
        sort_by=request.args.get("sort_by", "a"),
        order=request.args.get("order", "desc"),
        page=request.args.get("page", 1),
        page_size=request.args.get("page_size", 25),
    )
    return jsonify(res), 200


@api_bp.get("/signals/<int:signal_id>")
def get_signal_detail(signal_id: int):
    payload, status = _signal_service().get_signal_detail(signal_id)
    return jsonify(payload), status


@api_bp.get("/drugs/<drug>")
def get_drug_profile(drug: str):
    payload, status = _signal_service().get_drug_profile(drug)
    return jsonify(payload), status


@api_bp.get("/events/<event>")
def get_event_profile(event: str):
    payload, status = _signal_service().get_event_profile(event)
    return jsonify(payload), status


@api_bp.get("/focus-drugs")
def focus_drugs():
    payload = _signal_service().get_focus_drugs()
    return jsonify(payload), 200


@api_bp.get("/search/drugs")
def search_drugs():
    q = request.args.get("q", "")
    try:
        limit = int(request.args.get("limit", 10))
    except (ValueError, TypeError):
        limit = 10
    payload = _signal_service().search_drugs(q, limit=limit)
    return jsonify(payload), 200


@api_bp.post("/admin/refresh")
@_require_admin
def admin_refresh_start():
    """Start a quarterly refresh job (download -> ingest -> signals ->
    concordance). Returns 202 with a job id; poll the status URL."""
    payload = request.get_json(silent=True) or {}
    result, status = _refresh_service().start(payload)
    return jsonify(result), status


@api_bp.get("/admin/refresh")
@_require_admin
def admin_refresh_list():
    result, status = _refresh_service().list_jobs(request.args.get("limit", 20))
    return jsonify(result), status


@api_bp.get("/admin/refresh/<int:job_id>")
@_require_admin
def admin_refresh_status(job_id: int):
    result, status = _refresh_service().status(job_id)
    return jsonify(result), status


@api_bp.get("/search/events")
def search_events():
    q = request.args.get("q", "")
    try:
        limit = int(request.args.get("limit", 10))
    except (ValueError, TypeError):
        limit = 10
    payload = _signal_service().search_events(q, limit=limit)
    return jsonify(payload), 200


# ============================================================================
# Web Pages (spec §14–17, §40)
# ============================================================================

@pages_bp.get("/")
def index():
    return redirect("/dashboard")


@pages_bp.get("/dashboard")
def dashboard_page():
    stats = _signal_service().get_dashboard_stats()
    return render_template("dashboard.html", stats=stats, active_nav="dashboard")


@pages_bp.get("/signals")
def signals_page():
    screened_only = request.args.get("screened_only", "").lower() in ("true", "1", "yes")
    signals_data = _signal_service().list_signals(
        drug=request.args.get("drug"),
        event=request.args.get("event"),
        mode=request.args.get("mode", "all"),
        screened_only=screened_only,
        min_cases=request.args.get("min_cases", 3),
        sort_by=request.args.get("sort_by", "a"),
        order=request.args.get("order", "desc"),
        page=request.args.get("page", 1),
        page_size=request.args.get("page_size", 25),
    )
    return render_template(
        "signals.html",
        signals=signals_data,
        filters=signals_data["filters"],
        active_nav="signals",
    )


@pages_bp.get("/signals/<int:signal_id>")
def signal_detail_page(signal_id: int):
    detail, status = _signal_service().get_signal_detail(signal_id)
    if status != 200 or not detail:
        abort(404)
    return render_template("signal_detail.html", signal=detail, active_nav="signals")


@pages_bp.get("/drugs/<drug>")
def drug_detail_page(drug: str):
    profile, status = _signal_service().get_drug_profile(drug)
    if status != 200 or not profile:
        abort(404)
    return render_template("drug_detail.html", profile=profile, active_nav="signals")


@pages_bp.get("/events/<event>")
def event_detail_page(event: str):
    profile, status = _signal_service().get_event_profile(event)
    if status != 200 or not profile:
        abort(404)
    return render_template("event_detail.html", profile=profile, active_nav="signals")


@pages_bp.get("/data-quality")
def data_quality_page():
    dq, _ = _system_service().data_quality()
    report = dq.get("report") if dq else None
    return render_template("data_quality.html", report=report, active_nav="quality")


# ============================================================================
# Error Handling & Factory
# ============================================================================

def _handle_500(exc: BaseException):
    # Secure error responses (spec §48): no stack traces to clients.
    if request.path.startswith("/api/"):
        return jsonify({"status": "error", "message": "Internal server error"}), 500
    return "<h1>500 Internal Server Error</h1><p>An unexpected error occurred.</p>", 500


def _handle_404(exc: BaseException):
    if request.path.startswith("/api/"):
        return jsonify({"status": "error", "message": "Not found"}), 404
    return "<h1>404 Not Found</h1><p>The requested resource was not found.</p>", 404


def create_app(database_url: str | None = None, refresh_runner=None) -> Flask:
    template_dir = Path(__file__).resolve().parents[1] / "templates"
    static_dir = Path(__file__).resolve().parents[1] / "static"

    app = Flask(
        __name__,
        template_folder=str(template_dir),
        static_folder=str(static_dir),
        static_url_path="/static",
    )
    app.config["DATABASE_URL"] = database_url or settings.database_url()
    # Test seam: inject the quarterly-refresh runner (default: the CLI
    # orchestrator, imported lazily inside the job thread).
    app.config["REFRESH_RUNNER"] = refresh_runner

    # Custom Jinja filters
    def _number_format(val):
        if val is None:
            return "0"
        try:
            return f"{int(val):,}"
        except (ValueError, TypeError):
            return str(val)

    app.jinja_env.filters["number_format"] = _number_format

    app.register_blueprint(api_bp)
    app.register_blueprint(pages_bp)
    app.register_error_handler(404, _handle_404)
    app.register_error_handler(500, _handle_500)
    app.register_error_handler(Exception, _handle_500)
    return app
