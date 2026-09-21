# Sample workspace

A workspace is the git-backed home of everything a workflow needs to run. This one holds the
deep research workflow used for the demo.

## Layout

- `definitions/` — workflow definitions as YAML (`*.workflow.yaml`). The interpreter walks
  these directly; nothing is compiled.
- `skills/` — instruction files for agent steps, as markdown. Each starts with a front matter
  block carrying a `version:` number. A definition pins a skill with `@N`, for example
  `skills/report-reviewer.md@3`. If the file's version does not match the pin, validation
  reports a conflict rather than silently running newer instructions.
- `schemas/` — JSON Schema for each step's output. The validator reads these to check that a
  later step only references fields an earlier step actually produces, and that every
  branching value is handled somewhere.
- `fixtures/` — recorded tool responses used in dry runs: `search.json` (queries and results),
  `contents.json` (page text keyed by URL) and `http.json` (link status keyed by URL). They
  are keyed by request, so two dry runs see exactly the same world and differ only in what
  the model decided.
- `process-docs/` — the written processes the auditor turns into draft definitions.
- `cases/` — past cases with their known outcome, so a dry run has something to diverge from.

## About the process document

`process-docs/deep-research-process.md` is a constructed example, written for the demo and
deliberately incomplete so the audit has real gaps to find. It is not a customer document.
When a customer's own process document arrives it belongs in a private workspace, never here.

## Fixtures and fictional content

The fixture pages describe a plausible landscape but their facts are recorded material, not
research. Anything a dry run produces from them is marked simulated in its filename, its body
and its metadata.
