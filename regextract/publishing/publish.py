"""Stage 9 -- publish, and stage 10 -- diff.

Publishing writes a versioned, content-hashed record. Nothing is ever
overwritten in place: a correction produces a new version and an event
carrying the difference, so downstream can react to what changed rather than
re-reading everything.

Stage 10 is the one that turns extraction into the actual product. Compliance
customers care about regulatory *change*. Comparing revision 5 against
revision 4 at clause level, and sending only the deltas to review, is what
makes the system affordable at steady state and useful at all.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from ..contract import Clause, Decision, ExtractionRun, Item
from ..resolution.vectors import cosine, local_embedding


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


def write_outputs(run: ExtractionRun, output_dir: Path) -> dict[str, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    clauses_path = output_dir / "clauses.json"
    clauses_path.write_text(
        json.dumps([c.model_dump() for c in run.clauses], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    written["clauses"] = clauses_path

    extractions_path = output_dir / "extractions.json"
    extractions_path.write_text(
        json.dumps(run.model_dump(), indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    written["extractions"] = extractions_path

    queue_path = output_dir / "review_queue.csv"
    _write_review_queue(run.items, queue_path, run.flags)
    written["review_queue"] = queue_path

    return written


def _write_review_queue(items: list[Item], path: Path, flags=()) -> None:
    from ..routing.route import review_queue

    pending = review_queue(items)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "item_id", "impact", "clause_id", "kind", "type", "score",
            "agreement", "grounded", "match", "judge_priority", "reasons", "quote",
        ])
        # Clauses with no extraction at all come first. There is no item to
        # review, only a hole, and a reviewer has to know where it is.
        for flag in flags:
            if flag.code != "extraction_failed":
                continue
            for clause_id in flag.clause_ids:
                writer.writerow([
                    "", "high", clause_id, "clause", "extraction_failed", "", "", "", "", "",
                    "extraction_failed: the model returned nothing usable for this clause", "",
                ])
        for item in pending:
            writer.writerow([
                item.item_id,
                item.review.impact.value,
                item.clause_id,
                item.kind,
                item.type_name,
                f"{item.review.score:.3f}",
                item.review.agreement,
                item.evidence.grounded,
                item.evidence.match_kind.value,
                "" if item.review.judge_priority is None else item.review.judge_priority,
                "; ".join(item.review.reasons),
                (item.evidence.quote or "")[:180].replace("\n", " "),
            ])


def published_items(run: ExtractionRun) -> list[Item]:
    """What actually leaves the system.

    Two conditions, both required. Accepted -- by the router or by a
    reviewer -- AND grounded. The second is redundant today because routing
    and review both enforce it. It is here because this is the last gate
    before downstream, and a redundant check at the boundary is cheap
    insurance against a future refactor.
    """
    return [
        i for i in run.items
        if i.review.decision in (Decision.AUTO_ACCEPT, Decision.ACCEPTED) and i.evidence.grounded
    ]


# ---------------------------------------------------------------------------
# Diff -- stage 10
# ---------------------------------------------------------------------------


@dataclass
class ItemDelta:
    change: str            # added | removed | changed
    clause_id: str
    type_name: str
    before: dict | None
    after: dict | None

    def as_dict(self) -> dict:
        return {
            "change": self.change,
            "clause_id": self.clause_id,
            "type": self.type_name,
            "before": self.before,
            "after": self.after,
        }


def _key(item: Item, clause_id: str | None = None) -> tuple[str, str, str]:
    from ..verification.verify import signature

    return (clause_id or item.clause_id, item.kind, signature(item.kind, item.payload))


def match_clauses(previous: list[Clause], current: list[Clause], *,
                  threshold: float = 0.9, same_id_floor: float = 0.5) -> dict[str, str]:
    """Pair each clause of the old revision with its counterpart in the new one.

    Id first, text second -- the plan's rule. An id alone is not enough:
    insert a new 11.2 and the old 11.2 becomes 11.3, so the same id now holds
    different text. So a same-id pair must also look alike, and whatever is
    left over is paired by text similarity above a high threshold. This is the
    second of the two narrow jobs embeddings do in this project.
    """
    def vector(clause: Clause) -> list[float]:
        return local_embedding(f"{clause.heading} {clause.text}")

    old = {c.id: vector(c) for c in previous}
    new = {c.id: vector(c) for c in current}

    mapping: dict[str, str] = {}
    for clause_id in old.keys() & new.keys():
        if cosine(old[clause_id], new[clause_id]) >= same_id_floor or not any(old[clause_id]):
            mapping[clause_id] = clause_id

    left_old = [cid for cid in old if cid not in mapping]
    left_new = {cid for cid in new if cid not in mapping.values()}
    pairs = sorted(((cosine(old[o], new[n]), o, n) for o in left_old for n in left_new), reverse=True)
    for score, old_id, new_id in pairs:
        if score < threshold:
            break
        if old_id not in mapping and new_id in left_new:
            mapping[old_id] = new_id
            left_new.discard(new_id)
    return mapping


def renumbered_clauses(clause_map: dict[str, str]) -> list[dict]:
    return [{"from": old, "to": new} for old, new in sorted(clause_map.items()) if old != new]


def diff_runs(previous: ExtractionRun, current: ExtractionRun, *,
              clause_map: dict[str, str] | None = None) -> list[ItemDelta]:
    """Clause-level delta between two revisions of the same document.

    Facts in the old revision are keyed by the clause they map to in the new
    one, so a renumbered clause with the same facts produces no deltas. A
    clause with no counterpart keeps a key that can never match, so its facts
    show up as removed rather than colliding with a new clause that reused
    its id.
    """
    if clause_map is None:
        clause_map = match_clauses(previous.clauses, current.clauses)
    old = {_key(i, clause_map.get(i.clause_id, f"unmatched:{i.clause_id}")): i for i in previous.items}
    new = {_key(i): i for i in current.items}

    deltas: list[ItemDelta] = []
    for key, item in new.items():
        if key not in old:
            deltas.append(ItemDelta("added", item.clause_id, item.type_name, None, item.payload))
    for key, item in old.items():
        if key not in new:
            deltas.append(ItemDelta("removed", item.clause_id, item.type_name, item.payload, None))

    # A clause whose text moved but whose facts survived is not a change worth
    # a reviewer's time. Only facts count.
    return sorted(deltas, key=lambda d: (d.clause_id, d.change))


def changed_obligations_event(
    *, document_id: str, from_revision: str, to_revision: str, deltas: list[ItemDelta],
    renumbered: list[dict] | None = None,
) -> dict:
    """The event downstream systems subscribe to.

    Deliberately small: identifiers and the deltas, not the whole document.
    A consumer that needs full context fetches it by id.
    """
    obligations = [d for d in deltas if d.type_name == "obligation"]
    return {
        "event": "changed_obligations",
        "document_id": document_id,
        "from_revision": from_revision,
        "to_revision": to_revision,
        "counts": {
            "total_deltas": len(deltas),
            "obligation_deltas": len(obligations),
            "added": sum(1 for d in deltas if d.change == "added"),
            "removed": sum(1 for d in deltas if d.change == "removed"),
            "renumbered_clauses": len(renumbered or []),
        },
        "obligation_deltas": [d.as_dict() for d in obligations],
        "renumbered_clauses": renumbered or [],
    }
