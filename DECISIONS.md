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

**An edited instruction file keeps every version it replaces, beside it.** Saving from
the skill page writes the next version to the file and copies the text it had,
unchanged, to `.versions/<stem>/<n>.md` in the same directory; the step is pinned to
the new version and the change committed and carried to the drafts, as an answer is.
A pin to an older version reads the kept copy, so it is no longer a conflict; a file
changed by hand without keeping the old text still is. Alternative: rows in the
database, or reading old versions back out of git. Why: the files are the definition's
content and travel with it (a shallow clone, a Docker image with no `.git`, a rename
or delete of the workflow's own `skills/<name>/`), and a step elsewhere that reads the
same file keeps running what it was pinned to until someone changes it there.

**Checks and tools name a registered routine, not a file.** `run: checks.http_resolves`
resolves in `wf.interpret.registry`. Alternative: `run: checks/http_resolves.py` as in
the example YAML. Why: executing Python from the workspace would put code next to
definition content, which the brief forbids.

**Tools are recorded fixtures in every mode for this phase.** Search, fetch and link
checks read from `workspace/fixtures/`. Alternative: real calls in `live` mode. Why:
the demo brief says "stubbed tools, fixtures for data, nothing sent", and a live
provider would add a key and a network dependency the demo does not need. `NoSearch`
says so plainly if a live mode is ever wired without a provider.

**The link check asks the real servers; search and fetch stay recorded.**
`checks.http_resolves` requests every URL a report cites (HEAD, then GET if HEAD is
refused; redirects followed by hand, five hops, ten seconds), except the ones
`fixtures/http.json` recorded, which replay so a past case still comes out the way it
happened. The output's `vantage` says how many were live and how many recorded, and a
link that does not open is written into the step's decisions with its status or the
reason nothing answered. Addresses that resolve inside a private network are not
requested. `WF_LINK_CHECK=recorded` puts it back on fixtures alone; the tests do.
Alternative: keep it on fixtures like the other tools. Why: a report on a topic nobody
recorded cites URLs no fixture holds, so every link came back "does not open" — and
the one step that involves no model is the one that should visibly do real work. It
only reads, so it sends nothing anywhere.

**Search is Exa when there is a key, and a run can be real from the UI.** `ExaSearch`
answers `search` and `get_contents` in the shape the fixtures do, so no step changes;
`EXA_API_KEY` switches it on and `WF_SEARCH=recorded` switches it off again. This
supersedes "tools are recorded fixtures in every mode" for search, and applies to dry
runs as well: what the model decides depends on what it finds, so a dry run on
recorded pages would not show how the real one behaves. A past case run with a key set
therefore searches live and may diverge from what was recorded; that divergence is
real. "Run it for real" starts a `live` run: follow-up research starts, nothing is
marked simulated, open questions still refuse it, and nothing is sent.

**The run page shows what each step was sent and every tool call it made.** Rebuilt
from the session: the system prompt as every model activity wraps it, the message as
the step's input in its data region, and each search with the results it returned.
Alternative: a separate sessions page. Why: "what did it actually ask, and what did it
find?" is asked while looking at the step, and the answer belongs next to it.

**The report's markdown is kept beside its PDF, and the PDF reads the markdown itself.**
`tools.render_pdf` writes `report.md` next to `report.pdf` (both `SIMULATED-` in a dry
run, the markdown with the banner as its first line), and the routine records each as
an artefact through `RunnerContext.keep`; the step's output keeps its shape, so no
saved schema changes. Tables, lists, quotes and code are read by a small reader in
`render.py` and tables drawn with fpdf2's `table()`. Alternative: markdown to HTML to
`write_html`. Why: fpdf2's HTML tables fail on any tag inside a cell (a link, a bold
word), and `write_html` fetches the address of any image, which a report written from
fetched pages must not be able to make it do.

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

**OpenRouter is a second model activity, not a second client for the first one.**
`wf.activities.openrouter.OpenRouterModel` speaks the gateway's chat-completions
shape; `AnthropicModel` is untouched. Alternative: point the Anthropic SDK at
OpenRouter's Anthropic-compatible endpoint, which is one line. Why: that endpoint is
the vendor's API re-served, so the newest parts of it — the structured-output config
this code relies on for the `{output, decisions}` envelope — are only as available as
each provider behind it, and the whole point of the gateway here is to reach models
that are not that vendor's. Two implementations of one small protocol is the honest
shape: the system prompt, the envelope, the data regions, the tool loop and its call
limits are shared code, and only the wire shape differs.

**Which provider is a setting, and `default_model()` is the one place that reads it.**
`WF_MODEL_PROVIDER` names it; failing that, an environment with only an OpenRouter key
means OpenRouter. Alternative: infer it from the shape of the model name, since a
gateway model is written `vendor/model`. Why: the same name can be valid in both
places, and a provider inferred from a string is a provider nobody can override. The
two model defaults follow the provider, so switching it without naming models still
runs the same two models.

**The gateway's reported cost wins over the local price table.** OpenRouter returns
what each call cost, having chosen who served it; the table is the fallback when it
does not. Alternative: price everything locally. Why: the table cannot know which
provider or which tier answered, and a run's estimate is worth more than a tidy
single source.

**Schema enforcement through the gateway is off by default (`WF_OPENROUTER_STRICT`).**
The schema is always sent; asking for it to be enforced is opt-in. Alternative: strict
always. Why: strictness is a property of whichever provider serves the call, several
of them reject schemas that use `minItems` or `maximum` — both of which this code's
envelope and step schemas use — and a request refused for that reason fails the run,
while a loose schema plus the answer check we already do merely risks a retry.

**The app says which models it will use before it uses any of them.** Two `info` lines
at load — the provider, whether its key is set, and the model behind each of the five
tasks — and at `debug` where each name came from, what it costs, and which model every
agent step of every definition will run on. Alternative: leave it to the session
records, which already say which model answered each call. Why: those answer "what did
this run do", and the question at hand is usually "what is this process configured to
do", which until now could only be answered by reading five environment variables and
the fallback rules between them. A provider nobody meant to use is the failure this
makes loud.

**Deleting removes the definition and keeps the record.** Deleting a workflow removes
`definitions/<name>.workflow.yaml` and the `skills/<name>/` and `schemas/<name>/`
directories a draft wrote for it, and commits the removal, so deleting takes exactly
what saving added; instruction files shared between definitions sit at the top of
`skills/` and stay. The runs, their decisions and their artefacts are untouched, and
the run page still reads, because a report is built from the ledger and the definition
it ran from is in `workflow_version`. Deleting a draft takes the draft, its findings
and its edits, and leaves the agentic sessions it recorded. Alternative: cascade, or
refuse to delete anything that has runs. Why: a run is what happened and a session is
what a model was asked; neither becomes untrue when the thing they came from is
deleted, and a demo that cannot remove a wrong draft is worse than one whose history
outlives it.

**Every model call is streamed, and how long an answer may be is configuration.**
Both providers stream and then wait for the whole answer; nothing shows an answer
arriving. `WF_MAX_OUTPUT_TOKENS` sets the ceiling for all of them, defaulting to
32000. Alternative: buffered calls with a ceiling low enough to land inside one read
timeout, which is what a fixed 16000 was. Why: the ceiling is a property of whichever
model the environment named, and they differ by more than a factor of ten; a buffered
call ties that number to a timeout rather than to the model, and reading a long
document into a draft is where the cap was actually reached — an answer that hits it
is thrown away whole, so the number has to be the model's, not the transport's.

**A question with fixed choices is answered by a choice or in the chat, not in a text
box.** "Type your answer" is gone; every question has "Chat about this", which sends
the question's id with the message. Alternative: keep the text box and accept more
phrasings. Why: prose that is not one of the choices is usually not an answer but a
correction ("nobody waits, the topic is how it starts"), and only the chat can act on
that, by removing or changing the step the question rests on.

**The chat can remove a step, and removing one is recorded against the step list.**
`steps.<id>` set to null takes the step out and points whatever read it at the
workflow's inputs; the change is stored as the whole of `spec.steps` before and after.
Alternative: one change per field touched. Why: undo replays one change, and only the
whole list puts the step back where it was with its readers rewired to it.

**A wait before anything has happened is the workflow's input.** The extractor is told
so, and a draft that still opens with a wait reading nothing drops it, makes what it
produced an input and says so in "What I changed". Alternative: leave the wait and let
the person answer its deadline question. Why: that question has no right answer —
"First, get my topic" in the sample document produced exactly this.

**A step's instructions are written by a model, and the outline is what it falls back
to.** After the draft is built, each agent step gets one call (`audit:skill:<id>`, in
the audit's session, one step at a time) with the whole document, the step's place in
the process, its output fields and the hand-written files at the top of `skills/` as
examples. What comes back is held to what the runtime relies on: every output field
named, decisions asked for, the `<data>` rule stated. A part it left out is added at the
end rather than the prose thrown away. A failed or empty answer keeps the outline and
says so in "What I changed". `WF_SKILLS=template` skips the calls. Alternative: a better
template. Why: the outline gave every step the same five headings and a quote, and a
user read it, correctly, as a filled-in form; the hand-written instructions it sits
next to explain purpose, standards and why, which only reading the document gives.
Files restored because they went missing are still the outline: there is no document
reading in that path.

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

**A workflow has a default model, and each agent step can be moved off it from the
workflow page.** `spec.defaults.model` is what a step that names no model runs on;
the validator counts it, so a step on the default is not a question. The page offers
the models the deployment configured (quick and careful, by the judgement they bring)
and any the workflow already names, and nothing else. A change is committed and carried
to the drafts, like an answer. Alternative: a free-text model name. Why: a name the
deployment cannot run would only fail at the first call, and the choice people
actually wanted to make was "use this one unless I say otherwise".

**The auditor asks about a step's model only when the document singles the step out.**
A draft sets the workflow's default to the quick model and gives a step a model of
its own only when the extraction reads it as needing careful judgement; that step is
asked to confirm, with the document's words that singled it out, and "no" puts it
back on the default. Every other step is not asked. Alternative: ask of every step
whose judgement the document does not state, as before. Why: a person faced with
27 questions, most of them "how much judgement does this step need?", wanted to
answer once; the default and the model dropdown on the workflow page cover the rest.

**The same question asked of several steps is shown once, and answered for all of them
unless unticked.** Two questions are the same when they are the same kind of finding,
about the same field of different steps, with the same choices. Each step still gets
its own finding and its own change, so any one can be undone or changed later.
Alternative: one finding for the group. Why: the validator, the undo and the draft
diff all work per field, and a group finding would have to be taken apart again for
each of them.

**Whether a page supports its claim is an agent step, and a decisions model can run
it.** `check_support` asks, for each page the report cites, whether it says what the
claim says (plans/claim-support-experiment.md). It is an `agent` step, not a `check`:
a check is deterministic, and this is judgement. Jev, a decisions model, writes no
text and takes no instructions, so it sits behind the model activity as a router
(`DecisionsRouter`): a step naming a `typesafe/` model goes to OpenRouter's Decisions
endpoint, every other step to the provider as before. Alternative: a `check` with a
model, or a new step kind. Why: the step's model is then the only thing that differs
between Jev and Claude, which is what the experiment compares, and recording, replay
and the per-step model choice apply to both unchanged.

**The questions a decisions model answers are read from the step's output schema.** A
string with an `enum` is a choice, a boolean is a yes/no, the `description` is the
question and `x-criteria` says what each answer means; a `probabilities` property is
filled with the model's probabilities rather than asked. A text model is shown the same
schema with `x-criteria` written into the descriptions. Alternative: the questions in
the skill file, or in a field of their own on the step. Why: one place for them, so
Jev and Claude are asked the same thing in the same words; a skill is prose Jev never
reads, and the step model rejects unknown keys.

**The cited pages are fetched by a step of their own.** `fetch_pages` (a tool, no side
effects) reads the text of each link that opened, from the recorded fixtures or Exa,
and lists the pages it could not read. Alternative: give `check_support` the fetch
tool. Why: a decisions model cannot call tools, and a page every model is shown
fetched once is a page they were all shown the same.

**A run that broke is picked up at the step that broke, on the definition as it is
now.** Once the cause is fixed (a model name, an instruction file), "Pick up from where
it broke" on the run page, `POST /api/runs/{id}/retry` or `wf retry <id>` carries the
same run on. The steps that finished keep their results and are not run again; the
step that broke runs again whole, every item of a fan-out; the attempt that broke stays
in the record, marked `retried`, and the run page shows each step's latest attempt. An
earlier step whose definition changed since the run began is named in the run's record,
because its result is from before the change. Alternative: start a new run, or reuse
the fan-out items that had finished. Why: a new run pays again for everything before
the break, and it is usually the expensive part; and the fix is usually to the step
that broke, so its items are asked again rather than mixed across two versions of it.

**A step that broke can be skipped, and the run carries on without it.** For what cannot
be fixed from here, like a follow-up run that failed, "Skip it and carry on" (`POST
/api/runs/{id}/skip`, `wf retry --skip`) marks the step skipped, keeps its error, and
walks on from the next step, as if its condition had not been met; a later step that
reads its result gets nothing. A follow-up that fails now says, in its parent, which of
its steps broke and why, and the parent's step links to it. Alternative: only retry.
Why: some failures are outside the run (a source that will not answer, a follow-up that
is not worth what it would cost again), and the rest of the run is still worth having.

**A model name that could never work is a question before the run.** The validator
checks the shape of every model name (the vendor's own, or `vendor/model` through a
gateway) and raises a conflict for anything else, such as `-typesafe/jev-1.13`.
Alternative: a list of known models. Why: the list changes weekly and differs by
provider, and the failure this catches, a stray character, is a shape.
