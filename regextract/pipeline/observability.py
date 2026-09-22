"""Run observability.

Every stage records how long it took, what it produced and what it cost. The
result is a single run manifest written next to the outputs, which is what
makes a result explainable months later.

This is deliberately plain. In production these same events become
OpenTelemetry spans and the counters become metrics -- per-stage latency, cost
per document, grounding-failure rate, reviewer override rate. The shape does
not change, only the sink.
"""

from __future__ import annotations

import json
import platform
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class StageEvent:
    stage: str
    started_at: float
    duration_ms: float = 0.0
    counters: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "duration_ms": round(self.duration_ms, 1),
            "counters": self.counters,
            "error": self.error,
        }


class RunRecorder:
    def __init__(self, run_id: str | None = None):
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.events: list[StageEvent] = []
        self.started = time.perf_counter()
        self.notes: list[str] = []

    def stage(self, name: str) -> "_StageContext":
        return _StageContext(self, name)

    def note(self, message: str) -> None:
        self.notes.append(message)

    def total_ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000

    def manifest(self, *, settings_describe: dict, gateway_summary: dict,
                 routing: dict, structure: dict, flags: list) -> dict:
        return {
            "run_id": self.run_id,
            "total_ms": round(self.total_ms(), 1),
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
            "settings": settings_describe,
            "stages": [e.as_dict() for e in self.events],
            "model_calls": gateway_summary,
            "structure": structure,
            "routing": routing,
            "document_flags": [{"code": f.code, "detail": f.detail} for f in flags],
            "notes": self.notes,
        }

    def write(self, path: Path, manifest: dict) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        return path


class _StageContext:
    def __init__(self, recorder: RunRecorder, name: str):
        self.recorder = recorder
        self.event = StageEvent(stage=name, started_at=time.perf_counter())

    def __enter__(self) -> StageEvent:
        return self.event

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.event.duration_ms = (time.perf_counter() - self.event.started_at) * 1000
        if exc is not None:
            self.event.error = f"{exc_type.__name__}: {exc}"
        self.recorder.events.append(self.event)
        return False
