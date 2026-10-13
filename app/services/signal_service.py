"""Signal and analytics service layer (spec §14–17, §46).

Business logic for signal exploration, drug and event profiling,
and dashboard statistics.
"""
from __future__ import annotations

from typing import Any

from app.config import settings
from app.repositories import signal_repository as repo

SCIENTIFIC_DISCLAIMER = (
    "Decision-support screening prototype based on public FAERS spontaneous reports. "
    "Signal detection indicates statistical disproportionality in voluntary reporting, "
    "not causality, incidence rates, or clinical risk probability."
)


class SignalService:
    def __init__(self, database_url: str | None = None):
        self.database_url = database_url or settings.database_url()

    def get_dashboard_stats(self) -> dict[str, Any]:
        """Aggregate statistical metrics, focus drug breakdown, and prominent signals."""
        conn = repo.connect(self.database_url)
        try:
            focus = settings.focus_drugs()
            data = repo.get_dashboard_stats(conn, focus_drugs=focus)
            data["scientific_disclaimer"] = SCIENTIFIC_DISCLAIMER
            data["status"] = "ok"
            return data
        finally:
            conn.close()

    def list_signals(
        self,
        *,
        drug: str | None = None,
        event: str | None = None,
        mode: str = "all",
        screened_only: bool = False,
        prr_screen: bool | None = None,
        ror_screen: bool | None = None,
        min_cases: int = 3,
        sort_by: str = "a",
        order: str = "desc",
        page: int = 1,
        page_size: int = 25,
    ) -> dict[str, Any]:
        """List and paginate disproportionality signals."""
        # Sanitize pagination
        try:
            page = max(1, int(page))
        except (ValueError, TypeError):
            page = 1
        try:
            page_size = max(1, min(100, int(page_size)))
        except (ValueError, TypeError):
            page_size = 25
        try:
            min_cases = max(1, int(min_cases))
        except (ValueError, TypeError):
            min_cases = 3

        if mode not in ("all", "primary_suspect"):
            mode = "all"

        if sort_by not in ("a", "case_count", "prr", "ror", "chi_square", "drug", "event"):
            sort_by = "a"

        order = "asc" if str(order).lower() == "asc" else "desc"

        conn = repo.connect(self.database_url)
        try:
            result = repo.list_signals(
                conn,
                drug=drug,
                event=event,
                mode=mode,
                screened_only=screened_only,
                prr_screen=prr_screen,
                ror_screen=ror_screen,
                min_cases=min_cases,
                sort_by=sort_by,
                order=order,
                page=page,
                page_size=page_size,
            )
            result["status"] = "ok"
            result["filters"] = {
                "drug": drug,
                "event": event,
                "mode": mode,
                "screened_only": screened_only,
                "prr_screen": prr_screen,
                "ror_screen": ror_screen,
                "min_cases": min_cases,
                "sort_by": sort_by,
                "order": order,
            }
            result["scientific_disclaimer"] = SCIENTIFIC_DISCLAIMER
            return result
        finally:
            conn.close()

    def get_signal_detail(self, signal_id: int) -> tuple[dict[str, Any] | None, int]:
        """Signal detail including 2x2 contingency breakdown, sensitivity comparison, and trends."""
        conn = repo.connect(self.database_url)
        try:
            data = repo.get_signal_by_id(conn, signal_id)
            if not data:
                return {"status": "error", "message": f"Signal {signal_id} not found"}, 404
            data["status"] = "ok"
            data["scientific_disclaimer"] = SCIENTIFIC_DISCLAIMER
            return data, 200
        finally:
            conn.close()

    def get_drug_profile(self, drug: str) -> tuple[dict[str, Any] | None, int]:
        """Comprehensive drug profile with roles, demographics, and top reactions."""
        if not drug or not drug.strip():
            return {"status": "error", "message": "Drug name cannot be empty"}, 400

        conn = repo.connect(self.database_url)
        try:
            data = repo.get_drug_profile(conn, drug.strip())
            if not data:
                return {
                    "status": "error",
                    "message": f"No adverse event data found for drug '{drug.strip().upper()}'"
                }, 404
            data["status"] = "ok"
            data["scientific_disclaimer"] = SCIENTIFIC_DISCLAIMER
            return data, 200
        finally:
            conn.close()

    def get_event_profile(self, event: str) -> tuple[dict[str, Any] | None, int]:
        """Comprehensive event profile with demographics and top associated drugs."""
        if not event or not event.strip():
            return {"status": "error", "message": "Event term cannot be empty"}, 400

        conn = repo.connect(self.database_url)
        try:
            data = repo.get_event_profile(conn, event.strip())
            if not data:
                return {
                    "status": "error",
                    "message": f"No adverse event reports found for term '{event.strip().upper()}'"
                }, 404
            data["status"] = "ok"
            data["scientific_disclaimer"] = SCIENTIFIC_DISCLAIMER
            return data, 200
        finally:
            conn.close()

    def search_drugs(self, query: str, limit: int = 10) -> dict[str, Any]:
        """Autocomplete search for drugs."""
        if not query or len(query.strip()) < 1:
            return {"status": "ok", "items": []}
        conn = repo.connect(self.database_url)
        try:
            items = repo.search_drugs(conn, query.strip(), limit=limit)
            return {"status": "ok", "items": items}
        finally:
            conn.close()

    def search_events(self, query: str, limit: int = 10) -> dict[str, Any]:
        """Autocomplete search for event terms."""
        if not query or len(query.strip()) < 1:
            return {"status": "ok", "items": []}
        conn = repo.connect(self.database_url)
        try:
            items = repo.search_events(conn, query.strip(), limit=limit)
            return {"status": "ok", "items": items}
        finally:
            conn.close()

    def get_focus_drugs(self) -> dict[str, Any]:
        """List configured focus drugs with metadata."""
        focus = list(settings.focus_drugs())
        return {
            "status": "ok",
            "focus_drugs": focus,
            "note": "Analysis filter only; never used as the statistical background (spec §5, Rule 6)."
        }
