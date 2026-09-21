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
| `src/wf/activities` | Model calls, fixture-backed search and link checks, PDF rendering, guessing |
| `src/wf/audit` | Document in, draft definition and questions out; answers written back |
| `src/wf/dryrun` | Runs against a past case, guess points, divergence, two-run decision diff |
| `src/wf/api` and `src/wf/web` | FastAPI JSON API and the server-rendered pages |
| `workspace/` | A sample workspace: definitions, instruction files, output schemas, recorded fixtures, a degraded process document and a past case |
| `plans/` | The handover and design documents this was built from |
| `DECISIONS.md` | Anything decided here that the brief did not cover |

## Run it

```bash
uv sync
uv run wf validate deep-research                     # the sample validates cleanly
uv run wf run deep-research --case durable-execution # a dry run (needs ANTHROPIC_API_KEY)
uv run wf audit workspace/process-docs/deep-research-process.md
uv run wf serve --reload                             # http://127.0.0.1:8000
```

Without an API key, set `WF_FAKE_MODEL=1` to use an offline model that fills the
declared shapes and nothing more. It is enough to walk the pages, not to judge anything.

With Docker:

```bash
ANTHROPIC_API_KEY=... docker compose up --build      # app on :8000, Postgres beside it
```

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | | Read by the Anthropic SDK |
| `WF_WORKSPACE` | `workspace` | The workspace directory |
| `WF_DATABASE_URL` | `sqlite:///var/wf.db` | SQLAlchemy URL; Compose sets Postgres |
| `WF_ARTIFACTS_DIR` | `var/artifacts` | Where rendered PDFs go |
| `WF_FAKE_MODEL` | | Set to use the offline model |
| `WF_EXTRACTION_MODEL` | `claude-opus-5` | Model that turns a document into a draft |
| `WF_CHAT_MODEL` | `claude-sonnet-5` | Model behind the chat that edits the draft |
| `WF_MODEL_PRICING` | built in | JSON of `{model: [input, output]}` in dollars per million tokens |

Model names in step definitions are configuration in the YAML, not literals in code.

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

## Tests

```bash
uv run pytest
WF_TEST_DATABASE_URL=postgresql+psycopg://wf:wf@localhost:5432/wf uv run pytest
```

CI runs lint, the suite on SQLite and on Postgres, builds the image and smoke-tests
`wf validate` inside it, and publishes to GHCR on pushes to `main`.
