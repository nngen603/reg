"""Model routes: which model serves which job, and what happens when it fails.

Callers ask for a job -- `primary`, `second`, `judge`, `guardrail` -- never for
a provider's model string. The mapping lives here and in config, so swapping a
provider is a settings change, not a code change.

The fallback policy is where the judgment is:

  primary   retries only, no fallback. If the strongest model cannot answer,
            the clause goes to review. A weaker model quietly answering
            instead would lower quality with no trace of it in the output.

  second    may fall back, but only to a DIFFERENT family from the primary.
            A same-family fallback would turn the agreement signal into
            self-consistency without anyone noticing, so such fallbacks are
            dropped and the drop is reported.

  judge     triage only. Ideally not the primary's family either, because a
            model shares its own blind spots and will rank its own mistakes
            as low priority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ROUTE_NAMES = {
    "primary": "regextract-primary",
    "second": "regextract-second",
    "judge": "regextract-judge",
    "guardrail": "regextract-guardrail",
}

# Who trained the model, not who hosts it: groq/openai/gpt-oss-120b is an
# OpenAI-family model served by Groq. Agreement is about the former.
_FAMILY_HINTS = [
    ("claude", "anthropic"), ("anthropic", "anthropic"),
    ("gemini", "google"), ("gemma", "google"),
    ("gpt", "openai"), ("o1", "openai"), ("o3", "openai"), ("o4", "openai"),
    ("llama", "meta"),
    ("mistral", "mistral"), ("mixtral", "mistral"),
    ("qwen", "alibaba"), ("deepseek", "deepseek"), ("command", "cohere"),
]


def model_family(model: str) -> str:
    segments = [s for s in re.split(r"[/:.\-_]", model.lower()) if s]
    for hint, family in _FAMILY_HINTS:
        if any(segment.startswith(hint) for segment in segments):
            return family
    return segments[0] if segments else "unknown"


@dataclass
class RoutePlan:
    model_list: list[dict]
    fallbacks: list[dict]
    second_fallbacks: list[str]
    agreement: str
    notes: list[str] = field(default_factory=list)


def plan_routes(settings) -> RoutePlan:
    primary_family = model_family(settings.primary_model)
    model_list = [
        {"model_name": ROUTE_NAMES[route], "litellm_params": {"model": settings.model_for(route)}}
        for route in ROUTE_NAMES
    ]
    notes: list[str] = []

    kept: list[str] = []
    fallback_names: list[str] = []
    for model in settings.second_fallbacks:
        if model_family(model) == primary_family:
            notes.append(
                f"dropped second-run fallback {model}: same family as the primary "
                f"({primary_family}), so agreement would silently become self-consistency"
            )
            continue
        name = f"{ROUTE_NAMES['second']}-fallback-{len(kept)}"
        model_list.append({"model_name": name, "litellm_params": {"model": model}})
        kept.append(model)
        fallback_names.append(name)

    second_family = model_family(settings.second_model)
    if second_family == primary_family:
        agreement = (f"self-consistency (both {primary_family}): measures how much one "
                     "family varies, not whether it is right")
    else:
        agreement = f"two families ({primary_family} vs {second_family})"

    if model_family(settings.judge_model) == primary_family:
        notes.append(f"the judge shares the primary's family ({primary_family}) and may "
                     "share its blind spots")

    return RoutePlan(
        model_list=model_list,
        fallbacks=[{ROUTE_NAMES["second"]: fallback_names}] if fallback_names else [],
        second_fallbacks=kept,
        agreement=agreement,
        notes=notes,
    )
