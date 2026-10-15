"""Unit tests for HL7 SPL XML parser (spec §18, Phase 4).

Validates:
1. Complete SPL parsing: document metadata, section classification, text cleaning.
2. Handling of missing sections (graceful degradation when sections are absent).
3. Multiple sections with identical LOINC codes or titles.
4. Deeply nested subsections (parent category inheritance, hierarchical traversal).
5. Error handling for malformed XML or missing document identifiers.
6. Deterministic chunk ID generation and idempotency.
"""
from __future__ import annotations

from pathlib import Path
import pytest

from app.rag.spl_parser import (
    DocChunk,
    ParsedDocument,
    SPLParseError,
    clean_xml_text,
    classify_section,
    parse_spl_xml,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "spl"


def test_fixtures_exist():
    assert (FIXTURE_DIR / "complete_spl.xml").is_file()
    assert (FIXTURE_DIR / "missing_sections_spl.xml").is_file()
    assert (FIXTURE_DIR / "multiple_same_code_spl.xml").is_file()
    assert (FIXTURE_DIR / "nested_subsections_spl.xml").is_file()
    assert (FIXTURE_DIR / "malformed_spl.xml").is_file()
    assert (FIXTURE_DIR / "spl_v1.xml").is_file()
    assert (FIXTURE_DIR / "spl_v2.xml").is_file()


def test_parse_complete_spl_metadata():
    xml = (FIXTURE_DIR / "complete_spl.xml").read_text(encoding="utf-8")
    doc = parse_spl_xml(xml, drug="ATORVASTATIN")

    assert doc.drug == "ATORVASTATIN"
    assert doc.set_id == "sample-setid-complete-001"
    assert doc.version_number == "1"
    assert doc.effective_time == "20240115"
    assert "LIPITOR" in doc.title
    assert "Pfizer" in doc.sponsor
    assert len(doc.chunks) > 0


def test_parse_complete_spl_sections_and_subsections():
    xml = (FIXTURE_DIR / "complete_spl.xml").read_text(encoding="utf-8")
    doc = parse_spl_xml(xml, drug="ATORVASTATIN")

    sections = [c.section for c in doc.chunks]
    assert "BOXED_WARNING" in sections
    assert "INDICATIONS_AND_USAGE" in sections
    assert "CONTRAINDICATIONS" in sections
    assert "WARNINGS_AND_PRECAUTIONS" in sections
    assert "ADVERSE_REACTIONS" in sections

    # Check subsections under Warnings and Precautions
    warning_chunks = [c for c in doc.chunks if c.section == "WARNINGS_AND_PRECAUTIONS"]
    subsections = [c.subsection for c in warning_chunks if c.subsection]
    assert any("Myopathy and Rhabdomyolysis" in s for s in subsections)
    assert any("Immune-Mediated Necrotizing Myopathy" in s for s in subsections)

    # Check Adverse Reactions subsections
    ar_chunks = [c for c in doc.chunks if c.section == "ADVERSE_REACTIONS"]
    ar_subsections = [c.subsection for c in ar_chunks if c.subsection]
    assert any("Clinical Trials Experience" in s for s in ar_subsections)
    assert any("Postmarketing Experience" in s for s in ar_subsections)


def test_parse_text_cleaning_and_formatting():
    xml = (FIXTURE_DIR / "complete_spl.xml").read_text(encoding="utf-8")
    doc = parse_spl_xml(xml, drug="ATORVASTATIN")

    indications_chunk = next(c for c in doc.chunks if c.section == "INDICATIONS_AND_USAGE")
    # Verify lists are cleaned with bullets
    assert "- Prevention of Cardiovascular Disease in Adults" in indications_chunk.text
    assert "- Hyperlipidemia and Mixed Dyslipidemia" in indications_chunk.text

    rhabdo_chunk = next(
        c for c in doc.chunks
        if c.subsection and "Myopathy and Rhabdomyolysis" in c.subsection
    )
    assert "LIPITOR may cause myopathy" in rhabdo_chunk.text
    assert "Rare fatalities have occurred" in rhabdo_chunk.text


def test_deterministic_chunk_ids():
    xml = (FIXTURE_DIR / "complete_spl.xml").read_text(encoding="utf-8")
    doc1 = parse_spl_xml(xml, drug="ATORVASTATIN")
    doc2 = parse_spl_xml(xml, drug="ATORVASTATIN")

    assert len(doc1.chunks) == len(doc2.chunks)
    for c1, c2 in zip(doc1.chunks, doc2.chunks):
        assert c1.chunk_id == c2.chunk_id
        assert c1.chunk_index == c2.chunk_index
        assert c1.chunk_id.startswith("sample-setid-complete-001_")
        assert c1.metadata["set_id"] == "sample-setid-complete-001"
        assert c1.metadata["version_number"] == "1"


def test_parse_missing_sections():
    """Documents lacking warnings or adverse reactions parse without error."""
    xml = (FIXTURE_DIR / "missing_sections_spl.xml").read_text(encoding="utf-8")
    doc = parse_spl_xml(xml, drug="METFORMIN")

    assert doc.drug == "METFORMIN"
    assert doc.set_id == "sample-setid-missing-002"
    sections = {c.section for c in doc.chunks}
    assert "DESCRIPTION" in sections
    assert "INDICATIONS_AND_USAGE" in sections
    assert "WARNINGS_AND_PRECAUTIONS" not in sections
    assert "ADVERSE_REACTIONS" not in sections
    assert "BOXED_WARNING" not in sections
    assert len(doc.chunks) == 2


def test_parse_multiple_sections_same_code():
    """Multiple sections with the same LOINC code generate distinct, ordered chunks."""
    xml = (FIXTURE_DIR / "multiple_same_code_spl.xml").read_text(encoding="utf-8")
    doc = parse_spl_xml(xml, drug="IBUPROFEN")

    assert len(doc.chunks) == 4
    warning_chunks = [c for c in doc.chunks if c.section == "WARNINGS_AND_PRECAUTIONS"]
    assert len(warning_chunks) == 2
    assert "cardiac toxicity" in warning_chunks[0].text
    assert "renal impairment" in warning_chunks[1].text
    assert warning_chunks[0].chunk_index != warning_chunks[1].chunk_index
    assert warning_chunks[0].chunk_id != warning_chunks[1].chunk_id


def test_parse_deeply_nested_subsections():
    """3-level nested subsections inherit parent category and preserve hierarchy."""
    xml = (FIXTURE_DIR / "nested_subsections_spl.xml").read_text(encoding="utf-8")
    doc = parse_spl_xml(xml, drug="IBUPROFEN")

    assert len(doc.chunks) == 3
    # Top-level
    assert doc.chunks[0].section == "WARNINGS_AND_PRECAUTIONS"
    assert "Top-level warning text" in doc.chunks[0].text
    assert doc.chunks[0].subsection is None

    # Level 2
    assert doc.chunks[1].section == "WARNINGS_AND_PRECAUTIONS"
    assert "Cardiovascular Effects" in doc.chunks[1].subsection
    assert "Level 2 subsection text" in doc.chunks[1].text

    # Level 3
    assert doc.chunks[2].section == "WARNINGS_AND_PRECAUTIONS"
    assert "Cardiovascular Thrombotic Events" in doc.chunks[2].subsection
    assert "Level 3 sub-subsection detail" in doc.chunks[2].text
    assert doc.chunks[2].metadata.get("parent_title") == "5.1 Cardiovascular Effects"


def test_parse_malformed_xml_raises_error():
    xml = (FIXTURE_DIR / "malformed_spl.xml").read_text(encoding="utf-8")
    with pytest.raises(SPLParseError, match="Malformed XML content"):
        parse_spl_xml(xml, drug="TEST")


def test_parse_missing_set_id_raises_error():
    broken_xml = """<document xmlns="urn:hl7-org:v3">
        <title>No set ID document</title>
        <component><structuredBody><component><section><text>Hello</text></section></component></structuredBody></component>
    </document>"""
    with pytest.raises(SPLParseError, match="missing setId/id identifier"):
        parse_spl_xml(broken_xml, drug="TEST")


def test_version_change_metadata():
    v1_xml = (FIXTURE_DIR / "spl_v1.xml").read_text(encoding="utf-8")
    v2_xml = (FIXTURE_DIR / "spl_v2.xml").read_text(encoding="utf-8")

    doc_v1 = parse_spl_xml(v1_xml, drug="SERTRALINE")
    doc_v2 = parse_spl_xml(v2_xml, drug="SERTRALINE")

    assert doc_v1.set_id == doc_v2.set_id == "sample-setid-versioned-005"
    assert doc_v1.version_number == "1"
    assert doc_v2.version_number == "2"
    assert doc_v1.document_date == "2023-01-01"
    assert doc_v2.document_date == "2024-02-01"

    assert len(doc_v1.chunks) == 2
    assert len(doc_v2.chunks) == 3

    v1_warn = next(c for c in doc_v1.chunks if c.section == "WARNINGS_AND_PRECAUTIONS")
    v2_warn = next(c for c in doc_v2.chunks if c.section == "WARNINGS_AND_PRECAUTIONS")
    assert "Original warnings text" in v1_warn.text
    assert "Revised and expanded warnings text" in v2_warn.text


def test_classify_section_loinc_and_heuristics():
    assert classify_section("34088-5", None) == "BOXED_WARNING"
    assert classify_section("34067-9", None) == "INDICATIONS_AND_USAGE"
    assert classify_section("43685-7", None) == "WARNINGS_AND_PRECAUTIONS"
    assert classify_section("34084-4", None) == "ADVERSE_REACTIONS"
    assert classify_section("34073-7", None) == "DRUG_INTERACTIONS"
    assert classify_section("42229-5", "5.1 Serious Adverse Reactions") == "ADVERSE_REACTIONS"
    assert classify_section(None, "Boxed Warning: Fatalities") == "BOXED_WARNING"
    assert classify_section(None, "Indications and Usage for Drug X") == "INDICATIONS_AND_USAGE"
    assert classify_section(None, "Unknown Custom Section", parent_section="WARNINGS_AND_PRECAUTIONS") == "WARNINGS_AND_PRECAUTIONS"
    assert classify_section(None, "Random Section", parent_section=None) == "OTHER"
