"""HL7 Structured Product Labeling (SPL) XML Parser (spec §18, Phase 4).

Parses FDA DailyMed SPL XML documents into structured plain-text document chunks
for storage in `doc_chunks` and downstream section-aware retrieval.

Design requirements:
- Strictly plain text (no pgvector / embedding dependencies).
- Preserves document-level provenance: set_id, version_number, effective_time,
  document title, sponsor/labeler organization, canonical ingredient name.
- Preserves clinical section taxonomy: maps LOINC codes and headings into standard
  section identifiers (BOXED_WARNING, WARNINGS_AND_PRECAUTIONS, ADVERSE_REACTIONS, etc.).
- Recursively traverses nested subsections (e.g. 5.1, 6.1) and cleans HTML/XML
  markups into clean readable plain text (paragraphs, tables, lists).
- Generates deterministic, stable chunk identifiers for idempotent ingestion.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from typing import Any

# Standard LOINC codes for FDA SPL sections
LOINC_TO_SECTION: dict[str, str] = {
    # Boxed Warning
    "34066-1": "BOXED_WARNING",
    "34088-5": "BOXED_WARNING",
    # Indications & Usage
    "34067-9": "INDICATIONS_AND_USAGE",
    # Dosage & Administration
    "34068-7": "DOSAGE_AND_ADMINISTRATION",
    "43678-2": "DOSAGE_FORMS_AND_STRENGTHS",
    # Contraindications
    "34070-3": "CONTRAINDICATIONS",
    # Warnings & Precautions
    "43685-7": "WARNINGS_AND_PRECAUTIONS",
    "34071-1": "WARNINGS_AND_PRECAUTIONS",
    "42232-9": "WARNINGS_AND_PRECAUTIONS",
    # Adverse Reactions
    "34084-4": "ADVERSE_REACTIONS",
    # Drug Interactions
    "34073-7": "DRUG_INTERACTIONS",
    # Use in Specific Populations
    "43684-0": "USE_IN_SPECIFIC_POPULATIONS",
    "42228-7": "PREGNANCY",
    "77290-5": "LACTATION",
    "34081-0": "PEDIATRIC_USE",
    "34082-8": "GERIATRIC_USE",
    # Overdosage
    "34091-9": "OVERDOSAGE",
    # Description
    "34089-3": "DESCRIPTION",
    # Clinical Pharmacology
    "34090-1": "CLINICAL_PHARMACOLOGY",
    "43679-0": "MECHANISM_OF_ACTION",
    "43681-6": "PHARMACODYNAMICS",
    "43682-4": "PHARMACOKINETICS",
    # Nonclinical Toxicology
    "43680-8": "NONCLINICAL_TOXICOLOGY",
    # Clinical Studies
    "34092-7": "CLINICAL_STUDIES",
    # How Supplied / Storage
    "34069-5": "HOW_SUPPLIED",
    # Patient Counseling
    "34076-0": "PATIENT_COUNSELING_INFORMATION",
}

# Regex heuristics for fallback section classification by title
TITLE_TO_SECTION_HEURISTICS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"BOXED\s+WARNING", re.I), "BOXED_WARNING"),
    (re.compile(r"INDICATIONS?\s+(AND|&)\s+USAGE", re.I), "INDICATIONS_AND_USAGE"),
    (re.compile(r"DOSAGE\s+(AND|&)\s+ADMINISTRATION", re.I), "DOSAGE_AND_ADMINISTRATION"),
    (re.compile(r"DOSAGE\s+FORMS", re.I), "DOSAGE_FORMS_AND_STRENGTHS"),
    (re.compile(r"CONTRAINDICATIONS?", re.I), "CONTRAINDICATIONS"),
    (re.compile(r"WARNINGS?\s+(AND|&)\s+PRECAUTIONS?", re.I), "WARNINGS_AND_PRECAUTIONS"),
    (re.compile(r"^WARNINGS?", re.I), "WARNINGS_AND_PRECAUTIONS"),
    (re.compile(r"^PRECAUTIONS?", re.I), "WARNINGS_AND_PRECAUTIONS"),
    (re.compile(r"ADVERSE\s+REACTIONS?", re.I), "ADVERSE_REACTIONS"),
    (re.compile(r"DRUG\s+INTERACTIONS?", re.I), "DRUG_INTERACTIONS"),
    (re.compile(r"USE\s+IN\s+SPECIFIC\s+POPULATIONS?", re.I), "USE_IN_SPECIFIC_POPULATIONS"),
    (re.compile(r"OVERDOS(AGE|E)", re.I), "OVERDOSAGE"),
    (re.compile(r"CLINICAL\s+PHARMACOLOGY", re.I), "CLINICAL_PHARMACOLOGY"),
    (re.compile(r"NONCLINICAL\s+TOXICOLOGY", re.I), "NONCLINICAL_TOXICOLOGY"),
    (re.compile(r"CLINICAL\s+STUDIES", re.I), "CLINICAL_STUDIES"),
    (re.compile(r"HOW\s+SUPPLIED", re.I), "HOW_SUPPLIED"),
    (re.compile(r"PATIENT\s+COUNSELING", re.I), "PATIENT_COUNSELING_INFORMATION"),
    (re.compile(r"DESCRIPTION", re.I), "DESCRIPTION"),
]

HL7_NS = {"hl7": "urn:hl7-org:v3"}


class SPLParseError(Exception):
    """Raised when an SPL XML document is structurally invalid or unparseable."""


@dataclass(frozen=True)
class DocChunk:
    """Represents a discrete text section ready for insertion into `doc_chunks`."""

    chunk_id: str
    drug: str
    doc_type: str
    section: str
    subsection: str | None
    text: str
    source_url: str | None
    document_date: str | None
    chunk_index: int
    chunking_method: str = "section"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_db_tuple(self) -> tuple:
        return (
            self.chunk_id,
            self.drug,
            self.doc_type,
            self.section,
            self.subsection,
            self.text,
            self.source_url,
            self.document_date,
            self.chunk_index,
            self.chunking_method,
            json.dumps(self.metadata),
        )


@dataclass(frozen=True)
class ParsedDocument:
    """Structured representation of a parsed SPL document."""

    set_id: str
    version_number: str
    effective_time: str
    document_date: str | None
    title: str
    sponsor: str
    drug: str
    chunks: list[DocChunk]


def clean_xml_text(elem: ET.Element | None) -> str:
    """Extract plain text from an SPL element, preserving structural whitespace."""
    if elem is None:
        return ""

    def tag_name(el: ET.Element) -> str:
        return el.tag.split("}")[-1] if "}" in el.tag else el.tag

    pieces: list[str] = []

    def walk(node: ET.Element) -> None:
        tag = tag_name(node)
        if tag in ("paragraph", "p"):
            pieces.append("\n")
        elif tag == "item":
            pieces.append("\n- ")
        elif tag == "tr":
            pieces.append("\n")
        elif tag in ("td", "th"):
            pieces.append(" | ")

        if node.text:
            pieces.append(node.text)

        for child in node:
            walk(child)
            if child.tail:
                pieces.append(child.tail)

        if tag in ("paragraph", "p", "list", "table"):
            pieces.append("\n")

    walk(elem)
    raw = "".join(pieces)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.splitlines()]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _format_date(raw_date: str | None) -> str | None:
    if not raw_date:
        return None
    raw_date = raw_date.strip()
    if len(raw_date) >= 8 and raw_date[:8].isdigit():
        return f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
    return raw_date


def classify_section(code: str | None, title: str | None, parent_section: str | None = None) -> str:
    """Classify a section into a canonical category based on LOINC code and title."""
    if code and code in LOINC_TO_SECTION:
        return LOINC_TO_SECTION[code]

    if title:
        for pattern, canon in TITLE_TO_SECTION_HEURISTICS:
            if pattern.search(title):
                return canon

    if parent_section and parent_section != "OTHER":
        return parent_section

    return "OTHER"


def _clean_title(elem: ET.Element | None) -> str:
    if elem is None:
        return ""
    text = "".join(elem.itertext()).strip()
    return re.sub(r"\s+", " ", text)


def parse_spl_xml(
    xml_content: str | bytes,
    drug: str,
    source_url: str | None = None,
) -> ParsedDocument:
    """Parse an HL7 SPL XML document into metadata and DocChunks.

    Args:
        xml_content: Raw XML string or bytes.
        drug: Canonical drug name to associate with this label.
        source_url: Optional source URL or DailyMed endpoint.

    Returns:
        ParsedDocument containing document metadata and ordered DocChunk instances.

    Raises:
        SPLParseError: If XML is malformed or missing required SPL header fields.
    """
    drug_clean = drug.strip().upper()
    try:
        if isinstance(xml_content, str):
            xml_bytes = xml_content.encode("utf-8")
        else:
            xml_bytes = xml_content
        root = ET.fromstring(xml_bytes)
    except Exception as e:
        raise SPLParseError(f"Malformed XML content: {e}") from e

    # Namespace handling
    ns = HL7_NS if "urn:hl7-org:v3" in root.tag else {}
    find_prefix = "hl7:" if ns else ""

    # Header extraction
    set_id_elem = root.find(f".//{find_prefix}setId", ns)
    if set_id_elem is None:
        # Fallback to id element
        set_id_elem = root.find(f".//{find_prefix}id", ns)
    set_id = set_id_elem.attrib.get("root", "").strip() if set_id_elem is not None else ""
    if not set_id:
        raise SPLParseError("SPL document is missing setId/id identifier.")

    version_elem = root.find(f".//{find_prefix}versionNumber", ns)
    version = version_elem.attrib.get("value", "1").strip() if version_elem is not None else "1"

    eff_elem = root.find(f".//{find_prefix}effectiveTime", ns)
    raw_eff = eff_elem.attrib.get("value", "") if eff_elem is not None else ""
    document_date = _format_date(raw_eff)

    title_elem = root.find(f".//{find_prefix}title", ns)
    doc_title = _clean_title(title_elem)

    sponsor_elem = root.find(f".//{find_prefix}author//{find_prefix}representedOrganization/{find_prefix}name", ns)
    sponsor = _clean_title(sponsor_elem) if sponsor_elem is not None else ""

    if not source_url:
        source_url = f"https://dailymed.nlm.nih.gov/dailymed/services/v2/spls/{set_id}.xml"

    # Traverse sections
    # In HL7 SPL, top-level sections live under structuredBody/component/section
    structured_body = root.find(f".//{find_prefix}structuredBody", ns)
    if structured_body is not None:
        top_sections = structured_body.findall(f"./{find_prefix}component/{find_prefix}section", ns)
    else:
        # Fallback for documents without structuredBody
        top_sections = root.findall(f".//{find_prefix}section", ns)

    chunks: list[DocChunk] = []
    chunk_index = 0

    def process_section(
        sec: ET.Element,
        parent_category: str | None = None,
        parent_title: str | None = None,
    ) -> None:
        nonlocal chunk_index

        code_elem = sec.find(f"{find_prefix}code", ns)
        code = code_elem.attrib.get("code") if code_elem is not None else None
        code_name = code_elem.attrib.get("displayName") if code_elem is not None else None

        title_elem = sec.find(f"{find_prefix}title", ns)
        sec_title = _clean_title(title_elem)

        # Ignore SPL product data element sections (e.g. 48780-1) which are pure XML metadata
        if code == "48780-1":
            return

        category = classify_section(code, sec_title, parent_section=parent_category)
        subsections = sec.findall(f"./{find_prefix}component/{find_prefix}section", ns)

        text_elem = sec.find(f"{find_prefix}text", ns)
        direct_text = clean_xml_text(text_elem)

        # If direct text exists on this section, emit a chunk
        if direct_text:
            chunk_id = f"{set_id}_{chunk_index:04d}"
            metadata: dict[str, Any] = {
                "set_id": set_id,
                "version_number": version,
                "effective_time": raw_eff,
                "loinc_code": code,
                "loinc_display": code_name,
                "section_title": sec_title or parent_title,
                "sponsor": sponsor,
                "doc_title": doc_title,
            }
            if parent_category and parent_category != category:
                metadata["parent_category"] = parent_category
            if parent_title and parent_title != sec_title:
                metadata["parent_title"] = parent_title

            chunks.append(
                DocChunk(
                    chunk_id=chunk_id,
                    drug=drug_clean,
                    doc_type="SPL_LABEL",
                    section=category,
                    subsection=sec_title if parent_category else None,
                    text=direct_text,
                    source_url=source_url,
                    document_date=document_date,
                    chunk_index=chunk_index,
                    chunking_method="section",
                    metadata=metadata,
                )
            )
            chunk_index += 1

        # Process any child subsections
        effective_parent_cat = category if category != "OTHER" else parent_category
        effective_parent_title = sec_title or parent_title
        for sub in subsections:
            process_section(sub, parent_category=effective_parent_cat, parent_title=effective_parent_title)

    for top_sec in top_sections:
        process_section(top_sec)

    return ParsedDocument(
        set_id=set_id,
        version_number=version,
        effective_time=raw_eff,
        document_date=document_date,
        title=doc_title,
        sponsor=sponsor,
        drug=drug_clean,
        chunks=chunks,
    )
