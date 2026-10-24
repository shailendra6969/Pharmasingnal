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
import re
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


# Product-title signals used to pick which SPL represents a generic drug.
# Combination products list several actives in parentheses, and veterinary
# products dose animals — neither describes what an FAERS signal is about.
_VET_MARKERS = ("CANINE", "FELINE", "VETERINARY", "FOR ANIMAL", "ANIMAL HEALTH")
_ORAL_SOLID_FORMS = ("TABLET", "CAPSULE", "SOFTGEL")

# Below this, a search hit is refused rather than filed as the drug's label.
MIN_ACCEPT_SCORE = 1


def score_spl_title(title: str, drug: str) -> int | None:
    """Rank a DailyMed search hit as a stand-in for ``drug``'s label.

    Returns a score (higher is better) or ``None`` when the title cannot stand
    in for the drug's label: it must name the drug at all, and it must not be
    a veterinary product (those never describe the human drug an FAERS signal
    is about). Titles are upper-case SPL titles such as
    ``"LEVOTHYROXINE SODIUM TABLET [BRYANT RANCH PREPACK]"``.
    """
    title_u = title.strip().upper()
    drug_u = drug.strip().upper()
    if not title_u or not drug_u:
        return None
    if any(marker in title_u for marker in _VET_MARKERS):
        return None

    names_drug = title_u.startswith(drug_u) or bool(
        re.search(rf"\b{re.escape(drug_u)}\b", title_u)
    )
    if not names_drug:
        return None  # a dosage form alone never makes it this drug's label

    score = 0
    if title_u.startswith(drug_u):
        score += 3  # product named after the generic itself
    else:
        # Brand-led title naming the generic as its active, e.g.
        # "RUXIENCE (RITUXIMAB-TVVZ) INJECTION" — the label we want anyway.
        score += 2

    paren = re.search(r"\(([^)]*)\)", title_u)
    if paren:
        actives = [a.strip() for a in paren.group(1).split(",") if a.strip()]
        if len(actives) > 1:
            score -= 3  # combination product: not this drug's label
        elif actives and not actives[0].startswith(drug_u):
            score -= 1  # parenthesised active is a different substance

    if any(form in title_u for form in _ORAL_SOLID_FORMS):
        score += 1  # oral solid is the reference form for a label lookup
    return score


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

    def resolve_set_id(self, drug_name: str, pagesize: int = 10) -> tuple[str, str] | None:
        """Discover the best DailyMed SPL for a generic drug name.

        Used for drugs with no curated entry in ``DEFAULT_FOCUS_DRUG_SPLS``:
        searches by generic name and returns ``(set_id, title)`` for the
        highest-scoring single-ingredient product, or ``None`` when DailyMed
        lists nothing usable (combination- or veterinary-only results are
        refused rather than ingested).
        """
        results = self.search_spls(drug_name, name_type="generic", pagesize=pagesize)
        best: tuple[int, str, str] | None = None
        for item in results:
            set_id = item.get("setid")
            title = str(item.get("title") or "")
            if not set_id:
                continue
            score = score_spl_title(title, drug_name)
            if score is None or score < MIN_ACCEPT_SCORE:
                continue
            if best is None or score > best[0]:
                best = (score, str(set_id), title)
        if best is None:
            logger.info("No usable DailyMed SPL found for %s", drug_name)
            return None
        return best[1], best[2]

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
