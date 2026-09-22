"""Deterministic offline extractor.

This is NOT a model and it does not pretend to be one. It is a rule-based
stand-in that produces schema-valid, genuinely grounded extractions from the
clause text, so that the rest of the pipeline -- grounding gate, validators,
routing, evaluation, observability -- can be run and tested end to end with no
API key and no cost.

Anything it produces is labelled `source: stub` in the run manifest, and the
evaluation report refuses to present stub numbers as model quality.

Two deliberate behaviours worth knowing:

  * It never emits a monetary_threshold and never emits a date inside a clause.
    Those are the abstention tests (failure class E), and the stub has to get
    them right for the harness to be meaningful.

  * With fault injection switched on it perturbs a fixed share of quotes, so
    the grounding gate has something real to catch and the baseline number is
    demonstrable. Off by default.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Lexicons
# ---------------------------------------------------------------------------

_ROLES = [
    "manufacturer", "manufacturers", "trader", "traders", "applicant",
    "supplier", "client", "clients", "authorized representative",
    "authorised representative", "agent",
]
_ORGS = [
    "ESMA", "Emirates Authority for Standardization and Metrology",
    "Ports and Customs Authorities", "Ports and Customs authority",
    "Conformity Affairs Department",
]
_LOCATIONS = ["United Arab Emirates", "UAE", "Dubai", "Port of Entry"]

_STANDARD = re.compile(
    r"\b(?:UAE\.?\s?S?\s*/?\s*)?(?:GSO\s*/?\s*)?(?:IEC|BS|EN|ISO|GSO)\s?\d+(?:-\d+)*(?:\s*:\s*\d{4})?",
    re.I,
)
_NUMERIC = re.compile(
    r"\b\d+\s*(?:volts?|V\b|Ampere|amps?|A\b)|\bbetween\s+\d+\s+and\s+\d+\s+volts?",
    re.I,
)
_TIME_PERIOD = re.compile(
    r"\b(?:one|two|three|four|five|six|a)\s+(?:year|month|week|day)s?\b|\b\d+\s+(?:year|month|week|day)s?\b",
    re.I,
)
_LEGAL = re.compile(r"Federal\s+Law\s+No\.?\s*\d+|ECAS\s+General\s+Requirements", re.I)

_MODALITIES = [
    ("reserves the right", "reserves_right"),
    (" shall ", "shall"),
    (" must ", "must"),
    (" should ", "should"),
    (" may ", "may"),
    (" can ", "may"),
]

_XREF_EXTERNAL = re.compile(r"clause\s+no\.?\s*(\d+)\s+of\s+([A-Z][A-Za-z ]+?)(?:\.|,|$)", re.I)
_XREF_INTERNAL = re.compile(r"clause\s+(\d+)\s+of\s+this\s+document", re.I)
_XREF_ITEM = re.compile(r"\bitem\s+(\d+(?:\.\d+)*)", re.I)
_XREF_ANNEX = re.compile(r"\bAnnex\s+([IVX]+|\d+)\b", re.I)

_SENTENCE_SPLIT = re.compile(r"(?<=[.;:])\s+|\n+")


def _sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(text)]
    return [p for p in parts if len(p) > 12]


def _sentence_containing(sentences: list[str], needle: str, fallback: str) -> str:
    for sentence in sentences:
        if needle.lower() in sentence.lower():
            return sentence
    return fallback


def _context_for(clause_text: str, sentences: list[str], needle: str) -> str:
    """A quote that is guaranteed to contain `needle` and to be a real
    substring of the clause.

    Sentence, then line, then a character window. Falling through to a
    fallback that does not contain the entity would produce an item that
    fails its own validator -- a stub bug masquerading as a model error.
    """
    for sentence in sentences:
        if needle.lower() in sentence.lower():
            return sentence
    for line in clause_text.splitlines():
        if needle.lower() in line.lower():
            return line.strip()
    index = clause_text.lower().find(needle.lower())
    if index == -1:
        return needle
    start = max(0, index - 100)
    end = min(len(clause_text), index + len(needle) + 100)
    return clause_text[start:end].strip()


def _roman_to_int(value: str) -> str:
    numerals = {"I": 1, "V": 5, "X": 10}
    if not all(ch.upper() in numerals for ch in value):
        return value
    total = 0
    previous = 0
    for ch in reversed(value.upper()):
        current = numerals[ch]
        total = total - current if current < previous else total + current
        previous = max(previous, current)
    return str(total)


# ---------------------------------------------------------------------------
# Extraction rules
# ---------------------------------------------------------------------------


def _find_subject(sentence: str, modality_marker: str) -> str:
    lowered = sentence.lower()
    index = lowered.find(modality_marker.strip())

    # Passive voice: "Samples shall be collected by ESMA" -> the agent is ESMA.
    tail = sentence[index:] if index != -1 else sentence
    by_agent = re.search(r"\bby\s+(?:an?\s+|the\s+)?([A-Za-z][\w\- ]{2,40}?)(?:\s+for\b|[.,;]|$)", tail)
    if by_agent:
        candidate = by_agent.group(1).strip()
        for role in _ROLES + [o.lower() for o in _ORGS]:
            if role.lower() in candidate.lower():
                return candidate
        if candidate.isupper() or candidate.split()[0][:1].isupper():
            return candidate

    head = sentence[:index] if index != -1 else sentence
    for org in _ORGS:
        if org.lower() in head.lower():
            return org
    for role in _ROLES:
        if re.search(rf"\b{re.escape(role)}\b", head, re.I):
            return role
    return ""


def _obligations(clause_text: str, sentences: list[str]) -> list[dict]:
    found: list[dict] = []
    for sentence in sentences:
        padded = f" {sentence} "
        for marker, modality in _MODALITIES:
            if marker in padded.lower():
                subject = _find_subject(sentence, marker)
                index = padded.lower().find(marker)
                action = padded[index + len(marker):].strip(" .;:")
                condition = ""
                condition_match = re.search(
                    r"\b(a month before[^.]*|before[^.]*expiration[^.]*|where[^.]*|upon[^.]*|"
                    r"in case[^.]*|if[^.]*|when[^.]*)", sentence, re.I)
                if condition_match:
                    condition = condition_match.group(1).strip(" .;:")
                found.append({
                    "subject": subject,
                    "modality": modality,
                    "action": action[:220],
                    "condition": condition[:180],
                    "quote": sentence,
                    "confidence": 0.72,
                })
                break
    return found


def _entities(clause_text: str, sentences: list[str]) -> list[dict]:
    found: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(entity_type: str, text: str, normalized: str = "") -> None:
        key = (entity_type, text.strip().lower())
        if key in seen or not text.strip():
            return
        seen.add(key)
        found.append({
            "type": entity_type,
            "text": text.strip(),
            "normalized": normalized,
            "quote": _context_for(clause_text, sentences, text.strip()),
            "confidence": 0.7,
        })

    for match in _STANDARD.finditer(clause_text):
        add("standard_reference", match.group(0), re.sub(r"\s+", " ", match.group(0)))
    for match in _NUMERIC.finditer(clause_text):
        add("numeric_threshold", match.group(0), re.sub(r"\s+", " ", match.group(0)).lower())
    for match in _TIME_PERIOD.finditer(clause_text):
        raw = match.group(0)
        words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "a": 1}
        head = raw.split()[0].lower()
        value = words.get(head, head if head.isdigit() else "")
        unit = raw.split()[-1].lower().rstrip("s")
        add("time_period", raw, f"{value} {unit}".strip())
    for org in _ORGS:
        if org.lower() in clause_text.lower():
            add("organisation", org)
    for location in _LOCATIONS:
        if re.search(rf"\b{re.escape(location)}\b", clause_text):
            add("location", location)
    for role in _ROLES:
        if re.search(rf"\b{re.escape(role)}\b", clause_text, re.I):
            add("role", role.lower())
    for match in _LEGAL.finditer(clause_text):
        add("legal_reference", match.group(0))

    # Annex product rows: "1 Water Heater UAE.S / GSO/ IEC 60335-2-21: 2007"
    for line in clause_text.splitlines():
        row = re.match(r"^\s*\d{1,2}\s+([A-Z][A-Za-z&/ ,\-]{3,60}?)\s+(?=UAE|IEC|BS|GSO)", line)
        if row:
            add("product_category", row.group(1).strip())

    # Deliberately absent: monetary_threshold and date. CARL-01 contains
    # neither inside its clauses, and inventing one is exactly the failure
    # the grounding gate and failure class E exist to catch.
    return found


def _cross_references(clause_text: str, sentences: list[str]) -> list[dict]:
    found: list[dict] = []

    for match in _XREF_EXTERNAL.finditer(clause_text):
        found.append({
            "text": match.group(0).strip(" .,"),
            "scope": "external",
            "target_document": match.group(2).strip(),
            "target_clause_id": match.group(1),
            "quote": _sentence_containing(sentences, match.group(0)[:30], clause_text[:300]),
            "confidence": 0.68,
        })
    for match in _XREF_INTERNAL.finditer(clause_text):
        found.append({
            "text": match.group(0).strip(" .,"),
            "scope": "internal",
            "target_document": "",
            "target_clause_id": match.group(1),
            "quote": _sentence_containing(sentences, match.group(0)[:30], clause_text[:300]),
            "confidence": 0.74,
        })
    for match in _XREF_ITEM.finditer(clause_text):
        found.append({
            "text": match.group(0).strip(" .,"),
            "scope": "internal",
            "target_document": "",
            "target_clause_id": match.group(1),
            "quote": _sentence_containing(sentences, match.group(0)[:20], clause_text[:300]),
            "confidence": 0.7,
        })
    for match in _XREF_ANNEX.finditer(clause_text):
        found.append({
            "text": match.group(0).strip(" .,"),
            "scope": "internal",
            "target_document": "",
            "target_clause_id": f"ANNEX-{_roman_to_int(match.group(1))}",
            "quote": _sentence_containing(sentences, match.group(0)[:12], clause_text[:300]),
            "confidence": 0.66,
        })
    return found


def _clause_types(clause_id: str, heading: str, clause_text: str) -> list[str]:
    lowered = f"{heading} {clause_text}".lower()
    types: list[str] = []

    def mark(name: str) -> None:
        if name not in types:
            types.append(name)

    if re.match(r"^5\.\d+$", clause_id) or "definition" in lowered:
        mark("definition")
    if "shall not apply" in lowered or "excluded" in lowered:
        mark("exemption")
    if "voltage rating between" in lowered or clause_id.startswith("3.1.1"):
        mark("scope")
    if "reserves the right" in lowered or "shall be held" in lowered or "stop the registration" in lowered:
        mark("enforcement")
    if "valid for" in lowered or "renewal" in lowered or "expiration" in lowered:
        mark("validity_and_dates")
    if "responsible" in lowered or "bear all the cost" in lowered or "shall be paid by" in lowered:
        mark("responsibility")
    if re.match(r"^(7|8|9)(\.|$)", clause_id):
        mark("procedure")
    if re.match(r"^4(\.|$)", clause_id) or clause_id.startswith("ANNEX"):
        mark("normative_reference")
    if clause_id in {"FEES", "CONTACT", "1", "2"}:
        mark("administrative")
    if not types or " shall " in f" {lowered} ":
        mark("requirement")
    return types


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def synthesize(*, context: dict[str, Any], schema: dict | None = None) -> dict:
    clause_text: str = context.get("clause_text", "") or ""
    clause_id: str = context.get("clause_id", "") or ""
    heading: str = context.get("heading", "") or ""
    variant: int = int(context.get("variant", 0))
    inject_faults: bool = bool(context.get("inject_faults", False))

    sentences = _sentences(clause_text)
    payload = {
        "clause_types": _clause_types(clause_id, heading, clause_text),
        "obligations": _obligations(clause_text, sentences),
        "entities": _entities(clause_text, sentences),
        "cross_references": _cross_references(clause_text, sentences),
    }

    # The second run is meant to be a different model family. Offline we
    # cannot have one, so we simulate mild disagreement instead: drop the
    # lowest-confidence obligation. That gives the agreement signal something
    # to measure and keeps the routing logic honestly exercised.
    if variant == 1 and payload["obligations"]:
        payload["obligations"] = payload["obligations"][:-1] or payload["obligations"]

    if inject_faults:
        payload = _inject_faults(payload, clause_id)

    return payload


def _inject_faults(payload: dict, clause_id: str) -> dict:
    """Perturb a deterministic share of quotes so the grounding gate has
    something real to reject. Used only by `--inject-faults`."""
    bucket = sum(ord(c) for c in clause_id) % 5
    if bucket != 0:
        return payload
    for group in ("obligations", "entities", "cross_references"):
        for index, item in enumerate(payload.get(group, [])):
            if index == 0 and item.get("quote"):
                item["quote"] = item["quote"] + " and additionally a penalty of AED 50,000 applies"
                item["_injected_fault"] = True
    return payload
