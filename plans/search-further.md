# Going deeper is searching further, in the same run

Written 2026-09-29, from a design session after a user's online-research draft turned
"go deeper" into an agent step whose limits ("25 searches, 2 levels deep") were only
prose, and whose "how should it be done?" question was answered with *when* to do it.

## What was decided

"Go deeper" means: topics found while searching feed more searches in the same run, up
to a number of levels and searches, all ending up in one result. It is not recursion.
People never see "the workflow calls itself".

1. **A setting on the searching step.** `search_further: {follow, levels, max_searches,
   max_topics}` on an agent step that searches. The runtime runs the loop; the model
   only names topics.
2. **One merged result.** Lists are joined across levels (sources de-duplicated by
   address); single values keep the first run's. Later steps read the same fields as
   before. Each topic's own result is kept as `steps.<id>.further[*]` —
   `{topic, why, level, from, output}` — so the run page shows which topic found what.
3. **What a follow-up is given.** Its usual input, unchanged, plus a `further` block
   the runtime fills in: `topic` (what to search now), `why` (why it was worth it),
   `from` (the topic it was found under, empty for the first round's), `level`, and
   `already_found` (the addresses found so far). The original question is in the usual
   input, so it is not repeated. A fixed section added to the step's instructions says how
   to use it. The runtime never guesses which input key is "the topic".
4. **When is not how.** The rule for when a topic is worth following is the
   description of the `follow` field in the step's result (`new_topics`, a list of
   `{topic, why}`), asked as its own question. How to search stays the instructions.
   How far is a choice, not free text.
5. **Limits only, no stop.** Levels, the total search cap and the per-level topic cap
   are enforced by the runtime. The step's usual trust policy covers its result. The run
   records each topic followed or skipped, and why.
6. **Dry runs search further** exactly as real runs do, within the same caps: it is the
   only way to see the "when" rule at work before a real run.
7. **Recursion stays** for processes that want a separate report per follow-up topic,
   and for handing a step to another workflow. The extractor stops using it for "go
   deeper". The validator checks `with` against the called workflow's inputs.
8. **Existing drafts are offered the change.** A step after a searching step that says
   it goes deeper is folded into `search_further`, limits read from its words, when the
   person says yes.

Settled without asking, open to change:

- A document that gives no numbers is asked "how far may it go?" as a choice (1/2/3
  levels, 15/25/50 searches, 3 topics per level). Numbers it gives are taken.
- `tools.search.max_calls` still caps each run of the step; `max_searches` caps them
  all. A draft sets a round's cap to the total shared between the first round and each
  level (25 searches over 2 levels: 9 a round, never below 5), so the first round
  cannot spend it all. The validator asks when the total is below one run's cap. No money budget is
  required: the caps bound the cost.
- The validator requires the `follow` field (a list of `{topic, why}`) and sources with
  addresses in the step's result, and adds them the way it adds `sources` today.
- Changing `search_further` is a change to the step's tools for trust: its count of
  OKs starts again.

## Pieces, each a PR

1. **Runtime and schema.** The field, the loop, the caps, merging, `further[*]`, the
   instruction section, validator checks, the run's trail. Hand-written definitions can
   use it; drafts do not produce it yet. The "when is a topic worth following?"
   question comes with it: the validator asks it when the result has no topics list.
2. **Drafting.** The extractor emits `search_further` for "go deeper"; the three
   questions (how, when, how far).
3. **The suggestion.** Fold a separate "go deeper" step into the step it follows.
4. **Recursion's inputs.** `with` checked against the called workflow's inputs.

## How we will know

- The online-research draft, folded, runs a dry run that follows at most 3 topics a
  level for 2 levels and never exceeds 25 searches.
- The write step cites sources found at level 1 and 2 without being changed.
- The chat eval (see the design discussion) gains two cases from that session: "use
  claim-support and Jev on step 7" and the go-deeper answer that said *when*.
