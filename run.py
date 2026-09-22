#!/usr/bin/env python
"""Command line entry point.

    python run.py extract  --pdf data/CARL-01.pdf
    python run.py evaluate --pdf data/CARL-01.pdf
    python run.py diff     --previous outputs/v4/extractions.json --current outputs/v5/extractions.json
    python run.py redteam  --pdf data/CARL-01.pdf
    python run.py extract  --pdf data/CARL-01.pdf --review
    python run.py review   --thread <run_id> --decisions decisions.json
    python run.py verify-log
    python run.py doctor

Everything defaults to offline, so a fresh clone runs with no API key and no
cost. Add --live to call a real provider.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pathlib as _pathlib  # noqa: E402

from dotenv import load_dotenv  # noqa: E402

# Provider keys for --live live in .env, which is never committed or zipped.
# LiteLLM reads them from the environment, so load them before anything runs.
load_dotenv(_pathlib.Path(__file__).resolve().parent / ".env")

from regextract.config import PIPELINE_VERSION, get_settings, has_api_key  # noqa: E402


def _settings_from_args(args) -> "object":
    overrides = {
        "REGEXTRACT_OFFLINE": not getattr(args, "live", False),
        "REGEXTRACT_RECORD": getattr(args, "record", False),
        "REGEXTRACT_USE_QDRANT": getattr(args, "qdrant", False),
        "REGEXTRACT_USE_GUARDRAILS": getattr(args, "guardrails", False),
        "REGEXTRACT_USE_JUDGE": getattr(args, "judge", False),
        "REGEXTRACT_REVIEW": getattr(args, "review", False),
        "REGEXTRACT_AGREEMENT": not getattr(args, "no_agreement", False),
    }
    if getattr(args, "out", None):
        overrides["output_dir"] = Path(args.out)
    settings = get_settings(reload=True, **overrides)
    return settings


def _banner(settings) -> None:
    described = settings.describe()
    print("=" * 68)
    print(f" regextract {PIPELINE_VERSION}")
    for key, value in described.items():
        print(f"   {key:18} {value}")
    print("=" * 68)


def _print_stages(manifest: dict) -> None:
    print("\nstages")
    for event in manifest["stages"]:
        counters = event["counters"]
        head = ", ".join(f"{k}={v}" for k, v in list(counters.items())[:3])
        status = "" if not event["error"] else f"  ERROR {event['error']}"
        print(f"  {event['stage']:16} {event['duration_ms']:8.1f} ms   {head}{status}")


def cmd_extract(args) -> int:
    from regextract.pipeline.graph import run_pipeline

    settings = _settings_from_args(args)
    if not settings.offline and not has_api_key():
        print("ERROR: --live requires a provider key. Set ANTHROPIC_API_KEY or "
              "OPENAI_API_KEY, or drop --live to run offline.", file=sys.stderr)
        return 2

    _banner(settings)
    checkpointer = None
    if settings.review_mode:
        from regextract.pipeline.checkpointing import sqlite_checkpointer

        checkpointer = sqlite_checkpointer(settings.checkpoint_path)
    state = run_pipeline(
        pdf_path=args.pdf,
        issuer=args.issuer,
        jurisdiction=args.jurisdiction,
        settings=settings,
        inject_faults=args.inject_faults,
        checkpointer=checkpointer,
    )

    manifest = state["manifest"]
    _print_stages(manifest)
    if manifest.get("status") == "paused_for_review":
        return _report_pause(state, settings)
    routing = manifest["routing"]
    print(f"\nitems         {routing.get('items')}")
    print(f"grounded      {routing.get('grounded')} ({routing.get('grounding_rate', 0) * 100:.1f}%)")
    print(f"auto-accept   {routing.get('auto_accept')}")
    print(f"review        {routing.get('review')} ({routing.get('review_rate', 0) * 100:.1f}%)")
    if manifest["document_flags"]:
        print("\ndocument flags")
        for flag in manifest["document_flags"]:
            print(f"  - {flag['code']}: {flag['detail'][:90]}")
    print(f"\ncost          ${manifest['model_calls'].get('cost_usd', 0):.4f} "
          f"({manifest['model_calls'].get('calls', 0)} calls, "
          f"{manifest['model_calls'].get('by_source', {})})")
    print(f"outputs       {settings.output_dir}")
    return 0


def _report_pause(state, settings) -> int:
    request = state["review_request"]
    out = settings.output_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "pending_review.json").write_text(
        json.dumps(request, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    # The action is left blank on purpose: an unedited template is rejected,
    # so nobody accepts a whole queue by submitting it untouched.
    template = {"decisions": [{"item_id": i["item_id"], "action": "", "reviewer": "", "note": ""}
                              for i in request["items"]]}
    (out / "decisions.template.json").write_text(json.dumps(template, indent=2), encoding="utf-8")

    print(f"\npaused for review: {request['pending']} item(s) need a person  (run {state['run_id']})")
    print(f"  queue       {out / 'pending_review.json'}")
    print(f"  template    {out / 'decisions.template.json'}   (action: accept | reject | correct)")
    print(f"  resume      python run.py review --thread {state['run_id']} --decisions <file>")
    return 0


def cmd_review(args) -> int:
    from regextract.pipeline.checkpointing import sqlite_checkpointer
    from regextract.pipeline.graph import resume_pipeline
    from regextract.review.review import parse_decisions

    settings = _settings_from_args(args)
    raw = json.loads(Path(args.decisions).read_text(encoding="utf-8"))
    entries = raw.get("decisions", []) if isinstance(raw, dict) else raw
    _, problems = parse_decisions(entries)
    if problems:
        print("ERROR: fix the decisions file first:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 2

    try:
        state = resume_pipeline(run_id=args.thread, decisions=entries, settings=settings,
                                checkpointer=sqlite_checkpointer(settings.checkpoint_path))
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    manifest = state["manifest"]
    review = next((e for e in manifest["stages"] if e["stage"] == "human_review"), {}).get("counters", {})
    publish = next((e for e in reversed(manifest["stages"]) if e["stage"] == "publish"), {}).get("counters", {})
    print(f"review        accepted {review.get('accepted', 0)}, rejected {review.get('rejected', 0)}, "
          f"corrected {review.get('corrected', 0)}, problems {review.get('problems', 0)}, "
          f"still pending {review.get('still_pending', 0)}")
    for note in manifest.get("notes", []):
        if note.startswith("review:"):
            print(f"  {note}")
    print(f"published     {publish.get('published_items')}")
    print(f"publish log   {publish.get('publish_log')}")
    print(f"outputs       {settings.output_dir}")
    return 0


def cmd_verify_log(args) -> int:
    from regextract.publishing.publog import verify_log

    path = Path(args.log) if args.log else get_settings(reload=True).output_dir / "publish_log.jsonl"
    report = verify_log(path)
    print(f"log           {report['path']}")
    print(f"entries       {report['entries']}")
    if report["valid"]:
        print("chain         intact: every entry hashes to its content and points at the one before")
        return 0
    if not report["entries"]:
        print("chain         empty or missing")
        return 1
    print("chain         BROKEN")
    for problem in report["breaks"]:
        print(f"  line {problem['line']}: {problem['reason']}")
    return 1


def cmd_evaluate(args) -> int:
    from regextract.evaluation.evaluate import write_report
    from regextract.pipeline.graph import run_pipeline

    settings = _settings_from_args(args)
    if not settings.offline and not has_api_key():
        print("ERROR: --live requires a provider key.", file=sys.stderr)
        return 2

    _banner(settings)
    state = run_pipeline(
        pdf_path=args.pdf,
        issuer=args.issuer,
        jurisdiction=args.jurisdiction,
        settings=settings,
        inject_faults=args.inject_faults,
    )
    written = write_report(state, settings.output_dir, settings.gold_dir)
    summary = json.loads(written["eval_summary"].read_text(encoding="utf-8"))

    classes = summary["failure_classes"]
    baseline = summary["baseline"]
    print(f"\nfailure classes   {classes['passed']}/{classes['total']} passing")
    for result in classes["results"]:
        mark = "ok  " if result["passed"] else "FAIL"
        print(f"  [{mark}] {result['key']}  {result['title']}")
        if not result["passed"]:
            print(f"          expected: {result['expected']}")
            print(f"          actual:   {result['actual']}")

    print(f"\nbaseline          {baseline['headline']}")
    entities = summary["section_5"]["entities"]
    print(f"section 5 P/R     precision={entities['precision']} recall={entities['recall']} "
          f"f1={entities['f1']}  (n={summary['section_5']['labelled_items']}, "
          "too small to be a quality claim)")
    print(f"\nreport            {written['eval_report']}")
    return 0 if classes["passed"] == classes["total"] else 1


def cmd_diff(args) -> int:
    from regextract.contract import ExtractionRun
    from regextract.publishing.publish import (changed_obligations_event, diff_runs, match_clauses,
                                             renumbered_clauses)

    previous = ExtractionRun.model_validate_json(Path(args.previous).read_text(encoding="utf-8"))
    current = ExtractionRun.model_validate_json(Path(args.current).read_text(encoding="utf-8"))

    clause_map = match_clauses(previous.clauses, current.clauses)
    deltas = diff_runs(previous, current, clause_map=clause_map)
    event = changed_obligations_event(
        document_id=current.document.id,
        from_revision=previous.document.revision,
        to_revision=current.document.revision,
        deltas=deltas,
        renumbered=renumbered_clauses(clause_map),
    )
    print(json.dumps(event, indent=2))

    if args.out:
        Path(args.out).write_text(json.dumps(event, indent=2), encoding="utf-8")
        print(f"\nwritten to {args.out}", file=sys.stderr)
    return 0


def cmd_redteam(args) -> int:
    from regextract.evaluation.redteam import run_redteam, write_redteam_report

    settings = _settings_from_args(args)
    if not settings.offline and not has_api_key():
        print("ERROR: --live requires a provider key.", file=sys.stderr)
        return 2

    _banner(settings)
    result = run_redteam(pdf_path=args.pdf, issuer=args.issuer, jurisdiction=args.jurisdiction,
                         settings=settings)
    written = write_redteam_report(result, settings.output_dir)
    print(f"\n{result['summary']['headline']}\n")
    for row in result["rows"]:
        if "caught" not in row:
            print(f"  {row['attack']}  {row['status']}")
            continue
        print(f"  {row['attack']}  {row['category']:26} clause {row['clause_id']:7} "
              f"regex {row['deterministic']:<34} classifier {row['classifier']}")
    print(f"\nreport            {written['report']}")
    return 0


def cmd_doctor(args) -> int:
    """What is installed, what is configured, what will actually run."""
    import importlib

    print("environment")
    print(f"  python           {sys.version.split()[0]}")

    print("\nlibraries")
    for module, purpose in [
        ("pydantic", "output contract"),
        ("pdfplumber", "PDF ingest"),
        ("rapidfuzz", "grounding gate"),
        ("langgraph", "pipeline orchestration"),
        ("litellm", "model routing, retries, fallbacks"),
        ("instructor", "validated structured output"),
        ("qdrant_client", "hybrid search backend for reference resolution (optional)"),
        ("langgraph.checkpoint.sqlite", "review pauses that survive the process (--review)"),
    ]:
        try:
            importlib.import_module(module)
            status = "ok"
        except ImportError:
            status = "MISSING"
        print(f"  {module:16} {status:8} {purpose}")

    settings = get_settings(reload=True)
    print("\nconfiguration")
    for key, value in settings.describe().items():
        print(f"  {key:18} {value}")

    print("\ncredentials")
    print(f"  provider key present   {has_api_key()}")
    from regextract.llm import cassette as cassette_store

    print(f"  cassettes recorded     {cassette_store.count(settings.cassette_dir)}")
    print("\nOffline mode needs no credentials. --live needs a provider key.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="run.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub):
        sub.add_argument("--pdf", default="data/CARL-01.pdf")
        sub.add_argument("--issuer", default="ESMA")
        sub.add_argument("--jurisdiction", default="AE")
        sub.add_argument("--out", default=None, help="output directory")
        sub.add_argument("--live", action="store_true", help="call a real provider")
        sub.add_argument("--record", action="store_true", help="save responses as cassettes")
        sub.add_argument("--qdrant", action="store_true")
        sub.add_argument("--guardrails", action="store_true")
        sub.add_argument("--judge", action="store_true")
        sub.add_argument("--no-agreement", action="store_true", help="single run only")
        sub.add_argument("--inject-faults", action="store_true",
                         help="perturb some quotes so the grounding gate has something to reject")

    extract_parser = subparsers.add_parser("extract", help="run the pipeline")
    add_common(extract_parser)
    extract_parser.add_argument("--review", action="store_true",
                                help="pause for human review; resume with: run.py review")
    extract_parser.set_defaults(func=cmd_extract)

    review_parser = subparsers.add_parser("review", help="resume a run paused for human review")
    review_parser.add_argument("--thread", required=True, help="the run id printed when the run paused")
    review_parser.add_argument("--decisions", required=True, help="JSON file of reviewer decisions")
    review_parser.add_argument("--out", default=None, help="output directory of the paused run")
    review_parser.set_defaults(func=cmd_review)

    log_parser = subparsers.add_parser("verify-log", help="check the publish log's hash chain")
    log_parser.add_argument("--log", default=None, help="path to publish_log.jsonl")
    log_parser.set_defaults(func=cmd_verify_log)

    evaluate_parser = subparsers.add_parser("evaluate", help="run the pipeline, then evaluate it")
    add_common(evaluate_parser)
    evaluate_parser.set_defaults(func=cmd_evaluate)

    diff_parser = subparsers.add_parser("diff", help="compare two revisions")
    diff_parser.add_argument("--previous", required=True)
    diff_parser.add_argument("--current", required=True)
    diff_parser.add_argument("--out", default=None)
    diff_parser.set_defaults(func=cmd_diff)

    redteam_parser = subparsers.add_parser(
        "redteam", help="attack the guardrails with injected text and report what gets through")
    add_common(redteam_parser)
    redteam_parser.set_defaults(func=cmd_redteam)

    doctor_parser = subparsers.add_parser("doctor", help="check the environment")
    doctor_parser.set_defaults(func=cmd_doctor)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
