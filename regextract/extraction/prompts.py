"""Prompts, kept readable and versioned.

The system prompt is identical for every clause in a document. That is
deliberate: it is the stable prefix that prompt caching keys on, so on a
5,000-document backfill the taxonomy is billed once per cache window rather
than 300 times per document.

The rules below exist because of specific observed failures. Each one maps to
a failure class in the plan, and the comment says which.
"""

from __future__ import annotations

from ..config import PROMPT_VERSION

SYSTEM_PROMPT = f"""\
You extract structured facts from regulatory documents. Prompt version: {PROMPT_VERSION}.

You are given ONE clause at a time, plus the headings above it and the
document's definitions section for context. Return only facts supported by the
clause you were given.

CLAUSE TYPES
  definition           a term is being defined            e.g. "ESMA - The Emirates Authority for Standardization and Metrology"
  scope                what the document covers           e.g. "input voltage rating between 50 and 1000 volts"
  exemption            what is excluded                   e.g. "shall not apply to the products excluded by UAE / IEC 60335-1"
  requirement          something that must be satisfied   e.g. "Users Manual in Arabic and English language"
  responsibility       who carries a duty, cost or risk   e.g. "Applicant shall bear all the cost"
  procedure            an ordered process                 e.g. "Upon the acceptance of the Application, Product shall be evaluated"
  enforcement          a consequence or an authority's power  e.g. "ESMA reserves the right to inspect"
  validity_and_dates   validity periods, renewal, dates   e.g. "shall be valid for one year"
  normative_reference  a standards or reference list      e.g. "IEC 60335-1 : 2006"
  administrative       contact details, fee types, admin  e.g. "Conformity Affairs Department"

ENTITY TYPES
  organisation, individual, role, location, date, time_period,
  monetary_threshold, numeric_threshold, product_category,
  standard_reference, legal_reference

OBLIGATION STRENGTH
  shall / must    mandatory
  should          recommendation
  may / can       permission
  reserves_right  the authority has discretion

RULES

1. Quote word for word. Every fact carries a `quote` that must appear in the
   clause text exactly as written. Copy it, do not retype it from memory.

2. Never correct the source. If the document says "low voltage equipments" or
   "raise by any party", quote it that way. A corrected quote will fail
   verification and be rejected.

3. An empty list is a correct answer. If a clause contains no obligations, no
   entities or no references, return an empty list for that field. Do not
   invent an item to fill space. In particular:
     - if no monetary amount appears, return no monetary_threshold entity
     - if no calendar date appears, return no date entity

4. A year attached to a standard is not a date. "IEC 60335-1: 2007",
   "2002 5th Ed." and "+ A1:2004" are all part of the standard_reference.
   Do not emit a date entity for them.

5. Distinguish internal from external references. "clause 4 of this document"
   is internal, so set scope=internal and target_clause_id=4. "clause no. 6 of
   ECAS General Requirements" is external, so set scope=external and
   target_document to that document's name.

6. Subjects can be implicit. In "Samples shall be collected by ESMA" the
   subject is ESMA. In "Care should be taken" there is no subject at all, so
   return an empty string rather than guessing one.

7. Give an honest confidence between 0 and 1. It is used only to break ties,
   never to decide on its own whether something is published.

Return JSON matching the supplied schema. No prose, no explanation.
"""


def build_user_prompt(
    *,
    document_title: str,
    document_id: str,
    issuer: str,
    jurisdiction: str,
    parent_headings: list[str],
    definitions_digest: str,
    clause_id: str,
    clause_heading: str,
    clause_text: str,
) -> str:
    parents = " > ".join(parent_headings) if parent_headings else "(top level)"
    definitions_block = definitions_digest.strip() or "(none available)"
    return f"""\
DOCUMENT
  id:           {document_id}
  title:        {document_title}
  issuer:       {issuer}
  jurisdiction: {jurisdiction}

HEADINGS ABOVE THIS CLAUSE
  {parents}

DEFINITIONS DEFINED ELSEWHERE IN THIS DOCUMENT (for context only -- do not
extract facts from this block)
{definitions_block}

CLAUSE {clause_id} -- {clause_heading}
\"\"\"
{clause_text}
\"\"\"
"""


JUDGE_SYSTEM = """\
You are triaging a review queue for compliance analysts. You are NOT deciding
whether an extraction is correct -- a separate verification step already did
that, and your answer never publishes or blocks anything.

Your only job is to order the queue. Given an extracted fact and its clause,
return a single number from 0 to 10 for how much a human analyst should look at
this one first. Higher means more urgent.

Weigh: legal consequence if it is wrong, how ambiguous the source is, and
whether the extraction looks like it misread the clause.

Reply with the number only.
"""
