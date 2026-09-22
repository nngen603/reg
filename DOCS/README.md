# 📚 regextract — Documentation

> **In one line:** these guides explain, in simple words and with diagrams, how regextract turns a regulatory PDF into facts you can trust.

These docs are written for **any reader**. You do not need to be an engineer. Every technical word is explained the first time it appears, and [17 — Glossary](17_GLOSSARY.md) lists them all in plain English.

---

## 🧭 Where to start

| If you are... | Read these, in this order |
|---|---|
| **New to the project** (any background) | [01](01_SYSTEM_OVERVIEW.md) → [02](02_THE_TASK_AND_THE_SAMPLE_DOCUMENT.md) → [06](06_GROUNDING_GATE_AND_VERIFY.md) → [07](07_SCORING_AND_ROUTING.md) → [17](17_GLOSSARY.md) |
| **An engineer** who wants the detail | [01](01_SYSTEM_OVERVIEW.md) → [03](03_OUTPUT_CONTRACT.md) → [04](04_INGEST_AND_STRUCTURE.md) to [11](11_CHANGE_DETECTION.md) in order → [12](12_EVALUATION_AND_TESTING.md) → [16](16_TECH_STACK_CONFIG_AND_COMMANDS.md) |
| **Preparing to present it** | [18](18_PRESENTATION_GUIDE.md) → [01](01_SYSTEM_OVERVIEW.md) → [12](12_EVALUATION_AND_TESTING.md) → [13](13_LIVE_RUN_RESULTS.md) → [14](14_PATH_TO_PRODUCTION.md) → [15](15_DECISIONS_TRADEOFFS_AND_LIMITS.md) |

---

## 📖 Documentation index

| # | Guide | What it covers |
|---|---|---|
| 01 | [System Overview](01_SYSTEM_OVERVIEW.md) | The whole system in simple words: the one idea, the ten steps, the key numbers |
| 02 | [The Task and the Sample Document](02_THE_TASK_AND_THE_SAMPLE_DOCUMENT.md) | What was asked, how the problem was broken down, and the traps inside CARL-01 |
| 03 | [Output Contract](03_OUTPUT_CONTRACT.md) | The output format: facts, evidence, scores, ids, and the list of types |
| 04 | [Ingest and Structure](04_INGEST_AND_STRUCTURE.md) | Steps 1-2: from a PDF to clean text and a tree of clauses |
| 05 | [Extraction and Model Gateway](05_EXTRACTION_AND_MODEL_GATEWAY.md) | Step 4: how AI models are called, checked, and replaced safely; offline mode |
| 06 | [Grounding Gate and Verify](06_GROUNDING_GATE_AND_VERIFY.md) | Step 5: how our code finds every quote, plus rule checks and model agreement |
| 07 | [Scoring and Routing](07_SCORING_AND_ROUTING.md) | Step 7: the score, the impact levels, and who checks what |
| 08 | [Guardrails](08_GUARDRAILS.md) | Step 3: stopping hidden instructions inside documents, and the attack test |
| 09 | [Reference Linking](09_REFERENCE_LINKING.md) | Step 6: linking a reference to another document, or saying NOT_FOUND |
| 10 | [Human Review and Publish Log](10_HUMAN_REVIEW_AND_PUBLISH_LOG.md) | Steps 8-9: pausing for a person, corrections, and the tamper-evident log |
| 11 | [Change Detection](11_CHANGE_DETECTION.md) | Step 10: finding what changed between two revisions of a document |
| 12 | [Evaluation and Testing](12_EVALUATION_AND_TESTING.md) | How we prove it works: 9 failure classes, planted fakes, 109 tests |
| 13 | [Live Run Results](13_LIVE_RUN_RESULTS.md) | One run with real AI models: what happened, what it found, what was fixed |
| 14 | [Path to Production](14_PATH_TO_PRODUCTION.md) | Release, monitoring, scaling and cost for thousands of documents |
| 15 | [Decisions, Trade-offs and Limits](15_DECISIONS_TRADEOFFS_AND_LIMITS.md) | Other options we considered, why we chose this one, and what is not done yet |
| 16 | [Tech Stack, Config and Commands](16_TECH_STACK_CONFIG_AND_COMMANDS.md) | Libraries, settings, commands and output files |
| 17 | [Glossary](17_GLOSSARY.md) | Every technical word, in plain English |
| 18 | [Presentation Guide](18_PRESENTATION_GUIDE.md) | How to present the work: a 10-minute script, a demo, and points to be careful about |

---

## 🎨 How to read the diagrams

The diagrams use the same colours as the submitted `Solution Diagrams.pdf`:

| Colour | Meaning |
|---|---|
| White | Normal code |
| Blue | An AI model |
| Green | A check or a gate (a place where something can be stopped) |
| Yellow | A person |
| Purple | A guard against hidden instructions |
| Grey | Stored data |
| Red | Stopped or failed |

Solid arrows show the normal flow. Dashed arrows show loops, failures and saved state.

> [!TIP]
> GitHub draws these diagrams automatically. In VS Code, install a Mermaid preview extension to see them.

---

## 🔗 Other material in the submission

| File | What it is |
|---|---|
| [`../README.md`](../README.md) | The project README: quick start, stages, stack, limitations |
| [`../RUNBOOK.md`](../RUNBOOK.md) | How to run, check and test every part, step by step |
| [`../samples/`](../samples/) | Saved outputs: evaluation reports (offline, planted faults, live), red-team reports, a review queue |
| `Solution Design.pdf`, `Solution Diagrams.pdf`, `Solution Highlights.pdf` | The design documents (next to `regextract.zip` in the submission folder) |
