"""Evaluation.

Three things, in the order they should be read:

  1. The baseline number. How many raw candidate extractions failed the
     grounding gate -- that is, how many a naive single-pass pipeline would
     have published. This is the number that justifies the whole design, and
     it is free because both sides of the comparison already exist in a run.

  2. The failure-class table. Nine classes from section 8 of the plan, each
     stated generally with CARL-01 as the worked instance. These are
     assertions over the output JSON, not a separate test suite, because the
     table itself is something a reviewer reads.

  3. Precision and recall on hand-labelled section 5. Deliberately small, and
     the report says so rather than dressing up n=10 as a quality claim.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..contract import Decision, Item
from ..normalize import normalize_ws
from ..resolution.retrieval import evaluate_resolution


# ---------------------------------------------------------------------------
# Failure-class checks
# ---------------------------------------------------------------------------


@dataclass
class ClassResult:
    key: str
    title: str
    general: str
    expected: str
    actual: str
    passed: bool

    def as_row(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        return f"| {self.key} | {self.title} | {self.expected} | {self.actual} | {mark} |"


CheckFn = Callable[[dict], ClassResult]
_CHECKS: list[CheckFn] = []


def check(key: str, title: str, general: str):
    def wrap(fn):
        def runner(state: dict) -> ClassResult:
            try:
                passed, expected, actual = fn(state)
            except Exception as exc:  # noqa: BLE001
                passed, expected, actual = False, "check should run", f"error: {type(exc).__name__}: {exc}"
            return ClassResult(key, title, general, expected, str(actual), bool(passed))
        _CHECKS.append(runner)
        return runner
    return wrap


def _items(state: dict) -> list[Item]:
    return state.get("items", []) or []


def _clauses_by_id(state: dict) -> dict:
    return {c.id: c for c in state.get("clauses", []) or []}


@check("A", "Layout and text-layer artefacts",
       "PDF encoding artefacts, running headers and footers, watermarks, line numbers")
def _class_a(state):
    text = state["canonical"].text
    stray_u = re.findall(r"(?m)^\s*U[A-Z]{2,}", text)
    furniture = "Identification no" in text
    uae_intact = text.count("UAE") > 20
    passed = not stray_u and not furniture and uae_intact
    return (passed,
            "headings repaired, footer removed, 'UAE' untouched",
            f"stray-U headings={len(stray_u)}, footer_present={furniture}, UAE occurrences={text.count('UAE')}")


@check("B", "Clause split across pages",
       "any clause or table crossing a page break")
def _class_b(state):
    clause = _clauses_by_id(state).get("3.1.1")
    if not clause:
        return False, "clause 3.1.1 present, pages 2-3", "clause 3.1.1 missing"
    ok = (clause.page_start == 2 and clause.page_end == 3
          and "alternating" in clause.text and "direct current" in clause.text)
    return (ok, "one clause, pages 2-3, both halves joined",
            f"pages {clause.page_start}-{clause.page_end}, "
            f"has_alternating={'alternating' in clause.text}, "
            f"has_direct_current={'direct current' in clause.text}")


@check("C", "Irregular structure",
       "annexes, unnumbered sections, differing numbering conventions, cover pages")
def _class_c(state):
    by_id = _clauses_by_id(state)
    structure = state.get("structure", {})
    required = ["10", "12", "7.1.1.1", "7.1.1.6", "6.4.3", "FEES", "CONTACT", "ANNEX-1"]
    missing = [r for r in required if r not in by_id]
    ok = (not missing and not structure.get("numbering_gaps")
          and not structure.get("duplicate_ids") and structure.get("max_depth") == 4)
    return (ok, "all irregular ids present, no gaps, no duplicates, depth 4",
            f"missing={missing or 'none'}, gaps={structure.get('numbering_gaps')}, "
            f"duplicates={structure.get('duplicate_ids')}, depth={structure.get('max_depth')}")


@check("D", "Wrong entity type",
       "a number that looks like a different type -- years in citations, quantities without units")
def _class_d(state):
    items = _items(state)
    dates_in_refs = [i for i in items
                     if i.type_name == "date" and (i.clause_id.startswith("4") or i.clause_id.startswith("ANNEX"))]
    numeric = {normalize_ws(i.payload.get("text", "")) for i in items if i.type_name == "numeric_threshold"}
    expected_numeric = {"between 50 and 1000 volts", "75 volts", "1500 volts", "15 ampere"}
    ok = not dates_in_refs and expected_numeric.issubset(numeric)
    return (ok, "0 date entities in section 4 / Annex; all four thresholds exact",
            f"dates_in_standard_refs={len(dates_in_refs)}, "
            f"thresholds_found={sorted(numeric & expected_numeric)}, "
            f"missing={sorted(expected_numeric - numeric) or 'none'}")


@check("E", "Inventing a value that is not there",
       "the model makes something up -- the only failure that reaches downstream silently")
def _class_e(state):
    items = _items(state)
    document = state["document"]
    monetary = [i for i in items if i.type_name == "monetary_threshold"]
    individuals = [i for i in items if i.type_name == "individual"]
    body_dates = [i for i in items if i.type_name == "date"
                  and i.clause_id.split(".")[0].isdigit() and 1 <= int(i.clause_id.split(".")[0]) <= 12]
    ok = (not monetary and not individuals and not body_dates
          and document.effective_date is None and document.review_date == "2014-03-01")
    return (ok, "0 monetary, 0 individual, 0 dates in clauses 1-12; effective_date null; review_date set",
            f"monetary={len(monetary)}, individuals={len(individuals)}, body_dates={len(body_dates)}, "
            f"effective_date={document.effective_date}, review_date={document.review_date}")


@check("F", "Resolving references",
       "internal vs external, other documents, superseded targets, the same target named two ways")
def _class_f(state):
    items = _items(state)
    xrefs = [i for i in items if i.kind == "cross_reference"]

    def find(clause_id, scope, target=None):
        for x in xrefs:
            if x.clause_id == clause_id and x.payload.get("scope") == scope:
                if target is None or x.payload.get("target_clause_id") == target:
                    return x
        return None

    external = find("8.1", "external")
    internal_83 = find("8.3", "internal", "4")
    internal_52 = find("5.2", "internal", "3.1")
    annex_ref = next((x for x in xrefs
                      if str(x.payload.get("target_clause_id", "")).upper() == "ANNEX-1"), None)
    doc_named = bool(external and "ECAS General Requirements" in external.payload.get("target_document", ""))
    ok = all([external, internal_83, internal_52, annex_ref, doc_named])
    return (ok, "8.1 external+named; 8.3 -> 4; 5.2 -> 3.1; 'Annex I' -> ANNEX-1",
            f"8.1_external={bool(external)} (doc_named={doc_named}), "
            f"8.3->4={bool(internal_83)}, 5.2->3.1={bool(internal_52)}, "
            f"annex_normalised={bool(annex_ref)}")


@check("G", "Conflicting terms and verbatim fidelity",
       "a defined term drifts within one document; the model must not correct the source")
def _class_g(state):
    flags = [f.code for f in state.get("flags", []) or []]
    text = state["canonical"].text
    typos = ["low voltage equipments", "raise by any party", "misused the certificate"]
    preserved = [t for t in typos if t in text]
    ok = "conflicting_definition" in flags and len(preserved) == 3
    return (ok, "ECAS conflict flagged; all three source typos preserved verbatim",
            f"conflict_flagged={'conflicting_definition' in flags}, "
            f"typos_preserved={len(preserved)}/3")


@check("H", "Subject and modality",
       "passive voice hides the subject; permissions look like obligations")
def _class_h(state):
    items = _items(state)
    obligations = [i for i in items if i.kind == "obligation"]

    def has(clause_id, modality, subject_contains=None):
        for o in obligations:
            if o.clause_id != clause_id or o.payload.get("modality") != modality:
                continue
            if subject_contains is None:
                return True
            if subject_contains.lower() in (o.payload.get("subject") or "").lower():
                return True
        return False

    results = {
        "8.3 passive -> ESMA": has("8.3", "shall", "ESMA"),
        "8.5 applicant": has("8.5", "shall", "applicant"),
        "7.1.1 manufacturers/traders": has("7.1.1", "shall"),
        "11.3 should (recommendation)": has("11.3", "should"),
        "11.2 may (permission)": has("11.2", "may"),
        "10.5 reserves_right": has("10.5", "reserves_right"),
    }
    passed = sum(results.values())
    return (passed >= 5, "at least 5 of 6 subject/modality assertions",
            f"{passed}/6 pass" + ("" if passed == 6 else "; failed: " + ", ".join(k for k, v in results.items() if not v)))


@check("I", "Table relationships",
       "product-to-standard links: the output product-compliance customers actually use")
def _class_i(state):
    by_id = _clauses_by_id(state)
    annex = by_id.get("ANNEX-1")
    items = [i for i in _items(state) if i.clause_id == "ANNEX-1"]
    products = [i for i in items if i.type_name == "product_category"]
    standards = [i for i in items if i.type_name == "standard_reference"]
    grounded = all(i.evidence.grounded for i in products + standards)
    ok = bool(annex) and len(products) == 15 and len(standards) >= 10 and grounded
    return (ok, "Annex 1 is one clause; 15 products; standards linked; all string-verified",
            f"annex_is_one_clause={bool(annex)}, products={len(products)}, "
            f"standards={len(standards)}, all_grounded={grounded}")


def run_failure_classes(state: dict) -> list[ClassResult]:
    return [checker(state) for checker in _CHECKS]


# ---------------------------------------------------------------------------
# Baseline: what the grounding gate actually caught
# ---------------------------------------------------------------------------


def baseline_comparison(items: list[Item]) -> dict:
    total = len(items)
    ungrounded = [i for i in items if not i.evidence.grounded]
    fuzzy = [i for i in items if i.evidence.grounded and i.evidence.match_kind.value == "fuzzy"]
    published = [i for i in items if i.review.decision is Decision.AUTO_ACCEPT]
    return {
        "candidate_items": total,
        "failed_grounding_gate": len(ungrounded),
        "failed_grounding_pct": round(100 * len(ungrounded) / total, 2) if total else 0.0,
        "matched_only_fuzzily": len(fuzzy),
        "published_with_gate": len(published),
        "published_without_gate": len(published) + len(ungrounded),
        "headline": (
            f"{len(ungrounded)} of {total} candidate extractions "
            f"({100 * len(ungrounded) / total:.1f}%) failed the grounding gate. "
            "A pipeline without the gate would have published them."
            if ungrounded else
            f"All {total} candidate extractions were found in the source text. "
            "Run with --inject-faults to see the gate stop planted facts."
        ) if total else "no items",
    }


# ---------------------------------------------------------------------------
# Precision and recall on the labelled section
# ---------------------------------------------------------------------------


@dataclass
class PRResult:
    label: str
    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0
    missed: list[str] = field(default_factory=list)
    spurious: list[str] = field(default_factory=list)

    @property
    def precision(self) -> float:
        denominator = self.true_positive + self.false_positive
        return self.true_positive / denominator if denominator else 0.0

    @property
    def recall(self) -> float:
        denominator = self.true_positive + self.false_negative
        return self.true_positive / denominator if denominator else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


def evaluate_section_5(items: list[Item], gold_path: Path) -> dict:
    gold = json.loads(Path(gold_path).read_text(encoding="utf-8"))

    scoped = [i for i in items if i.clause_id.startswith("5.")]
    predicted_entities = {
        (i.clause_id, i.type_name, normalize_ws(i.payload.get("text", "")))
        for i in scoped if i.kind == "entity"
    }
    expected_entities = {
        (g["clause_id"], g["type"], normalize_ws(g["text"]))
        for g in gold["entities"]
    }

    entity_result = PRResult("entities")
    entity_result.true_positive = len(predicted_entities & expected_entities)
    entity_result.false_positive = len(predicted_entities - expected_entities)
    entity_result.false_negative = len(expected_entities - predicted_entities)
    entity_result.missed = sorted(f"{c}:{t}:{x}" for c, t, x in expected_entities - predicted_entities)
    entity_result.spurious = sorted(f"{c}:{t}:{x}" for c, t, x in predicted_entities - expected_entities)

    predicted_refs = {
        (i.clause_id, i.payload.get("scope"), str(i.payload.get("target_clause_id")))
        for i in scoped if i.kind == "cross_reference"
    }
    expected_refs = {
        (g["clause_id"], g["scope"], str(g["target_clause_id"]))
        for g in gold.get("cross_references", [])
    }
    ref_result = PRResult("cross_references")
    ref_result.true_positive = len(predicted_refs & expected_refs)
    ref_result.false_positive = len(predicted_refs - expected_refs)
    ref_result.false_negative = len(expected_refs - predicted_refs)
    ref_result.missed = sorted(f"{c}:{s}:{t}" for c, s, t in expected_refs - predicted_refs)

    abstention = {
        entity_type: sum(1 for i in scoped if i.type_name == entity_type)
        for entity_type in gold["must_be_empty"] if not entity_type.startswith("_")
    }

    grounded = sum(1 for i in scoped if i.evidence.grounded)
    return {
        "labelled_items": len(expected_entities) + len(expected_refs),
        "entities": _pr_dict(entity_result),
        "cross_references": _pr_dict(ref_result),
        "abstention_violations": {k: v for k, v in abstention.items() if v},
        "grounding_rate": round(grounded / len(scoped), 4) if scoped else 0.0,
        "caveat": (
            "n is about a dozen. These numbers show the harness computes "
            "precision and recall correctly. They are not a quality claim."
        ),
    }


def _pr_dict(result: PRResult) -> dict:
    return {
        "true_positive": result.true_positive,
        "false_positive": result.false_positive,
        "false_negative": result.false_negative,
        "precision": round(result.precision, 3),
        "recall": round(result.recall, 3),
        "f1": round(result.f1, 3),
        "missed": result.missed,
        "spurious": result.spurious[:10],
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def build_report(state: dict, gold_dir: Path) -> tuple[str, dict]:
    items = _items(state)
    settings = state["settings"]

    classes = run_failure_classes(state)
    baseline = baseline_comparison(items)
    section_5 = evaluate_section_5(items, Path(gold_dir) / "section_5.json")
    resolution_gold = Path(gold_dir) / "resolution.json"
    resolution = evaluate_resolution(settings, resolution_gold) if resolution_gold.exists() else None

    passed = sum(1 for c in classes if c.passed)
    stub_run = settings.offline

    lines: list[str] = []
    lines.append("# Evaluation report\n")
    lines.append(f"Document: **{state['document'].id}** rev {state['document'].revision} "
                 f"| run `{state['recorder'].run_id}`\n")

    if stub_run:
        lines.append(
            "> **Offline run.** Extractions came from the deterministic rule-based stub, "
            "not from a model. Everything below shows that the harness works end to end. "
            "None of it is a claim about model quality. Run with `--live` against a real "
            "provider for that.\n"
        )

    lines.append("## 1. Baseline -- what the grounding gate caught\n")
    lines.append(f"**{baseline['headline']}**\n")
    lines.append(f"- candidate extractions: {baseline['candidate_items']}")
    lines.append(f"- failed the gate: {baseline['failed_grounding_gate']} "
                 f"({baseline['failed_grounding_pct']}%)")
    lines.append(f"- matched only fuzzily (kept, but flagged): {baseline['matched_only_fuzzily']}")
    lines.append(f"- published with the gate: {baseline['published_with_gate']}")
    lines.append(f"- would have been published without it: {baseline['published_without_gate']}\n")
    if baseline["failed_grounding_gate"] == 0 and stub_run:
        lines.append("_The stub quotes real sentences, so nothing fails the gate on a clean "
                     "offline run. Use `--inject-faults` to see the gate reject real items._\n")

    lines.append("## 2. Failure classes\n")
    lines.append(f"**{passed} of {len(classes)} passing.**\n")
    lines.append("| # | Class | Expected | Actual | Result |")
    lines.append("|---|-------|----------|--------|--------|")
    for result in classes:
        lines.append(result.as_row())
    lines.append("")

    lines.append("## 3. Precision and recall -- section 5, hand-labelled\n")
    lines.append(f"_{section_5['caveat']}_\n")
    entities = section_5["entities"]
    refs = section_5["cross_references"]
    lines.append("| Group | TP | FP | FN | Precision | Recall | F1 |")
    lines.append("|-------|----|----|----|-----------|--------|-----|")
    lines.append(f"| entities | {entities['true_positive']} | {entities['false_positive']} | "
                 f"{entities['false_negative']} | {entities['precision']} | {entities['recall']} | {entities['f1']} |")
    lines.append(f"| cross-references | {refs['true_positive']} | {refs['false_positive']} | "
                 f"{refs['false_negative']} | {refs['precision']} | {refs['recall']} | {refs['f1']} |")
    lines.append("")
    if entities["missed"]:
        lines.append(f"Missed: {entities['missed']}\n")
    if entities["spurious"]:
        lines.append(f"Not in gold (may still be correct -- gold is not exhaustive): "
                     f"{entities['spurious']}\n")
    if section_5["abstention_violations"]:
        lines.append(f"**Abstention violated:** {section_5['abstention_violations']}\n")
    else:
        lines.append("Abstention holds: no monetary, date or individual entities in section 5.\n")

    lines.append("## 4. Routing\n")
    from ..routing.route import routing_summary

    routing = routing_summary(items)
    lines.append(f"- items: {routing.get('items')}")
    lines.append(f"- grounding rate: {routing.get('grounding_rate')}")
    lines.append(f"- auto-accepted: {routing.get('auto_accept')} "
                 f"| held for review: {routing.get('review')} "
                 f"({routing.get('review_rate', 0) * 100:.1f}%)")
    lines.append(f"- by tier: {routing.get('by_tier')}")
    flags = state.get("flags", []) or []
    lines.append(f"- document flags: {[f.code for f in flags] or 'none'}\n")
    failed = next((f for f in flags if f.code == "extraction_failed"), None)
    if failed:
        lines.append(f"> **{len(failed.clause_ids)} clause(s) were never extracted** "
                     f"({failed.clause_ids[:10]}). The document is held for review: an "
                     "unanswered clause looks exactly like a clause with no facts in it.\n")
    lines.append("> Routing thresholds are placeholders, not calibrated. "
                 "Section 5 of the design document describes the calibration that replaces them.\n")

    if resolution:
        lines.append("## 5. Reference resolution -- clean vs noisy catalogue\n")
        lines.append(f"_{resolution['caveat']}_\n")
        lines.append(
            f"Catalogue: {resolution['catalog']['clean']} documents clean, "
            f"{resolution['catalog']['noisy']} with unrelated titles mixed in. "
            f"Accuracy clean {resolution['clean_accuracy']}, noisy {resolution['noisy_accuracy']}; "
            f"wrong links: {resolution['wrong_links']}.\n")
        lines.append("| Reference | Expected | Clean | Noisy |")
        lines.append("|-----------|----------|-------|-------|")
        for row in resolution["rows"]:
            lines.append(f"| {row['query']} | {row['expected'] or 'NOT_FOUND'} | "
                         f"{row['clean'] or 'NOT_FOUND'} | {row['noisy'] or 'NOT_FOUND'} |")
        linked = [i for i in items if i.resolution is not None]
        if linked:
            lines.append("\nIn this run: " + "; ".join(
                f"{i.clause_id} {i.kind} -> {i.resolution.target_document_id or 'NOT_FOUND'}"
                for i in linked) + "\n")

    summary = {
        "failure_classes": {"passed": passed, "total": len(classes),
                            "results": [c.__dict__ for c in classes]},
        "baseline": baseline,
        "section_5": section_5,
        "resolution": resolution,
        "routing": routing,
        "offline_stub_run": stub_run,
    }
    return "\n".join(lines), summary


def write_report(state: dict, output_dir: Path, gold_dir: Path) -> dict[str, Path]:
    markdown, summary = build_report(state, gold_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    md_path = output_dir / "eval_report.md"
    md_path.write_text(markdown, encoding="utf-8")
    json_path = output_dir / "eval_summary.json"
    json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str),
                         encoding="utf-8")
    return {"eval_report": md_path, "eval_summary": json_path}
