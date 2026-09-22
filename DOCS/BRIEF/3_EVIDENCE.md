# 3️⃣ Evidence

> **In one line:** Offline runs prove the checks work; one live run proves they work against real model mistakes; neither proves quality, and the documents say so in the same place they report the numbers.

---

## 🧭 The two kinds of run, and what each can prove

| | **Offline** | **Live** |
|---|---|---|
| Who answers | A deterministic rule-based stub, or a recorded cassette | Real providers over the network |
| Cost | $0 | ~$0.02 for CARL-01 on free tiers |
| Determinism | Total | None |
| **Can prove** | The checks fire correctly on known inputs; every failure class; regression | Real providers return what the code expects; real mistakes are caught; failures fail closed |
| **Cannot prove** | Anything about model behaviour — the stub never makes a real mistake | Quality at scale (n=1 document, n=1 run, small models) |

The stub always quotes real sentences, so a clean offline run has a 100% grounding rate by construction. That is why `--inject-faults` exists.

---

## 🎯 The nine failure classes

Not one accuracy number. Nine named failure modes, each with a specific assertion, so a regression names itself rather than moving a mean.

| # | Class | The specific trap in CARL-01 | Offline | Live |
|---|---|---|---|---|
| **A** | Layout / text-layer artefacts | Stray "U" from underlined headings; a footer on every page — and "UAE" must survive | ✅ | ✅ |
| **B** | Clause split across pages | 3.1.1 runs over pages 2–3 | ✅ | ✅ |
| **C** | Irregular numbering | "10" with no full stop; unnumbered FEES; depth 4 | ✅ | ✅ |
| **D** | Wrong entity type | `IEC 60335-1: 2007` — the year is part of a code, **not a date** | ✅ | ❌ |
| **E** | Inventing a value | FEES lists fee *types* with no amounts; no effective date; no named person | ✅ | ✅ |
| **F** | References | 8.1 external+named; 8.3→4; 5.2→3.1; "Annex I"→ANNEX-1 | ✅ | ❌ |
| **G** | Conflicting terms + verbatim fidelity | ECAS two ways; **3 source typos must be preserved** | ✅ | ✅ |
| **H** | Subject and modality | Passive voice ("shall be collected by ESMA"), *should*, *may*, *reserves the right* | ✅ | ✅ |
| **I** | Tables | Annex 1 maps 15 products to standards | ✅ | ❌ |

**Offline: 9 of 9. Live: 6 of 9.** Every live failure is a *model* weakness and the report names which assertion failed:

- **D** — the small model did not label "75 volts", "1500 volts", "15 ampere" as numeric thresholds
- **F** — three of four checks passed; "Annex I" was not linked to ANNEX-1
- **I** — the ANNEX-1 reply was **truncated**, so 0 products came back

**Why classes and not one number:** an accuracy number that moves from 0.87 to 0.84 tells you nothing about what to fix. Class D failing tells you exactly which assertion broke, on which clause, with which value.

---

## 🧨 Fault injection — the gate measured against itself

`--inject-faults` plants deliberately wrong facts (fabricated quotes, altered numbers, dropped negations) into the stub's output.

| | Clean run | With injected faults |
|---|---|---|
| Candidate extractions | 153 | 153 |
| **Failed the gate** | 0 (0.0%) | **18 (11.8%)** |
| **Published** | 133 | **0** |
| **Would have published without the gate** | 133 | **18** |

That last row is the whole argument for the gate, stated as a measurement: a pipeline without it publishes every planted fact.

---

## 🌐 The live run — 11 September 2026

**Setup:** the same code, the same document, real providers, **free-tier** accounts — so one request at a time, and small models. The design names Claude Opus 5 and GPT-4.1; **this run used neither**. It used `open-mistral-nemo` (primary) and `gpt-oss-120b` on Groq (second family). Two settings differed: 6 transport retries instead of 2, and Instructor's JSON mode instead of tool-call mode.

### What happened

| Measure | Value | Reading |
|---|---|---|
| Calls | 146 | 73 clauses × 2 families |
| Failed | 2 | Both ran out of output budget — ANNEX-1 on the primary, one Groq call |
| Facts | 353 | |
| Exact match | 320 | Passed the gate |
| Fuzzy match | 15 | Kept, **always** sent to a person |
| **Not found** | **18 (5.1%)** | **Stopped. Score 0.** First time the gate met real, unplanted model mistakes |
| **Auto-published** | **0 of 353** | See below |

### Why nothing published — and why that is correct

Two **blocking** flags fired; either alone holds the whole document:

1. **`extraction_failed`** — ANNEX-1 never got a usable answer. A missing answer looks exactly like "this clause has no facts", so the system cannot vouch for completeness and says so.
2. **`high_grounding_failure_rate`** — 5.1% > the 5% limit. At that rate the other scores stop being trustworthy.

Plus `conflicting_definition` (ECAS), which is *targeted* — it escalates only items using the disputed term.

By tier: 49 high, 198 medium, 106 low — all to review. The judge was skipped by design.

> **The system was handed weak model output and did not publish it. It held the document and said why.** That is the designed behaviour, not a failure.

### The four things only a live run could show

| Finding | Why offline could never show it |
|---|---|
| **Provenance caught a silent model swap.** Mistral was asked for `open-mistral-nemo` and served `ministral-8b-2512` on all 72 calls. Nobody was told | Offline there is only the stub |
| **Truncation fails closed.** Two cut-off replies became errors, not empty answers, and one held the document | Offline calls never fail |
| **The gate stops real mistakes.** 18 of 18 | The stub always quotes real sentences |
| **Real providers break the contract in real ways.** Some wrapped the reply in a one-item list | No provider is involved offline |

### Three bugs the live run found — and fixed

| Bug | Cause | Fix |
|---|---|---|
| The judge returned nothing | It was given **8 tokens** to answer with one number — but `gpt-oss-20b` is a reasoning model, and the budget covers thinking too | 512 tokens, with the reason in the code comment |
| Replies wrapped as `[{...}]` | Some models wrap a single object in a list | The contract accepts **exactly** that shape and nothing looser. A test covers it |
| API keys not loaded | Keys were in `.env`; the model library reads the environment | `run.py` loads `.env` first |

---

## ⚠️ The uncomfortable numbers, reported anyway

### Agreement: 23%

| Kind | Agreed | Total | Share |
|---|---|---|---|
| Obligations | 9 | 90 | 10% |
| Entities | 68 | 247 | 28% |
| **All** | **80** | **353** | **23%** |

**This is mostly a measurement artefact.** The comparison keys on subject, modality and the **first 80 characters of the action**. Two models describing the same obligation in different words score zero — "submit a filled ECAS application form" vs "submit the application form with the listed documents" are both right and score as disagreement.

**Why it was published rather than quietly loosened:** a looser comparison raises agreement, raises scores and publishes more facts. With no gold set to say whether the extra agreement is *real*, loosening a trust signal to improve a headline number is the exact failure the design exists to prevent. The fix is designed (compare located quote spans as a pre-filter, then subject + modality) and not shipped, because it must be measured first.

**Low agreement is safe in one direction** — a disputed fact cannot auto-publish (max score 0.55) — and expensive in the other: it sends more work to people.

### Precision 0.16, recall 0.80 — on ~11 labelled items

| Group | TP | FP | FN | Precision | Recall |
|---|---|---|---|---|---|
| entities | 8 | 42 | 2 | **0.16** | 0.80 |
| cross-references | 1 | 1 | 0 | 0.50 | 1.00 |

With eleven labelled items this is **not a quality claim** and never was. What it does establish is that the harness computes precision and recall correctly **against real model output** rather than against a stand-in that cannot be wrong. The small model added many loose entities ("components", "equipment" as product categories) and missed "UAE" as a location twice.

*(The offline equivalent is 0.909 / 1.0 — which measures the stub, not a model, and is equally not a quality claim.)*

---

## 🛡️ The red team

Seven prompt-injection attacks, each appended to a **real** CARL-01 clause so it travels the normal path.

| | Offline (pattern scanner only) | Live (scanner + safety model) |
|---|---|---|
| Attacks caught | **5 of 7** | **6 of 7** |
| Facts from attacked clauses auto-published **without** the guard | 17 | 7 |
| …**with** the guard | 5 | **3** |

- **A6** (addressed to "automated processing systems") — missed by regex, **caught** by the policy classifier. This is exactly the gap the classifier exists to close: no pattern list names every phrasing aimed at a machine.
- **A7** (forged text that reads like ordinary regulation) — missed by both, and **no prompt-level filter can catch it**. The defence is *source integrity*: fetch only from the issuer, keep the content hash ingest already records, and alert when a file's fingerprint changes without a new revision.

---

## 📋 What the evidence does and does not support

| ✅ Supported | ❌ Not supported |
|---|---|
| The pipeline runs end to end with real providers | Quality with the models the design names |
| The gate stops real fabricated quotes (18/18 live, 18/18 injected) | That the thresholds are right — they are placeholders |
| A failed or truncated call holds the document | Precision or recall at any meaningful scale (~11 labelled items) |
| Provenance catches a provider silently swapping models | That 23% is the real agreement rate |
| Two families surface misses the primary made (77) | That the reranker floor is calibrated (Jina got 6/8 vs the lexical reranker's 8/8) |
| Both guard layers work with a real safety model (6/7) | That any of this generalises to other documents, layouts or languages |

### Known measurement gaps

- **The gate's false-rejection rate is never measured.** We know it stopped 18; we do not know how many of those were good quotes. A gate that wrongly rejects trains reviewers to rubber-stamp.
- **5.1% vs a 5% threshold is one fact wide.** The 95% interval on 18/353 is roughly [3.3%, 7.9%] — the threshold sits inside it. The document was held anyway, because `extraction_failed` fired independently.
- **The gold set is self-authored**, tiny, and grown from reviewer corrections — which are a *biased* sample, because they only come from facts the router already doubted.

---

## 🔁 Reproduce any of it

```bash
# offline: everything, free, no API key
python run.py evaluate --pdf data/CARL-01.pdf
python run.py evaluate --pdf data/CARL-01.pdf --inject-faults
python run.py redteam
pytest -q                                    # 109 tests, ~4s

# replay the live run from the recorded cassettes -- free
REGEXTRACT_PRIMARY_MODEL=mistral/open-mistral-nemo \
REGEXTRACT_SECOND_MODEL=groq/openai/gpt-oss-120b \
REGEXTRACT_SECOND_FALLBACKS='[]' \
REGEXTRACT_INSTRUCTOR_MODE=json \
python run.py evaluate --pdf data/CARL-01.pdf
```

The long replay command is needed because a cassette key includes the **model name** — the defaults name different models, so their keys would match no recording. The 2 failed live calls were never recorded, so on replay the stub answers those two clauses: the replay is very close to the live run, not identical.

---

**Next:** [4 — Limits and production](4_LIMITS_AND_PRODUCTION.md)
