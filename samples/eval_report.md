# Evaluation report

Document: **CARL-01** rev 4 | run `d12bce71a7c2`

> **Offline run.** Extractions came from the deterministic rule-based stub, not from a model. Everything below shows that the harness works end to end. None of it is a claim about model quality. Run with `--live` against a real provider for that.

## 1. Baseline -- what the grounding gate caught

**All 153 candidate extractions were found in the source text. Run with --inject-faults to see the gate stop planted facts.**

- candidate extractions: 153
- failed the gate: 0 (0.0%)
- matched only fuzzily (kept, but flagged): 0
- published with the gate: 133
- would have been published without it: 133

_The stub quotes real sentences, so nothing fails the gate on a clean offline run. Use `--inject-faults` to see the gate reject real items._

## 2. Failure classes

**9 of 9 passing.**

| # | Class | Expected | Actual | Result |
|---|-------|----------|--------|--------|
| A | Layout and text-layer artefacts | headings repaired, footer removed, 'UAE' untouched | stray-U headings=0, footer_present=False, UAE occurrences=39 | PASS |
| B | Clause split across pages | one clause, pages 2-3, both halves joined | pages 2-3, has_alternating=True, has_direct_current=True | PASS |
| C | Irregular structure | all irregular ids present, no gaps, no duplicates, depth 4 | missing=none, gaps=[], duplicates=[], depth=4 | PASS |
| D | Wrong entity type | 0 date entities in section 4 / Annex; all four thresholds exact | dates_in_standard_refs=0, thresholds_found=['15 ampere', '1500 volts', '75 volts', 'between 50 and 1000 volts'], missing=none | PASS |
| E | Inventing a value that is not there | 0 monetary, 0 individual, 0 dates in clauses 1-12; effective_date null; review_date set | monetary=0, individuals=0, body_dates=0, effective_date=None, review_date=2014-03-01 | PASS |
| F | Resolving references | 8.1 external+named; 8.3 -> 4; 5.2 -> 3.1; 'Annex I' -> ANNEX-1 | 8.1_external=True (doc_named=True), 8.3->4=True, 5.2->3.1=True, annex_normalised=True | PASS |
| G | Conflicting terms and verbatim fidelity | ECAS conflict flagged; all three source typos preserved verbatim | conflict_flagged=True, typos_preserved=3/3 | PASS |
| H | Subject and modality | at least 5 of 6 subject/modality assertions | 6/6 pass | PASS |
| I | Table relationships | Annex 1 is one clause; 15 products; standards linked; all string-verified | annex_is_one_clause=True, products=15, standards=12, all_grounded=True | PASS |

## 3. Precision and recall -- section 5, hand-labelled

_n is about a dozen. These numbers show the harness computes precision and recall correctly. They are not a quality claim._

| Group | TP | FP | FN | Precision | Recall | F1 |
|-------|----|----|----|-----------|--------|-----|
| entities | 10 | 1 | 0 | 0.909 | 1.0 | 0.952 |
| cross-references | 1 | 0 | 0 | 1.0 | 1.0 | 1.0 |

Not in gold (may still be correct -- gold is not exhaustive): ['5.3:role:supplier']

Abstention holds: no monetary, date or individual entities in section 5.

## 4. Routing

- items: 153
- grounding rate: 1.0
- auto-accepted: 133 | held for review: 20 (13.1%)
- by tier: {'low': {'total': 67, 'auto_accept': 64}, 'medium': {'total': 67, 'auto_accept': 58}, 'high': {'total': 19, 'auto_accept': 11}}
- document flags: ['conflicting_definition']

> Routing thresholds are placeholders, not calibrated. Section 5 of the design document describes the calibration that replaces them.

## 5. Reference resolution -- clean vs noisy catalogue

_Eight hand-written cases. They show the floor and the number check doing their job under noise; they are not a recall measurement._

Catalogue: 6 documents clean, 211 with unrelated titles mixed in. Accuracy clean 1.0, noisy 1.0; wrong links: 0.

| Reference | Expected | Clean | Noisy |
|-----------|----------|-------|-------|
| ECAS General Requirements | ECAS-GR | ECAS-GR | ECAS-GR |
| Federal Law No. 28 | AE-FL-28 | AE-FL-28 | AE-FL-28 |
| ECAS General Requirement | ECAS-GR | ECAS-GR | ECAS-GR |
| Emirates Conformity Assessment Scheme General Requirements | ECAS-GR | ECAS-GR | ECAS-GR |
| Federal Law No. 2 | NOT_FOUND | NOT_FOUND | NOT_FOUND |
| Federal Law No. 99 | NOT_FOUND | NOT_FOUND | NOT_FOUND |
| GSO Technical Regulation for Low Voltage Electrical Equipment | NOT_FOUND | NOT_FOUND | NOT_FOUND |
| General Requirements | NOT_FOUND | NOT_FOUND | NOT_FOUND |

In this run: 5.1 entity -> AE-FL-28; 8.1 entity -> ECAS-GR; 8.1 cross_reference -> ECAS-GR
