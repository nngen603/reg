"""A stand-in for a model provider, for driving the live code path offline.

It speaks the OpenAI tool-call format that Instructor sends through LiteLLM.
By default it answers extraction calls from the same rule-based stub the
offline mode uses, so a live-mode run with it should reproduce an offline run
exactly -- which tests that routing, validation and provenance do not change
what the pipeline concludes. It can also play back a fixed script of replies.
"""

import json
import re

import litellm

from regextract.llm import stub

_CLAUSE = re.compile(r'CLAUSE (\S+) -- ([^\n]*)\n"""\n(.*)\n"""', re.S)

SERVED = {
    "regextract-primary": "claude-opus-5",
    "regextract-second": "gpt-4.1",
    "regextract-judge": "gpt-oss-120b",
    "regextract-guardrail": "gpt-oss-safeguard-20b",
}


def tool_response(name, arguments, *, model="fake-model", finish="tool_calls", usage=(120, 40)):
    return litellm.ModelResponse(
        model=model,
        choices=[{"index": 0, "finish_reason": finish, "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)}}],
        }}],
        usage={"prompt_tokens": usage[0], "completion_tokens": usage[1],
               "total_tokens": usage[0] + usage[1]},
    )


def text_response(content, *, model="fake-model"):
    return litellm.ModelResponse(
        model=model,
        choices=[{"index": 0, "finish_reason": "stop",
                  "message": {"role": "assistant", "content": content}}],
        usage={"prompt_tokens": 50, "completion_tokens": 2, "total_tokens": 52},
    )


class FakeLLM:
    def __init__(self, script=None, verdict=None):
        self.script = list(script or [])
        self.verdict = verdict          # callable(user_text) -> GuardrailVerdict dict
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.script:
            return self.script.pop(0)

        route = kwargs.get("model", "")
        served = SERVED.get(route, "fake-model")
        tools = kwargs.get("tools") or []
        if not tools:
            return text_response("5", model=served)

        name = tools[0]["function"]["name"]
        user = next(m["content"] for m in kwargs["messages"] if m["role"] == "user")
        if name == "ClauseExtractionDraft":
            match = _CLAUSE.search(user)
            clause_id, heading, text = match.groups() if match else ("", "", user)
            payload = stub.synthesize(context={
                "clause_id": clause_id, "clause_text": text, "heading": heading,
                "variant": 1 if route == "regextract-second" else 0,
            })
            return tool_response(name, payload, model=served)
        if name == "GuardrailVerdict":
            verdict = (self.verdict(user) if self.verdict
                       else {"safe": True, "severity": "none", "reason": "ordinary regulatory text"})
            return tool_response(name, verdict, model=served)
        raise AssertionError(f"FakeLLM has no answer for a {name} request")
