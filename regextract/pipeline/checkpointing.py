"""Checkpointers for the pipeline graph.

LangGraph serialises every value it checkpoints. It is also moving towards
refusing to deserialise any class it has not been told about, which is the
right default: a checkpoint store is an input, and rebuilding arbitrary types
from an input is how deserialisation attacks work. So every type the pipeline
keeps in its state is listed here, once, explicitly.

If a new type is added to the pipeline state and not to this list, the
checkpoint test fails and names it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer


def state_types() -> list[type]:
    from .. import contract
    from ..document.ingest import CanonicalDocument, Page
    from ..extraction.extract import ClauseDraft
    from ..guardrails.rails import GuardrailFinding
    from ..llm.gateway import CallMeta

    return [
        contract.Document, contract.Clause, contract.Item, contract.Evidence,
        contract.Provenance, contract.ReviewSignal, contract.DocumentFlag,
        contract.ExtractionRun, contract.Resolution, contract.ClauseType,
        contract.Decision, contract.ImpactTier, contract.MatchKind,
        CanonicalDocument, Page, ClauseDraft, CallMeta, GuardrailFinding,
    ]


def serializer() -> JsonPlusSerializer:
    allowed = [(t.__module__, t.__name__) for t in state_types()]
    return JsonPlusSerializer(allowed_msgpack_modules=allowed)


def memory_checkpointer() -> InMemorySaver:
    """In-process. Enough for tests and for a run that pauses and resumes in
    the same Python process."""
    return InMemorySaver(serde=serializer())


def sqlite_checkpointer(path: str | Path):
    """On disk. What lets a run pause for human review in one process and be
    resumed by `run.py review` in another. Postgres in production."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), check_same_thread=False)
    return SqliteSaver(connection, serde=serializer())
