"""Stage 5 -- verify.

Trust is checked here, not requested in the prompt. Three things happen:

  1. The grounding gate. Every quote must be locatable in the clause's own
     source text. Offsets are computed by us from that match, never taken from
     the model. If the quote cannot be found the item is marked ungrounded and
     can never be published, whatever confidence was attached to it.

  2. Validators. Cheap, deterministic checks per fact type -- does the standard
     reference look like a standard reference, does an internal cross-reference
     point at a clause that actually exists, does a monetary value have a
     currency next to it.

  3. Agreement. Where two model families both ran, do they say the same thing
     after normalisation.

Document-level checks run at the end. If the tree looks wrong or the same term
is defined two different ways, we stop trusting item-level scores for that
document and send the whole thing to review.
"""

from __future__ import annotations

import re
from datetime import date

from ..config import PROMPT_VERSION, TAXONOMY_VERSION, Settings, get_settings
from ..contract import (
    Clause,
    Document,
    DocumentFlag,
    Evidence,
    Item,
    MatchKind,
    Provenance,
    ReviewSignal,
    impact_tier,
)
from ..document.ingest import CanonicalDocument
from ..extraction.extract import ClauseDraft
from ..normalize import locate, normalize_ws, values_agree

STANDARD_PATTERN = re.compile(
    r"(IEC|BS|EN|ISO|GSO|UAE\.?\s?S?)\s?\d+(-\d+)*(\s*:\s*\d{4})?", re.I
)
CURRENCY_PATTERN = re.compile(
    r"\b(AED|USD|EUR|GBP|SAR|dirham|dollar|euro|pound)s?\b|[$€£]", re.I
)
NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")
ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


# ---------------------------------------------------------------------------
# Signatures -- used for agreement and for stable ids
# ---------------------------------------------------------------------------


def signature(kind: str, payload: dict) -> str:
    if kind == "obligation":
        return "|".join([
            normalize_ws(payload.get("subject", "")),
            payload.get("modality", ""),
            normalize_ws(payload.get("action", ""))[:80],
        ])
    if kind == "entity":
        return "|".join([payload.get("type", ""), normalize_ws(payload.get("text", ""))])
    return "|".join([
        payload.get("scope", ""),
        normalize_ws(payload.get("target_clause_id", "")),
        normalize_ws(payload.get("target_document", "")),
    ])


def _agreement(kind: str, payload: dict, other_run: dict | None) -> float:
    """1.0 if the other run produced the same fact, else 0.0.

    Returns 0.5 when there was no second run at all -- the signal is simply
    unavailable, which is different from the two runs disagreeing.
    """
    if other_run is None:
        return 0.5
    group = {"obligation": "obligations", "entity": "entities",
             "cross_reference": "cross_references"}[kind]
    target = signature(kind, payload)
    for candidate in other_run.get(group, []) or []:
        if signature(kind, candidate) == target:
            return 1.0
        if kind == "entity" and candidate.get("type") == payload.get("type"):
            if values_agree(candidate.get("text", ""), payload.get("text", "")):
                return 1.0
    return 0.0


_GROUPS = (("obligation", "obligations"), ("entity", "entities"),
           ("cross_reference", "cross_references"))


def facts_missed_by_primary(primary: dict, secondary: dict) -> list[tuple[str, dict]]:
    """Facts the second family found and the primary did not."""
    return [(kind, payload)
            for kind, group in _GROUPS
            for payload in (secondary.get(group) or [])
            if _agreement(kind, payload, primary) != 1.0]


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def run_validators(kind: str, payload: dict, evidence: Evidence,
                   clause_ids: set[str]) -> dict[str, bool]:
    checks: dict[str, bool] = {"evidence_grounded": evidence.grounded}
    quote = evidence.quote or ""

    if kind == "entity":
        text = payload.get("text", "")
        entity_type = payload.get("type", "")
        checks["entity_text_in_quote"] = normalize_ws(text) in normalize_ws(quote)

        if entity_type == "standard_reference":
            checks["standard_pattern"] = bool(STANDARD_PATTERN.search(text))
        if entity_type == "monetary_threshold":
            # A monetary value with no currency anywhere near it is almost
            # always a misread number. Failure class D.
            checks["monetary_has_currency"] = bool(CURRENCY_PATTERN.search(quote))
        if entity_type == "numeric_threshold":
            numbers = NUMBER_PATTERN.findall(text)
            checks["numeric_value_in_quote"] = all(n in quote for n in numbers) if numbers else False
        if entity_type == "time_period":
            normalized = payload.get("normalized", "")
            checks["time_period_normalised"] = bool(re.match(r"^\d+\s+\w+$", normalized.strip()))
        if entity_type == "date":
            normalized = payload.get("normalized", "")
            checks["date_parses"] = bool(ISO_DATE.search(normalized)) or _parses_as_date(normalized)

    elif kind == "cross_reference":
        if payload.get("scope") == "internal":
            target = payload.get("target_clause_id", "")
            checks["internal_target_exists"] = target in clause_ids
        else:
            checks["external_has_document"] = bool(payload.get("target_document", "").strip())

    elif kind == "obligation":
        checks["has_action"] = bool(payload.get("action", "").strip())
        checks["modality_known"] = payload.get("modality") in {
            "shall", "must", "may", "should", "reserves_right"
        }

    return checks


def _parses_as_date(value: str) -> bool:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            from datetime import datetime

            datetime.strptime(value.strip(), fmt)
            return True
        except (ValueError, TypeError):
            continue
    return False


# ---------------------------------------------------------------------------
# Item construction
# ---------------------------------------------------------------------------


def _make_items_for_clause(
    *,
    draft: ClauseDraft,
    clause: Clause,
    canonical: CanonicalDocument,
    document: Document,
    clause_ids: set[str],
    settings: Settings,
    run_id: str,
) -> list[Item]:
    items: list[Item] = []
    primary = draft.primary
    # A second run that errored is a missing signal, not a disagreement.
    # Treating its empty payload as "the other model found nothing" would
    # report models_disagree for every fact in the clause.
    secondary = None if draft.secondary_failed else draft.secondary
    notes: list[str] = []
    if draft.secondary_failed:
        notes.append("second_run_failed")
    if draft.primary_error:
        notes.append("primary_call_error")

    groups = [(kind, [(p, False) for p in (primary.get(group) or [])]) for kind, group in _GROUPS]
    if secondary is not None and not draft.primary_error:
        # A fact only the second family found is a possible miss by the
        # primary. It goes to a person instead of being dropped without trace.
        missed = facts_missed_by_primary(primary, secondary)
        groups += [(kind, [(p, True) for k, p in missed if k == kind]) for kind, _ in _GROUPS]

    for kind, payloads in groups:
        for payload, second_only in payloads:
            payload = dict(payload)
            payload.pop("_injected_fault", None)
            quote = payload.get("quote", "")

            location = locate(quote, clause.text, settings.fuzzy_threshold)
            evidence = Evidence(
                clause_id=clause.id,
                quote=quote,
                grounded=location.found,
                match_kind=location.kind,
                match_score=location.score,
            )
            if location.found:
                absolute_start = clause.char_start + (location.char_start or 0)
                absolute_end = clause.char_start + (location.char_end or 0)
                evidence.char_start = absolute_start
                evidence.char_end = absolute_end
                evidence.page = canonical.page_for_offset(absolute_start)

            type_name = (
                payload.get("type") if kind == "entity"
                else "cross_reference" if kind == "cross_reference"
                else "obligation"
            )

            review = ReviewSignal(
                agreement=0.0 if second_only else _agreement(kind, payload, secondary),
                validators=run_validators(kind, payload, evidence, clause_ids),
                impact=impact_tier(type_name or "", primary.get("clause_types") or []),
                notes=list(notes) + (["second_model_only"] if second_only else []),
            )
            meta = (draft.metas[1] if second_only and len(draft.metas) > 1
                    else draft.metas[0] if draft.metas else None)

            items.append(
                Item(
                    item_id=Item.make_id(document.id, clause.id, kind, payload),
                    kind=kind,
                    type_name=type_name or kind,
                    clause_id=clause.id,
                    payload=payload,
                    evidence=evidence,
                    review=review,
                    provenance=Provenance(
                        model=meta.model if meta else "",
                        # The model that actually answered. After a fallback, or
                        # when a provider serves a dated snapshot, it differs from
                        # the configured one, and a result is only traceable if
                        # provenance says so.
                        model_version=(meta.served_model or meta.model) if meta else "",
                        prompt_version=PROMPT_VERSION,
                        taxonomy_version=TAXONOMY_VERSION,
                        run_id=run_id,
                        timestamp=date.today().isoformat(),
                    ),
                )
            )

    return items


def verify(
    *,
    drafts: list[ClauseDraft],
    clauses: list[Clause],
    canonical: CanonicalDocument,
    document: Document,
    run_id: str,
    settings: Settings | None = None,
) -> list[Item]:
    settings = settings or get_settings()
    by_id = {c.id: c for c in clauses}
    clause_ids = set(by_id)

    items: list[Item] = []
    for draft in drafts:
        clause = by_id.get(draft.clause_id)
        if clause is None:
            continue
        items.extend(
            _make_items_for_clause(
                draft=draft,
                clause=clause,
                canonical=canonical,
                document=document,
                clause_ids=clause_ids,
                settings=settings,
                run_id=run_id,
            )
        )
    return items


# ---------------------------------------------------------------------------
# Document-level checks -- section 7.4 of the plan
# ---------------------------------------------------------------------------

_ACRONYM = re.compile(r"\b([A-Z]{3,6})\b")


def find_conflicting_definitions(text: str) -> list[tuple[str, list[str]]]:
    """Detect an acronym expanded two different ways in the same document.

    CARL-01 defines ECAS as "Emirates Conformity Assessment Scheme" in section
    1 and as "Emirates Conformity Assessment Systems" in clause 5.6. Both are
    load-bearing terms, and a downstream system that picks one at random is
    quietly wrong. General rule, not a hard-coded case: find expansions
    written as "Expansion (ACRONYM)" or "ACRONYM - Expansion", and flag any
    acronym with more than one distinct expansion.
    """
    conflicts: list[tuple[str, list[str]]] = []
    acronyms = {a for a in _ACRONYM.findall(text) if a not in {"AND", "THE", "FOR", "PDF"}}

    for acronym in sorted(acronyms):
        expansions: set[str] = set()

        for match in re.finditer(
            rf"([A-Z][A-Za-z]+(?:\s+[A-Za-z]+){{1,6}})\s*\(\s*{acronym}\s*\)", text
        ):
            expansions.add(normalize_ws(match.group(1)))

        for match in re.finditer(
            rf"\b{acronym}\b\s*[-–—]\s*([A-Z][A-Za-z]+(?:\s+[A-Za-z]+){{1,6}})", text
        ):
            expansions.add(normalize_ws(match.group(1)))

        # "the Examplia Standards Authority" and "Examplia Standards Authority" are
        # the same expansion: drop a leading article before comparing.
        trimmed = {re.sub(r"^(?:the|a|an)\s+", "", _last_n_words(e, 4))
                   for e in expansions if len(e.split()) >= 2}
        if len(trimmed) > 1:
            conflicts.append((acronym, sorted(trimmed)))

    return conflicts


def _last_n_words(text: str, n: int) -> str:
    words = text.split()
    return " ".join(words[-n:]) if len(words) > n else text


def document_flags(
    *,
    items: list[Item],
    canonical: CanonicalDocument,
    structure: dict,
    settings: Settings | None = None,
    drafts: list[ClauseDraft] | None = None,
) -> list[DocumentFlag]:
    settings = settings or get_settings()
    flags: list[DocumentFlag] = []

    # A clause the model never answered is a hole in the document, and the
    # hole looks exactly like "no obligations here". Fail closed: say so.
    failed = [d for d in (drafts or []) if d.primary_error]
    if failed:
        flags.append(DocumentFlag(
            code="extraction_failed",
            detail=f"{len(failed)} clause(s) returned no usable extraction: "
                   f"{[d.clause_id for d in failed][:20]}; first error: {failed[0].primary_error}",
            clause_ids=[d.clause_id for d in failed],
        ))

    if items:
        ungrounded = sum(1 for i in items if not i.evidence.grounded)
        rate = ungrounded / len(items)
        if rate > settings.max_grounding_failure_rate:
            flags.append(DocumentFlag(
                code="high_grounding_failure_rate",
                detail=f"{ungrounded}/{len(items)} items ungrounded ({rate:.1%}), "
                       f"threshold {settings.max_grounding_failure_rate:.0%}",
            ))

    if structure.get("numbering_gaps"):
        flags.append(DocumentFlag(
            code="numbering_gaps",
            detail=f"missing top-level sections: {structure['numbering_gaps']}",
        ))
    if structure.get("duplicate_ids"):
        flags.append(DocumentFlag(
            code="duplicate_clause_ids",
            detail=f"duplicates: {structure['duplicate_ids']}",
        ))

    for acronym, expansions in find_conflicting_definitions(canonical.text):
        flags.append(DocumentFlag(
            code="conflicting_definition",
            detail=f"{acronym} expanded {len(expansions)} ways: {expansions}",
        ))

    return flags
