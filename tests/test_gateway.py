"""The model gateway: routes, fallbacks, validation and provenance.

The live code path is driven by a fake completion function that speaks the
same tool-call format as a real provider, so everything except the network is
exercised: Router route names, Instructor validation and retries, truncation
handling, and served-model provenance.
"""

import pytest
from pydantic import BaseModel, Field

from regextract.config import Settings
from regextract.evaluation.evaluate import run_failure_classes
from regextract.pipeline.graph import run_pipeline
from regextract.llm import Gateway, model_family, plan_routes
from fake_llm import FakeLLM, tool_response


class Verdict(BaseModel):
    ok: bool
    confidence: float = Field(ge=0, le=1)


def live(tmp_path, **overrides):
    return Settings(output_dir=tmp_path, REGEXTRACT_OFFLINE=False, **overrides)


def ask(gateway, route="primary"):
    return gateway.structured(system="s", user="u", response_model=Verdict, route=route, tag="t")


# --- routes and the fallback policy -----------------------------------------

@pytest.mark.parametrize("model, family", [
    ("anthropic/claude-opus-5", "anthropic"),
    ("openai/gpt-4.1", "openai"),
    ("groq/openai/gpt-oss-120b", "openai"),
    ("vertex_ai/gemini-2.5-flash", "google"),
    ("bedrock/anthropic.claude-3-haiku", "anthropic"),
    ("groq/llama-3.3-70b-versatile", "meta"),
])
def test_model_family_looks_past_the_hosting_provider(model, family):
    assert model_family(model) == family


def test_a_same_family_fallback_for_the_second_run_is_dropped(tmp_path):
    settings = live(tmp_path, REGEXTRACT_SECOND_FALLBACKS=[
        "anthropic/claude-haiku-4-5", "groq/openai/gpt-oss-120b"])
    plan = plan_routes(settings)
    assert plan.second_fallbacks == ["groq/openai/gpt-oss-120b"]
    assert plan.fallbacks == [{"regextract-second": ["regextract-second-fallback-0"]}]
    assert any("claude-haiku-4-5" in note and "self-consistency" in note for note in plan.notes)


def test_the_primary_never_falls_back(tmp_path):
    plan = plan_routes(live(tmp_path))
    assert all("regextract-primary" not in rule for rule in plan.fallbacks)


def test_same_family_agreement_is_labelled_for_what_it_is(tmp_path):
    settings = live(tmp_path, REGEXTRACT_SECOND_MODEL="anthropic/claude-sonnet-5")
    assert settings.describe()["agreement"].startswith("self-consistency")


def test_default_judge_is_not_the_primary_family():
    settings = Settings()
    assert model_family(settings.judge_model) != model_family(settings.primary_model)


# --- validation, retries and provenance -------------------------------------

def test_a_valid_reply_is_validated_and_the_serving_model_recorded(tmp_path):
    fake = FakeLLM(script=[tool_response("Verdict", {"ok": True, "confidence": 0.9},
                                         model="gpt-4.1-2025-04-14")])
    payload, meta = ask(Gateway(live(tmp_path), completion=fake), route="second")
    assert payload == {"ok": True, "confidence": 0.9}
    assert meta.error is None and meta.source == "live"
    assert meta.served_model == "gpt-4.1-2025-04-14"
    assert meta.fallback_used is False            # a dated snapshot is the same model
    assert fake.calls[0]["model"] == "regextract-second"


def test_an_invalid_reply_is_sent_back_with_the_error_and_retried(tmp_path):
    fake = FakeLLM(script=[
        tool_response("Verdict", {"ok": True, "confidence": 1.7}),
        tool_response("Verdict", {"ok": True, "confidence": 0.7}),
    ])
    payload, meta = ask(Gateway(live(tmp_path), completion=fake))
    assert payload["confidence"] == 0.7 and meta.error is None
    assert len(fake.calls) == 2
    assert "less than or equal to 1" in str(fake.calls[1]["messages"])


def test_a_reply_that_never_validates_is_an_error_not_an_answer(tmp_path):
    bad = tool_response("Verdict", {"ok": "maybe", "confidence": 5})
    payload, meta = ask(Gateway(live(tmp_path), completion=FakeLLM(script=[bad, bad, bad])))
    assert meta.error
    assert payload == {"ok": "", "confidence": ""}


def test_a_transport_error_is_recorded(tmp_path):
    def unreachable(**kwargs):
        raise ConnectionError("provider unreachable")

    payload, meta = ask(Gateway(live(tmp_path), completion=unreachable))
    assert "provider unreachable" in meta.error


def test_a_truncated_reply_is_flagged(tmp_path):
    fake = FakeLLM(script=[tool_response("Verdict", {"ok": True, "confidence": 0.5}, finish="length")])
    _, meta = ask(Gateway(live(tmp_path), completion=fake))
    assert meta.error.startswith("truncated")


def test_an_answer_from_a_different_model_is_reported_as_a_fallback(tmp_path):
    fake = FakeLLM(script=[tool_response("Verdict", {"ok": True, "confidence": 0.5},
                                         model="gpt-oss-120b")])
    gateway = Gateway(live(tmp_path), completion=fake)
    _, meta = ask(gateway, route="second")
    assert meta.fallback_used is True
    assert gateway.stats.summary()["fallbacks_used"] == 1


# --- the whole pipeline through the live path -------------------------------

def test_the_live_path_reproduces_the_offline_run(pdf_path, tmp_path, state):
    settings = live(tmp_path)
    result = run_pipeline(pdf_path=pdf_path, issuer="ESMA", jurisdiction="AE", settings=settings,
                          gateway=Gateway(settings, completion=FakeLLM()))

    assert {i.item_id for i in result["items"]} == {i.item_id for i in state["items"]}
    assert set(result["manifest"]["model_calls"]["by_source"]) == {"live"}
    # Provenance names the model that answered, not just the one configured.
    assert {i.provenance.model_version for i in result["items"]} == {"claude-opus-5"}
    assert not [r.key for r in run_failure_classes(result) if not r.passed]


def test_the_judge_route_orders_the_queue_and_nothing_else(pdf_path, tmp_path):
    settings = live(tmp_path, REGEXTRACT_USE_JUDGE=True)
    result = run_pipeline(pdf_path=pdf_path, issuer="ESMA", jurisdiction="AE", settings=settings,
                          gateway=Gateway(settings, completion=FakeLLM()))
    judged = [i for i in result["items"] if i.review.judge_priority is not None]
    assert judged
    assert all(i.review.decision.value != "auto_accept" for i in judged)
