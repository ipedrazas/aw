# aw: agentic workflows

Take a process a team already has, written for people, and turn it into an explicit
workflow by asking about the gaps. Then run that workflow against a case they know
and show where it had to guess.

The bet: agentic workflows fail because processes written for humans are silently
incomplete, and because nobody can see what drives an agent's decisions. So the job
of this prototype is to make every gap and every decision visible. Throughput,
generality and polish are not goals.

## What is here

| Path | What |
| --- | --- |
| `src/wf/schema` | The definition models, loader and version pins |
| `src/wf/expr` | The restricted expression language (`${steps.review.output.verdict == "go_deeper"}`) |
| `src/wf/validate` | Structural and semantic passes; every failure is a typed finding with a plain question |
| `src/wf/interpret` | The interpreter: walks a definition, executes through activities, records decisions |
| `src/wf/activities` | Model calls (Anthropic or OpenRouter), fixture-backed search and link checks, PDF rendering, guessing |
| `src/wf/audit` | Document in, draft definition and questions out; answers written back |
| `src/wf/dryrun` | Runs against a past case, guess points, divergence, two-run decision diff |
| `src/wf/api` and `src/wf/web` | FastAPI JSON API and the server-rendered pages |
| `src/wf/store/sessions.py` | Agentic sessions: every exchange with a model, kept |
| `src/wf/logs.py` | One place that decides where log lines go and how loud they are |
| `src/wf/startup.py` | What the log says as the app loads: the provider, and the model each task will be asked of |
| `workspace/` | A sample workspace: definitions, instruction files, output schemas, recorded fixtures, a degraded process document and a past case |
| `plans/` | The handover and design documents this was built from |
| `DECISIONS.md` | Anything decided here that the brief did not cover |

## Run it

```bash
uv sync
uv run wf validate deep-research                     # the sample validates cleanly
uv run wf run deep-research --case durable-execution # a dry run (needs ANTHROPIC_API_KEY)
uv run wf audit workspace/process-docs/deep-research-process.md
uv run wf sessions                                   # what the models were asked
uv run wf serve --reload                             # http://127.0.0.1:8000
```

With [task](https://taskfile.dev) there is a name for each of those, and a few more:

```bash
task                     # the list
task check               # what CI runs: lint, format, tests
task demo                # a dry run offline, then the session it recorded
task serve:debug         # serve with every prompt and answer printed
task sessions:show -- <id>
```

`Taskfile.yml` is the fuller set; the `Makefile` keeps the short one for anyone
without task installed.

Without an API key, set `WF_FAKE_MODEL=1` to use an offline model that fills the
declared shapes and nothing more. It is enough to walk the pages, not to judge anything.

Models can come from Anthropic directly or through [OpenRouter](https://openrouter.ai),
which fronts many vendors behind one key. Set `OPENROUTER_API_KEY` and the models are
named the way the gateway names them, `vendor/model`:

```bash
export OPENROUTER_API_KEY=sk-or-...
export WF_CAREFUL_MODEL=openai/gpt-5      # or anything else it carries
uv run wf run deep-research --case durable-execution
```

With both keys in the environment, `WF_MODEL_PROVIDER=openrouter` settles it. Nothing
above `src/wf/settings.py` knows which is in use: a step asks for careful judgement and
gets whatever is configured.

With Docker:

```bash
ANTHROPIC_API_KEY=... docker compose up --build      # app on :8000, Postgres beside it
```

The container writes drafts back into the mounted `./workspace`, so it runs as the
owner of those files: uid/gid 1000 by default. If the mount appears as a different
owner inside the container, build with that one instead. The `var` volume keeps the
ownership it was created with, so remove it after changing the uid (it holds rendered
artifacts, not the database):

```bash
docker compose down
docker volume rm aw_wfvar
UID=$(id -u) GID=$(id -g) docker compose up --build
```

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | | Read by the Anthropic SDK |
| `OPENROUTER_API_KEY` | | The gateway's bearer token; on its own it also picks the provider |
| `WF_MODEL_PROVIDER` | `anthropic` | `anthropic` or `openrouter`; needed only when both keys are set |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | For a proxy in front of the gateway |
| `WF_OPENROUTER_STRICT` | | `1` asks the gateway to enforce the output schema, not suggest it |
| `WF_WORKSPACE` | `workspace` | The workspace directory |
| `WF_DATABASE_URL` | `sqlite:///var/wf.db` | SQLAlchemy URL; Compose sets Postgres |
| `WF_ARTIFACTS_DIR` | `var/artifacts` | Where rendered PDFs go |
| `WF_FAKE_MODEL` | | Set to use the offline model |
| `WF_QUICK_MODEL` | `claude-sonnet-5` | The model behind "quick judgement" in a step |
| `WF_CAREFUL_MODEL` | `claude-opus-5` | The model behind "careful judgement" in a step |
| `WF_EXTRACTION_MODEL` | the careful model | Turns a document into a draft |
| `WF_CHAT_MODEL` | the quick model | Answers in the chat that edits the draft |
| `WF_GUESS_MODEL` | the quick model | Fills a gap the document left |
| `WF_MAX_OUTPUT_TOKENS` | `32000` | How long one answer may be before it is cut off; answers are streamed, so raise it for a model that allows more |
| `WF_MODEL_PRICING` | built in | JSON of `{model: [input, output]}` in dollars per million tokens |
| `WF_LOG_LEVEL` | `info` | `debug` prints every prompt and every answer |
| `WF_LOG_FORMAT` | `text` | `json` for one object per line, with the fields |
| `WF_LOG_FILE` | | A path for the main log; unset means stderr |
| `WF_LOG_HEALTHCHECK` | | `on` puts the healthcheck's access lines back in the main log |
| `WF_HEALTH_LOG_FILE` | | A path for them instead; Compose sets `/app/var/health.log` |
| `WF_SESSION_LOG` | `full` | What is kept of each session: `full`, `meta`, `off` |
| `WF_SESSION_MAX_CHARS` | `40000` | How much of one prompt or answer is kept; `0` keeps all |
| `SESSION_ROOT` | `/app/var/sessions` | Where each session is also mirrored, one file per session |

Model names in step definitions are configuration in the YAML, not literals in code.
The code names a model in one place, `src/wf/settings.py`, and everything else asks
for a level of judgement instead. The two model defaults follow the provider: under
OpenRouter they are the same two models written `anthropic/...`, so switching provider
without naming models still runs. A model configured without a row in
`WF_MODEL_PRICING` is costed as the careful one — except through OpenRouter, which
reports what each call actually cost, having routed it.

## Logs and sessions

Two different questions, answered separately: what is printed while the work happens,
and what is kept once it has.

**Printed.** As the app loads it says what it intends to do, before it does any of
it: the provider, whether its key is set, and the model behind each of the five tasks.

```
INFO  wf.startup  provider openrouter (named by OPENROUTER_API_KEY), OPENROUTER_API_KEY is set, gateway https://openrouter.ai/api/v1, schemas suggested
INFO  wf.startup  models by task: quick=anthropic/claude-sonnet-5, careful=anthropic/claude-opus-5, extraction=…, chat=…, guess=…
```

At `debug` each of those gets a line of its own saying which variable named it and
what it costs per million tokens, followed by a line per agent step of every
definition in the workspace — which model will run it, and whether the step named one
or the run will have to guess.

Then, while the work happens: `WF_LOG_LEVEL=info`, the default, gives one line per
model call — which step asked, which model answered, how long it took, what it cost.
`debug` adds the instructions, the input and the answer in full, which is how you
watch a session as it runs. `WF_LOG_FORMAT=json` makes each line an object with those
values as fields, for when something else is reading the log.

The container's healthcheck asks for `/healthz` every thirty seconds. Those access
lines are taken out of the main log; Compose points `WF_HEALTH_LOG_FILE` at
`/app/var/health.log`, so they are kept, just not in the way:

```bash
task logs                # the app, without the heartbeat
task logs:health         # the heartbeat, on its own
WF_LOG_HEALTHCHECK=on … # or put it back in line
```

Nothing else is filtered. A log that hides more than its own heartbeat cannot be
trusted.

**Kept.** Every exchange with a model is a row under an *agentic session*: the run,
the audit of a document, or the turn of the chat that caused it. A session records the
instructions as the model got them, the input, the answer, the tools it called and
what each one returned, the tokens and the cost. A run record says what the workflow
decided; the session says what the model was actually asked, which is what you need
when the answer is wrong and the decision looks reasonable.

```bash
wf sessions                      # newest first, with calls and cost
wf sessions <id>                 # the calls in it, with tools and decisions
wf sessions <id> --prompts       # and the instructions, input and answer in full
wf sessions --run <run_id>       # the sessions of one run
curl localhost:8000/api/sessions
curl localhost:8000/api/runs/<run_id>/sessions
```

Sessions land in the same database as the runs (`agent_session` and `model_call`), so
Postgres in Compose and SQLite in a checkout. `WF_SESSION_LOG=meta` keeps the
counts and the timings without the prompt bodies, for when they are too large or too
sensitive to store; `off` keeps nothing, and the log lines still happen.

Each session is also mirrored to its own file, `<SESSION_ROOT>/<session id>.log`, kept
up to date as calls land in it. `SESSION_ROOT` defaults to `/app/var/sessions` — the
container's `var/` directory, created if it does not exist — and moves with the
variable like `WF_ARTIFACTS_DIR` does. That file is what survives a restart for a
database that does not, and what `SessionLog.list_from_disk` / `.get_from_disk` read,
independently of Postgres or SQLite.

## What stays true

- The definition is a static description of a program. No inline code; expressions
  read state and nothing else; no steps are created at runtime.
- The interpreter is deterministic given the same definition, inputs and recorded
  step outputs. A test guards this.
- Every side effect goes through an activity with declared retries and timeouts.
- Fetched content and the customer's document enter model context as data. Text
  that reads as instructions is recorded as an ignored decision, not dropped.
- Simulated output is marked at the source: in the filename, on every page, in the
  metadata.
- Nothing leaves the system in a dry run. Anything that would is recorded instead.
- Every exchange with a model is recorded, including the ones that failed. Recording
  is not a precondition for working: if the store cannot be written, the run carries
  on and says so in the log.

## Tests

```bash
uv run pytest
WF_TEST_DATABASE_URL=postgresql+psycopg://wf:wf@localhost:5432/wf uv run pytest
```

CI runs lint, the suite on SQLite and on Postgres, builds the image and smoke-tests
`wf validate` inside it, and publishes to GHCR on pushes to `main`.
