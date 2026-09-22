"""An append-only, hash-chained publish log.

Compliance customers do not just need the data; they need to prove it.
So "published items never change quietly" (plan, section 4) has to be
checkable, not just true. Every publication, withdrawal and reviewer decision
is one line in publish_log.jsonl, and each line carries the SHA-256 of the line
before it. Edit, delete or reorder any past line and `run.py verify-log` names
the line where the chain breaks.

Each entry carries its own hash and the hash of the entry before it
(prev_hash), and two checks verify them. In production the log would also sit
in object storage under a compliance-mode lock, so not even an administrator
can remove an entry. On
its own, a file gives tamper evidence, not tamper resistance.

Re-running an unchanged document adds a run record and no new publications:
an item is logged again only when its content changes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _entry_hash(entry: dict) -> str:
    return _sha256(_canonical({k: v for k, v in entry.items() if k != "entry_hash"}))


def content_hash(item) -> str:
    return _sha256(_canonical({
        "item_id": item.item_id, "clause_id": item.clause_id, "kind": item.kind,
        "type": item.type_name, "payload": item.payload, "quote": item.evidence.quote,
        "char_start": item.evidence.char_start, "char_end": item.evidence.char_end,
        "decision": item.review.decision.value,
    }))


def read_entries(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class PublishLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.entries = read_entries(self.path)
        # item_id -> what is currently published for it
        self._live: dict[str, dict] = {}
        for entry in self.entries:
            body = entry["body"]
            if entry["event"] == "published":
                self._live[body["item_id"]] = body
            elif entry["event"] == "withdrawn":
                self._live.pop(body["item_id"], None)

    @property
    def head(self) -> str:
        return self.entries[-1]["entry_hash"] if self.entries else GENESIS

    def append(self, event: str, body: dict) -> dict:
        entry = {
            "seq": len(self.entries),
            "event": event,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "body": body,
            "prev_hash": self.head,
        }
        entry["entry_hash"] = _entry_hash(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(_canonical(entry) + "\n")
        self.entries.append(entry)
        return entry

    def record(self, *, published: list, document, run_id: str, review_events=()) -> dict:
        for event in review_events:
            self.append("review_decision", event)

        new = unchanged = withdrawn = 0
        current_ids = set()
        for item in published:
            current_ids.add(item.item_id)
            digest = content_hash(item)
            if self._live.get(item.item_id, {}).get("content_hash") == digest:
                unchanged += 1
                continue
            body = {
                "item_id": item.item_id, "document_id": document.id, "revision": document.revision,
                "clause_id": item.clause_id, "kind": item.kind, "type": item.type_name,
                "decision": item.review.decision.value, "reviewer": item.review.reviewer or None,
                "supersedes": item.supersedes, "content_hash": digest, "run_id": run_id,
            }
            self.append("published", body)
            self._live[item.item_id] = body
            new += 1

        # Something published before and not now has changed too, and says so.
        for item_id, body in list(self._live.items()):
            if body.get("document_id") == document.id and item_id not in current_ids:
                self.append("withdrawn", {"item_id": item_id, "document_id": document.id,
                                          "run_id": run_id, "reason": "no longer published"})
                del self._live[item_id]
                withdrawn += 1

        self.append("run_recorded", {
            "run_id": run_id, "document_id": document.id, "revision": document.revision,
            "source_hash": document.source_hash, "published_new": new,
            "published_unchanged": unchanged, "withdrawn": withdrawn,
            "review_decisions": len(review_events),
        })
        return {"published_new": new, "published_unchanged": unchanged, "withdrawn": withdrawn,
                "review_decisions": len(review_events), "log_entries": len(self.entries)}


def verify_log(path: Path) -> dict:
    """Two checks: every entry hashes to what it claims, and every
    entry points at the one before it."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    breaks: list[dict] = []
    expected_prev = GENESIS
    for number, line in enumerate(lines, start=1):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            breaks.append({"line": number, "reason": "not valid JSON"})
            expected_prev = None
            continue
        if entry.get("entry_hash") != _entry_hash(entry):
            breaks.append({"line": number, "reason": "entry_hash does not match the content: "
                                                     "this entry was edited after it was written"})
        elif expected_prev is not None and entry.get("prev_hash") != expected_prev:
            breaks.append({"line": number, "reason": "prev_hash does not match the previous entry: "
                                                     "an entry was inserted, deleted or reordered"})
        expected_prev = entry.get("entry_hash")
    return {"valid": not breaks and bool(lines), "entries": len(lines), "breaks": breaks,
            "path": str(path)}
