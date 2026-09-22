# 1️⃣ Problem and approach

> **In one line:** The brief asks for extraction with evidence and a review signal, at 5–300+ pages and thousands of documents, where precision beats latency — so the system is built around verification rather than around extraction.

---

## 📌 The brief

Design an AI system that reads regulatory documents and extracts:

- **clauses** — numbered units of a document, classified (definition, requirement, exemption, …)
- **entities** — organisations, dates, money and numeric thresholds, product categories, references to other laws and standards

Every result must carry **evidence** (where it came from) and a **confidence or review signal**. Plus a **proof of concept** for one important or risky part.

Constraints given: **5 to 300+ pages**, **thousands of documents**, **high precision matters more than latency**.

Sample: **CARL-01** — 8 pages, UAE, registration of low-voltage electrical products. 73 clauses after segmentation.

---

## 🎯 What the constraints actually decide

Three lines in the brief determine most of the architecture:

| Constraint | What it rules in | What it rules out |
|---|---|---|
| **Precision > latency** | A hard gate that refuses to publish unverified facts; human review as a first-class pipeline stage | Anything that trades correctness for throughput; "publish and correct later" |
| **300+ pages** | Per-clause calls, parallelism, per-clause retry and evaluation | One call per document — the *reply* is the limit, not the context window |
| **Thousands of documents** | Change detection as the product; steady-state cost dominated by re-extraction avoided | Anything that re-reads a whole document on every revision |

---

## 💡 The approach

**Verification-first.** The design assumes the model is wrong until checked, and puts every check in deterministic code that the model cannot influence or skip.

| The model is allowed to | The model is never allowed to |
|---|---|
| Propose a fact (subject, modality, action, condition) | State where it came from — **we** compute page and char offsets |
| Quote a supporting sentence | Decide whether its quote is real — **we** locate it |
| Report its own confidence | Have that confidence be the deciding signal — it is weighted 0.20 and mostly breaks ties |
| Classify a clause | Decide the impact tier — that is a lookup from the type |
| (as a judge) Order the review queue | Publish, reject, or override anything |

### The trust ladder, strongest first

1. **Grounding** — a *gate*, not a signal. Ungrounded is never publishable at any score.
2. **Agreement** — two model *families* (not two samples of one model) saying the same thing. Weight 0.45.
3. **Validators** — cheap deterministic per-type checks. Weight 0.35.
4. **Self-reported confidence** — weakest, weight 0.20.

The **order** matters more than the weights, and the weights only apply once the gate is passed.

---

## 🔬 Why the grounding gate was chosen as the proof of concept

The brief asked for the most important or risky part. The riskiest failure in this domain is not a missed fact — it is a **confidently fabricated one that reaches a compliance system with a citation attached**. A missed obligation is a gap someone eventually notices. A fabricated obligation with a plausible clause reference is acted upon.

The gate is also the part that is cheapest to get wrong subtly: ask the model for the page number and everything *looks* correct, because the model will always give you one.

---

## ⚖️ What was rejected, and when it would win

### Overall shape

| Option | Why not | When it would win |
|---|---|---|
| **Whole document in one call** | Output budget, not input, is the limit at 300 pages. One retry costs the whole document. No per-clause evaluation. (CARL-01 is 8 pages, so this *would* have worked — running the production shape on a small document was a choice) | Documents under ~30 pages; a pilot; or as an *extra* document-level check on top |
| **Agent loop** (extractor + critic + verifier) | A check the model may skip is not a gate. Variable cost and step count break both the budget and the audit trail | Open-ended analyst questions; unfamiliar document types |
| **Classical NLP** (layout model, rules, trained classifiers) | Needs ~2,000 labelled clauses that do not exist. Cannot do obligations — subject/modality/action with an absent subject is a pragmatic judgement, not a span | Phase 3, once reviewer corrections have produced the labels. It is the cost end state |
| **RAG inside extraction** | Extraction reads a document we already have. Adding retrieval creates a search-quality problem that does not exist | Search is used, but only for *linking references* and *matching clauses across revisions* |

### Trust

| Option | Why not |
|---|---|
| Ask the model for page and char offsets | Trusting the thing being checked. The model gives a quote; code finds it |
| Model confidence as the main signal | Self-reported confidence is poorly calibrated. A model is confidently wrong |
| One model sampled twice | Measures the model's *variance*, not its correctness. Same blind spots |
| An LLM judge as a release gate | The thing being tested cannot be the test. The judge here only **orders** the review queue |
| A fallback model for the primary | Quality drops silently — nobody sees a weaker model answered. The primary only retries; failure raises a flag. A *second-run* fallback is allowed, but only to a different family (same-family fallbacks are dropped at startup) |
| Treat a failed call as "no facts" | The silent failure. An empty answer looks exactly like "nothing here" — so it is flagged explicitly |

---

## 📐 Taxonomy

**10 clause types**, merged down from 13 — `liability → responsibility`, `penalty + authority_power → enforcement`, `fees → administrative`. Merging happened where analysts could not draw a reliable boundary: a category two people label differently is not a category.

```
definition · scope · exemption · requirement · responsibility
procedure · enforcement · validity_and_dates · normative_reference · administrative
```

**11 entity types**, and the two that carry the most risk are `monetary_threshold` and `numeric_threshold` — a misread number is the failure with the largest downstream consequence, which is why both have dedicated validators.

**Impact tier** is derived from the type, not decided by the model:

| Tier | Contains | Publish bar |
|---|---|---|
| **High** | validity_and_dates, enforcement, exemption, scope, dates, all thresholds | 0.90 **and** full agreement **and** every validator passing |
| **Medium** | requirement, responsibility, procedure, references, product_category | 0.80 |
| **Low** | everything else | 0.75 |

**Obligations are never low impact** — medium by default, high when they sit in a clause about validity, enforcement, exemption or scope. Downstream systems act on obligations, so they do not get the cheap threshold.

---

## 🧩 What the sample document taught the design

| Observation in CARL-01 | Design consequence |
|---|---|
| The document defines its own vocabulary and then relies on it | Every clause prompt carries a digest of the document's definitions section |
| **ECAS** is expanded two different ways — "Scheme" in §1, "Systems" in 5.6 | A generic conflicting-definition check; items using a disputed term are individually escalated, not blanket-blocked |
| 11.4 says renewal "shall be required a month before the expiration" and never says *by whom* | `subject` may be an empty string. Forcing the model to name one would make it invent one — the worst failure available |
| 7.1.1's list lives in 7.1.1.1–7.1.1.6; numbering carries meaning | Segmentation is deterministic code over clause numbering, never a model's opinion |
| Annex 1 is a 15-row product→standard table | Table content is recovered from the text layer; it is also where the live run's truncation failure landed |
| The source contains 3 typos | Quotes are preserved **verbatim** — a "corrected" quote would not ground, and would not be the source's words |

---

**Next:** [2 — How it works](2_HOW_IT_WORKS.md)
