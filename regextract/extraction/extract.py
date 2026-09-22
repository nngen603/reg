"""Stage 4 -- extract.

One call per clause, with the parent headings and the definitions section as
context. Per clause rather than per document because it gives exact offsets,
parallelism, bounded cost, and the ability to retry or evaluate one clause at a
time. Section 14 of the plan compares this against the whole-document
alternative.

When agreement is switched on, every clause is extracted twice: once with the
primary model and once with a second provider family. Two samples from one
model measure how much that model varies. Two families disagreeing is evidence.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from ..config import Settings, get_settings
from ..contract import Clause, ClauseExtractionDraft, Document
from ..llm.gateway import CallMeta, Gateway
from .prompts import SYSTEM_PROMPT, build_user_prompt


@dataclass
class ClauseDraft:
    """What the model(s) returned for one clause, before any verification."""

    clause_id: str
    runs: list[dict]                 # one payload per model run
    metas: list[CallMeta]

    @property
    def primary(self) -> dict:
        return self.runs[0] if self.runs else {}

    @property
    def secondary(self) -> dict | None:
        return self.runs[1] if len(self.runs) > 1 else None

    @property
    def primary_error(self) -> str | None:
        """Why the primary call produced nothing trustworthy, or None.

        A failed call returns an empty, schema-shaped payload so that one bad
        clause cannot crash a run. That makes the failure indistinguishable
        from a correct "this clause has no facts" answer -- failure class E --
        unless it is surfaced explicitly, which is what this is for.
        """
        if not self.metas:
            return "no model call was made"
        return self.metas[0].error

    @property
    def secondary_failed(self) -> bool:
        return len(self.metas) > 1 and self.metas[1].error is not None


def definitions_digest(clauses: list[Clause], limit: int = 1800) -> str:
    """A short block of the document's own definitions, passed as context.

    Regulatory text defines its terms and then leans on them. Without this the
    model has to guess what "Specific Requirement" or "Approved Supplier" mean.
    Truncated because it rides in every single clause prompt.
    """
    # The definitions section is found by its heading, not by its number.
    roots = {c.id for c in clauses
             if c.parent is None and "definition" in (c.heading or "").lower()}
    lines: list[str] = []
    for clause in clauses:
        if any(clause.id.startswith(root + ".") for root in roots) and clause.text:
            condensed = " ".join(clause.text.split())
            lines.append(f"  {condensed}")
    block = "\n".join(lines)
    return block[:limit]


def parent_headings(clause: Clause, by_id: dict[str, Clause]) -> list[str]:
    headings: list[str] = []
    parent_id = clause.parent
    guard = 0
    while parent_id and guard < 8:
        parent = by_id.get(parent_id)
        if not parent:
            break
        headings.insert(0, f"{parent.id} {parent.heading}".strip())
        parent_id = parent.parent
        guard += 1
    return headings


def extract_clause(
    *,
    gateway: Gateway,
    document: Document,
    clause: Clause,
    parents: list[str],
    definitions: str,
    settings: Settings,
    inject_faults: bool = False,
) -> ClauseDraft:
    user_prompt = build_user_prompt(
        document_title=document.title,
        document_id=document.id,
        issuer=document.issuer,
        jurisdiction=document.jurisdiction,
        parent_headings=parents,
        definitions_digest=definitions,
        clause_id=clause.id,
        clause_heading=clause.heading,
        clause_text=clause.text,
    )

    routes = ["primary"]
    if settings.agreement_enabled:
        routes.append("second")

    runs: list[dict] = []
    metas: list[CallMeta] = []
    for variant, route in enumerate(routes):
        payload, meta = gateway.structured(
            system=SYSTEM_PROMPT,
            user=user_prompt,
            response_model=ClauseExtractionDraft,
            route=route,
            tag=f"{document.id}:{clause.id}:run{variant}",
            stub_context={
                "clause_id": clause.id,
                "clause_text": clause.text,
                "heading": clause.heading,
                "variant": variant,
                "inject_faults": inject_faults,
            },
        )
        runs.append(payload)
        metas.append(meta)

    return ClauseDraft(clause_id=clause.id, runs=runs, metas=metas)


def extract_document(
    *,
    gateway: Gateway,
    document: Document,
    clauses: list[Clause],
    settings: Settings | None = None,
    inject_faults: bool = False,
) -> list[ClauseDraft]:
    settings = settings or get_settings()
    by_id = {c.id: c for c in clauses}
    definitions = definitions_digest(clauses)

    def work(clause: Clause) -> ClauseDraft:
        return extract_clause(
            gateway=gateway,
            document=document,
            clause=clause,
            parents=parent_headings(clause, by_id),
            definitions=definitions,
            settings=settings,
            inject_faults=inject_faults,
        )

    # Offline work is pure CPU, so threads would only add overhead.
    if settings.offline or settings.llm_concurrency <= 1:
        return [work(clause) for clause in clauses]

    with ThreadPoolExecutor(max_workers=settings.llm_concurrency) as pool:
        return list(pool.map(work, clauses))
