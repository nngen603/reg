"""The model gateway.

One interface for every model call in the pipeline. Callers name a route --
`primary`, `second`, `judge`, `guardrail` -- and a pydantic response model;
they never name a provider. Three things sit behind it:

  LiteLLM Router -- sends the call to the model configured for the route,
              retries transport errors, and applies the fallback policy in
              llm/routes.py. Retries and fallbacks live in-process, so there
              is no gateway service to run. This replaces the optional Portkey hop.

  Instructor -- every structured reply is validated against the pydantic
              contract. A reply that does not fit is sent back to the model
              with the validation error, up to `validation_retries` times.
              Before this,
              a reply was json.loads-ed and trusted as it came.

  Cassettes / stub -- offline mode. Replays a recorded response if one
              exists, otherwise falls back to a deterministic rule-based
              stub. This is what lets the whole pipeline, the evals and the
              test suite run with no API key and no cost.

Every call returns the payload plus a CallMeta: the model configured, the
model that actually answered (a fallback changes it, and provenance has to say
so), where the answer came from, what it cost and how long it took. That meta
feeds the observability manifest and the provenance on every item.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel

from ..config import Settings, get_settings
from . import cassette as cassette_store
from . import stub
from .routes import ROUTE_NAMES, plan_routes

_INSTRUCTOR_MODES = {"tool_call": "TOOLS", "json_schema": "JSON_SCHEMA", "json": "JSON"}
_TRUNCATED = "truncated: finish_reason=length (raise max_tokens)"


@dataclass
class CallMeta:
    model: str
    source: str                      # live | cassette | stub | skipped
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    cache_key: str = ""
    served_model: str = ""           # what actually answered
    fallback_used: bool = False

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "served_model": self.served_model,
            "fallback_used": self.fallback_used,
            "source": self.source,
            "latency_ms": round(self.latency_ms, 1),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "error": self.error,
        }


@dataclass
class GatewayStats:
    calls: list[CallMeta] = field(default_factory=list)

    def add(self, meta: CallMeta) -> None:
        self.calls.append(meta)

    def summary(self) -> dict:
        if not self.calls:
            return {"calls": 0}
        return {
            "calls": len(self.calls),
            "by_source": dict(Counter(c.source for c in self.calls)),
            "served_models": dict(Counter(c.served_model for c in self.calls if c.served_model)),
            "fallbacks_used": sum(1 for c in self.calls if c.fallback_used),
            "input_tokens": sum(c.input_tokens for c in self.calls),
            "output_tokens": sum(c.output_tokens for c in self.calls),
            "cost_usd": round(sum(c.cost_usd for c in self.calls), 4),
            "total_latency_ms": round(sum(c.latency_ms for c in self.calls), 1),
            "errors": [c.error for c in self.calls if c.error],
        }


class Gateway:
    def __init__(self, settings: Settings | None = None, *,
                 completion: Callable[..., Any] | None = None):
        """`completion` stands in for the Router's completion function. Tests
        use it to drive the live path -- routing names, validation, retries,
        provenance -- without a network call."""
        self.settings = settings or get_settings()
        self.stats = GatewayStats()
        self.route_notes: list[str] = []
        self._completion = completion
        self._router = None
        self._instructor = None

    # -- public ----------------------------------------------------------

    def structured(
        self,
        *,
        system: str,
        user: str,
        response_model: type[BaseModel],
        route: str,
        tag: str,
        stub_context: dict | None = None,
        allow_stub: bool = True,
    ) -> tuple[dict, CallMeta]:
        """Ask for an object matching `response_model`. Always returns a dict;
        on failure it is empty and `meta.error` says why."""
        model = self.settings.model_for(route)
        key = cassette_store.make_key(model=model, system=system, user=user, tag=tag)

        if self.settings.offline:
            recorded = cassette_store.load(self.settings.cassette_dir, key)
            if recorded is not None:
                meta = CallMeta(model=model, source="cassette", cache_key=key)
                self.stats.add(meta)
                return recorded, meta
            if not allow_stub:
                # Nothing recorded and no stub for this job: report it as not
                # run, which is different from failed.
                meta = CallMeta(model=model, source="skipped", cache_key=key)
                self.stats.add(meta)
                return {}, meta
            payload = stub.synthesize(context=stub_context or {})
            meta = CallMeta(model=f"{model} (stub)", source="stub", cache_key=key)
            self.stats.add(meta)
            return payload, meta

        payload, meta = self._live_structured(
            system=system, user=user, response_model=response_model, route=route, model=model)
        meta.cache_key = key
        if self.settings.record and meta.error is None:
            cassette_store.save(self.settings.cassette_dir, key, payload)
        self.stats.add(meta)
        return payload, meta

    def text(self, *, system: str, user: str, route: str, max_tokens: int = 512) -> tuple[str, CallMeta]:
        """Free-text call. Used by the judge, which is triage only."""
        model = self.settings.model_for(route)
        if self.settings.offline:
            meta = CallMeta(model=f"{model} (stub)", source="stub")
            self.stats.add(meta)
            return "", meta

        started = time.perf_counter()
        try:
            response = self._complete()(
                model=ROUTE_NAMES[route],
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                max_tokens=max_tokens,
            )
            content = response.choices[0].message.content or ""
            meta = self._meta_from_response(model, response, started)
        except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
            content, meta = "", CallMeta(model=model, source="live", error=_describe(exc),
                                         latency_ms=(time.perf_counter() - started) * 1000)
        self.stats.add(meta)
        return content, meta

    # -- internals -------------------------------------------------------

    def _complete(self) -> Callable[..., Any]:
        if self._completion is not None:
            return self._completion
        if self._router is None:
            from litellm import Router

            plan = plan_routes(self.settings)
            self.route_notes = plan.notes
            self._router = Router(
                model_list=plan.model_list,
                fallbacks=plan.fallbacks,
                num_retries=self.settings.num_retries,
                timeout=self.settings.request_timeout,
            )
        return self._router.completion

    def _client(self):
        if self._instructor is None:
            import instructor

            mode_name = _INSTRUCTOR_MODES.get(self.settings.instructor_mode, "TOOLS")
            self._instructor = instructor.from_litellm(
                self._complete(), mode=getattr(instructor.Mode, mode_name))
        return self._instructor

    def _live_structured(self, *, system: str, user: str, response_model: type[BaseModel],
                         route: str, model: str) -> tuple[dict, CallMeta]:
        started = time.perf_counter()
        try:
            parsed, completion = self._client().chat.completions.create_with_completion(
                model=ROUTE_NAMES[route],
                response_model=response_model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                max_retries=self.settings.validation_retries,
                max_tokens=self.settings.max_tokens,
                temperature=self.settings.temperature,
            )
        except Exception as exc:  # noqa: BLE001 - becomes meta.error, and routing fails closed on it
            error = _describe(exc)
            if type(exc).__name__ == "IncompleteOutputException":
                # Instructor's own finish_reason check. Same wording as ours
                # below, so one search of the manifest finds every truncation.
                error = _TRUNCATED
            meta = CallMeta(model=model, source="live", error=error,
                            latency_ms=(time.perf_counter() - started) * 1000)
            return _empty_for(response_model), meta

        meta = self._meta_from_response(model, completion, started)
        # stop_reason guard. A truncated response is a silent data-loss bug:
        # max_tokens caps thinking AND output together on current models, so a
        # tight budget cuts the answer off mid-way with no exception raised.
        # Instructor catches most of these first; this covers the rest.
        finish = getattr(completion.choices[0], "finish_reason", None)
        if finish in {"length", "max_tokens"}:
            meta.error = _TRUNCATED
        return parsed.model_dump(mode="json"), meta

    @staticmethod
    def _meta_from_response(model: str, response: Any, started: float) -> CallMeta:
        usage = getattr(response, "usage", None)
        served = str(getattr(response, "model", "") or "")
        cost = 0.0
        try:
            import litellm

            cost = float(litellm.completion_cost(completion_response=response) or 0.0)
        except Exception:  # noqa: BLE001 - cost is best effort, never fatal
            cost = 0.0
        return CallMeta(
            model=model,
            source="live",
            latency_ms=(time.perf_counter() - started) * 1000,
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            cost_usd=cost,
            served_model=served,
            fallback_used=bool(served) and not _same_model(model, served),
        )


def _same_model(configured: str, served: str) -> bool:
    """'openai/gpt-4.1' served as 'gpt-4.1-2025-04-14' is the same model -- a
    dated snapshot, not a fallback."""
    a = configured.rsplit("/", 1)[-1].lower()
    b = served.rsplit("/", 1)[-1].lower()
    return a.startswith(b) or b.startswith(a)


def _describe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:300]}"


def _empty_for(response_model: type[BaseModel]) -> dict:
    """A schema-shaped empty result, so one failed call cannot crash a run."""
    empty: dict[str, Any] = {}
    for name, spec in (response_model.model_json_schema().get("properties") or {}).items():
        empty[name] = [] if spec.get("type") == "array" else ""
    return empty
