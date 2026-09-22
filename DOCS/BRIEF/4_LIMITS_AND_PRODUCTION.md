# 4️⃣ Limits and production

> **In one line:** What is wrong with this code today, what was traded away on purpose, and what it takes to run it for real — in the order the work has to happen.

---

## 🐛 Known defects

These were found by attacking the project's own claims — taking each docstring that states a property and asking what input falsifies it. **Three of the four contradict a comment in their own file.** All are verified by running the submitted code.

### D1 — Item ids are not stable

`Item.make_id` hashes `sorted(payload.items())`, and the payload includes the model's self-reported `confidence` and the quote text.

```python
p1 = {..., "confidence": 0.90}          ->  CARL-01:7.1:1a88b1a47053
p2 = {..., "confidence": 0.85}          ->  CARL-01:7.1:894bd1f61071
```

The docstring claims *"Re-running the same document gives the same ids, so diffing versions against each other is meaningful"* — and it is wrong twice. The ids are not stable, **and** the diff does not use them: `diff_runs` keys on `(clause_id, kind, signature(...))`, which excludes confidence and quote. So the diff is safe.

**Where it actually bites:** `PublishLog` keys its live-publication map on `item_id`. A changed id misses the lookup, so an unchanged fact is logged as a **new publication** and the old id is logged as **withdrawn** — churn in the one artefact a customer audits. `content_hash` includes the payload too, so it trips the same wire a second way.

**Why it was invisible:** `temperature=0` and offline replay make payloads byte-identical. **`test_item_ids_are_stable_across_runs` exists**, has exactly the right name, and runs the same deterministic pipeline twice — it asserts that a deterministic function is deterministic. A vacuous test is worse than a missing one, because a missing test is an open question.

**Fix:** hash the `signature()` fields, not the whole payload. Confidence and quote are attributes of a fact, not part of its identity.

### D2 — The fuzzy path accepts a number transposition

`meaning_tokens` returns a **sorted** list, so it checks *which* numbers appear, not *where*.

```python
source  = "The supplier shall submit 30 copies within 5 days of the request."
swapped = "The supplier shall submit 5 copies within 30 days of the request."

meaning_tokens(source) == meaning_tokens(swapped)   # ['30','5'] == ['30','5']
locate(swapped, source, 92.0)  ->  found=True, FUZZY, 95.4    # GROUNDED

# control: a changed number is correctly refused
locate("...within 9 days...", source)  ->  found=False
```

The file's own docstring says a close match's *"numbers and negations must be exactly those of the source text it matched"*.

**Blast radius — bounded, and this matters:** `route_item` downgrades **every** fuzzy match to review. So a transposed quote **cannot auto-publish**. The residual risk is that it reaches a reviewer carrying the label `grounded: true` — a green light that has not been earned.

**Why no test caught it:** `test_trust_fixes.py` parametrises three alterations and all three are **substitutions** (a dropped "not", one→two, 15→13). `sorted()` catches every one. Nobody wrote the transposition row.

**Fix:** compare the tokens **in order** — `sorted(...)` → the ordered list — plus a test with exactly this pair. Then re-ground the 15 live fuzzy matches to confirm the tightening costs nothing real.

### D3 — Cassette replay bypasses the contract

```python
if self.settings.offline:
    recorded = cassette_store.load(...)
    if recorded is not None:
        return recorded, meta        # never validated against response_model
```

Only the live path goes through Instructor. The cassette key includes the model, prompt version, taxonomy version and prompt text — but **not the response schema**. Add a required field to `ClauseExtractionDraft` and all 144 cassettes still replay, still pass, and produce items, while live traffic would fail on the first call. The suite goes green on a contract change that breaks production.

Cassettes also store **only the payload** — no served model, tokens, cost, `finish_reason` or timestamp — which is why the live replay cannot reproduce the two truncation failures.

**Fix:** validate the replayed payload through `response_model`; put a schema hash in the cassette key; record `CallMeta` alongside the payload.

### D4 — `find_conflicting_definitions` is O(acronyms × text)

Two regexes compiled and run over the whole document **per distinct acronym**, single-threaded, at the end of every run:

| ~320 pages, distinct acronyms | +0 | +50 | +200 | +500 |
|---|---|---|---|---|
| Time | 1.1 s | 4.3 s | 15.2 s | **34.7 s** |

A long technical regulation with 200+ defined acronyms is ordinary. It never surfaced because it is 28.7 ms of a 23-minute run — when one stage is 93% of the wall clock it hides every other cost curve.

**There is a security angle:** this runs over **untrusted input**. A document with a few thousand distinct capitalised tokens turns a 1-second check into minutes of CPU per worker, for free.

**Fix:** one pass with a single generic regex, bucketed by acronym in a dict — O(text length) regardless of acronym count.

### Also known

- **Gateway lazy-init race.** `self._router` / `self._instructor` are unguarded check-then-set across 4 threads. Benign (the loser's router is discarded) but wrong at 50 workers, where two routers means two independent retry budgets against one quota. One `threading.Lock`.
- **A failed primary discards the second family's good answers.** `if secondary is not None and not draft.primary_error` — the clause produces zero items plus a blocking flag. Defensible (comparing against an empty primary would flood review) but the reviewer gets a blank instead of a starting point.
- **`second_model_only` items sit in the grounding-rate denominator** that decides whether to block the document — speculative facts moving a threshold meant to measure the primary's reliability.

---

## ⚖️ Deliberate trade-offs

| Traded away | For | Revisit when |
|---|---|---|
| Cost (2× calls for agreement) | An independent second opinion and the only channel that surfaces misses | Per-type gold-set data shows the second run adds nothing for a type |
| Latency (per-clause calls) | Exact offsets, parallelism, per-clause retry and evaluation | Never for 300-page documents; a cached-prefix hybrid is worth testing |
| Recall (a strict gate, strict agreement) | Precision — the brief's stated priority | The false-rejection rate is measured and turns out to be high |
| A database | A repo that clones and runs with no services, and diffable artefacts | Immediately, in production |
| Table extraction engine | One ingest path to maintain | Many large tables; Annex 1's 15 rows come from the text layer today |
| Scanned documents | A text-layer path that works | See below — OCR weakens the grounding guarantee and needs confidence carried into evidence |
| Trace export (OTel) | The run manifest already records per-stage timing, counters, served models and cost | Multiple services; the manifest changes *where* data goes, not the design |
| A prompt promotion gate | Offline, the stub ignores prompts, so the gate would measure nothing | The gold set exists. Then it is the highest-value control in the system |

---

## 🚀 Path to production

### Today vs what is needed

| Area | Today | Production |
|---|---|---|
| **Storage** | JSON files, a CSV, JSONL, SQLite | Postgres (documents → revisions → clauses → facts → review events); big artefacts in object storage; publish log append-only **plus** object-lock retention |
| **Orchestration** | LangGraph in one process | LangGraph per document inside a worker; a queue across documents; idempotent on `(source_hash, pipeline_version, prompt_version)` |
| **Concurrency** | 4 configured, **1** in the live run (free tier) | 20–50 behind a **shared token bucket** with jittered backoff. Raising concurrency without a distributed limiter turns a rate limit into a self-inflicted outage |
| **Checkpoints** | Full `PipelineState` — including the whole document text and every raw draft — re-serialised after every node | Ids and small results in state; artefacts in object storage by reference |
| **Review** | CSV, or a LangGraph interrupt | Assignment, locking, SLAs, a keyboard-first UI showing **reasons not scores**, adjudication on a sample |
| **Thresholds** | Placeholders (0.90/0.80/0.75, fuzzy 92, 5%) — declared as such in every manifest | A calibration table per (type, tier, score band), set on the Wilson **lower bound**, refreshed on any model/prompt/taxonomy change |
| **Observability** | Per-run manifest | OTel spans; alerts on grounding-failure rate, review rate, override rate, served-model drift, cost per document |
| **Releases** | Version strings bumped by hand | A prompt registry with a promotion gate — no prompt ships without passing the failure classes and per-type precision. Canary by document share |
| **Security** | Guard on clause text; keys from `.env`; hash chain | Per-tenant isolation **at the data layer**; secrets manager; PII detection; signed source fetch with fingerprint alerting; size and time budgets on anything parsed from a downloaded file |

### Sequence — the order matters more than the list

| Phase | Weeks | Deliverable | Why here |
|---|---|---|---|
| **0 — Fix the known defects** | 1 | D1–D4 above; lock the publish-log append; property-based tests over `locate()` | Cheap, and three of them undermine claims the system makes about itself |
| **1 — The gold set** | 4–6 | ~300 labelled items across the 4 types downstream acts on, with **adjudication** and an unbiased random stratum | **The critical dependency.** Calibration, the promotion gate, per-type thresholds, the agreement fix and every cost lever are blocked on it |
| **2 — Calibrate** | 2 | Real thresholds; agreement v2 (quote-span overlap as pre-filter, subject + modality as the claim), proven on the gold set | Turns a placeholder system into a measured one |
| **3 — Platform** | 6–8 | Postgres, queue, workers, real review UI, OTel, prompt registry | Worth building once there is something to measure against |
| **4 — Scale and cost** | ongoing | Batch API for backfills; reorder the prompt so the fixed part caches; skip unchanged clauses; single-run and cheaper models for low-impact types | **Every saving must pass the gold set. A saving that lowers precision is not a saving** |

### Sizing

| | |
|---|---|
| Cost per document, frontier models | ~$3 (design estimate); ~$0.02 measured on free tiers |
| Biggest steady-state lever | Skipping unchanged clauses — most clauses in a new revision are unchanged |
| Second biggest | Prompt caching, after reordering so the fixed parts (document details, definitions) come first. Definitions are ~76% of the user prompt |
| First thing that breaks at 300 pages | **Not memory** — the model's *output* budget. ANNEX-1 already proved it at 8 pages. Fix by splitting oversized clauses before extraction, not by raising `max_tokens` |

### The three numbers that decide whether this works

1. **Review rate vs reviewer capacity.** If precision-preserving thresholds send 60% to review and capacity supports 15%, the economics do not close and no prompt work fixes it.
2. **The gate's false-rejection rate.** Never measured. A gate that wrongly stops good facts trains reviewers to rubber-stamp — worse than no gate.
3. **Per-type precision at the publish boundary.** Auto-publish turns on **per fact type**, only where measurement supports it, and never for a type analysts already do better on.

---

## 🔒 What does not change

| | |
|---|---|
| **The grounding gate is a gate** | Not a weighted signal. Ungrounded never publishes, at any score |
| **Offsets are ours** | Computed from our own match, never taken from the model |
| **Failures are loud** | A failed or truncated call is flagged, never treated as "no facts here" |
| **Provenance records who actually answered** | Not who we asked. Mistral silently served a different model on all 72 calls |
| **Two independent families** | Agreement is evidence; the second family is the only channel that surfaces what the primary *missed* |
| **A reviewer cannot publish unsupported evidence either** | Accepting an ungrounded item is refused; a corrected quote goes through the same gate |
| **The judge only orders the queue** | It can never publish, reject or override |

---

## 🎯 Honest summary

This is a **proof of concept for the riskiest part of a design**, run once against real providers on one 8-page document with small free-tier models. It demonstrates that the machinery behaves — fabricated quotes stopped, truncation held the document, a silent model swap caught, nothing weak published. It demonstrates **nothing about quality** with the models the design names, and every threshold in it is a placeholder that says so out loud.

The gap between this and a product is not model work. It is a gold set, a calibration table, a datastore and a review tool — in that order.

---

**Back to:** [the index](README.md)
