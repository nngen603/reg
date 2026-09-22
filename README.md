# regextract

Turns a regulatory PDF into structured clauses and facts. Every fact carries
the exact source text it came from, and nothing is published unless we can
find that text in the document ourselves.

Built against CARL-01, an 8-page ESMA registration requirement from the UAE.

For how to run, test and validate every part of it, see **[RUNBOOK.md](RUNBOOK.md)**.

---

## The one idea

An extraction that reaches a compliance system with no evidence behind it
fails silently. Nobody finds out. Every other kind of failure is loud.

So the centre of this project is a **grounding gate**. The model gives us a
quote. We find that quote in the source ourselves and compute the offsets from
where we found it. The model never supplies a location. If we cannot find the
quote, the fact is never published, no matter how confident the model was.

Everything else — routing, review, change detection — sits on top of that one
rule, and the rest of the system is held to the same standard: a failure has
to be loud. A model call that fails does not become "no facts". A guardrail
that cannot screen a clause does not pass it. A reviewer's correction goes
through the same gate as a model's.

## Quick start

```bash
pip install -r requirements.txt
python run.py doctor                          # check what is installed
python run.py evaluate --pdf data/CARL-01.pdf # run the pipeline and score it
python run.py redteam  --pdf data/CARL-01.pdf # attack the guardrails, see what gets through
python -m pytest -q                           # 109 tests
```

No API key needed. Offline is the default, and everything above runs for free.

## What you get

```
outputs/
  clauses.json        the clause tree: ids, headings, text, page and char ranges
  extractions.json    every fact, with evidence, validators, score and decision,
                      and a corpus link on each external reference
  review_queue.csv    what a person still has to look at, most urgent first, with reasons
  publish_log.jsonl   append-only, hash-chained record of every publication and reviewer decision
  run_manifest.json   every stage: timing, counters, cost, models that answered, settings, flags
  eval_report.md      failure classes, the baseline number, precision and recall, reference resolution
  eval_summary.json   the same, machine readable
  redteam_report.md   what the guardrails caught and missed (python run.py redteam)
```

## The stages

```
ingest → segment → guardrails → extract → verify → resolve → document_flags → route → [judge] → [human_review] → publish
                                                                                                        ↓
                                                                                                      diff  (next revision)
```

| Stage | What it does | Model involved? |
|---|---|---|
| **ingest** | PDF to one clean text with character offsets. Strips the repeating header and footer, harvests document metadata from them, repairs text-layer artefacts | no |
| **segment** | Builds the clause tree from numbering patterns | no |
| **guardrails** | A regex scanner that always runs, and an optional policy classifier, screen document text for injection before it enters a prompt | classifier only, optional |
| **extract** | One structured call per clause, with parent headings and definitions as context. Twice, with two model families, if agreement is on. Every reply is validated against the contract | yes |
| **verify** | Locates every quote in the source. Runs validators. Compares the two runs. A call that failed is flagged, never read as "no facts" | no |
| **resolve** | Links external references to documents already in the corpus, or says NOT_FOUND | no (embeddings) |
| **document_flags** | Document-level problems that override item-level scores | no |
| **route** | Combines the signals, applies impact-weighted thresholds, writes reasons | no |
| **judge** | Orders the review queue. Never decides what is in it | yes, optional |
| **human_review** | In review mode, pauses the run for a person and resumes it with their decisions | no |
| **publish** | Versioned output, the review queue, and an entry per publication in the hash-chained log | no |
| **diff** | Compares two revisions at clause level, matching renumbered clauses, and emits a changed-obligations event | no |

Extraction is the only stage that always calls a model. That is deliberate —
it keeps the surface where things can go wrong small, and it keeps most of the
system unit-testable.

## Trust, risk by risk

| Risk | What stops it |
|---|---|
| A fact the source does not support | The grounding gate. We locate the quote; the model never supplies a location |
| A model call that fails or is cut off | Fails closed. The clause is flagged `extraction_failed`, the document is held, and the clause heads the review queue |
| A reply that does not fit the contract | Validated against the pydantic contract and sent back with the validation error. Still invalid is an error, not an answer |
| One model agreeing with itself | The second run is a different model family. A fallback in the primary's family is dropped |
| A provider quietly swapping models | Provenance records the model that actually answered. The manifest counts fallbacks |
| Instructions hidden in document text | A regex scanner plus a policy classifier. A clause either one flags is reviewed, and the classifier fails closed |
| A wrong link to another regulation | Hybrid search, a reranker, a score floor and a number check; otherwise NOT_FOUND |
| A published fact changing quietly | Corrections create new versions, and every publication is in a hash-chained log that `verify-log` checks |

## The stack, and why each piece is here

| Library | Used for | Why it earns its place |
|---|---|---|
| **LangGraph** | Pipeline orchestration | Each stage is a named node with typed state, and the conditional edges are real. The state holds data only — live objects travel in the runtime context — so a checkpointer works, and `interrupt()` lets a run wait for a person and resume, in another process, from a SQLite checkpoint |
| **LiteLLM Router** | Every model call | Named routes (primary, second, judge, guardrail), transport retries and a fallback policy. The primary never falls back to a weaker model. The second run may, but only to a different family, or agreement would silently become self-consistency |
| **Instructor** | Structured output | Every reply is validated against the contract, and an invalid one is re-asked with the validation error |
| **pydantic** | The output contract | Defined once, and used as the schema the model fills, the validation, the shape the pipeline works on, and what downstream receives |
| **pdfplumber** | PDF ingest | Character-level positions, which is what makes the published offsets real rather than approximate |
| **rapidfuzz** | Grounding gate, lexical reranker | Fast partial matching with alignment, so a fuzzy match still tells us *where* it matched |
| **Qdrant** | Reference resolution backend | Optional. Named dense and sparse (BM25) vectors, fused with RRF inside Qdrant. The same fusion runs in process without it |

### Where the stack is deliberately not used

**Retrieval is not in the extraction path.** Extraction reads a document we
already have in hand. It is not retrieval, and adding a vector store there
would invent a retrieval-quality problem where none exists. Embeddings do
exactly two jobs: linking an external reference to a document already in the
corpus, and matching clauses across revisions.

**Standard codes are not resolved by similarity.** "IEC 60335-1" and
"IEC 60335-2-13" embed next to each other, and they are different standards.
Identifiers need deterministic normalisation.

**LLM-as-judge never gates anything.** It orders the review queue. It cannot
promote an item to auto-accept, cannot reject one, and cannot override the
grounding gate. A model should not be both the thing under test and the test.

**LangChain is not used.** LangGraph orchestrates and LiteLLM calls the models,
so there is nothing left for it to do. Deterministic segmentation means no text
splitters, and pdfplumber means no document loaders.

**No table-extraction engine.** Annex 1 is treated as one clause and its
fifteen product-to-standard links are recovered from linear text. A table
engine would mean maintaining two extraction paths.

**Considered and not built:**

- *A prompt promotion gate* — a prompt registry plus an eval gate that blocks a
  worse prompt from going live. The offline stub ignores prompts, so a gate
  like that only means something on live runs. Offline, `run.py evaluate`
  exiting non-zero when a failure class regresses is the regression gate.
- *Trace export* to a tracing backend. The run manifest already records
  per-stage timing, counters, the models that answered and cost. Exporting it
  as spans changes where the data goes, not the design.
- *A critic agent.* Two model families extracting independently already give
  the second opinion. A third call would add cost for little.

## Offline mode

Offline is the default, and it works two ways. If a recorded response
(a *cassette*) exists for a call, it replays it. If not, a deterministic
rule-based stub produces schema-valid, genuinely grounded extractions from the
clause text.

The stub is not a model and does not pretend to be one. Everything it produces
is labelled `source: stub` in the run manifest, and the evaluation report
refuses to present stub numbers as model quality. Its purpose is to make the
pipeline, the evals, the routing and the tests runnable for free.

The live path — routing, validation and retry, truncation, fallbacks,
provenance — is tested without a network, by a fake provider that speaks the
same tool-call format. A live-mode run through it reproduces the offline run's
item ids and passes all nine failure classes.

To use real models:

```bash
export ANTHROPIC_API_KEY=...   # primary
export OPENAI_API_KEY=...      # agreement run
export GROQ_API_KEY=...        # second-run fallback, judge, guardrail classifier
python run.py extract --pdf data/CARL-01.pdf --live --record
```

`--record` saves the responses, so the next offline run replays real model
output instead of the stub.


## Live test run

One live run on CARL-01 used free-tier models: Mistral (open-mistral-nemo requested) as the main model, OpenAI gpt-oss-120b on Groq as the second family, gpt-oss-safeguard-20b as the safety model, and Jina for embeddings and reranking. The reports are in `samples/eval_report_live.md`, `samples/redteam_report_live.md` and `samples/run_manifest_live.json`, and Appendix E of the design document summarises them.

The model replies were recorded in `cassettes/`, so the run replays offline with no API key:

```bash
REGEXTRACT_PRIMARY_MODEL=mistral/open-mistral-nemo REGEXTRACT_SECOND_MODEL=groq/openai/gpt-oss-120b REGEXTRACT_SECOND_FALLBACKS='[]' REGEXTRACT_INSTRUCTOR_MODE=json python run.py evaluate --pdf data/CARL-01.pdf
```

Two of the 146 calls failed live and were not recorded, so on replay the offline stand-in answers those two clauses.

## Credits

The code, tests and documents were written with help from Claude Code (Anthropic); Appendix D of the design document gives the details.

## Limitations

Worth stating plainly, because the useful conversation starts here.

- **The gold set is about a dozen items.** Enough to prove the harness
  computes precision and recall correctly. Not enough to make a quality claim,
  and the report says so.
- **The routing thresholds are placeholders.** They are not derived from data.
  What the code does show is the mechanism that would set them: precision per
  confidence band, per type, measured on a real gold set.
- **English native PDFs only.** HTML and scanned pages fit the same design but
  are not built. OCR would change the fuzzy thresholds in the grounding gate,
  so it is not a drop-in.
- **One document.** Change detection is tested against synthesised revisions —
  removed facts, and a renumbered clause whose old id is reused — because only
  one revision of CARL-01 was available.
- **Offline numbers are stub numbers.** Run `--live` for anything else.
- **The live path is tested against a fake provider, not real ones.**
  Instructor's tool-call mode is the default; `REGEXTRACT_INSTRUCTOR_MODE`
  switches it if a provider disagrees.
- **The corpus catalogue is a fixture:** the two documents CARL-01 cites, four
  labelled synthetic near-misses, and about two hundred unrelated titles as
  noise.
- **The publish log is tamper-evident, not tamper-proof.** Proof needs
  object-lock storage underneath it.
- **Review has no interface.** Decisions are a JSON file, which is enough to
  show the loop and not what an analyst should use every day.

## Layout

The package is grouped by pipeline step, numbered as in the design: 1 ingest,
2 structure, 3 guard, 4 extract, 5 verify, 6 resolve, 7 score and route,
8 review, 9 publish, 10 diff.

```
regextract/
  config.py         settings and feature flags
  contract.py       pydantic models -- the output contract
  normalize.py      text normalisation and quote location (the gate's matching layer)
  pipeline/         the LangGraph run: pause and resume, checkpoints, the run manifest
    graph.py  checkpointing.py  observability.py
  document/         steps 1-2: reads the PDF and builds the clause tree
    ingest.py  segment.py
  guardrails/       step 3: screens document text for hidden instructions
    rails.py
  extraction/       step 4: one structured model call per clause, and its prompts
    extract.py  prompts.py
  llm/              model access: gateway, routes, cassettes and the offline stub
    gateway.py  routes.py  cassette.py  stub.py
  verification/     step 5: the grounding gate, validators and document flags
    verify.py
  resolution/       step 6: links references to documents already in the corpus
    retrieval.py  vectors.py
  routing/          step 7: scores each fact and routes it; the judge orders the queue
    route.py  judge.py
  review/           step 8: a reviewer's accept, reject and correct decisions
    review.py
  publishing/       steps 9-10: outputs, the hash-chained publish log, and the diff
    publish.py  publog.py
  evaluation/       the evaluation harness and the guardrail red team
    evaluate.py  redteam.py
data/               CARL-01, the corpus catalogue fixture, distractor titles
gold/               hand-labelled data
tests/              109 tests
run.py              CLI
```
