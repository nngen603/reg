"""The checkpointer has to actually work, or 'resumable' is a claim, not a fact."""

import logging
import warnings

from regextract.pipeline.checkpointing import memory_checkpointer
from regextract.pipeline.graph import build_graph, run_pipeline


def test_pipeline_state_round_trips_through_a_checkpointer(settings, pdf_path, caplog):
    saver = memory_checkpointer()
    with warnings.catch_warnings(record=True) as caught, caplog.at_level(logging.WARNING):
        warnings.simplefilter("always")
        state = run_pipeline(pdf_path=pdf_path, issuer="ESMA", jurisdiction="AE",
                             settings=settings, checkpointer=saver)
        snapshot = build_graph(checkpointer=saver).get_state(
            {"configurable": {"thread_id": state["run_id"]}})

    restored = snapshot.values
    assert len(restored["items"]) == len(state["items"])
    assert type(restored["items"][0]).__name__ == "Item"
    assert type(restored["canonical"]).__name__ == "CanonicalDocument"

    # Every type in the state must be registered. An unregistered one is
    # deserialised with a warning today and refused in a future release.
    messages = [str(w.message) for w in caught] + [r.getMessage() for r in caplog.records]
    unregistered = [m for m in messages if "unregistered type" in m]
    assert not unregistered, unregistered


def test_live_objects_are_not_in_the_checkpointed_state(settings, pdf_path):
    saver = memory_checkpointer()
    state = run_pipeline(pdf_path=pdf_path, issuer="ESMA", jurisdiction="AE",
                         settings=settings, checkpointer=saver)
    restored = build_graph(checkpointer=saver).get_state(
        {"configurable": {"thread_id": state["run_id"]}}).values
    assert not {"settings", "gateway", "recorder"} & set(restored)
