"""Human review as a real graph step, and the hash-chained publish log."""

import json

import pytest

from regextract.config import Settings
from regextract.contract import Decision
from regextract.pipeline.checkpointing import memory_checkpointer, sqlite_checkpointer
from regextract.pipeline.graph import resume_pipeline, run_pipeline
from regextract.publishing.publish import published_items
from regextract.publishing.publog import verify_log


def reviewing(root):
    return Settings(output_dir=root / "out", gold_dir=root / "gold", REGEXTRACT_REVIEW=True)


def pause(pdf_path, settings, saver):
    return run_pipeline(pdf_path=pdf_path, issuer="ESMA", jurisdiction="AE",
                        settings=settings, checkpointer=saver)


def pending_items(state):
    return [i for i in state["items"] if i.review.decision is Decision.REVIEW]


# --- the pause ---------------------------------------------------------------

@pytest.fixture(scope="module")
def reviewed(pdf_path, tmp_path_factory):
    """One run paused for review, then resumed with one of each decision."""
    root = tmp_path_factory.mktemp("review")
    settings, saver = reviewing(root), memory_checkpointer()
    paused = pause(pdf_path, settings, saver)

    pending = pending_items(paused)
    entity = next(i for i in pending if i.kind == "entity")
    accept, reject = [i for i in pending if i is not entity][:2]
    decisions = [
        {"item_id": accept.item_id, "action": "accept", "reviewer": "analyst-a"},
        {"item_id": reject.item_id, "action": "reject", "reviewer": "analyst-a", "note": "misread"},
        {"item_id": entity.item_id, "action": "correct", "reviewer": "analyst-b",
         "payload": {"normalized": "reviewed value"}},
    ]
    final = resume_pipeline(run_id=paused["run_id"], decisions=decisions,
                            settings=settings, checkpointer=saver)
    return {"settings": settings, "paused": paused, "final": final,
            "ids": {"accept": accept.item_id, "reject": reject.item_id, "correct": entity.item_id}}


def test_the_run_pauses_with_the_queue_as_its_question(reviewed):
    paused = reviewed["paused"]
    assert paused["manifest"]["status"] == "paused_for_review"
    assert paused["review_request"]["pending"] == len(pending_items(paused)) > 0
    assert "run" not in paused              # nothing is published while it waits


def test_accept_and_reject_are_applied_and_stamped(reviewed):
    final, ids = reviewed["final"], reviewed["ids"]
    by_id = {i.item_id: i for i in final["items"]}
    assert by_id[ids["accept"]].review.decision is Decision.ACCEPTED
    assert by_id[ids["accept"]].review.reviewer == "analyst-a"
    assert by_id[ids["reject"]].review.decision is Decision.REJECT

    published = {i.item_id for i in published_items(final["run"])}
    assert ids["accept"] in published
    assert ids["reject"] not in published


def test_a_correction_is_a_new_version_and_the_original_is_kept(reviewed):
    final, original_id = reviewed["final"], reviewed["ids"]["correct"]
    by_id = {i.item_id: i for i in final["items"]}
    assert by_id[original_id].review.decision is Decision.SUPERSEDED

    corrected = next(i for i in final["items"] if i.supersedes == original_id)
    assert corrected.payload["normalized"] == "reviewed value"
    assert corrected.review.decision is Decision.ACCEPTED
    assert corrected.evidence.grounded
    assert corrected.item_id in {i.item_id for i in published_items(final["run"])}

    gold = reviewed["settings"].gold_dir / "reviewer_corrections.jsonl"
    assert len(gold.read_text(encoding="utf-8").splitlines()) == 1


def test_the_resumed_run_completes_with_its_whole_history(reviewed):
    manifest = reviewed["final"]["manifest"]
    stages = [e["stage"] for e in manifest["stages"]]
    assert manifest["status"] == "completed"
    assert stages.index("extract") < stages.index("human_review") < stages.index("publish")


def test_reviewer_decisions_are_in_the_publish_log(reviewed):
    log = reviewed["settings"].output_dir / "publish_log.jsonl"
    events = [json.loads(line)["event"] for line in log.read_text(encoding="utf-8").splitlines()]
    assert events.count("review_decision") == 3
    assert verify_log(log)["valid"]


def test_a_correction_whose_quote_is_not_in_the_source_is_refused(pdf_path, tmp_path):
    settings, saver = reviewing(tmp_path), memory_checkpointer()
    paused = pause(pdf_path, settings, saver)
    target = pending_items(paused)[0]
    final = resume_pipeline(run_id=paused["run_id"], settings=settings, checkpointer=saver, decisions=[{
        "item_id": target.item_id, "action": "correct", "reviewer": "analyst-a",
        "payload": {"quote": "A penalty of AED 50,000 shall be imposed."}}])

    item = next(i for i in final["items"] if i.item_id == target.item_id)
    assert item.review.decision is Decision.REVIEW
    assert any(r.startswith("correction_not_grounded") for r in item.review.reasons)
    assert item.item_id not in {i.item_id for i in published_items(final["run"])}


def test_the_pause_survives_the_process_ending(pdf_path, tmp_path):
    settings = reviewing(tmp_path)
    database = tmp_path / "checkpoints.sqlite"
    paused = pause(pdf_path, settings, sqlite_checkpointer(database))

    # A new connection to the same file stands in for a new process.
    target = pending_items(paused)[0]
    final = resume_pipeline(run_id=paused["run_id"], settings=settings,
                            checkpointer=sqlite_checkpointer(database),
                            decisions=[{"item_id": target.item_id, "action": "accept",
                                        "reviewer": "analyst-a"}])
    assert final["manifest"]["status"] == "completed"
    assert target.item_id in {i.item_id for i in published_items(final["run"])}


def test_resuming_a_run_that_is_not_waiting_is_refused(tmp_path):
    with pytest.raises(ValueError):
        resume_pipeline(run_id="no-such-run", decisions=[], settings=reviewing(tmp_path),
                        checkpointer=memory_checkpointer())


# --- the publish log ---------------------------------------------------------

@pytest.fixture
def logged_twice(pdf_path, tmp_path):
    settings = Settings(output_dir=tmp_path)
    runs = [run_pipeline(pdf_path=pdf_path, issuer="ESMA", jurisdiction="AE", settings=settings)
            for _ in range(2)]
    return settings.output_dir / "publish_log.jsonl", runs


def _log_stats(state):
    publish = next(e for e in state["manifest"]["stages"] if e["stage"] == "publish")
    return publish["counters"]["publish_log"]


def test_rerunning_an_unchanged_document_publishes_nothing_new(logged_twice):
    _, (first, second) = logged_twice
    assert _log_stats(first)["published_new"] > 0
    assert _log_stats(second)["published_new"] == 0
    assert _log_stats(second)["published_unchanged"] == _log_stats(first)["published_new"]


def test_an_edited_entry_is_detected(logged_twice):
    log, _ = logged_twice
    lines = log.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[3])
    entry["body"]["decision"] = "reject"
    lines[3] = json.dumps(entry)
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = verify_log(log)
    assert not report["valid"]
    assert report["breaks"][0]["line"] == 4
    assert "edited" in report["breaks"][0]["reason"]


def test_a_deleted_entry_breaks_the_chain(logged_twice):
    log, _ = logged_twice
    lines = log.read_text(encoding="utf-8").splitlines()
    del lines[5]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = verify_log(log)
    assert not report["valid"]
    assert "inserted, deleted or reordered" in report["breaks"][0]["reason"]
