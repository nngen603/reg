"""Stage 1 -- ingest.

Turns a PDF into one canonical text with character offsets, strips the
repeating header and footer, harvests document metadata out of them, and
repairs text-layer artefacts.

Two rules drive the design.

First, later stages must never care what the source format was. Everything
after this point sees the same canonical model whether the input was a native
PDF, HTML, or a scan.

Second, artefact repair is deliberately conservative. In CARL-01 the headings
come out as `UINTRODUCTIONU:` and `U12. LIABILITY`, so a naive rule would be
"strip a leading U before capitals". That rule would also turn `UAE STANDARD`
into `AE STANDARD` in the section 4 table. So repair only runs on lines that
already look like damaged headings, and only for patterns we can point at.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import PIPELINE_VERSION
from ..contract import Document

# ---------------------------------------------------------------------------
# Artefact repair
# ---------------------------------------------------------------------------

# Safe anywhere: the PDF wraps links as 0TU...U0T.
_LINK_WRAPPER = re.compile(r"0TU(.+?)U0T")
_STRAY_0T = re.compile(r"\b0T\b")

# Only applied to lines that look like headings.
_R1_NUMBERED = re.compile(r"^(\s*\d+(?:\.\d+)*\.?\s+)U(?=[A-Z])")
_R2_BARE_WORD = re.compile(r"^U([A-Z]{3,})\s*$")
_R3_BEFORE_NUMBER = re.compile(r"^(\s*)U(?=\d+\.)")
# Only applied once one of R1..R3 has already fired on the same line, so a
# legitimate heading ending in "U" is never touched.
_R4_TRAILING = re.compile(r"([A-Z]{2,})U(?=\s*:?\s*$)")

_HEADING_HINT = re.compile(r"^\s*(?:U?\d+(?:\.\d+)*\.?\s+|U?[A-Z][A-Z /&\-]{2,})")


def looks_like_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 90:
        return False
    letters = [c for c in stripped if c.isalpha()]
    if not letters:
        return False
    upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
    return upper_ratio > 0.6 and bool(_HEADING_HINT.match(stripped))


def repair_line(line: str) -> tuple[str, bool]:
    """Return (repaired_line, changed)."""
    original = line
    line = _LINK_WRAPPER.sub(r"\1", line)
    line = _STRAY_0T.sub("", line)

    if looks_like_heading(line):
        fired = False
        for pattern, replacement in (
            (_R1_NUMBERED, r"\1"),
            (_R2_BARE_WORD, r"\1"),
            (_R3_BEFORE_NUMBER, r"\1"),
        ):
            new_line, count = pattern.subn(replacement, line)
            if count:
                line = new_line
                fired = True
        if fired:
            line = _R4_TRAILING.sub(r"\1", line)

    return line, line != original


# ---------------------------------------------------------------------------
# Header and footer detection
# ---------------------------------------------------------------------------

_DIGITS = re.compile(r"\d+")


def _line_signature(line: str) -> str:
    """Collapse a line to a shape, so 'Page (1) of (8)' and 'Page (2) of (8)'
    are recognised as the same repeating furniture."""
    return _DIGITS.sub("#", " ".join(line.split())).lower()


def find_repeating_lines(pages: list[str], min_share: float = 0.6) -> set[str]:
    if len(pages) < 2:
        return set()
    counts: dict[str, int] = {}
    for page in pages:
        seen_on_this_page = set()
        for line in page.splitlines():
            if not line.strip():
                continue
            sig = _line_signature(line)
            if sig not in seen_on_this_page:
                seen_on_this_page.add(sig)
                counts[sig] = counts.get(sig, 0) + 1
    threshold = max(2, int(len(pages) * min_share))
    return {sig for sig, n in counts.items() if n >= threshold}


# ---------------------------------------------------------------------------
# Metadata harvest
# ---------------------------------------------------------------------------

_META_PATTERNS = {
    "id": re.compile(r"Identification\s+no\.?\s*:\s*([A-Z0-9\-]+)", re.I),
    "revision": re.compile(r"Revision\s*:\s*(\S+)", re.I),
    "review_date": re.compile(r"Date\s+of\s+Review\s*:\s*(.+?)(?:\s{2,}|$)", re.I),
    "page_count": re.compile(r"Page\s*\(?\d+\)?\s*of\s*\(?(\d+)\)?", re.I),
    "title": re.compile(r"Form\s*:\s*(.+?)(?:\s{2,}|Identification|$)", re.I),
}

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def parse_long_date(value: str) -> str | None:
    """'March 1, 2014' -> '2014-03-01'. Returns None if it does not parse."""
    match = re.search(r"([A-Za-z]+)\s+(\d{1,2})\s*,\s*(\d{4})", value)
    if not match:
        return None
    month = _MONTHS.get(match.group(1).lower())
    if not month:
        return None
    return f"{int(match.group(3)):04d}-{month:02d}-{int(match.group(2)):02d}"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class Page:
    number: int
    text: str
    char_start: int
    char_end: int


@dataclass
class CanonicalDocument:
    document: Document
    text: str
    pages: list[Page]
    raw_text: str
    removed_lines: list[str] = field(default_factory=list)
    artefact_fixes: int = 0

    def page_for_offset(self, offset: int) -> int | None:
        for page in self.pages:
            if page.char_start <= offset < page.char_end:
                return page.number
        return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def ingest_pdf(path: str | Path, issuer: str = "", jurisdiction: str = "") -> CanonicalDocument:
    import pdfplumber

    path = Path(path)
    raw_pages: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            raw_pages.append(page.extract_text() or "")

    raw_text = "\n".join(raw_pages)
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()[:16]

    furniture = find_repeating_lines(raw_pages)
    meta: dict[str, str] = {}
    removed: list[str] = []
    fixes = 0

    cleaned_pages: list[str] = []
    for page_text in raw_pages:
        kept: list[str] = []
        for line in page_text.splitlines():
            if line.strip() and _line_signature(line) in furniture:
                removed.append(line.strip())
                for key, pattern in _META_PATTERNS.items():
                    if key not in meta:
                        found = pattern.search(line)
                        if found:
                            meta[key] = found.group(1).strip()
                continue
            repaired, changed = repair_line(line)
            fixes += int(changed)
            kept.append(repaired)
        cleaned_pages.append("\n".join(kept).strip())

    # Stitch into one canonical text, remembering where each page starts.
    parts: list[str] = []
    pages: list[Page] = []
    cursor = 0
    for index, text in enumerate(cleaned_pages, start=1):
        start = cursor
        parts.append(text)
        cursor += len(text)
        if index < len(cleaned_pages):
            parts.append("\n")
            cursor += 1
        pages.append(Page(number=index, text=text, char_start=start, char_end=cursor))

    canonical = "".join(parts)

    review_date = parse_long_date(meta.get("review_date", "")) if meta.get("review_date") else None
    document = Document(
        id=meta.get("id", path.stem),
        title=meta.get("title", ""),
        issuer=issuer,
        jurisdiction=jurisdiction,
        revision=meta.get("revision", ""),
        review_date=review_date,
        # Deliberately left null. CARL-01 has no effective date -- the only
        # date in the furniture is a REVIEW date. See failure class E.
        effective_date=None,
        source_hash=source_hash,
        page_count=int(meta.get("page_count", len(raw_pages))),
        pipeline_version=PIPELINE_VERSION,
    )

    return CanonicalDocument(
        document=document,
        text=canonical,
        pages=pages,
        raw_text=raw_text,
        removed_lines=removed,
        artefact_fixes=fixes,
    )
