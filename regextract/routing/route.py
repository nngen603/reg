"""Stage 7 -- score and route.

Signals are combined in the order the plan ranks them, and that order matters
more than the weights:

  1. grounding    a gate, not a signal. Ungrounded is never publishable.
  2. agreement    two model families saying the same thing
  3. validators   deterministic checks
  4. confidence   self-reported, weakest, breaks ties only

The thresholds are PLACEHOLDERS. They are not derived from data and the code
says so out loud in the run manifest. Section 7.3 of the plan describes the
calibration table that would replace them: precision per confidence band,
measured on a real gold set, per type.
"""

from __future__ import annotations

from ..config import Settings, get_settings
from ..contract import Decision, Item

# Weights apply only once the gate has been passed.
W_AGREEMENT = 0.45
W_VALIDATORS = 0.35
W_CONFIDENCE = 0.20


def _self_confidence(item: Item) -> float:
    try:
        return max(0.0, min(1.0, float(item.payload.get("confidence", 0.0))))
    except (TypeError, ValueError):
        return 0.0


def score_item(item: Item) -> tuple[float, list[str]]:
    reasons: list[str] = []

    if not item.evidence.grounded:
        # The gate. No score can rescue this.
        return 0.0, ["evidence_not_grounded"]

    if item.evidence.match_kind.value == "fuzzy":
        reasons.append(f"evidence_matched_fuzzily ({item.evidence.match_score:.0f})")

    validators = item.review.validators or {}
    checked = {k: v for k, v in validators.items() if k != "evidence_grounded"}
    validator_score = (sum(1 for v in checked.values() if v) / len(checked)) if checked else 1.0
    for name, passed in checked.items():
        if not passed:
            reasons.append(f"validator_failed:{name}")

    agreement = item.review.agreement
    if agreement == 0.0:
        reasons.append("models_disagree")
    elif agreement == 0.5:
        if "second_run_failed" in item.review.notes:
            reasons.append("second_run_failed")
        else:
            reasons.append("single_run_no_agreement_signal")

    confidence = _self_confidence(item)
    score = (W_AGREEMENT * agreement) + (W_VALIDATORS * validator_score) + (W_CONFIDENCE * confidence)
    return round(score, 4), reasons


def route_item(item: Item, settings: Settings) -> Item:
    score, reasons = score_item(item)
    tier = item.review.impact.value
    threshold = settings.threshold_for(tier)

    decision = Decision.REVIEW
    if item.evidence.grounded:
        gates_ok = score >= threshold
        if tier == "high":
            # High impact needs full agreement and a clean validator sweep as
            # well as the score. Section 7.3.
            validators = {k: v for k, v in (item.review.validators or {}).items()
                          if k != "evidence_grounded"}
            all_validators_pass = all(validators.values()) if validators else True
            gates_ok = gates_ok and item.review.agreement == 1.0 and all_validators_pass
            if item.review.agreement != 1.0:
                reasons.append("high_impact_requires_full_agreement")
        if gates_ok:
            decision = Decision.AUTO_ACCEPT

    if decision is Decision.AUTO_ACCEPT and item.evidence.match_kind.value == "fuzzy":
        # A close match is evidence, not proof: a person confirms it.
        decision = Decision.REVIEW

    if "second_model_only" in item.review.notes:
        # Only the second family found this fact: a possible miss by the primary.
        decision = Decision.REVIEW
        reasons.append("found_only_by_second_model")

    if "primary_call_error" in item.review.notes:
        # The call errored but still returned something -- a truncated reply,
        # say. Whatever came back is not trustworthy enough to auto-accept.
        decision = Decision.REVIEW
        reasons.append("primary_call_error")

    if decision is Decision.REVIEW and tier == "high" and "high_impact_type" not in reasons:
        reasons.append("high_impact_type")

    item.review.score = score
    item.review.decision = decision
    item.review.reasons = reasons
    return item


def route(items: list[Item], settings: Settings | None = None) -> list[Item]:
    settings = settings or get_settings()
    return [route_item(item, settings) for item in items]


# A structural problem invalidates every score on the document. A terminology
# problem only invalidates the items that use the disputed term, so it is
# escalated to those items instead of the whole run. This is a refinement of
# section 7.4 of the plan, which lists both as document-level triggers -- the
# narrower rule is more useful to an analyst and no less safe.
BLOCKING_FLAG_CODES = {
    "high_grounding_failure_rate",
    "numbering_gaps",
    "duplicate_clause_ids",
    "low_ocr_confidence",
    # A clause the model never answered means the document cannot be vouched
    # for as complete. The operational fix is to re-run that clause, which is
    # cheap because every call is idempotent on content hash.
    "extraction_failed",
}


def apply_document_flags(items: list[Item], flags: list) -> list[Item]:
    """Let a document-level problem override item-level optimism."""
    if not flags:
        return items

    blocking = [f for f in flags if f.code in BLOCKING_FLAG_CODES]
    targeted = [f for f in flags if f.code not in BLOCKING_FLAG_CODES]

    if blocking:
        codes = ",".join(f.code for f in blocking)
        for item in items:
            item.review.decision = Decision.REVIEW
            item.review.reasons.append(f"document_flagged:{codes}")

    for flag in targeted:
        if flag.code != "conflicting_definition":
            continue
        term = flag.detail.split()[0]        # the acronym itself
        for item in items:
            haystack = f"{item.payload} {item.evidence.quote}"
            if term in haystack:
                item.review.decision = Decision.REVIEW
                item.review.reasons.append(f"uses_disputed_term:{term}")

    return items


_TIER_ORDER = {"high": 0, "medium": 1, "low": 2}


def review_queue(items: list[Item]) -> list[Item]:
    """Everything a human has to look at, most urgent first.

    Ordered by impact, then by how uncertain we are, then by the judge's
    triage priority if it ran. Judge output only ever changes the ORDER of
    this list -- it never decides what goes in it.
    """
    pending = [i for i in items if i.review.decision is Decision.REVIEW]
    return sorted(
        pending,
        key=lambda i: (
            _TIER_ORDER.get(i.review.impact.value, 3),
            -(i.review.judge_priority or 0.0),
            i.review.score,
        ),
    )


def routing_summary(items: list[Item]) -> dict:
    total = len(items)
    if total == 0:
        return {"items": 0}
    def count(decision: Decision) -> int:
        return sum(1 for i in items if i.review.decision is decision)

    auto = count(Decision.AUTO_ACCEPT)
    pending = count(Decision.REVIEW)
    grounded = sum(1 for i in items if i.evidence.grounded)
    by_tier: dict[str, dict[str, int]] = {}
    for item in items:
        tier = by_tier.setdefault(item.review.impact.value, {"total": 0, "auto_accept": 0})
        tier["total"] += 1
        if item.review.decision is Decision.AUTO_ACCEPT:
            tier["auto_accept"] += 1
    return {
        "items": total,
        "grounded": grounded,
        "grounding_rate": round(grounded / total, 4),
        "auto_accept": auto,
        "review": pending,
        "review_rate": round(pending / total, 4),
        "accepted_by_reviewer": count(Decision.ACCEPTED),
        "rejected": count(Decision.REJECT),
        "superseded": count(Decision.SUPERSEDED),
        "by_tier": by_tier,
    }
