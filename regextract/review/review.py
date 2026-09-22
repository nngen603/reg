"""Human review, as a real step in the graph.

Items the router could not auto-accept wait for a person. With review mode on,
the pipeline pauses at the `human_review` node -- LangGraph's interrupt(),
checkpointed, so the pause survives the process ending -- and resumes when a
reviewer's decisions arrive. It uses LangGraph's
interrupt() and Command(resume=...), not a polling loop, and not a CSV that
someone has to remember to read back in.

Three decisions, and one rule that applies to all of them:

  accept    the item publishes as extracted, marked as accepted by a person
  reject    it never publishes
  correct   a new version replaces it; the original is kept, marked superseded

The rule: a reviewer cannot publish unsupported evidence either. A corrected
quote goes through the same grounding gate as a model's. Corrections are
also appended to the gold set, because reviewer corrections are how the gold
set grows (plan, section 12.3).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from ..contract import Decision, Evidence, Item
from ..normalize import locate


class ReviewDecision(BaseModel):
    item_id: str
    action: Literal["accept", "reject", "correct"]
    reviewer: str = Field(min_length=1)
    note: str = ""
    payload: dict = Field(default_factory=dict,
                          description="For `correct` only: the fields that change")


def review_request(items: list[Item], run_id: str) -> dict:
    """What the pause hands to the outside world: what a reviewer needs to
    decide, most urgent first, and how to answer."""
    from ..routing.route import review_queue

    pending = review_queue(items)
    return {
        "run_id": run_id,
        "pending": len(pending),
        "items": [
            {
                "item_id": i.item_id,
                "impact": i.review.impact.value,
                "clause_id": i.clause_id,
                "kind": i.kind,
                "type": i.type_name,
                "reasons": i.review.reasons,
                "quote": i.evidence.quote,
                "payload": i.payload,
            }
            for i in pending
        ],
        "resume_with": f"python run.py review --thread {run_id} --decisions decisions.json",
    }


@dataclass
class ReviewOutcome:
    items: list[Item]
    events: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    corrections: list[dict] = field(default_factory=list)


def parse_decisions(raw: list) -> tuple[list[ReviewDecision], list[str]]:
    decisions: list[ReviewDecision] = []
    problems: list[str] = []
    for index, entry in enumerate(raw or []):
        try:
            decisions.append(ReviewDecision.model_validate(entry))
        except ValidationError as exc:
            problems.append(f"decision #{index + 1} is invalid: {exc.errors()[0]['msg']}")
    return decisions, problems


def apply_decisions(*, items: list[Item], decisions: list[ReviewDecision], clauses: list,
                    canonical, document_id: str, run_id: str,
                    fuzzy_threshold: float) -> ReviewOutcome:
    by_id = {i.item_id: i for i in items}
    clause_by_id = {c.id: c for c in clauses}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    outcome = ReviewOutcome(items=list(items))

    for decision in decisions:
        item = by_id.get(decision.item_id)
        if item is None:
            outcome.problems.append(f"{decision.item_id}: no such item in this run")
            continue
        if item.review.decision is not Decision.REVIEW:
            outcome.problems.append(
                f"{decision.item_id}: not pending review (it is {item.review.decision.value})")
            continue

        if decision.action == "accept" and not item.evidence.grounded:
            # A person cannot publish unsupported evidence either: correct the
            # quote from the clause text, or reject the fact.
            problem = ("accept_refused_not_grounded: the quote is not in the clause text; "
                       "correct it with a quote from the clause, or reject it")
            item.review.reasons.append(problem)
            outcome.problems.append(f"{decision.item_id}: {problem}")
            continue

        new_item_id = None
        if decision.action == "correct":
            corrected, problem = _corrected(item, decision, clause_by_id.get(item.clause_id),
                                            canonical, document_id, fuzzy_threshold, now)
            if problem:
                # A refused correction is not a rejection: the item stays
                # pending, and the reason is on it for the next reviewer.
                item.review.reasons.append(problem)
                outcome.problems.append(f"{decision.item_id}: {problem}")
                continue
            item.review.decision = Decision.SUPERSEDED
            outcome.items.append(corrected)
            new_item_id = corrected.item_id
            outcome.corrections.append({
                "document_id": document_id, "clause_id": item.clause_id, "kind": item.kind,
                "type": item.type_name, "before": item.payload, "after": corrected.payload,
                "reviewer": decision.reviewer, "note": decision.note, "decided_at": now,
                "run_id": run_id,
            })
        else:
            item.review.decision = Decision.ACCEPTED if decision.action == "accept" else Decision.REJECT

        _stamp(item, decision, now)
        outcome.events.append({
            "item_id": item.item_id, "action": decision.action, "reviewer": decision.reviewer,
            "note": decision.note, "decided_at": now, "run_id": run_id,
            "new_item_id": new_item_id,
        })
    return outcome


def _stamp(item: Item, decision: ReviewDecision, now: str) -> None:
    item.review.reviewer = decision.reviewer
    item.review.reviewed_at = now
    item.review.review_note = decision.note


def _corrected(item: Item, decision: ReviewDecision, clause, canonical, document_id: str,
               fuzzy_threshold: float, now: str) -> tuple[Item | None, str | None]:
    payload = {**item.payload, **decision.payload}
    if payload == item.payload:
        return None, "correction changes nothing; use accept"
    if clause is None:
        return None, "correction refused: the clause is not in this run"

    quote = payload.get("quote") or item.evidence.quote
    location = locate(quote, clause.text, fuzzy_threshold)
    if not location.found:
        return None, "correction_not_grounded: the corrected quote is not in the clause text"

    start = clause.char_start + (location.char_start or 0)
    corrected = item.model_copy(deep=True)
    corrected.item_id = Item.make_id(document_id, item.clause_id, item.kind, payload)
    corrected.payload = payload
    corrected.supersedes = item.item_id
    corrected.evidence = Evidence(
        clause_id=clause.id, quote=quote, grounded=True,
        match_kind=location.kind, match_score=location.score,
        char_start=start, char_end=clause.char_start + (location.char_end or 0),
        page=canonical.page_for_offset(start),
    )
    corrected.review.decision = Decision.ACCEPTED
    corrected.review.reasons = ["reviewer_correction"]
    corrected.provenance.model = f"reviewer:{decision.reviewer}"
    corrected.provenance.model_version = ""
    _stamp(corrected, decision, now)
    return corrected, None


def append_gold_corrections(path: Path, corrections: list[dict]) -> None:
    if not corrections:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for correction in corrections:
            handle.write(json.dumps(correction, ensure_ascii=False, default=str) + "\n")
