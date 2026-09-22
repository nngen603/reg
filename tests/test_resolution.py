"""Reference resolution (hybrid retrieval) and clause matching across revisions."""

import pytest

from regextract.config import Settings
from regextract.contract import Clause
from regextract.publishing.publish import diff_runs, match_clauses, renumbered_clauses
from regextract.resolution.retrieval import (BM25, HybridIndex, evaluate_resolution, load_catalog,
                                           reciprocal_rank_fusion, resolve, tokenize)


@pytest.fixture(scope="module")
def offline(tmp_path_factory):
    return Settings(output_dir=tmp_path_factory.mktemp("resolution"))


@pytest.fixture(scope="module")
def noisy_index(offline):
    return HybridIndex(load_catalog(offline.catalog_path, offline.distractors_path), offline)


# --- the pieces -------------------------------------------------------------

def test_bm25_rewards_the_rare_exact_token():
    bm25 = BM25([tokenize("Federal Law No. 28"), tokenize("Federal Law No. 24"),
                 tokenize("ECAS General Requirements")])
    scores = bm25.scores(tokenize("law no 28"))
    assert scores.index(max(scores)) == 0


def test_rrf_rewards_agreement_between_rankings():
    fused = reciprocal_rank_fusion([[0, 1, 2], [1, 0, 2]])
    assert fused[0] == fused[1] > fused[2]


# --- resolution -------------------------------------------------------------

@pytest.mark.parametrize("query, expected", [
    ("ECAS General Requirements", "ECAS-GR"),
    ("Federal Law No. 28", "AE-FL-28"),
])
def test_the_real_carl01_references_resolve_through_the_noise(noisy_index, offline, query, expected):
    resolution = resolve(query, noisy_index, offline)
    assert resolution.status == "resolved"
    assert resolution.target_document_id == expected


def test_the_number_check_refuses_a_near_identical_title(noisy_index, offline):
    resolution = resolve("Federal Law No. 2", noisy_index, offline)
    assert resolution.status == "not_found"
    assert "number check" in resolution.reason


def test_a_plausible_but_absent_document_is_not_found(noisy_index, offline):
    resolution = resolve("GSO Technical Regulation for Low Voltage Electrical Equipment",
                         noisy_index, offline)
    assert resolution.status == "not_found"


def test_noise_does_not_cost_accuracy_or_create_wrong_links(offline):
    report = evaluate_resolution(offline, offline.gold_dir / "resolution.json")
    assert report["clean_accuracy"] == 1.0
    assert report["noisy_accuracy"] == 1.0
    assert report["wrong_links"] == 0
    assert report["catalog"]["noisy"] > report["catalog"]["clean"] + 100


def test_qdrant_does_the_same_fusion_natively(tmp_path):
    settings = Settings(output_dir=tmp_path, REGEXTRACT_USE_QDRANT=True)
    index = HybridIndex(load_catalog(settings.catalog_path, settings.distractors_path), settings)
    assert index.backend == "qdrant"
    resolution = resolve("ECAS General Requirements", index, settings)
    assert resolution.target_document_id == "ECAS-GR"
    assert "qdrant" in resolution.method


def test_the_pipeline_links_carl01_references_to_the_corpus(items):
    resolved = {(i.clause_id, i.kind): i.resolution.target_document_id
                for i in items if i.resolution and i.resolution.status == "resolved"}
    assert resolved[("8.1", "cross_reference")] == "ECAS-GR"
    assert resolved[("5.1", "entity")] == "AE-FL-28"


# --- clause matching across revisions ---------------------------------------

def _renumber_with_id_reuse(run):
    """Revision 5: clause 11.4 moves to 11.9 unchanged, and a NEW clause takes
    the id 11.4. An id-only diff reads that as churn in both places."""
    rev5 = run.model_copy(deep=True)
    for clause in rev5.clauses:
        if clause.id == "11.4":
            clause.id = "11.9"
    for item in rev5.items:
        if item.clause_id == "11.4":
            item.clause_id = "11.9"
    rev5.clauses.append(Clause(id="11.4", heading="",
                               text="Suppliers shall keep test records for five years."))
    return rev5


def test_a_renumbered_clause_is_matched_by_text_not_by_its_old_id(state):
    run = state["run"]
    clause_map = match_clauses(run.clauses, _renumber_with_id_reuse(run).clauses)
    assert clause_map["11.4"] == "11.9"
    assert {"from": "11.4", "to": "11.9"} in renumbered_clauses(clause_map)


def test_renumbering_alone_produces_no_fact_deltas(state):
    run = state["run"]
    rev5 = _renumber_with_id_reuse(run)
    assert diff_runs(run, rev5) == []

    id_only = {c.id: c.id for c in run.clauses}
    assert diff_runs(run, rev5, clause_map=id_only), "an id-only diff should have reported churn"
