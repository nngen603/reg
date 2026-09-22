"""Text normalisation and quote location.

This is the machinery behind the grounding gate, and it is more important than
it looks. The PDF text layer breaks words across lines. In Annex 1 of CARL-01 a
standard comes out as:

    UAE.S 60335-\\n2-13

A model quoting that clause will write "UAE.S 60335-2-13". So an exact string
match against the raw source fails on perfectly ordinary content. Normalisation
is what makes the gate usable rather than a source of false alarms.

We locate a quote in three passes, cheapest and strictest first:

  1. exact match after collapsing whitespace and folding unicode punctuation
  2. exact match after removing whitespace entirely  (catches broken tokens)
  3. fuzzy match at a high threshold                 (catches OCR-style noise)

Every pass maps its answer back to offsets in the ORIGINAL text, so the
`char_start` / `char_end` we publish always point at the real source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from .contract import MatchKind

# Unicode characters that PDFs love and string comparison hates.
_FOLD = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "―": "-", "−": "-",
    " ": " ", " ": " ", " ": " ", " ": " ",
    "​": "", "‌": "", "‍": "", "﻿": "",
    "­": "",           # soft hyphen: a line-break artefact, never content
    "…": "...",
}


def fold_char(ch: str) -> str:
    return _FOLD.get(ch, ch)


@dataclass
class Normalized:
    """A normalised view of a string that remembers where it came from.

    `index_map[i]` is the offset in the original string of normalised char `i`.
    That is what lets us report real offsets after matching on a cleaned copy.
    """

    text: str
    index_map: list[int]

    @classmethod
    def build(cls, original: str, collapse_whitespace: bool = True) -> "Normalized":
        chars: list[str] = []
        index_map: list[int] = []
        prev_was_space = False

        for i, raw in enumerate(original):
            folded = fold_char(raw)
            if folded == "":
                continue                      # dropped entirely, e.g. soft hyphen
            if folded.isspace():
                if not collapse_whitespace:
                    prev_was_space = True
                    continue                  # tight mode: drop all whitespace
                if prev_was_space:
                    continue                  # collapse a run into one space
                chars.append(" ")
                index_map.append(i)
                prev_was_space = True
            else:
                chars.append(folded.casefold())
                index_map.append(i)
                prev_was_space = False

        return cls("".join(chars).strip(), _trim_map(chars, index_map))

    def to_original_span(self, start: int, end: int) -> tuple[int, int]:
        """Map a span in normalised coordinates back to the original string."""
        if not self.index_map:
            return (0, 0)
        start = max(0, min(start, len(self.index_map) - 1))
        end = max(start + 1, min(end, len(self.index_map)))
        origin_start = self.index_map[start]
        origin_end = self.index_map[end - 1] + 1
        return (origin_start, origin_end)


def _trim_map(chars: list[str], index_map: list[int]) -> list[int]:
    """Keep index_map aligned with the text after .strip()."""
    lead = 0
    while lead < len(chars) and chars[lead].isspace():
        lead += 1
    trail = len(chars)
    while trail > lead and chars[trail - 1].isspace():
        trail -= 1
    return index_map[lead:trail]


def normalize_ws(text: str) -> str:
    """Collapsed, folded, lowercased. For comparing two strings for equality."""
    return Normalized.build(text, collapse_whitespace=True).text


def tighten(text: str) -> str:
    """All whitespace removed. For comparing across line-break damage."""
    return Normalized.build(text, collapse_whitespace=False).text


@dataclass
class Location:
    found: bool
    char_start: int | None
    char_end: int | None
    kind: MatchKind
    score: float


NOT_FOUND = Location(False, None, None, MatchKind.NONE, 0.0)


def locate(quote: str, source: str, fuzzy_threshold: float = 92.0) -> Location:
    """Find `quote` inside `source`. Returns offsets into `source` as given.

    This is the grounding gate. If it returns NOT_FOUND the item can never be
    published, no matter what score the model attached to it.
    """
    if not quote or not quote.strip() or not source:
        return NOT_FOUND

    # Pass 1 -- exact, after collapsing whitespace and folding punctuation.
    loose_source = Normalized.build(source, collapse_whitespace=True)
    loose_quote = normalize_ws(quote)
    if loose_quote:
        idx = loose_source.text.find(loose_quote)
        if idx != -1:
            start, end = loose_source.to_original_span(idx, idx + len(loose_quote))
            return Location(True, start, end, MatchKind.EXACT, 100.0)

    # Pass 2 -- exact, with whitespace removed. Catches "60335-\n2-13".
    tight_source = Normalized.build(source, collapse_whitespace=False)
    tight_quote = tighten(quote)
    if tight_quote:
        idx = tight_source.text.find(tight_quote)
        if idx != -1:
            start, end = tight_source.to_original_span(idx, idx + len(tight_quote))
            return Location(True, start, end, MatchKind.EXACT, 100.0)

    # Pass 3 -- fuzzy, high threshold. Deliberately last and deliberately strict.
    # A close match may absorb noise in a word, never a change of meaning: the
    # quote must fit inside the source, and its numbers and negations must be
    # exactly those of the source text it matched.
    if loose_quote and loose_source.text and len(loose_quote) <= len(loose_source.text):
        alignment = fuzz.partial_ratio_alignment(
            loose_quote, loose_source.text, score_cutoff=fuzzy_threshold
        )
        if alignment is not None:
            s, e = _widen_to_words(loose_source.text, alignment.dest_start, alignment.dest_end)
            if meaning_tokens(loose_source.text[s:e]) == meaning_tokens(loose_quote):
                start, end = loose_source.to_original_span(
                    alignment.dest_start, alignment.dest_end
                )
                return Location(True, start, end, MatchKind.FUZZY, float(alignment.score))

    return NOT_FOUND


_NUMBER_WORDS = {
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
    "nineteen", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
    "hundred", "thousand", "million", "billion", "first", "second", "third", "half", "double", "twice",
}
_NEGATIONS = {"not", "no", "never", "none", "nor", "neither", "without", "except", "unless", "cannot"}
_MEANING_TOKEN = re.compile(r"\d+(?:[.,]\d+)*|[a-z]+(?:'t)?")


def meaning_tokens(text: str) -> list[str]:
    """The words a close match may not change: numbers and negations."""
    tokens = _MEANING_TOKEN.findall(normalize_ws(text))
    return sorted(t for t in tokens
                  if t[0].isdigit() or t in _NUMBER_WORDS or t in _NEGATIONS or t.endswith("n't"))


def _widen_to_words(text: str, start: int, end: int) -> tuple[int, int]:
    """Stretch a span to whole words, so a number cut in half is compared whole."""
    while start > 0 and text[start - 1].isalnum():
        start -= 1
    while end < len(text) and text[end].isalnum():
        end += 1
    return start, end


def values_agree(left: str, right: str) -> bool:
    """Do two extracted values mean the same thing?

    Used for the agreement signal. Compared after normalisation, so
    "One Year" and "one  year" agree, and casing or spacing differences from
    two different providers do not create false disagreement.
    """
    return normalize_ws(left) == normalize_ws(right)
