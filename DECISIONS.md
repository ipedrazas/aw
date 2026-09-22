# Decisions

Anything decided here that the handover brief does not cover. One short entry each:
the decision, the alternative, why. Open questions from the brief are proposed, not
decided, and are marked as such.

## Recorded

**Required-ness is checked by the validator, not by Pydantic.** The definition models
accept a step with holes (only `id` and `kind` are mandatory to parse) and reject
unknown keys. Alternative: strict Pydantic models. Why: an audit draft is incomplete
by design, and the product needs the *list* of holes as typed findings with
questions, not the first parse error.

**"Continue" is a declared outcome.** A branching field's enum value that no `when`
tests must be listed in the producing step's `output.continue_on`, or it is a `gap`
("What happens when the verdict is accept?"). Alternative: treat untested values as
implicit fall-through. Why: silence about an outcome is the most common real gap,
so the definition has to say "nothing more runs" out loud.

**Instruction versions live in the file's front matter, and git records the commit.**
`skills/x.md@3` requires the file to declare `version: 3`; a mismatch is a `conflict`
("the instructions changed since"). The run records the file's last commit and its
sha256. Alternative: `@N` means the Nth commit touching the file. Why: works in a
shallow clone and in a Docker image with no `.git`, and the version bump is an
explicit act by whoever edits the instructions.

**Checks and tools name a registered routine, not a file.** `run: checks.http_resolves`
resolves in `wf.interpret.registry`. Alternative: `run: checks/http_resolves.py` as in
the example YAML. Why: executing Python from the workspace would put code next to
definition content, which the brief forbids.

**Tools are recorded fixtures in every mode for this phase.** Search, fetch and link
checks read from `workspace/fixtures/`. Alternative: real calls in `live` mode. Why:
the demo brief says "stubbed tools, fixtures for data, nothing sent", and a live
provider would add a key and a network dependency the demo does not need. `NoSearch`
says so plainly if a live mode is ever wired without a provider.

**SQLite when `WF_DATABASE_URL` is unset; Postgres in Docker Compose and CI.** Same
SQLAlchemy models, JSON columns on both. Alternative: Postgres only. Why: a fresh
checkout runs and tests with nothing else installed; CI runs the suite against a
Postgres service as well, so the Postgres path is exercised on every push.

**Dry runs show follow-ups, they do not start them.** A `subworkflow` step in `dry`
mode records "would start ... one level deeper" with the budget left, and returns no
outputs. Alternative: run children in dry mode too. Why: the interesting output of a
dry run is the guess points and the divergence; starting three child runs multiplies
model cost without adding either. `RunConfig(subworkflows="run")` starts them.

**Agent step outputs are an envelope `{output, decisions}` enforced by the response
schema.** `decision_log: required` becomes `minItems: 1`. Alternative: parse decisions
out of prose. Why: the brief says enforce via schema.

**Approval gates and human waits stop a live run.** A `tool` step with
`requires_approval` or a `wait` step in `live` mode ends the run with status
`waiting` and a decision saying why. In `dry` mode both are recorded and skipped.
Alternative: proceed with a warning. Why: Phase 1 has no gates; pretending otherwise
would let something leave the system unapproved.

**Model guesses by default, heuristic guesses in tests.** `ModelGuesser` chooses among
a finding's options and gives a reason; it falls back to `HeuristicGuesser` when there
are no options or the call fails. Why: a reasoned guess is what the demo shows, and a
deterministic guesser is what the tests need.

**Fan-out cap and enum size are separate limits.** The reviewer's output schema caps
`followup_topics` at 3 and `limits.max_fanout` caps what starts. If the model returns
more than the schema allows, the step fails ("did not match its declared shape")
rather than being trimmed. Why: the schema is the contract; trimming would hide a
model that ignores it.

**Fetched pages with instruction-like text are kept and flagged.** The runtime wraps
every tool result in a `<data>` region, scans it, and records an `ignored` decision
naming the source. The text is not removed. Why: the brief says record, not drop, and
the model still needs the page to judge the source.

**Sessions are recorded at the model boundary, one wrap around the activity.**
`SessionRecorder` wraps whatever `ModelActivity` the process is using, so the steps,
the extraction, the chat and the guesser all record through the same code, and the
interpreter learns nothing about storage. Alternative: record in the interpreter,
where the run and the step are already in hand. Why: the interpreter is not the only
thing that asks a model, and a call made outside a run — a guess, a chat turn — is
exactly the one you go looking for later.

**What a session belongs to travels in a context variable, not in an argument.**
`session_span` marks the work and `step_span` marks the step; the recorder reads
them. Alternative: thread a session id through `complete()`. Why: `ModelActivity` is
the seam the fake, scripted, recording and replaying models all implement, and
widening it for bookkeeping would make every one of them carry it.

**A session row is written by the first call in it, and its totals are kept up to
date as calls land.** Alternative: open the row when the work starts and write the
totals at the end. Why: work that never asks a model leaves no empty session, and a
session whose process was killed is honest about where it got to rather than sitting
at zero.

**Prompts and answers are stored, and the store is not load-bearing.**
`WF_SESSION_LOG=full` is the default, `meta` keeps the counts and timings without the
bodies, `off` keeps nothing. A failure to write is logged and the run carries on.
Alternative: metadata by default, bodies behind a flag. Why: the prompt is the thing
you need when the decision reads well and the answer is wrong; and a debugging
feature that can fail a run is worse than no feature.

**The healthcheck is filtered where it is logged, not where it is served.** A filter
on `uvicorn.access` drops `/healthz` from the main log and, when
`WF_HEALTH_LOG_FILE` is set, writes it to a log of its own. Alternative: stretch the
Docker healthcheck interval, or turn the access log off. Why: the probe every thirty
seconds is doing its job, and turning the access log off to quieten it would hide
real traffic. Nothing else is filtered.

**Logging is configured once, and uvicorn is told not to configure it.**
`wf serve` passes `log_config=None`, and `wf.logs.setup_logging` owns the handlers,
replacing only the ones it installed itself. Alternative: a uvicorn log config
dictionary. Why: the CLI, the tests and the API all want the same levels and the same
format, and `--reload` starts a second process that has to arrive at the same place.

**`Taskfile.yml` beside the `Makefile`, not instead of it.** The Taskfile has the
full set — sessions, logs, debug serving, a demo — and the Makefile keeps the nine
short targets. Alternative: make the Makefile call `task`. Why: a checkout should be
runnable without installing another tool, and a shim that fails when `task` is
missing is worse than a little duplication. Both files say to change the other.

## Proposed, not decided (open questions from the brief)

**How much may the extractor infer before a field becomes an `assumption`?** Proposal:
a field is *stated* only if the model attaches a passage id whose text supports it;
anything with no passage is an `assumption` finding, shown and reversible. Titles and
`shows_user` are exempt: they are presentation, not process.

**Findings ordered by what they unblock or by confidence?** Proposal: by what they
unblock (number of later steps that read from the step), then conflicts before gaps.
The UI shows this order. Confidence is not modelled yet.

**Where do fixtures come from for a process whose tools we cannot call?** Proposal:
the customer records a handful of real responses once, or we write them from their
past case by hand and label them as constructed. Either way the fixture file is
committed beside the definition and shown as such.

**Trust promotion per workflow or per workflow version?** Not built in this phase. The
`step_run` row records the instruction commit and model, so either can be computed
later. Proposal: per step, reset by any change to what drives the step, which is what
the example YAML's `reset_on` says.

**Two people editing one instruction file.** Not built. The workspace is a git
repository, so the proposal is last-writer-on-a-branch with the diff shown before
"keep", which is the same screen as a change tested against baselines.

## Working notes

- Sessions have no page of their own yet. The run page shows steps, decisions and
  tool calls; the prompts behind them are in `wf sessions` and `/api/sessions`. A
  "what it was asked" panel on the run page is the obvious next thing.
- `may_repeat` is accepted by the brief as a control-flow location but no milestone
  needs it, so it is not in the schema yet. Adding it later is a schema change, not an
  architecture change.
- The sample workspace lives inside this repository rather than as its own git
  repository, so that a checkout is self-contained. `wf.store.repo` uses the enclosing
  repository for commits and history.
