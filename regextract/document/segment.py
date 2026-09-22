"""Stage 2 -- structure.

Deterministic. No model involved.

Numbering patterns are cheap to get right in code and expensive to get right
probabilistically, and every stage after this one depends on the tree being
correct. So the LLM is a fallback for documents where numbering fails
completely, not the primary mechanism.

The hard part in CARL-01 is telling a clause number from a table row. Annex 1
rows begin "1 Water Heater ...", "2 Extension Cords ...", and section 4 rows
begin "1 UAE / IEC 60335-1: 2007 ...". None of those are clauses. Three rules
separate them:

  * `2.1`, `7.1.1.2`  -- any dotted number is a clause
  * `6.` with a period -- a clause
  * `10` with no period -- a clause only if the rest of the line is upper case,
    which is what makes "10 INSPECTION AND MARKET MONITORING" work while
    "10 Electromechanical Kitchen Appliances" stays a table row

Unnumbered sections (FEES, the contact block, ANNEX 1) get synthetic ids.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..contract import Clause
from .ingest import CanonicalDocument

_DOTTED = re.compile(r"^(\d+(?:\.\d+)+)\.?\s+(\S.*)$")
_TOP_WITH_PERIOD = re.compile(r"^(\d+)\.\s+(\S.*)$")
_TOP_NO_PERIOD = re.compile(r"^(\d+)\s+(\S.*)$")

_ANNEX = re.compile(r"^ANNEX\s+(\w+)\b[.:]?\s*(.*)$", re.I)
_FEES = re.compile(r"^FEES\s*$", re.I)
_CONTACT = re.compile(r"^For\s+more\s+information", re.I)


def _upper_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if c.isupper()) / len(letters)


@dataclass
class _Start:
    clause_id: str
    heading: str
    line_start: int      # offset of the line in canonical text
    swallow_rest: bool = False   # take everything to the next marker (annex, contact)


def _classify(line: str) -> tuple[str, str] | None:
    """Return (clause_id, heading_text) if this line starts a clause."""
    stripped = line.strip()
    if not stripped:
        return None

    match = _DOTTED.match(stripped)
    if match:
        return match.group(1), match.group(2)

    match = _TOP_WITH_PERIOD.match(stripped)
    if match:
        return match.group(1), match.group(2)

    match = _TOP_NO_PERIOD.match(stripped)
    if match and _upper_ratio(match.group(2)) > 0.8:
        # "10 INSPECTION AND MARKET MONITORING" -- yes.
        # "10 Electromechanical Kitchen Appliances" -- no, that is a table row.
        return match.group(1), match.group(2)

    return None


def segment(canonical: CanonicalDocument) -> list[Clause]:
    text = canonical.text
    starts: list[_Start] = []

    offset = 0
    annex_seen = False
    contact_seen = False

    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        line_offset = offset
        offset += len(line)

        if not stripped:
            continue

        # Annex swallows everything to the end of the document. It is one
        # clause on purpose -- no table engine in this build. See failure
        # class I in the plan.
        if not annex_seen:
            annex_match = _ANNEX.match(stripped)
            if annex_match:
                annex_seen = True
                label = annex_match.group(1).upper()
                starts.append(_Start(f"ANNEX-{label}", stripped, line_offset, swallow_rest=True))
                continue

        if annex_seen:
            continue  # everything after the annex heading belongs to the annex

        if not contact_seen and _CONTACT.match(stripped):
            contact_seen = True
            starts.append(_Start("CONTACT", stripped, line_offset))
            continue

        if _FEES.match(stripped):
            starts.append(_Start("FEES", "FEES", line_offset))
            continue

        classified = _classify(stripped)
        if classified:
            clause_id, heading = classified
            starts.append(_Start(clause_id, heading, line_offset))

    return _build_clauses(starts, canonical)


def _build_clauses(starts: list[_Start], canonical: CanonicalDocument) -> list[Clause]:
    text = canonical.text
    clauses: list[Clause] = []

    for index, start in enumerate(starts):
        end = len(text) if index == len(starts) - 1 else starts[index + 1].line_start
        body = text[start.line_start:end].rstrip()

        page_start = canonical.page_for_offset(start.line_start) or 0
        last_char = max(start.line_start, start.line_start + len(body) - 1)
        page_end = canonical.page_for_offset(last_char) or page_start

        parent = None
        path = [start.clause_id]
        if "." in start.clause_id and start.clause_id[0].isdigit():
            parts = start.clause_id.split(".")
            path = parts
            parent = ".".join(parts[:-1])

        clauses.append(
            Clause(
                id=start.clause_id,
                parent=parent,
                path=path,
                heading=start.heading,
                text=body,
                page_start=page_start,
                page_end=page_end,
                char_start=start.line_start,
                char_end=start.line_start + len(body),
            )
        )

    return clauses


# ---------------------------------------------------------------------------
# Structure checks -- these feed the document-level review triggers
# ---------------------------------------------------------------------------


def structure_report(clauses: list[Clause]) -> dict:
    """Numbering gaps, duplicates, and how much text landed outside any clause.

    Section 7.4 of the plan uses these as document-level triggers: if the tree
    looks wrong, we do not trust item-level confidence scores on that document.
    """
    ids = [c.id for c in clauses]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})

    numeric_tops = sorted(
        {int(c.id.split(".")[0]) for c in clauses if c.id.split(".")[0].isdigit()}
    )
    gaps: list[int] = []
    if numeric_tops:
        expected = set(range(min(numeric_tops), max(numeric_tops) + 1))
        gaps = sorted(expected - set(numeric_tops))

    return {
        "clause_count": len(clauses),
        "top_level_sections": numeric_tops,
        "numbering_gaps": gaps,
        "duplicate_ids": duplicates,
        "max_depth": max((c.depth for c in clauses), default=0),
        "synthetic_ids": [c.id for c in clauses if not c.id[0].isdigit()],
    }
