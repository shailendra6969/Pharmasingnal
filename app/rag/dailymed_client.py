"""DailyMed REST API Client (spec §18, Phase 4).

Interacts with the official National Library of Medicine (NLM) DailyMed API to:
1. Search and discover Structured Product Label (SPL) set IDs for given drugs.
2. Fetch and cache raw SPL XML files on local disk under `data/raw/dailymed/`.

Strict provenance & separation:
- Raw downloaded XML files are kept strictly isolated in `data/raw/dailymed/`
  (ignored by Git), separate from parsed chunks and database storage.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

DAILYMED_API_BASE = "https://dailymed.nlm.nih.gov/dailymed/services/v2"

# Authoritative FDA reference / representative SPL Set IDs for the 5 focus drugs
DEFAULT_FOCUS_DRUG_SPLS: dict[str, dict[str, str]] = {
    "ATORVASTATIN": {
        "set_id": "a60cc18b-0631-4cf0-b021-9f52224ece65",
        "brand_name": "LIPITOR",
        "labeler": "VIATRIS SPECIALTY LLC",
    },
    "SERTRALINE": {
        "set_id": "fda754f6-d0f3-4dce-a17a-927d64f912f7",
        "brand_name": "ZOLOFT",
        "labeler": "VIATRIS SPECIALTY LLC",
    },
    "METFORMIN": {
        "set_id": "ef77c7dd-a65f-4adc-a0d9-a93779b459a0",
        "brand_name": "METFORMIN HYDROCHLORIDE EXTENDED RELEASE",
        "labeler": "AUROBINDO PHARMA LIMITED",
    },
    "IBUPROFEN": {
        "set_id": "1f01c10a-9434-91a4-2ee4-352315a6b610",
        "brand_name": "ADVIL",
        "labeler": "HALEON US HOLDINGS LLC",
    },
    "OMEPRAZOLE": {
        "set_id": "b6761f84-53ac-4745-a8c8-1e5427d7e179",
        "brand_name": "PRILOSEC",
        "labeler": "COVIS PHARMA US, INC",
    },
}


def raw_spl_storage_path(drug: str, set_id: str) -> Path:
    """Return destination path in data/raw/dailymed/<drug>/<set_id>.xml."""
    base = settings.PROJECT_ROOT / "data" / "raw" / "dailymed" / drug.strip().upper()
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{set_id}.xml"


class DailyMedClient:
    """Client for DailyMed REST API v2."""

    def __init__(self, user_agent: str = "PharmaSignal/1.0 (DrugSafetyPipeline)") -> None:
        self.user_agent = user_agent

    def _get(self, url: str, timeout: int = 15) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    def search_spls(
        self,
        drug_name: str,
        name_type: str | None = None,
        pagesize: int = 10,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """Search DailyMed for SPLs matching drug_name."""
        params: dict[str, str | int] = {
            "drug_name": drug_name,
            "pagesize": pagesize,
            "page": page,
        }
        if name_type:
            params["name_type"] = name_type
        query = urllib.parse.urlencode(params)
        url = f"{DAILYMED_API_BASE}/spls.json?{query}"
        raw = self._get(url)
        data = json.loads(raw.decode("utf-8"))
        return data.get("data", [])

    def fetch_spl_xml(self, set_id: str) -> str:
        """Fetch raw SPL XML text from DailyMed by set_id."""
        url = f"{DAILYMED_API_BASE}/spls/{set_id}.xml"
        raw_bytes = self._get(url)
        return raw_bytes.decode("utf-8", errors="replace")

    def download_spl(
        self,
        drug: str,
        set_id: str,
        dest_path: Path | None = None,
        force: bool = False,
    ) -> tuple[Path, str]:
        """Download SPL XML to disk and return (path, xml_content)."""
        target_path = dest_path or raw_spl_storage_path(drug, set_id)
        if target_path.exists() and not force:
            logger.info("Using cached SPL XML for %s (%s) at %s", drug, set_id, target_path)
            content = target_path.read_text(encoding="utf-8")
            return target_path, content

        logger.info("Fetching SPL XML from DailyMed for %s (set_id=%s)", drug, set_id)
        xml_content = self.fetch_spl_xml(set_id)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(xml_content, encoding="utf-8")
        return target_path, xml_content
