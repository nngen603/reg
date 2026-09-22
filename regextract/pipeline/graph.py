"""The pipeline, as a LangGraph state graph.

Why a graph rather than one function that calls each step in turn:

  * every stage is a named node with typed state in and out, so a failure is
    attributable to a stage rather than to "the pipeline"
  * the conditional edges are real. An empty document skips extraction. The
    judge only runs when it is switched on. A document with blocking flags
    skips triage, because ordering a queue that is going to be fully reviewed
    anyway is wasted money
  * with a checkpointer the run is resumable and each node's state can be
    inspected after the fact, which is what you want when a 300-page document
    fails at step 5 of 10
  * a run can stop and wait for a person. In review mode the graph pauses at
    `human_review` (interrupt), the pause is checkpointed, and
    `resume_pipeline` continues it -- in another process if the checkpointer
    is on disk -- with the reviewer's decisions

The state holds data only. The live objects a run needs -- settings, the model
gateway, the run recorder -- travel in LangGraph's runtime context, which is
never checkpointed. Keeping them in the state looks simpler and breaks the
checkpointer outright: a model client is not serialisable.

The nodes themselves are plain functions from the other modules. The graph
adds orchestration, not logic -- that separation is what keeps the stages
unit-testable on their own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt

from ..config import Settings, get_settings
from ..contract import Decision, Document, DocumentFlag, ExtractionRun
from ..document.ingest import ingest_pdf
from ..document.segment import segment, structure_report
from ..extraction.extract import extract_document
from ..guardrails import classify_clauses, scan_clauses
from ..llm import Gateway
from ..publishing.publish import published_items, write_outputs
from ..publishing.publog import PublishLog
from ..resolution.retrieval import resolve_references
from ..review.review import append_gold_corrections, apply_decisions, parse_decisions, review_request
from ..routing.judge import score_priority
from ..routing.route import BLOCKING_FLAG_CODES, apply_document_flags, route, routing_summary
from ..verification.verify import document_flags, verify
from .checkpointing import memory_checkpointer, sqlite_checkpointer
from .observability import RunRecorder


@dataclass
class Services:
    """Runtime context: what every node needs and no checkpoint may hold."""

    settings: Settings
    gateway: Gateway
    recorder: RunRecorder


class PipelineState(TypedDict, total=False):
    # inputs
    pdf_path: str
    issuer: str
    jurisdiction: str
    inject_faults: bool
    run_id: str
    review_mode: bool

    # stage outputs
    canonical: Any
    document: Document
    clauses: list
    structure: dict
    guardrail_findings: list
    drafts: list
    items: list
    flags: list
    run_judge: bool
    model_calls: dict
    resolution_stats: dict
    review_events: list
    written: dict
    run: ExtractionRun


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def node_ingest(state: PipelineState, runtime: Runtime[Services]) -> dict:
    with runtime.context.recorder.stage("ingest") as event:
        canonical = ingest_pdf(
            state["pdf_path"],
            issuer=state.get("issuer", ""),
            jurisdiction=state.get("jurisdiction", ""),
        )
        event.counters = {
            "pages": len(canonical.pages),
            "chars": len(canonical.text),
            "artefacts_repaired": canonical.artefact_fixes,
            "furniture_lines_removed": len(canonical.removed_lines),
        }
    return {"canonical": canonical, "document": canonical.document}


def node_segment(state: PipelineState, runtime: Runtime[Services]) -> dict:
    with runtime.context.recorder.stage("segment") as event:
        clauses = segment(state["canonical"])
        structure = structure_report(clauses)
        event.counters = structure
    return {"clauses": clauses, "structure": structure}


def node_guard(state: PipelineState, runtime: Runtime[Services]) -> dict:
    """Input guardrails on untrusted document text, before it enters a prompt.

    The deterministic scanner always runs; the policy classifier runs when
    switched on. Any clause either layer flags is forced to review at route.
    """
    recorder = runtime.context.recorder
    with recorder.stage("guardrails") as event:
        deterministic = scan_clauses(state["clauses"])
        classifier = classify_clauses(
            gateway=runtime.context.gateway,
            clauses=state["clauses"],
            document_id=state["document"].id,
            settings=runtime.context.settings,
        )
        findings = deterministic + classifier.findings
        event.counters = {
            "deterministic_findings": len(deterministic),
            "classifier": classifier.status,
            "classifier_findings": len(classifier.findings),
            "clauses_screened": classifier.screened,
            "high_severity": sum(1 for f in findings if f.severity == "high"),
        }
        if findings:
            recorder.note(
                f"guardrails flagged {len(findings)} clause(s) containing "
                f"injection-shaped text; those clauses are forced to review"
            )
    return {"guardrail_findings": findings}


def node_extract(state: PipelineState, runtime: Runtime[Services]) -> dict:
    with runtime.context.recorder.stage("extract") as event:
        drafts = extract_document(
            gateway=runtime.context.gateway,
            document=state["document"],
            clauses=state["clauses"],
            settings=runtime.context.settings,
            inject_faults=state.get("inject_faults", False),
        )
        event.counters = {
            "clauses_extracted": len(drafts),
            "model_calls": sum(len(d.metas) for d in drafts),
        }
    return {"drafts": drafts}


def node_verify(state: PipelineState, runtime: Runtime[Services]) -> dict:
    with runtime.context.recorder.stage("verify") as event:
        items = verify(
            drafts=state["drafts"],
            clauses=state["clauses"],
            canonical=state["canonical"],
            document=state["document"],
            run_id=state["run_id"],
            settings=runtime.context.settings,
        )
        grounded = sum(1 for i in items if i.evidence.grounded)
        event.counters = {
            "items": len(items),
            "grounded": grounded,
            "ungrounded": len(items) - grounded,
            "grounding_rate": round(grounded / len(items), 4) if items else 0.0,
        }
    return {"items": items}


def node_resolve(state: PipelineState, runtime: Runtime[Services]) -> dict:
    """Link external references to documents already in the corpus, or say
    NOT_FOUND. Information for downstream, not a routing signal: most
    references point at documents we do not hold, and that is normal."""
    settings = runtime.context.settings
    with runtime.context.recorder.stage("resolve") as event:
        if settings.use_resolution:
            items, counters = resolve_references(state["items"], settings)
        else:
            items, counters = state["items"], {"enabled": False}
        event.counters = counters
    return {"items": items, "resolution_stats": counters}


def node_flag(state: PipelineState, runtime: Runtime[Services]) -> dict:
    with runtime.context.recorder.stage("document_flags") as event:
        flags = document_flags(
            items=state["items"],
            canonical=state["canonical"],
            structure=state["structure"],
            settings=runtime.context.settings,
            drafts=state.get("drafts"),
        )
        # Injection findings are a document-level trust problem too.
        for finding in state.get("guardrail_findings", []) or []:
            if finding.severity == "high":
                flags.append(DocumentFlag(
                    code="prompt_injection_suspected",
                    detail=f"clause {finding.clause_id}: {finding.rule}",
                    clause_ids=[finding.clause_id],
                ))
        event.counters = {"flags": [f.code for f in flags]}
    return {"flags": flags}


def node_route(state: PipelineState, runtime: Runtime[Services]) -> dict:
    settings = runtime.context.settings
    recorder = runtime.context.recorder
    with recorder.stage("route") as event:
        items = route(state["items"], settings)
        items = apply_document_flags(items, state["flags"])
        # Any clause the guardrail flagged is forced to review regardless of
        # score, and the reason names which layer and rule fired.
        flagged: dict[str, set[str]] = {}
        for finding in state.get("guardrail_findings") or []:
            flagged.setdefault(finding.clause_id, set()).add(finding.rule)
        for item in items:
            if item.clause_id in flagged:
                item.review.decision = Decision.REVIEW
                rules = ",".join(sorted(flagged[item.clause_id]))
                item.review.reasons.append(f"guardrail_flagged_clause:{rules}")
        event.counters = routing_summary(items)
    return {
        "items": items,
        "run_judge": _should_judge(settings, state["flags"], recorder),
        "model_calls": runtime.context.gateway.stats.summary(),
    }


def _should_judge(settings: Settings, flags: list, recorder: RunRecorder) -> bool:
    if not settings.use_judge:
        return False
    # Ordering a queue that is going to be reviewed in full anyway is money
    # spent to sort something nobody will act on differently.
    if any(f.code in BLOCKING_FLAG_CODES for f in flags):
        recorder.note("judge skipped: document already fully flagged for review")
        return False
    return True


def node_judge(state: PipelineState, runtime: Runtime[Services]) -> dict:
    with runtime.context.recorder.stage("judge") as event:
        items = score_priority(
            gateway=runtime.context.gateway,
            items=state["items"],
            clauses=state["clauses"],
            settings=runtime.context.settings,
        )
        scored = sum(1 for i in items if i.review.judge_priority is not None)
        event.counters = {"items_prioritised": scored, "note": "triage only, never a gate"}
    return {"items": items, "model_calls": runtime.context.gateway.stats.summary()}


def node_human_review(state: PipelineState, runtime: Runtime[Services]) -> dict:
    """Wait for a person. LangGraph re-runs this node from the top when the
    run resumes, so nothing before interrupt() may have a side effect."""
    answer = interrupt(review_request(state["items"], state["run_id"]))

    settings = runtime.context.settings
    recorder = runtime.context.recorder
    with recorder.stage("human_review") as event:
        decisions, problems = parse_decisions((answer or {}).get("decisions", []))
        outcome = apply_decisions(
            items=state["items"], decisions=decisions, clauses=state["clauses"],
            canonical=state["canonical"], document_id=state["document"].id,
            run_id=state["run_id"], fuzzy_threshold=settings.fuzzy_threshold,
        )
        append_gold_corrections(settings.gold_dir / "reviewer_corrections.jsonl",
                                outcome.corrections)
        problems += outcome.problems
        for problem in problems:
            recorder.note(f"review: {problem}")
        actions = [e["action"] for e in outcome.events]
        event.counters = {
            "decisions": len(decisions),
            "accepted": actions.count("accept"),
            "rejected": actions.count("reject"),
            "corrected": actions.count("correct"),
            "problems": len(problems),
            "still_pending": sum(1 for i in outcome.items if i.review.decision is Decision.REVIEW),
        }
    return {"items": outcome.items, "review_events": outcome.events}


def node_publish(state: PipelineState, runtime: Runtime[Services]) -> dict:
    settings = runtime.context.settings
    items = state.get("items", [])
    with runtime.context.recorder.stage("publish") as event:
        run = ExtractionRun(
            document=state["document"],
            clauses=state.get("clauses", []),
            items=items,
            flags=state.get("flags", []),
            stats={
                "structure": state.get("structure", {}),
                "routing": routing_summary(items),
                "model_calls": _model_calls(state, runtime),
                "guardrails": [f.as_dict() for f in (state.get("guardrail_findings") or [])],
                "resolution": state.get("resolution_stats", {}),
            },
        )
        written = write_outputs(run, settings.output_dir)
        published = published_items(run)
        log_stats = PublishLog(settings.output_dir / "publish_log.jsonl").record(
            published=published, document=run.document, run_id=state["run_id"],
            review_events=state.get("review_events") or [],
        )
        event.counters = {
            "published_items": len(published),
            "held_for_review": sum(1 for i in items if i.review.decision is Decision.REVIEW),
            "publish_log": log_stats,
            "files": {k: str(v.name) for k, v in written.items()},
        }
    return {"run": run, "written": written}


def _model_calls(state: PipelineState, runtime: Runtime[Services]) -> dict:
    """This process's calls, or -- after a resume, when this process made
    none -- the snapshot taken before the pause."""
    live = runtime.context.gateway.stats.summary()
    return live if live.get("calls") else state.get("model_calls", live)


# ---------------------------------------------------------------------------
# Conditional edges
# ---------------------------------------------------------------------------


def has_clauses(state: PipelineState) -> str:
    return "extract" if state.get("clauses") else "publish_empty"


def after_route(state: PipelineState) -> str:
    return "judge" if state.get("run_judge") else review_or_publish(state)


def review_or_publish(state: PipelineState) -> str:
    waiting = any(i.review.decision is Decision.REVIEW for i in state.get("items", []))
    return "human_review" if state.get("review_mode") and waiting else "publish"


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_graph(checkpointer=None):
    graph = StateGraph(PipelineState, context_schema=Services)

    graph.add_node("ingest", node_ingest)
    graph.add_node("segment", node_segment)
    graph.add_node("guardrails", node_guard)
    graph.add_node("extract", node_extract)
    graph.add_node("verify", node_verify)
    graph.add_node("resolve", node_resolve)
    graph.add_node("document_flags", node_flag)
    graph.add_node("route", node_route)
    graph.add_node("judge", node_judge)
    graph.add_node("human_review", node_human_review)
    graph.add_node("publish", node_publish)
    graph.add_node("publish_empty", node_publish)

    graph.add_edge(START, "ingest")
    graph.add_edge("ingest", "segment")
    graph.add_edge("segment", "guardrails")
    graph.add_conditional_edges("guardrails", has_clauses,
                                {"extract": "extract", "publish_empty": "publish_empty"})
    graph.add_edge("extract", "verify")
    graph.add_edge("verify", "resolve")
    graph.add_edge("resolve", "document_flags")
    graph.add_edge("document_flags", "route")
    graph.add_conditional_edges("route", after_route,
                                {"judge": "judge", "human_review": "human_review", "publish": "publish"})
    graph.add_conditional_edges("judge", review_or_publish,
                                {"human_review": "human_review", "publish": "publish"})
    graph.add_edge("human_review", "publish")
    graph.add_edge("publish", END)
    graph.add_edge("publish_empty", END)

    return graph.compile(checkpointer=checkpointer)


def run_pipeline(
    *,
    pdf_path: str,
    issuer: str = "",
    jurisdiction: str = "",
    settings: Settings | None = None,
    inject_faults: bool = False,
    checkpointer=None,
    gateway: Gateway | None = None,
) -> PipelineState:
    settings = settings or get_settings()
    recorder = RunRecorder()
    services = Services(settings=settings, gateway=gateway or Gateway(settings), recorder=recorder)
    if settings.review_mode and checkpointer is None:
        # interrupt() needs somewhere to keep the paused run.
        checkpointer = memory_checkpointer()

    app = build_graph(checkpointer=checkpointer)
    initial: PipelineState = {
        "pdf_path": pdf_path,
        "issuer": issuer,
        "jurisdiction": jurisdiction,
        "inject_faults": inject_faults,
        "run_id": recorder.run_id,
        "review_mode": settings.review_mode,
    }
    config = {"configurable": {"thread_id": recorder.run_id}} if checkpointer else {}
    final = dict(app.invoke(initial, config=config, context=services))
    return _finish(final, services, checkpointer)


def resume_pipeline(
    *,
    run_id: str,
    decisions: list[dict],
    settings: Settings | None = None,
    checkpointer=None,
    gateway: Gateway | None = None,
) -> PipelineState:
    """Continue a run paused at human_review, with a reviewer's decisions.

    With an on-disk checkpointer this works across processes: the run paused
    in `run.py extract --review`, and `run.py review` resumes it.
    """
    settings = settings or get_settings()
    checkpointer = checkpointer or sqlite_checkpointer(settings.checkpoint_path)
    app = build_graph(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": run_id}}
    if not app.get_state(config).next:
        raise ValueError(f"run {run_id} is not waiting for review")

    manifest_path = settings.output_dir / "run_manifest.json"
    previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
    services = Services(settings=settings, gateway=gateway or Gateway(settings),
                        recorder=RunRecorder(run_id=run_id))
    final = dict(app.invoke(Command(resume={"decisions": decisions}), config=config, context=services))
    return _finish(final, services, checkpointer, previous)


def _finish(final: dict, services: Services, checkpointer, previous: dict | None = None) -> PipelineState:
    settings, recorder = services.settings, services.recorder
    interrupts = final.pop("__interrupt__", None)

    calls = services.gateway.stats.summary()
    if not calls.get("calls"):
        calls = final.get("model_calls", calls)
    manifest = recorder.manifest(
        settings_describe=settings.describe(),
        gateway_summary=calls,
        routing=routing_summary(final.get("items", [])),
        structure=final.get("structure", {}),
        flags=final.get("flags", []),
    )
    if previous and previous.get("run_id") == recorder.run_id:
        # One run, two processes: keep the history from before the pause.
        manifest["stages"] = previous.get("stages", []) + manifest["stages"]
        manifest["notes"] = previous.get("notes", []) + manifest["notes"]
        manifest["total_ms"] = round(previous.get("total_ms", 0) + manifest["total_ms"], 1)

    manifest["status"] = "paused_for_review" if interrupts else "completed"
    if interrupts:
        final["review_request"] = interrupts[0].value
        manifest["pending_review"] = final["review_request"]["pending"]
    recorder.write(settings.output_dir / "run_manifest.json", manifest)

    # Handed back to the caller alongside the data -- never checkpointed.
    final.update(settings=settings, gateway=services.gateway, recorder=recorder,
                 manifest=manifest, checkpointer=checkpointer)
    return final  # type: ignore[return-value]
