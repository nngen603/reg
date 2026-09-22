"""Checks added after the final review.

A close (fuzzy) quote match must not change what a fact means, a fact that only
the second model finds must reach a person, and a fact whose quote was never
found cannot be accepted into publication.
"""

import pytest

from regextract.config import Settings
from regextract.contract import Clause, Decision, Evidence, Item, MatchKind, ReviewSignal, impact_tier
from regextract.evaluation.evaluate import baseline_comparison
from regextract.extraction.extract import definitions_digest
from regextract.normalize import locate
from regextract.pipeline.checkpointing import memory_checkpointer
from regextract.pipeline.graph import resume_pipeline, run_pipeline
from regextract.publishing.publish import published_items
from regextract.routing.route import route_item
from regextract.verification.verify import facts_missed_by_primary


def _item(type_name, *, match_kind=MatchKind.EXACT, agreement=1.0, notes=()):
    return Item(
        item_id="t", kind="entity", type_name=type_name, clause_id="1",
        payload={"type": type_name, "text": "x", "confidence": 0.99},
        evidence=Evidence(clause_id="1", quote="x", grounded=True,
                          match_kind=match_kind, match_score=97.0),
        review=ReviewSignal(agreement=agreement, validators={"evidence_grounded": True},
                            impact=impact_tier(type_name), notes=list(notes)),
    )


# --- a close match cannot change the meaning --------------------------------

ALTERED = [
    ("3.1.2", "This document shall apply to the products excluded by UAE / IEC 60335-1."),  # "not" dropped
    ("11.4", "ECAS Registration Certificate shall be valid for two years."),                 # one -> two
    ("6.4.3", "BS 546 Plug Configurations for 13 Ampere Appliances."),                        # 15 -> 13
]


@pytest.mark.parametrize("clause_id, altered", ALTERED)
def test_a_close_match_that_changes_a_number_or_a_not_is_refused(clauses_by_id, settings,
                                                                  clause_id, altered):
    assert not locate(altered, clauses_by_id[clause_id].text, settings.fuzzy_threshold).found


def test_a_close_match_with_the_same_meaning_is_still_found(clauses_by_id, settings):
    """OCR-style noise in a word is what the fuzzy pass is for."""
    noisy = "ECAS Registratlon Certificate shall be valid for one year."
    result = locate(noisy, clauses_by_id["11.4"].text, settings.fuzzy_threshold)
    assert result.found and result.kind is MatchKind.FUZZY


def test_a_quote_longer_than_its_clause_is_not_a_close_match(clauses_by_id, settings):
    source = clauses_by_id["11.4"].text
    padded = source + " and additionally a penalty of AED 50,000 applies"
    assert not locate(padded, source, settings.fuzzy_threshold).found


def test_a_close_match_always_goes_to_a_person(settings):
    routed = route_item(_item("organisation", match_kind=MatchKind.FUZZY), settings)
    assert routed.review.decision is Decision.REVIEW
    assert any(r.startswith("evidence_matched_fuzzily") for r in routed.review.reasons)


def test_every_planted_fact_is_stopped(pdf_path, tmp_path):
    state = run_pipeline(pdf_path=pdf_path, issuer="ESMA", jurisdiction="AE",
                         settings=Settings(output_dir=tmp_path), inject_faults=True)
    planted = [i for i in state["items"]
               if "additionally a penalty of AED 50,000" in i.evidence.quote]
    assert planted
    assert not [i.clause_id for i in planted if i.evidence.grounded]


# --- the second model also guards recall -------------------------------------

def test_a_fact_only_the_second_model_found_is_kept():
    primary = {"entities": [{"type": "time_period", "text": "one year", "quote": "valid for one year"}]}
    secondary = {"entities": [
        {"type": "time_period", "text": "one year", "quote": "valid for one year"},
        {"type": "organisation", "text": "ECAS", "quote": "ECAS Registration Certificate"},
    ]}
    missed = facts_missed_by_primary(primary, secondary)
    assert [(kind, payload["text"]) for kind, payload in missed] == [("entity", "ECAS")]


def test_a_fact_only_the_second_model_found_goes_to_a_person(settings):
    routed = route_item(_item("organisation", agreement=0.0, notes=["second_model_only"]), settings)
    assert routed.review.decision is Decision.REVIEW
    assert "found_only_by_second_model" in routed.review.reasons


# --- review cannot publish a fact without evidence ---------------------------

def test_a_fact_whose_quote_was_not_found_cannot_be_accepted(pdf_path, tmp_path):
    settings = Settings(output_dir=tmp_path / "out", gold_dir=tmp_path / "gold", REGEXTRACT_REVIEW=True)
    saver = memory_checkpointer()
    paused = run_pipeline(pdf_path=pdf_path, issuer="ESMA", jurisdiction="AE", settings=settings,
                          checkpointer=saver, inject_faults=True)
    target = next(i for i in paused["items"] if not i.evidence.grounded)
    final = resume_pipeline(run_id=paused["run_id"], settings=settings, checkpointer=saver,
                            decisions=[{"item_id": target.item_id, "action": "accept",
                                        "reviewer": "analyst-a"}])
    item = next(i for i in final["items"] if i.item_id == target.item_id)
    assert item.review.decision is Decision.REVIEW
    assert any(r.startswith("accept_refused_not_grounded") for r in item.review.reasons)
    assert item.item_id not in {i.item_id for i in published_items(final["run"])}


# --- smaller fixes -------------------------------------------------------------

def test_definitions_are_found_by_heading_not_by_clause_number(clauses_by_id):
    clauses = [Clause(id="2", heading="DEFINITIONS", text="2. DEFINITIONS"),
               Clause(id="2.1", parent="2", text="2.1 Supplier: the person who places the product."),
               Clause(id="5.1", parent="5", text="5.1 Nothing is defined here.")]
    digest = definitions_digest(clauses)
    assert "Supplier" in digest and "Nothing is defined" not in digest
    assert "5.1" in definitions_digest(list(clauses_by_id.values()))


def test_the_headline_makes_sense_when_nothing_failed(items):
    headline = baseline_comparison(items)["headline"]
    assert "would have published" not in headline


def test_a_reply_wrapped_in_a_one_item_list_is_accepted():
    from regextract.contract import ClauseExtractionDraft
    empty = {"clause_types": [], "obligations": [], "entities": [], "cross_references": []}
    assert ClauseExtractionDraft.model_validate([empty]).obligations == []
    with pytest.raises(Exception):
        ClauseExtractionDraft.model_validate([empty, empty])
