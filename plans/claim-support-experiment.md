# Does the page support the claim? Jev against Haiku and Sonnet, inside the app

Written 2026-09-24. A plan for an experiment we will publish (blog post and a LinkedIn
article), run as a feature of the app rather than as a benchmark beside it.

## The question

The link check answers "does the URL open". It says, next to its result, what it does
not check: *whether the page supports the claim*. That is what users want to know. A
page can be on the topic of the report and still not say what the sentence citing it
says.

So the question is per citation, not per report: **given this claim and the page it
cites, does the page support it?** The report already carries `sources[{line, claim,
url}]`, so the pairs exist.

It is a closed decision with a confidence, asked once per citation, many times per
run. That is the shape Jev (TypeSafe's first "System One" model) is built for: typed
answers with calibrated probabilities, no text, 70–500 ms. It is also a shape any
Claude model can answer with a constrained schema. The experiment decides which model
belongs in the step, and under which conditions the answer changes.

## Hypotheses (fixed before any run)

Written down now so the results cannot move them.

1. **Accuracy.** On human-labelled real citations, Jev's macro-F1 is within 5 points of
   Sonnet 5 (no reasoning) and at or above Haiku 4.5.
2. **The hard case.** All models do well on off-topic citations. The gap opens on
   *on-topic but unsupportive* citations and on *altered claims* (a flipped number,
   date or negation). We expect Jev to lose most there, and Sonnet with reasoning to
   lose least.
3. **Calibration.** Jev's probabilities are better calibrated (lower ECE) than any
   confidence we can get out of Claude at the same cost. Getting comparable
   calibration from Claude (sampling five times) costs at least 5× the calls.
4. **Latency and cost.** Per report, the `check_support` step with Jev is at least 10×
   faster at p50 and at least 10× cheaper than with Haiku.
5. **Long pages.** On pages over 20k tokens, all models do better on the best passage
   than on the whole page, and the gap is widest for Jev.

A hypothesis that fails is reported as failed, as prominently as one that holds.

## The step

A new step, after `check_links`, in `deep-research`:

```yaml
- id: check_support
  kind: agent
  title: Check the sources say what the report says
  model: ${model}                 # the only thing that varies between arms
  skill: skills/claim-support.md@1
  for_each: ${steps.check_links.output.sources[?link_opens]}
  input:
    claim: ${item.claim}
    page: ${item.page_text}       # frozen text, see "Page text"
  output:
    schema: schemas/claim-support.json
  writes: sources[].support       # verdict + probability
  does_not_check:
    - How reliable or recent the source is
  on_fail: annotate               # a confident "contradicts" pauses; see thresholds
```

`schemas/claim-support.json`:

```json
{
  "verdict": ["supports", "partly", "not_supported", "contradicts", "unreadable"],
  "probability": "number 0..1"
}
```

`check_links` stays as it is: deterministic, no model. This is a separate step because
it is a model step and falls under the trust and eval rules; mixing it into the link
check would break the promise the link check makes.

Jev returns no text, so the step has no decision log from Jev arms. The step's
decisions are written by the interpreter from the verdicts ("3 of 14 sources do not
say what the report says"), the same for every arm.

## The arms

| Arm | Model | Confidence from |
| --- | --- | --- |
| jev | Jev | its own probability |
| haiku | Haiku 4.5, no reasoning | stated in the answer |
| haiku-x5 | Haiku 4.5, 5 samples | agreement across samples |
| sonnet | Sonnet 5, no reasoning | stated in the answer |
| sonnet-think | Sonnet 5, reasoning on (adaptive, high effort) | stated in the answer |

Claude's API gives no token probabilities, so its confidence is either stated in the
answer or measured by sampling. We report both and what each costs. Reasoning-on is
included because the vendor's own demo compared against reasoning-off models; we
should not repeat that.

**Access.** The Claude arms go through the existing model activity. Jev goes through
OpenRouter, but not through chat/completions: the gateway refuses it there ("a
decisions model", found 2026-09-24) and serves it on `/api/alpha/decisions`. A request
carries a `state` (claim and page) and typed questions; the answer carries the choice
and a probability per option. So Jev needs its own small activity beside the model
activity, not a second implementation of it, and the calibration arm gets its
probabilities directly. The step asks Jev two questions in one call: `verdict` (a
choice among the five) and `supports` (a yes/no with the probability of yes, the
number the thresholds are tuned on).

Sonnet 5's reasoning is adaptive: switched on, the model decides whether to think,
and cannot be made to. So "sonnet-think" is reported with its reasoning tokens per
citation, and results are split by whether it actually thought.

## Smoke test, 2026-09-24

`01` and `02`, four hand-written claims against one short passage, every arm once.
Not evidence for any hypothesis; it checks the plumbing and shows what to watch.

| Arm | Right | Round trip | Cost a call |
| --- | --- | --- | --- |
| jev | 4 / 4 | 284–337 ms | $0.000024 |
| haiku | 4 / 4 | 1.6–2.4 s | $0.00105 |
| sonnet | 4 / 4 | 2.5–3.4 s | $0.00247 |
| sonnet-think | 4 / 4 | 2.8–3.4 s | $0.00248 |

- Cost: Jev is about 44× cheaper than Haiku and 100× cheaper than Sonnet a call.
  Latency: about 5× faster than Haiku, 9× faster than Sonnet, short of the 10× in
  hypothesis 4. The hypothesis stands as written; the test split decides it.
- sonnet-think thought about none of the four at effort medium. At effort high it
  thought about one: the partly claim, the only ambiguous one (62 reasoning tokens),
  and its probability for partly went from 0.55 to 0.75. Adaptive reasoning spent
  where the doubt was. Its round trips were slower on every claim (3.1–7.9 s),
  including the three it did not think about; one run, so that is noise until the
  test split says otherwise.
- Haiku's yes/no probability copied its "supports" verdict probability in every case
  (0.95/0.95, 0.4/0.4, 0.0/0.0, 0.01/0.01), including 0.4 for a claim half of which
  is not on the page. Sonnet's did not. Jev answers the two questions in parallel,
  unable to see each other. See "Open".
- Jev's probabilities sat at or near 0 and 1; Claude's stated ones are round numbers
  (0.95, 0.05). Neither says anything about calibration yet.
- The partly case split the models: Jev gave partly 1.0, Sonnet partly 0.55 and
  not_supported 0.28. "Half the claim is missing" sits on the boundary between the two
  verdicts; worth watching in the human labels too.

## Running it: dry runs, not a harness

Each arm is a variant of the workflow definition that differs only in `model` on
`check_support`. Each variant is dry-run against the same cases with recorded page
text, and sessions are recorded so every run replays. This is the app's own "a change,
tested before it goes live" feature; the article can say we chose the model the way
our users test a change.

A small script walks the dry-run results and writes one table: case, source, label,
condition tags, and each arm's verdict, probability, latency and cost.

## The dataset

### Real citations

- Run `deep-research` live on 15–20 briefs from different fields: technical,
  regulatory, medical, finance, current events. Target 300–500 `(claim, url)` pairs.
  Each run becomes a case in `workspace/cases/`.
- The existing `durable-execution` case stays in; its dead analyst link is a good
  example of why "opened then" is not "opens now".

### Page text, frozen

Every page is fetched once, its text extracted, and saved beside the case in the shape
of `fixtures/contents.json`, with the fetch date. Every arm reads the same text.
Pages that come back as a paywall, a cookie wall or a JavaScript shell are kept: the
right answer for them is `unreadable`, and that is a result worth reporting.

### Labels

- Human labels on a stratified sample of about 200 real pairs, with the five verdicts.
- A second labeller on 60 of them: the user who first asked whether the links in a
  report make sense, not only whether they open. They label blind to the first labels
  and to every model's verdict. Report the agreement between the two (Cohen's kappa);
  it is the ceiling any model can be measured against. No model is a labeller: every
  label on the test split is a person's (decided 2026-09-24).
- Labelled in the app, on a small page that shows the claim beside the page text and
  takes one click per verdict. Its screenshot goes in the article.

### Negatives built from the real pairs, labelled by construction

| Kind | How | Label | Expected |
| --- | --- | --- | --- |
| off-topic | the URL from a claim in a different report | not_supported | easy for all |
| on-topic | the URL from another claim in the same report | not_supported | the main result |
| altered | the claim with a number, date or negation flipped | contradicts | where a fast model slips |

Constructed pairs are reported separately from real ones, never pooled into one score.

### Splits

A development split (about 20%) to tune the prompt for each arm, the same effort for
each. The test split is not looked at until the final run.

## Conditions to slice by

| Condition | Levels |
| --- | --- |
| Difficulty | real / off-topic / on-topic / altered |
| Page length | under 2k tokens / 2k–20k / over 20k |
| Content | HTML article / PDF / table or data page |
| Input | whole page / best passage (chunk, score each against the claim, keep the best) |

The best-passage input is the same for every arm, so no model is helped by retrieval
the others do not get.

## Measures

- **Catching bad citations.** Recall on `not_supported` + `contradicts`, at a false
  alarm rate users would accept. Shown the way the app would feel it: "at this
  threshold, a typical report pauses N times, and M of those are real."
- **Agreement with humans.** Macro-F1 and a confusion matrix per arm, against the
  inter-labeller ceiling.
- **Calibration.** Expected calibration error and a reliability plot per arm.
- **Latency.** p50 and p95 per citation, and the whole step per report (what the user
  waits for).
- **Cost.** Per citation and per report, from what the provider says it charged.
- **Stability.** Every arm run twice; how often a verdict changes.

## Publishing

- The claims, URLs, labels, verdicts and the scripts are published (decided
  2026-09-24). The page text is
  not (copyright); the fetch dates are, so anyone can refetch.
- The post says where Jev wins and where it does not. "Jev for this, Sonnet for that"
  is the expected shape of the conclusion, and a stronger post than a sweep.
- The vendor's benchmarks are self-reported; we say ours are too, and publish enough
  to check them.

## Order of work

1. **Jev access.** One call through OpenRouter
   (`experiments/claim-support/01-jev-through-openrouter.sh`); decide between it and a
   direct adapter. **Done 2026-09-24:** OpenRouter's Decisions endpoint. Four
   hand-written claims (supports, partly, not supported, contradicts) against one
   ~560-token passage: all four verdicts right, 317–421 ms round trip, about $0.000024
   a call. A smoke test, not evidence: the cases are easy and the probabilities came
   back at or near 1, which is what calibration will have to earn on real pages.
   `02-claude-through-openrouter.sh` asks Haiku 4.5, Sonnet 5 and Sonnet 5 with
   reasoning the same four questions, built from the same `questions.json`, and lays
   every arm's answers side by side.
2. **The step.** `check_support`, its skill and schema, the interpreter-written
   decisions, one arm working end to end on the existing case. **Built 2026-09-24**
   on `feat/check-the-page-says-it`: `fetch_pages` then `check_support` after the link
   check; Jev behind a `DecisionsRouter`; the questions in
   `schemas/claim_support.json` (DECISIONS.md). Proven end to end in tests with a
   mocked Decisions endpoint, not yet against the live one. Still to do:
   - the summary decision written from the verdicts ("3 of 14 pages do not say what
     the report says"); for now the verdicts are the step's outputs only;
   - the reasoning switch for the sonnet-think arm: the app's OpenRouter activity
     sends no `reasoning` field;
   - pages longer than Jev's 32k tokens: sent whole, so the gateway will refuse them;
   - Exa caps the text it returns per page, so live pages may arrive cut.
3. **Cases.** Write the briefs, run them live, freeze page text. This is the step that
   costs real model spend (about 20 deep-research runs).
4. **Labelling page** in the app; label the sample; second labeller.
5. **Negatives.** Script the three constructed kinds from the labelled pairs.
6. **Dev split.** Tune each arm's prompt.
7. **Test run.** Every arm, twice, via dry runs; the results table.
8. **Write-up.** Blog post first, LinkedIn article cut from it.

## Open

- Claude answers both questions in one call and can let one answer lean on the other
  (Haiku did, in the smoke test); Jev cannot. Keep one call, the realistic and cheap
  way to use Claude, and report the coupling; or ask Claude each question in its own
  call, twice the cost, to match Jev's independence. Decide before the dev split.

- Jev takes 32k tokens of input (OpenRouter, `typesafe/jev-1.13`). Pages over that
  cannot go whole to Jev, so for them the whole-page arm is Claude only and hypothesis 5
  is tested on pages between 20k and 32k tokens.
