"""Record and replay model responses.

Cassettes make an LLM pipeline testable. Record once against a real provider,
then every later run, CI job and eval is deterministic and free. The key is a
hash of everything that could change the answer, so editing a prompt
invalidates the recording rather than silently replaying a stale one.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..config import PROMPT_VERSION, TAXONOMY_VERSION


def make_key(*, model: str, system: str, user: str, tag: str) -> str:
    basis = "|".join([model, PROMPT_VERSION, TAXONOMY_VERSION, tag, system, user])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def load(directory: Path, key: str) -> dict | None:
    path = Path(directory) / f"{key}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def save(directory: Path, key: str, payload: dict) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{key}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def count(directory: Path) -> int:
    directory = Path(directory)
    if not directory.exists():
        return 0
    return len(list(directory.glob("*.json")))
