"""Unit tests for DailyMed SPL discovery (set_id resolution, spec §18).

Everything here is offline: only the title-scoring heuristic and the
selection logic are exercised, with the HTTP search stubbed out. These guard
what ``scripts/ingest_labels.py`` will file as a drug's label when the drug
has no curated entry — a combination or veterinary product must never be
ingested as the label an FAERS signal is checked against.
"""
from __future__ import annotations

from app.rag.dailymed_client import (
    MIN_ACCEPT_SCORE,
    DailyMedClient,
    score_spl_title,
)


# ------------------------------------------------------------ scoring -------

def test_generic_named_product_scores_highest():
    clean = score_spl_title("ASPIRIN TABLET, FILM COATED [AMERISOURCE BERGEN]", "ASPIRIN")
    retailer = score_spl_title(
        "DG HEALTH PAIN RELIEF (ACETAMINOPHEN) TABLET [DOLGENCORP]", "ACETAMINOPHEN"
    )
    assert clean is not None and retailer is not None
    assert clean >= MIN_ACCEPT_SCORE and retailer >= MIN_ACCEPT_SCORE
    assert clean > retailer  # named after the generic beats a store brand


def test_combination_product_is_refused_or_outranked():
    combo = score_spl_title(
        "GENCARE HEADACHE RELIEF (ACETAMINOPHEN, ASPIRIN, CAFFEINE) TABLET [X]",
        "ACETAMINOPHEN",
    )
    single = score_spl_title("ACETAMINOPHEN TABLET, FILM COATED [Y]", "ACETAMINOPHEN")
    assert combo is not None
    # never acceptable on its own, and always ranked below the plain product
    assert combo < MIN_ACCEPT_SCORE
    assert single is not None and single > combo


def test_veterinary_product_is_rejected_entirely():
    assert score_spl_title(
        "THYRO-TABS CANINE (LEVOTHYROXINE SODIUM) TABLET [LLOYD, INC.]", "LEVOTHYROXINE"
    ) is None


def test_brand_led_title_naming_the_generic_as_active_is_accepted():
    """Biologics are sold under brand names; the generic is still the active."""
    score = score_spl_title(
        "RUXIENCE (RITUXIMAB-PVVR) INJECTION, SOLUTION [PFIZER]", "RITUXIMAB"
    )
    assert score is not None and score >= MIN_ACCEPT_SCORE


def test_title_that_never_names_the_drug_is_rejected():
    """A dosage form alone must never make a product this drug's label —
    otherwise any irrelevant tablet returned by search would be accepted."""
    assert score_spl_title("SOMETHING ELSE ENTIRELY TABLET [Z]", "METFORMIN") is None
    assert score_spl_title("PAIN RELIEF TABLET [Z]", "METFORMIN") is None


def test_empty_inputs_never_score():
    assert score_spl_title("", "ASPIRIN") is None
    assert score_spl_title("ASPIRIN TABLET", "") is None


# ------------------------------------------------------------ resolution ----

def _client_returning(items: list[dict]) -> DailyMedClient:
    client = DailyMedClient()
    client.search_spls = lambda drug_name, name_type=None, pagesize=10, page=1: items  # type: ignore[method-assign]
    return client


def test_resolve_set_id_picks_the_best_scoring_candidate():
    client = _client_returning([
        {"setid": "combo", "title": "COLD RELIEF (ACETAMINOPHEN, CAFFEINE) TABLET [A]"},
        {"setid": "brand", "title": "TYLENOL (ACETAMINOPHEN) TABLET [B]"},
        {"setid": "plain", "title": "ACETAMINOPHEN TABLET, FILM COATED [C]"},
    ])
    set_id, title = client.resolve_set_id("ACETAMINOPHEN") or ("", "")
    assert set_id == "plain"
    assert title.startswith("ACETAMINOPHEN")


def test_resolve_set_id_returns_none_when_nothing_is_usable():
    client = _client_returning([
        {"setid": "vet", "title": "THYRO-TABS CANINE (LEVOTHYROXINE) TABLET [X]"},
        {"setid": "combo", "title": "MULTI (LEVOTHYROXINE, BIOTIN) TABLET [Y]"},
        {"setid": None, "title": "LEVOTHYROXINE TABLET [Z]"},
    ])
    assert client.resolve_set_id("LEVOTHYROXINE") is None


def test_resolve_set_id_searches_by_generic_name():
    """The lookup must ask DailyMed for generic matches, not brand matches."""
    seen: dict = {}

    def fake_search(drug_name, name_type=None, pagesize=10, page=1):
        seen.update(drug_name=drug_name, name_type=name_type)
        return []

    client = DailyMedClient()
    client.search_spls = fake_search  # type: ignore[method-assign]
    assert client.resolve_set_id("PANTOPRAZOLE") is None
    assert seen == {"drug_name": "PANTOPRAZOLE", "name_type": "generic"}