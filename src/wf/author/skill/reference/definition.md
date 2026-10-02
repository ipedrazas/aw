# The workflow definition

A workflow is one YAML document. Unknown keys are refused, so a typo is caught rather than ignored.

```yaml
apiVersion: workflows.tavon.io/v1alpha1
kind: Workflow
metadata:
  name: market-scan            # short slug, lower case, hyphens
  version: 1
  description: One sentence on what it does, in their words.
spec:
  inputs:                      # what a run starts from
    topic: { type: string, required: true, description: What to scan. }
  outputs:                     # what it hands back: kept as a file when a run finishes
    report: ${steps.write.output}
  defaults:
    trust: { policy: earned, promote_after: 3 }   # only what they said, or leave out
  steps: [ ... ]               # run in this order
```

Input types: `string`, `integer`, `number`, `boolean`, `list`, `object`.

Always name what the workflow hands back under `outputs`. When a run finishes, each output is kept as a file the person can open: a result with `body_md` as a markdown report, anything else as JSON. A PDF needs a step that runs `tools.render_pdf`.

Leave out `budget`, `trust`, `model`, `requires_approval` and `limits` unless the person told you what they should be. The system fills sensible defaults and asks them.

## Steps

Every step has `id` (lower case, underscores), `kind`, `title` (a few words in their language) and `description` (one plain sentence).

### agent: a step that uses judgement

```yaml
- id: research
  kind: agent
  title: Search for sources
  description: Searches, reads what it finds, and keeps what is worth citing.
  skill: skills/market-scan/research.md@1
  tools:
    search: { max_calls: 20 }
    get_contents: { max_calls: 30 }
  input:
    topic: ${inputs.topic}
  output:
    schema: schemas/market-scan/findings.json
```

`tools` only lists tools from `what_the_system_can_do`. `model` is left out unless they chose one.

A step can search further in the same run (what "go deeper" usually means), with limits they set:

```yaml
  search_further: { follow: new_topics, levels: 2, max_searches: 25, max_topics: 3 }
```

Its result then has a `new_topics` list of `{topic, why}`.

### check: a mechanical test with no judgement

Only when a routine does it.

```yaml
- id: check_links
  kind: check
  title: Check the links
  run: checks.http_resolves
  input:
    sources: ${steps.write.output.sources}
  checks: [The URL answers within 10 seconds]
  does_not_check: [Whether the page supports the claim]
  on_fail: annotate            # annotate | pause | fail
```

A routine gives back a fixed shape. The system writes it for the step, so leave its `output.schema` out; read what it gives back from `what_the_system_can_do`.

### tool: something that acts

```yaml
- id: assemble
  kind: tool
  title: Put it in a PDF
  run: tools.render_pdf
  input: { report: ${steps.write.output} }
  output: { artifact: report.pdf }
  side_effects: none
```

Anything that leaves the system (`tools.send_email`) lists what it does under `side_effects` and needs `requires_approval` set to who approves it. Ask them who.

### subworkflow: hand the work to another workflow

```yaml
- id: deep_dive
  kind: subworkflow
  title: Research each follow-up
  workflow: deep-research@4
  for_each: ${steps.review.output.followup_topics}
  with: { topic: ${item.topic} }
  limits: { max_depth: 1, max_fanout: 3, budget: inherit }
```

### wait: the run stops until a person does something

```yaml
- id: sign_off
  kind: wait
  title: Wait for the editor
  deadline: 2d
  on_timeout: remind           # continue | stop | escalate | remind
```

What the process starts from is an input, never a wait.

## Passing work along

Values are expressions inside `${...}`:

- `${inputs.topic}`: a run input.
- `${steps.write.output}`: everything a step gave back; `${steps.write.output.sources}` one field of it.
- `${item.claim}`: inside a step with `for_each`, the current item.
- `${steps.deep_dive.outputs[*].report}`: one field from every item of a `for_each` step.

Conditions use `==`, `!=`, `<`, `>`, `and`, `or`, `not`, strings in double quotes. Nothing else: no functions, no arithmetic beyond `+`.

## Running only sometimes, once per item, and going back

```yaml
  when: ${steps.review.output.verdict == "revise"}     # runs only then
  for_each: ${steps.find.output.items}                 # once per item, one after another
  max_fanout: 20                                       # at most this many items
  may_repeat:                                          # after this step, go back
    when: ${steps.review.output.verdict == "revise"}
    to: write
    limit: 2
```

Every value of a field that decides the way (an `enum` in its result shape) needs a step whose `when` picks it, or to be listed on the deciding step:

```yaml
  output:
    schema: schemas/market-scan/review.json
    continue_on:
      verdict: [accept]        # nothing special happens: the run carries on
```

## Result shapes

JSON Schema, one file per shape, under `schemas/<name>/`. Objects say `additionalProperties: false` and list every property under `required`; a field that may be empty is `["string", "null"]`. A field that decides the way has an `enum`.

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["verdict", "reason"],
  "properties": {
    "verdict": {"type": "string", "enum": ["accept", "revise"]},
    "reason": {"type": "string"}
  }
}
```
