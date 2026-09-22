"""End-to-end behaviour of the pipeline, and the invariants it must never break."""

from regextract.contract import Decision
from regextract.evaluation.evaluate import baseline_comparison, run_failure_classes
from regextract.publishing.publish import published_items


# --- the core invariant -----------------------------------------------------

def test_ungrounded_items_are_never_auto_accepted(items):
    """The gate. If this test ever fails, the whole trust story is void."""
    for item in items:
        if not item.evidence.grounded:
            assert item.review.decision is not Decision.AUTO_ACCEPT, item.item_id


def test_nothing_publishes_without_evidence(state):
    for item in published_items(state["run"]):
        assert item.evidence.grounded
        assert item.evidence.char_start is not None
        assert item.evidence.quote.strip()


def test_every_published_item_quote_is_really_in_the_source(state, clauses_by_id):
    """Re-verify independently of the pipeline, straight against clause text."""
    from regextract.normalize import locate

    for item in published_items(state["run"])[:80]:
        clause = clauses_by_id[item.clause_id]
        assert locate(item.evidence.quote, clause.text).found, item.item_id


def test_offsets_point_at_the_canonical_text(state, items):
    canonical = state["canonical"].text
    checked = 0
    for item in items:
        if not item.evidence.grounded:
            continue
        start, end = item.evidence.char_start, item.evidence.char_end
        assert 0 <= start < end <= len(canonical)
        assert item.evidence.page is not None
        checked += 1
    assert checked > 100


# --- failure classes --------------------------------------------------------

def test_all_failure_classes_pass(state):
    results = run_failure_classes(state)
    failed = [f"{r.key} {r.title}: expected {r.expected}, got {r.actual}"
              for r in results if not r.passed]
    assert not failed, "\n".join(failed)


def test_there_are_nine_failure_classes(state):
    assert len(run_failure_classes(state)) == 9


# --- provenance and identity ------------------------------------------------

def test_every_item_carries_provenance(items):
    for item in items:
        assert item.provenance.prompt_version
        assert item.provenance.taxonomy_version
        assert item.provenance.run_id


def test_item_ids_are_stable_across_runs(settings, pdf_path):
    """Same document in, same ids out. This is what makes diffing revisions work."""
    from regextract.pipeline.graph import run_pipeline

    first = run_pipeline(pdf_path=pdf_path, issuer="ESMA", jurisdiction="AE", settings=settings)
    second = run_pipeline(pdf_path=pdf_path, issuer="ESMA", jurisdiction="AE", settings=settings)
    assert {i.item_id for i in first["items"]} == {i.item_id for i in second["items"]}


# --- observability ----------------------------------------------------------

def test_manifest_records_every_stage(state):
    stages = {e["stage"] for e in state["manifest"]["stages"]}
    assert {"ingest", "segment", "guardrails", "extract",
            "verify", "document_flags", "route", "publish"} <= stages


def test_manifest_declares_offline_mode_honestly(state):
    assert "offline" in state["manifest"]["settings"]["mode"]


def test_baseline_counts_add_up(items):
    baseline = baseline_comparison(items)
    assert baseline["published_without_gate"] >= baseline["published_with_gate"]
    assert baseline["candidate_items"] == len(items)
