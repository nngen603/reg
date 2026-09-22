"""The output contract.

This is the deliverable. Downstream compliance systems read these objects, never
the prompts. Defined once here in pydantic, and used four ways:

  1. the JSON schema handed to the model as an enforced output format
  2. runtime validation of the model's response
  3. the shape verification/verify.py and routing/route.py operate on
  4. the shape published to downstream

One important split. The `*Draft` models are what the LLM is allowed to produce.
They contain a `quote` but no offsets. We compute page and character offsets
ourselves by locating that quote in the source text. So the model cannot invent
a location -- it can only claim a quote, and we check the claim.
"""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class ClauseType(str, Enum):
    """Ten types. Merged down from thirteen: liability -> responsibility,
    penalty + authority_power -> enforcement, fees -> administrative."""

    DEFINITION = "definition"
    SCOPE = "scope"
    EXEMPTION = "exemption"
    REQUIREMENT = "requirement"
    RESPONSIBILITY = "responsibility"
    PROCEDURE = "procedure"
    ENFORCEMENT = "enforcement"
    VALIDITY_AND_DATES = "validity_and_dates"
    NORMATIVE_REFERENCE = "normative_reference"
    ADMINISTRATIVE = "administrative"


class EntityType(str, Enum):
    ORGANISATION = "organisation"
    INDIVIDUAL = "individual"
    ROLE = "role"
    LOCATION = "location"
    DATE = "date"
    TIME_PERIOD = "time_period"
    MONETARY_THRESHOLD = "monetary_threshold"
    NUMERIC_THRESHOLD = "numeric_threshold"
    PRODUCT_CATEGORY = "product_category"
    STANDARD_REFERENCE = "standard_reference"
    LEGAL_REFERENCE = "legal_reference"


class Modality(str, Enum):
    SHALL = "shall"
    MUST = "must"
    MAY = "may"
    SHOULD = "should"
    RESERVES_RIGHT = "reserves_right"


class Decision(str, Enum):
    AUTO_ACCEPT = "auto_accept"    # passed every gate; no person needed
    REVIEW = "review"              # waiting for a person
    ACCEPTED = "accepted"          # a reviewer accepted it as extracted
    REJECT = "reject"              # a reviewer rejected it
    SUPERSEDED = "superseded"      # replaced by a reviewer's corrected version


class ImpactTier(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class MatchKind(str, Enum):
    EXACT = "exact"
    FUZZY = "fuzzy"
    NONE = "none"


# Which tier does a given type fall into? Drives the routing thresholds.
# Section 7.3 of the plan.
_HIGH = {
    "validity_and_dates", "enforcement", "exemption", "scope",
    "time_period", "monetary_threshold", "numeric_threshold", "date",
}
_MEDIUM = {
    "requirement", "responsibility", "procedure", "normative_reference",
    "cross_reference", "standard_reference", "legal_reference", "product_category",
}


def impact_tier(type_name: str, clause_types=()) -> ImpactTier:
    """Obligations are what downstream systems act on, so they are never low
    impact: medium by default, and high when the clause they sit in is about
    validity, enforcement, exemption or scope."""
    if type_name == "obligation":
        return ImpactTier.HIGH if set(clause_types or ()) & _HIGH else ImpactTier.MEDIUM
    if type_name in _HIGH:
        return ImpactTier.HIGH
    if type_name in _MEDIUM:
        return ImpactTier.MEDIUM
    return ImpactTier.LOW


# --------------------------------------------------------------------------
# What the model is allowed to produce
# --------------------------------------------------------------------------


class ObligationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(description="Who must act. A role, e.g. 'supplier'. Empty string if the source never says.")
    modality: Modality
    action: str = Field(description="What must be done.")
    condition: str = Field(description="Any condition or timing attached. Empty string if none.")
    quote: str = Field(description="The sentence from the clause that supports this, word for word.")
    confidence: float = Field(ge=0.0, le=1.0)


class EntityDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: EntityType
    text: str = Field(description="The entity as it appears in the source, word for word.")
    normalized: str = Field(description="A normalised form, e.g. '1 year' or 'ISO-8601 date'. Empty string if not applicable.")
    quote: str = Field(description="The sentence containing this entity, word for word.")
    confidence: float = Field(ge=0.0, le=1.0)


class CrossReferenceDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(description="The reference as written, e.g. 'clause 4 of this document'.")
    scope: Literal["internal", "external"]
    target_document: str = Field(description="Named document for an external reference. Empty string if internal.")
    target_clause_id: str = Field(description="Target clause id if resolvable, e.g. '4'. Empty string otherwise.")
    quote: str = Field(description="The sentence containing this reference, word for word.")
    confidence: float = Field(ge=0.0, le=1.0)


class ClauseExtractionDraft(BaseModel):
    """Exactly what one extraction call returns for one clause."""

    model_config = ConfigDict(extra="forbid")

    clause_types: list[ClauseType]
    obligations: list[ObligationDraft]
    entities: list[EntityDraft]
    cross_references: list[CrossReferenceDraft]

    @model_validator(mode="before")
    @classmethod
    def _unwrap_single_item_list(cls, value):
        # Some models wrap the reply in a one-item list. Accept exactly that,
        # nothing looser: any other shape still fails validation.
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
            return value[0]
        return value


# --------------------------------------------------------------------------
# What the pipeline produces
# --------------------------------------------------------------------------


class Evidence(BaseModel):
    """Where a claim came from, and whether we could actually find it.

    page / char_start / char_end are computed by us from the source text.
    They are never taken from the model.
    """

    clause_id: str
    page: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    quote: str
    grounded: bool = False
    match_kind: MatchKind = MatchKind.NONE
    match_score: float = 0.0


class Provenance(BaseModel):
    model: str = ""
    model_version: str = ""
    prompt_version: str = ""
    taxonomy_version: str = ""
    run_id: str = ""
    timestamp: str = ""


class ReviewSignal(BaseModel):
    score: float = 0.0
    decision: Decision = Decision.REVIEW
    reasons: list[str] = Field(default_factory=list)
    agreement: float = 0.0
    validators: dict[str, bool] = Field(default_factory=dict)
    impact: ImpactTier = ImpactTier.LOW
    judge_priority: float | None = None  # LLM-as-judge triage only, never a gate
    # Signals raised before routing, e.g. a failed model call. Routing reads
    # them on every pass, so they survive a re-route instead of being
    # overwritten with the freshly computed reasons.
    notes: list[str] = Field(default_factory=list)
    # Set when a person decides. Empty for anything the router decided alone.
    reviewer: str = ""
    reviewed_at: str = ""
    review_note: str = ""


class Resolution(BaseModel):
    """Where an external reference points, if we can say.

    Computed by us against the corpus catalogue, never by the model.
    `not_found` is a correct answer: most references point at documents we do
    not hold, and a wrong link is worse than no link.
    """

    status: Literal["resolved", "not_found"]
    target_document_id: str = ""
    target_title: str = ""
    score: float = 0.0
    method: str = ""
    reason: str = ""
    candidates_considered: int = 0


class Item(BaseModel):
    """One extracted fact, verified and routed.

    Flat on purpose: routing, review queues and evaluation all work over a
    single list, whatever kind of fact it is.
    """

    item_id: str
    kind: Literal["obligation", "entity", "cross_reference"]
    type_name: str
    clause_id: str
    payload: dict[str, Any]
    evidence: Evidence
    review: ReviewSignal = Field(default_factory=ReviewSignal)
    provenance: Provenance = Field(default_factory=Provenance)
    # Only on external references. Information for downstream, not a routing
    # signal: an unresolved reference is normal, not suspicious.
    resolution: Resolution | None = None
    # On a reviewer's corrected version: the item it replaces. The original is
    # kept and marked superseded, so a correction is never a silent overwrite.
    supersedes: str | None = None

    @staticmethod
    def make_id(document_id: str, clause_id: str, kind: str, payload: dict) -> str:
        """Stable id: document + clause + a hash of the content itself.

        Re-running the same document gives the same ids, so diffing versions
        against each other is meaningful.
        """
        basis = f"{kind}|{sorted(payload.items())!r}"
        digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]
        return f"{document_id}:{clause_id}:{digest}"


class Clause(BaseModel):
    id: str
    parent: str | None = None
    path: list[str] = Field(default_factory=list)
    heading: str = ""
    text: str = ""
    page_start: int = 0
    page_end: int = 0
    char_start: int = 0
    char_end: int = 0
    clause_types: list[ClauseType] = Field(default_factory=list)

    @property
    def depth(self) -> int:
        return len(self.path)


class Document(BaseModel):
    id: str
    title: str = ""
    issuer: str = ""
    jurisdiction: str = ""
    revision: str = ""
    review_date: str | None = None
    effective_date: str | None = None
    language: str = "en"
    source_hash: str = ""
    page_count: int = 0
    pipeline_version: str = ""


class DocumentFlag(BaseModel):
    """A document-level problem. Section 7.4 of the plan.

    Any of these routes the whole document to review rather than trusting
    item-level scores.
    """

    code: str
    detail: str
    clause_ids: list[str] = Field(default_factory=list)


class ExtractionRun(BaseModel):
    """Everything one run produced. This is what gets written to disk."""

    document: Document
    clauses: list[Clause] = Field(default_factory=list)
    items: list[Item] = Field(default_factory=list)
    flags: list[DocumentFlag] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
