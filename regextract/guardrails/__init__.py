"""Input guardrails for untrusted document text: a deterministic scanner that
always runs, and a policy classifier model on top."""

from .rails import (
    GUARDRAIL_POLICY,
    ClassifierResult,
    GuardrailFinding,
    GuardrailVerdict,
    classify_clauses,
    scan_clauses,
    scan_text,
)

__all__ = ["scan_text", "scan_clauses", "classify_clauses", "GuardrailFinding",
           "GuardrailVerdict", "ClassifierResult", "GUARDRAIL_POLICY"]
