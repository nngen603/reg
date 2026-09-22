"""Settings and feature flags.

Every optional integration is off by default and can be turned on one at a
time. The core pipeline runs without any of them, and without an API key at
all in offline mode. That is what makes the project testable for free.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent

PIPELINE_VERSION = "0.2.0"
PROMPT_VERSION = "extract-v1"
TAXONOMY_VERSION = "taxonomy-v1-10types"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -- run mode ---------------------------------------------------------
    # offline replays recorded model responses from cassettes/. No API key,
    # no cost, fully deterministic. This is the default so that a fresh
    # clone runs and the test suite passes with nothing configured.
    offline: bool = Field(default=True, alias="REGEXTRACT_OFFLINE")
    record: bool = Field(default=False, alias="REGEXTRACT_RECORD")

    # -- models -----------------------------------------------------------
    # Any LiteLLM model string. Code never uses these directly: it asks for
    # a route (llm/routes.py) and the route maps to one of these.
    primary_model: str = Field(default="anthropic/claude-opus-5", alias="REGEXTRACT_PRIMARY_MODEL")
    # A different family from the primary, so "agreement" means two families
    # agreeing, not one model sampled twice. See section 7.2 of the plan.
    second_model: str = Field(default="openai/gpt-4.1", alias="REGEXTRACT_SECOND_MODEL")
    # Tried in order if the second model is down. A fallback in the primary's
    # family is dropped at startup. The primary deliberately has no fallback:
    # if it cannot answer, the clause is reviewed, not answered by a weaker model.
    second_fallbacks: list[str] = Field(default_factory=lambda: ["groq/openai/gpt-oss-120b"],
                                        alias="REGEXTRACT_SECOND_FALLBACKS")
    agreement_enabled: bool = Field(default=True, alias="REGEXTRACT_AGREEMENT")
    max_tokens: int = Field(default=4000, alias="REGEXTRACT_MAX_TOKENS")
    temperature: float = Field(default=0.0, alias="REGEXTRACT_TEMPERATURE")
    llm_concurrency: int = Field(default=4, alias="REGEXTRACT_CONCURRENCY")

    # Transport retries (rate limits, timeouts, 5xx) belong to the Router.
    # Validation retries belong to Instructor: a reply that does not fit the
    # contract is sent back with the validation error.
    num_retries: int = Field(default=2, alias="REGEXTRACT_NUM_RETRIES")
    request_timeout: float = Field(default=120.0, alias="REGEXTRACT_REQUEST_TIMEOUT")
    validation_retries: int = Field(default=1, alias="REGEXTRACT_VALIDATION_RETRIES")
    # How Instructor asks for structure: tool_call (most portable across
    # providers), json_schema, or json.
    instructor_mode: str = Field(default="tool_call", alias="REGEXTRACT_INSTRUCTOR_MODE")

    # -- optional integrations -------------------------------------------
    use_qdrant: bool = Field(default=False, alias="REGEXTRACT_USE_QDRANT")
    qdrant_url: str = Field(default=":memory:", alias="QDRANT_URL")
    qdrant_collection: str = Field(default="regextract_clauses", alias="QDRANT_COLLECTION")

    use_guardrails: bool = Field(default=False, alias="REGEXTRACT_USE_GUARDRAILS")
    guardrail_model: str = Field(default="groq/openai/gpt-oss-safeguard-20b",
                                 alias="REGEXTRACT_GUARDRAIL_MODEL")

    use_judge: bool = Field(default=False, alias="REGEXTRACT_USE_JUDGE")
    # Not the primary's family: a judge that shares the extractor's blind
    # spots ranks the extractor's mistakes as low priority.
    judge_model: str = Field(default="groq/openai/gpt-oss-120b", alias="REGEXTRACT_JUDGE_MODEL")

    # -- reference resolution (resolution/retrieval.py) --------------------
    # Hybrid search over the corpus catalogue, a reranker, then a floor below
    # which the answer is NOT_FOUND.
    use_resolution: bool = Field(default=True, alias="REGEXTRACT_RESOLVE")
    catalog_path: Path = Field(default=PROJECT_ROOT / "data" / "corpus_catalog.json",
                               alias="REGEXTRACT_CATALOG")
    distractors_path: Path | None = Field(default=PROJECT_ROOT / "data" / "distractor_titles.txt",
                                          alias="REGEXTRACT_DISTRACTORS")
    embedding_model: str = Field(default="text-embedding-3-small", alias="REGEXTRACT_EMBEDDING_MODEL")
    # Empty means the deterministic lexical reranker. A model reranker can
    # run through LiteLLM, for example jina_ai/jina-reranker-v2-base-multilingual.
    rerank_model: str = Field(default="", alias="REGEXTRACT_RERANK_MODEL")
    resolution_min_score: float = Field(default=0.90, alias="REGEXTRACT_RESOLUTION_MIN_SCORE")
    resolution_min_score_model: float = Field(default=0.50, alias="REGEXTRACT_RESOLUTION_MIN_SCORE_MODEL")
    resolution_candidates: int = Field(default=10, alias="REGEXTRACT_RESOLUTION_CANDIDATES")

    # -- human review -----------------------------------------------------
    # On: the run pauses at human_review (a LangGraph interrupt, checkpointed)
    # whenever items need a person, and `run.py review` resumes it with their
    # decisions. Off: the queue is written to review_queue.csv and the run
    # completes, as before.
    review_mode: bool = Field(default=False, alias="REGEXTRACT_REVIEW")
    checkpoint_path: Path = Field(default=PROJECT_ROOT / "outputs" / "checkpoints.sqlite",
                                  alias="REGEXTRACT_CHECKPOINTS")

    # -- grounding gate ---------------------------------------------------
    # Exact match after normalisation is tried first. Fuzzy is the fallback,
    # and the threshold is high on purpose. Raising it makes the gate
    # stricter and pushes more items to review.
    fuzzy_threshold: float = Field(default=92.0, alias="REGEXTRACT_FUZZY_THRESHOLD")

    # -- routing thresholds ----------------------------------------------
    # PLACEHOLDERS. Not derived from data. Section 7.3 of the plan explains
    # why, and describes the calibration table that would replace them.
    threshold_high: float = Field(default=0.90, alias="REGEXTRACT_THRESHOLD_HIGH")
    threshold_medium: float = Field(default=0.80, alias="REGEXTRACT_THRESHOLD_MEDIUM")
    threshold_low: float = Field(default=0.75, alias="REGEXTRACT_THRESHOLD_LOW")

    # -- document-level triggers -----------------------------------------
    max_grounding_failure_rate: float = Field(default=0.05, alias="REGEXTRACT_MAX_UNGROUNDED")

    # -- paths ------------------------------------------------------------
    output_dir: Path = Field(default=PROJECT_ROOT / "outputs")
    cassette_dir: Path = Field(default=PROJECT_ROOT / "cassettes")
    gold_dir: Path = Field(default=PROJECT_ROOT / "gold")

    def threshold_for(self, tier: str) -> float:
        return {
            "high": self.threshold_high,
            "medium": self.threshold_medium,
            "low": self.threshold_low,
        }[tier]

    def model_for(self, route: str) -> str:
        return {
            "primary": self.primary_model,
            "second": self.second_model,
            "judge": self.judge_model,
            "guardrail": self.guardrail_model,
        }[route]

    def describe(self) -> dict:
        """What is actually switched on. Printed at the start of every run so
        a result can be explained later."""
        from .llm.routes import plan_routes

        plan = plan_routes(self)
        return {
            "mode": "offline (cassette replay)" if self.offline else "live",
            "recording": self.record,
            "primary_model": self.primary_model,
            "second_model": self.second_model if self.agreement_enabled else None,
            "second_fallbacks": plan.second_fallbacks if self.agreement_enabled else [],
            "agreement": plan.agreement if self.agreement_enabled else "off (single run)",
            "retries": f"{self.num_retries} transport, {self.validation_retries} validation",
            "qdrant": self.use_qdrant,
            "guardrails": self.use_guardrails,
            "judge": self.use_judge,
            "resolution": (f"hybrid (bm25 + dense) -> {self.rerank_model or 'lexical'} rerank -> floor"
                           if self.use_resolution else False),
            "review": "pause for a person (interrupt)" if self.review_mode else "queue file only",
            "route_notes": plan.notes,
            "pipeline_version": PIPELINE_VERSION,
            "prompt_version": PROMPT_VERSION,
            "taxonomy_version": TAXONOMY_VERSION,
        }


_settings: Settings | None = None


def get_settings(reload: bool = False, **overrides) -> Settings:
    global _settings
    if _settings is None or reload or overrides:
        _settings = Settings(**overrides)
    return _settings


def has_api_key() -> bool:
    """True if any provider key looks present. Used to fail fast with a clear
    message rather than a stack trace from inside the SDK."""
    return any(
        os.environ.get(k)
        for k in (
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "GROQ_API_KEY",
            "MISTRAL_API_KEY",
        )
    )
