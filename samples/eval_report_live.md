# Evaluation report

Document: **CARL-01** rev 4 | run `cfab27a122e3`

## 1. Baseline -- what the grounding gate caught

**18 of 353 candidate extractions (5.1%) failed the grounding gate. A pipeline without the gate would have published them.**

- candidate extractions: 353
- failed the gate: 18 (5.1%)
- matched only fuzzily (kept, but flagged): 15
- published with the gate: 0
- would have been published without it: 18

## 2. Failure classes

**6 of 9 passing.**

| # | Class | Expected | Actual | Result |
|---|-------|----------|--------|--------|
| A | Layout and text-layer artefacts | headings repaired, footer removed, 'UAE' untouched | stray-U headings=0, footer_present=False, UAE occurrences=39 | PASS |
| B | Clause split across pages | one clause, pages 2-3, both halves joined | pages 2-3, has_alternating=True, has_direct_current=True | PASS |
| C | Irregular structure | all irregular ids present, no gaps, no duplicates, depth 4 | missing=none, gaps=[], duplicates=[], depth=4 | PASS |
| D | Wrong entity type | 0 date entities in section 4 / Annex; all four thresholds exact | dates_in_standard_refs=0, thresholds_found=[], missing=['15 ampere', '1500 volts', '75 volts', 'between 50 and 1000 volts'] | FAIL |
| E | Inventing a value that is not there | 0 monetary, 0 individual, 0 dates in clauses 1-12; effective_date null; review_date set | monetary=0, individuals=0, body_dates=0, effective_date=None, review_date=2014-03-01 | PASS |
| F | Resolving references | 8.1 external+named; 8.3 -> 4; 5.2 -> 3.1; 'Annex I' -> ANNEX-1 | 8.1_external=True (doc_named=True), 8.3->4=True, 5.2->3.1=True, annex_normalised=False | FAIL |
| G | Conflicting terms and verbatim fidelity | ECAS conflict flagged; all three source typos preserved verbatim | conflict_flagged=True, typos_preserved=3/3 | PASS |
| H | Subject and modality | at least 5 of 6 subject/modality assertions | 6/6 pass | PASS |
| I | Table relationships | Annex 1 is one clause; 15 products; standards linked; all string-verified | annex_is_one_clause=True, products=0, standards=0, all_grounded=True | FAIL |

## 3. Precision and recall -- section 5, hand-labelled

_n is about a dozen. These numbers show the harness computes precision and recall correctly. They are not a quality claim._

| Group | TP | FP | FN | Precision | Recall | F1 |
|-------|----|----|----|-----------|--------|-----|
| entities | 8 | 42 | 2 | 0.16 | 0.8 | 0.267 |
| cross-references | 1 | 1 | 0 | 0.5 | 1.0 | 0.667 |

Missed: ['5.1:location:uae', '5.5:location:uae']

Not in gold (may still be correct -- gold is not exhaustive): ['5.10:product_category:product standard', '5.1:organisation:esma - the emirates authority for standardization and metrology', '5.1:product_category:low voltage appliances', '5.1:role:competent government entity', '5.1:role:standards body', '5.2:product_category:components', '5.2:product_category:electrical or electronic equipment', '5.2:product_category:equipment', '5.2:product_category:finished products', '5.2:product_category:installations']

Abstention holds: no monetary, date or individual entities in section 5.

## 4. Routing

- items: 353
- grounding rate: 0.949
- auto-accepted: 0 | held for review: 353 (100.0%)
- by tier: {'high': {'total': 49, 'auto_accept': 0}, 'low': {'total': 106, 'auto_accept': 0}, 'medium': {'total': 198, 'auto_accept': 0}}
- document flags: ['extraction_failed', 'high_grounding_failure_rate', 'conflicting_definition']

> **1 clause(s) were never extracted** (['ANNEX-1']). The document is held for review: an unanswered clause looks exactly like a clause with no facts in it.

> Routing thresholds are placeholders, not calibrated. Section 5 of the design document describes the calibration that replaces them.

## 5. Reference resolution -- clean vs noisy catalogue

_Eight hand-written cases. They show the floor and the number check doing their job under noise; they are not a recall measurement._

Catalogue: 6 documents clean, 211 with unrelated titles mixed in. Accuracy clean 0.75, noisy 0.75; wrong links: 4.

| Reference | Expected | Clean | Noisy |
|-----------|----------|-------|-------|
| ECAS General Requirements | ECAS-GR | ECAS-GR | ECAS-GR |
| Federal Law No. 28 | AE-FL-28 | AE-FL-28 | AE-FL-28 |
| ECAS General Requirement | ECAS-GR | ECAS-GR | ECAS-GR |
| Emirates Conformity Assessment Scheme General Requirements | ECAS-GR | ECAS-GR | ECAS-GR |
| Federal Law No. 2 | NOT_FOUND | NOT_FOUND | NOT_FOUND |
| Federal Law No. 99 | NOT_FOUND | NOT_FOUND | NOT_FOUND |
| GSO Technical Regulation for Low Voltage Electrical Equipment | NOT_FOUND | SYN-LVD | SYN-LVD |
| General Requirements | NOT_FOUND | ECAS-GR | ECAS-GR |

In this run: 2 entity -> ECAS-GR; 2 entity -> ECAS-GR; 3.1.2 entity -> NOT_FOUND; 5.1 entity -> AE-FL-28; 5.3 entity -> NOT_FOUND; 5.3 entity -> NOT_FOUND; 5.4 entity -> NOT_FOUND; 5.5 entity -> NOT_FOUND; 5.5 entity -> NOT_FOUND; 5.7 entity -> NOT_FOUND; 5.8 entity -> NOT_FOUND; 6.1 entity -> NOT_FOUND; 6.1 entity -> NOT_FOUND; 7.1.1.5 entity -> NOT_FOUND; 8.1 cross_reference -> ECAS-GR; 9 cross_reference -> NOT_FOUND; 9 cross_reference -> NOT_FOUND; 9 cross_reference -> NOT_FOUND; 9.1 entity -> NOT_FOUND; 10.1 entity -> SYN-ECAS-REG; 10.1 entity -> NOT_FOUND; 10.2 entity -> SYN-ECAS-REG; 10.3 entity -> SYN-ECAS-REG; 11 entity -> NOT_FOUND; 11 cross_reference -> NOT_FOUND; 11.1 entity -> NOT_FOUND; 12.2 entity -> NOT_FOUND; 12.2 entity -> NOT_FOUND
