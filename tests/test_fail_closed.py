"""A failed model call must never look like a correct 'nothing here' answer."""

import csv

import pytest

from regextract.config import Settings
from regextract.contract import Decision
from regextract.pipeline.graph import run_pipeline
from regextract.publishing.publish import published_items
from fakes import FailingGateway


@pytest.fixture(scope="module")
def failed_primary(pdf_path, tmp_path_factory):
    settings = Settings(output_dir=tmp_path_factory.mktemp("failed_primary"))
    return run_pipeline(pdf_path=pdf_path, issuer="ESMA", jurisdiction="AE", settings=settings,
                        gateway=FailingGateway(settings, ":11.4:run0"))


def test_a_failed_clause_raises_a_blocking_document_flag(failed_primary):
    flags = {f.code: f for f in failed_primary["flags"]}
    assert "extraction_failed" in flags
    assert flags["extraction_failed"].clause_ids == ["11.4"]


def test_a_failed_clause_holds_the_whole_document(failed_primary):
    assert not [i for i in failed_primary["items"] if i.clause_id == "11.4"]
    assert all(i.review.decision is Decision.REVIEW for i in failed_primary["items"])
    assert published_items(failed_primary["run"]) == []


def test_the_failed_clause_heads_the_review_queue(failed_primary):
    path = failed_primary["written"]["review_queue"]
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    assert rows[0]["clause_id"] == "11.4"
    assert rows[0]["type"] == "extraction_failed"


def test_a_failed_second_run_is_a_missing_signal_not_a_disagreement(pdf_path, tmp_path):
    settings = Settings(output_dir=tmp_path)
    state = run_pipeline(pdf_path=pdf_path, issuer="ESMA", jurisdiction="AE", settings=settings,
                         gateway=FailingGateway(settings, ":11.4:run1"))

    items = [i for i in state["items"] if i.clause_id == "11.4"]
    assert items
    for item in items:
        assert item.review.agreement == 0.5
        assert "second_run_failed" in item.review.reasons
        assert "models_disagree" not in item.review.reasons
    assert "extraction_failed" not in {f.code for f in state["flags"]}
