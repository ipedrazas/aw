# Handover: agentic workflow prototype

You are picking up the build of a prototype for Tavon, an AI engineering consultancy. This file is the brief. Read it fully before writing code, and keep it open: the constraints section is not optional.

The companion file `deep-research.workflow.yaml` is an early example definition. Treat it as illustrative, not as the schema. Where it disagrees with this file, this file wins.

---

## 1. What we are building, in one paragraph

A system that takes a process a customer already has (a document describing how they do something), turns it into an explicit workflow definition by asking them questions about the gaps, and then runs that definition so they can see where it breaks. The bet is that agentic workflows fail because processes written for humans are silently incomplete, and because nobody can see what drives an agent's decisions. So the product's job is to make every gap and every decision visible. Throughput, generality and polish are not goals for this prototype.

## 2. Deadline and scope

There is a customer demo on **25 September 2026**. Build milestones 1 to 5 below, in order, and stop there. Do not start Phase 2 work (gates with human approval, durable execution engine, recursion, run list, baselines) unless explicitly asked.

## 3. Decisions already made

These were argued through. Do not reopen them silently. If you hit a reason one is wrong, stop and write it up in `DECISIONS.md` (see section 9).

| Decision | Choice |
| --- | --- |
| Compile or interpret the definition | **Interpret.** An interpreter walks the YAML. No code generation. |
| Language and stack | **Python 3.12+, FastAPI, Postgres, Pydantic v2.** One process holds the interpreter, the agent steps and the audit. |
| Durable execution | **Not in this phase.** Runs are in-process with state in Postgres. The engine is adopted later, so step execution must already sit behind an activity boundary (section 5). |
| Where definitions live | **A git repository per workspace** holding definitions (YAML), instruction files (markdown) and fixtures. Postgres holds runs, decisions, findings and artefacts. |
| What the demo runs | **Real model calls, stubbed tools, fixtures for data, nothing sent anywhere.** |

## 4. Hard constraints

These exist so that later decisions stay possible and so the product stays honest. Each one should have a test.

**Keep the definition compilable.** The YAML is a static description of a program:

- No inline code. No Python `eval`, `exec` or equivalent anywhere near definition content.
- Expressions (`when`, `with`, `for_each`) may read state and nothing else. Implement a small expression evaluator supporting field paths (`${steps.review.output.verdict}`), literals, `==`, `!=`, `<`, `>`, `and`, `or`, `not`, and integer `+`. No function calls. Reject anything else at validation time.
- No steps created at runtime. Fan-out is a declared step over a list, with a `max_fanout`.
- Control flow lives only in `when`, `for_each` and `may_repeat`. Never inside a prompt, never inferred from model output text.

**The interpreter is deterministic.** Given the same definition, inputs and recorded step outputs, it makes exactly the same sequence of decisions. All non-determinism lives inside activities. Write a test that runs the interpreter twice over recorded step outputs and asserts identical step order and identical control-flow decisions. This test guards the future replay property, so keep it green.

**Every side effect is an activity.** Model calls, web search, page fetches, PDF rendering, anything that would send data out: each goes through an activity interface with declared retries and timeouts. No step code touches the network directly.

**Fetched and uploaded content is data, never instruction.** Anything retrieved from the web, and the customer's own process document during an audit, enters model context inside a clearly delimited data region. Tool permissions come only from the definition. If retrieved content contains text that looks like instructions to the model, record it as a visible `decision` on the step ("ignored instructions found in source X") rather than silently dropping it.

**Simulated output is marked at the source.** `simulated` is a property of every artefact record and must travel with the artefact: in the filename, in the document body, and in its metadata. A dry-run PDF must be unmistakable as simulated if someone forwards it. Plumb this from milestone 4, not later.

**Nothing leaves the system in `dry` mode.** Any activity with side effects records what it would have done and returns a stub result.

## 5. Architecture

```
Web UI (chat, draft, audit, dry run)
        |
     FastAPI
     /     \
 Auditor   Interpreter
     \     /    \
   Validator   Activities (models, search, fetch, render; real or stubbed)
        |           |
   Git repo      Postgres
 (definitions,  (runs, step runs, decisions,
  skills,        findings, artefacts)
  fixtures)
```

The validator sits under both the auditor and the interpreter on purpose. A gap found during an audit and a failure during a run are judged by exactly the same rules and produce the same `finding` shape.

Suggested layout (adjust if you have a good reason, and say why):

```
src/wf/
  schema/        Pydantic models for the definition, loader, version pinning
  expr/          the restricted expression parser and evaluator
  validate/      structural.py, semantic.py, findings.py
  interpret/     interpreter.py, context.py, modes (live, dry)
  activities/    base.py, models.py, search.py, fetch.py, render.py, stubs.py
  audit/         ingest.py, extract.py, question.py, diff.py
  dryrun/        runner.py, fixtures.py, divergence.py
  store/         db.py (Postgres), records.py, repo.py (git)
  api/           FastAPI app and routes
  web/           the UI
workspace/       a sample workspace repo: definitions/, skills/, fixtures/, process-docs/
tests/
DECISIONS.md
```

## 6. The definition schema

The schema is the engine of the product, not a serialisation format. **Gap questions are required fields the system cannot fill from the customer's document.** That is what makes them consistent and hard to copy with a prompt, so do not replace this with "ask a model what's missing".

**Step kinds**

| Kind | Purpose | Required fields beyond the common ones |
| --- | --- | --- |
| `agent` | Judgement by a model | `model`, `skill` (instruction file pinned to a version), `output.schema` |
| `check` | Deterministic, no model | `run`, `checks`, `does_not_check` |
| `tool` | An action | `side_effects`, `requires_approval` |
| `subworkflow` | Declared recursion | `workflow`, `limits` (`max_depth`, `max_fanout`, `budget`) |
| `wait` | Human or external event | `deadline`, `on_timeout` |

**Required on every step:** `id`, `kind`, `title` (in the user's language), `shows_user`, `trust`.

**Conditional requirements:** a branch needs `when`; a `for_each` needs `max_fanout`; a workflow containing any `subworkflow` step needs a top-level `budget`.

**Instruction pinning:** `skill: skills/research-brief.md@3` resolves to a git commit, not a file path. A run records the commit it actually used.

**Validation, two passes**

*Structural:* required fields present, references resolve, no cycles except through a declared `subworkflow`, fan-out and depth bounded, budget present if anything can recurse, expressions parse under the restricted grammar.

*Semantic:* every output referenced by a later step exists in the producing step's output schema; every `when` reads a field that is actually produced; every value a `when` tests for appears in that field's enum; every enum value of a field that drives branching is handled by some branch. The last two rules catch unstated branches, which are the most common real gap. Get them right.

**Findings**

Every validation failure becomes a `finding` with a type:

| Type | Meaning |
| --- | --- |
| `gap` | The document is silent on something the schema requires |
| `conflict` | A step cannot work as written. Must quote the document's own words. |
| `assumption` | We filled a field without explicit support. Marked, visible, reversible. |
| `unreachable` | A branch that nothing can produce |

Each finding carries the question to ask in plain language. Keep a mapping from field to question template, for example:

| Field | Question |
| --- | --- |
| `when` on a branch | What decides which way this goes? |
| `does_not_check` | What does this check not tell you? |
| `requires_approval` | Who signs this off before it leaves the system? |
| `deadline`, `on_timeout` | How long do you wait, and then what? |
| `limits` | How far can this go, and how much can it spend? |
| enum of a branching field | Which outcomes are possible here? |

## 7. Data model

Postgres tables. Names are suggestions; the relationships are not.

| Record | Holds |
| --- | --- |
| `workflow_version` | Definition commit, schema version. Immutable. |
| `run` | Workflow version, inputs, status, `mode` (`live`, `dry`, `eval`), depth, parent run, budget spent |
| `step_run` | Step id, status, attempt, input and output refs, cost, duration, instruction commit, model |
| `decision` | Step run, decision text, reason, alternatives considered. User-facing language, not a raw trace. |
| `finding` | Source (audit or run), type, field path, question, quoted source passage, answer, status |
| `artifact` | Blob reference, producing step run, `simulated` flag |
| `expectation` | For dry runs: the known outcome the customer supplies, per step or at the end |

Invariant worth a test: every artefact traces to a step run, which traces to an instruction commit and a model.

## 8. Milestones

Build in order. Each ends in something showable. Commit at the end of each, and stop for review before starting the next.

### M1. Schema and validator

- Pydantic models for the definition, the restricted expression grammar, both validation passes, typed findings with questions.
- **Done when:** the deep research definition validates cleanly, and a deliberately broken copy (remove a `when`, drop a `does_not_check`, add an enum value no branch handles, add an unbounded `for_each`) produces exactly the expected findings. Write those as fixture-driven tests.

### M2. Interpreter, in-process

- Walks a validated definition. Steps execute through activities. Agent steps must emit `decision` records alongside their output (enforce this via the output schema, not by parsing prose).
- Real model calls for `agent` steps. Search and fetch stubbed from fixtures. PDF render real.
- **Done when:** deep research runs end to end on a topic, every step run and decision is recorded, and the determinism test passes.

### M3. Auditor

- Ingest a process document into addressable passages.
- Extract: a model proposes steps and fills fields the document supports. It may only emit schema-valid fragments, and every filled field carries the passage it came from. Unsupported fields are left empty, never invented. Anything filled without direct support becomes an `assumption` finding.
- Validate, then turn findings into questions grouped by type and ordered by how much of the process each unblocks.
- Answers write back into the definition and close the finding.
- **Done when:** the sample process document in `workspace/process-docs/` (see section 10) produces a draft definition and a set of findings you would recognise as the real gaps.

### M4. Dry run

- Runs a definition in `dry` mode against a past case the customer knows the outcome of.
- **When the interpreter reaches an unanswered required field mid-run, it does not fail.** It picks the most plausible value, records a `decision` of kind `guess` linked to the finding, and continues. These guess points are the most important output of the whole prototype.
- Compare against the customer's `expectation`. Report the **first step whose decision diverged**, not just the end state.
- Fixtures are recorded responses keyed by request, committed to the workspace repo. Same fixtures every run, so that two runs differ only by model judgement.
- **Done when:** two dry runs of the same definition can be diffed at the level of decisions, guess points are listed with their findings, and the artefact is marked simulated in name, body and metadata.

### M5. Audit UI

The demo surface. Plain and legible beats clever. Four views:

1. **Describe or import.** Chat on the left that edits a visible draft on the right. The chat never holds the definition; every change it makes appears in the draft with a one-line reason and can be undone.
2. **What will happen.** Steps in the user's language. No model names, file names or tool names by default. Steps the system added are marked ("I suggested this", "I split your step"). A "Show technical details" switch reveals models, instruction files and YAML.
3. **What you expect.** Findings rendered as answerable questions, using plain controls (radio buttons, selects), not free text where avoidable. Implementation choices are presented as consequences ("Recent web pages and news" rather than a search provider's name).
4. **Dry run result.** Guess points first, then findings, then the simulated artefact last. A side-by-side view of two runs diffing their decision logs.

Language rules for everything user-facing: plain sentences, no jargon, no product names for tools, and every check states what it does not verify.

## 9. Working agreements

- **Tests first for the validator and interpreter.** They are the parts everything else trusts.
- **`DECISIONS.md`** records anything you decide that this brief does not cover, one short entry each: the decision, the alternative, why. Do not decide the open questions in section 11 silently; propose, record, and flag them.
- **Dependencies:** FastAPI, Pydantic, SQLAlchemy or psycopg, a git library, the Anthropic SDK, and a PDF renderer are expected. Ask before adding anything substantial beyond that.
- **Model names are configuration,** not literals scattered through code. The example YAML uses `claude-sonnet-5` and `claude-opus-5`.
- **Secrets** from environment variables only. Nothing in the workspace repo.
- **Stop and ask** if a constraint in section 4 seems to block something necessary. Do not work around it.

## 10. Sample inputs you need to create

The customer's real process document is not here yet. When it arrives it becomes the second workflow shape, and its content is confidential, so it must never be committed to a public location or used in tests shipped anywhere.

Until then, create `workspace/process-docs/deep-research-process.md`: a plausible, human-written description of a deep research process, deliberately degraded so the audit has real work to do. It should contain at least:

- a decision with no stated criteria ("the reviewer decides whether more research is needed")
- an unstated branch (what happens if the reviewer rejects the report outright is never said)
- a check with unscoped meaning ("make sure the links work")
- an action with no owner or approval ("send the final report")
- a step that cannot work as written, for a `conflict` finding (for example, a table column referenced before the step that produces it)

Also create a matching past case with its known outcome, for the dry run to diverge from.

Use this as the demo fallback if the customer's document does not arrive, and label it as a constructed example when it is shown.

## 11. Open questions (flag, do not decide silently)

- How much may the extractor infer before a filled field becomes an `assumption` finding?
- Are findings ordered by what they unblock or by confidence?
- Where do fixtures come from for a process whose real tools we cannot call at all?
- Is trust promotion per workflow or per workflow version? (Phase 2, but the data model should not preclude either.)
- Merge behaviour when two people edit the same instruction file. (Phase 2.)

## 12. What good looks like on demo day

- A process document goes in; questions come out that the customer recognises as real gaps in their process.
- The definition is shown beside their document, with what it made explicit.
- A dry run shows where the process had to guess, and why.
- A second dry run of the same definition diverges, and the decision diff shows where and why, while the steps, limits and checks stay identical.
- Nothing produced in the demo can be mistaken for real work.
