"""LLM-as-judge -- triage only.

This is the one place a model is asked for an opinion about another model's
output, and it is deliberately fenced in.

  It orders the review queue. It does not decide what goes in it.

Nothing here can promote an item to auto_accept, demote it to reject, or
override the grounding gate. A model cannot be both the thing under test and
the test. Section 10.2 of the plan: "LLM-as-judge only for triage and
prioritisation, never as the release gate."

Off by default. When off, the queue falls back to ordering by impact tier and
score, which is already a sensible order.
"""

from __future__ import annotations

import re

from ..config import Settings, get_settings
from ..contract import Clause, Item
from ..extraction.prompts import JUDGE_SYSTEM
from ..llm.gateway import Gateway

_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _prompt_for(item: Item, clause: Clause | None) -> str:
    clause_text = (clause.text[:1200] if clause else "")
    return (
        f"CLAUSE {item.clause_id}\n\"\"\"\n{clause_text}\n\"\"\"\n\n"
        f"EXTRACTED FACT\n"
        f"  kind:      {item.kind}\n"
        f"  type:      {item.type_name}\n"
        f"  payload:   {item.payload}\n"
        f"  grounded:  {item.evidence.grounded} ({item.evidence.match_kind.value})\n"
        f"  agreement: {item.review.agreement}\n"
        f"  validators failing: "
        f"{[k for k, v in item.review.validators.items() if not v] or 'none'}\n\n"
        "How urgently should an analyst look at this one? 0 to 10."
    )


def score_priority(
    *,
    gateway: Gateway,
    items: list[Item],
    clauses: list[Clause],
    settings: Settings | None = None,
    limit: int = 40,
) -> list[Item]:
    """Attach a triage priority to items already destined for review.

    Only the top `limit` items are judged. Judging the whole queue costs money
    to reorder things nobody will reach today.
    """
    settings = settings or get_settings()
    if not settings.use_judge:
        return items

    by_id = {c.id: c for c in clauses}
    pending = [i for i in items if i.review.decision.value != "auto_accept"]
    pending.sort(key=lambda i: (i.review.impact.value != "high", i.review.score))

    for item in pending[:limit]:
        reply, meta = gateway.text(
            system=JUDGE_SYSTEM,
            user=_prompt_for(item, by_id.get(item.clause_id)),
            route="judge",
            # The budget covers the model's thinking as well as its answer. A
            # reasoning model given only a few tokens returns an empty reply.
            max_tokens=512,
        )
        if meta.error or not reply:
            continue
        found = _NUMBER.search(reply)
        if found:
            item.review.judge_priority = max(0.0, min(10.0, float(found.group(0))))

    return items
