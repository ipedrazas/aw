# Capabilities say what they take and give; the model picks, code wires

Written 2026-09-29, from a design session after the first real dry run of the redrafted
online-researcher (run `8979b2e2`). Searching further worked; the wiring around it did
not:

- the workflow handed back the claim check's `page_checks`, not the report: a draft
  takes the last AI step as the result;
- "Add all sources at the very end" became `tools.fetch_pages`, the closest-sounding
  routine, because a check or tool step had to name one; nothing appended the sources;
- claim-support ran once, before any page was read, on the whole pipeline ("the input
  contained a full research pipeline rather than one isolated claim"): every step read
  everything before it;
- the link check opened every address in its input (115), not the report's citations,
  because the report gave back no citations for it to take. In piece 3, a step that sends something out is never offered as the result (its
result is only that it went), and a model's results come before a routine's among the
choices.

## What was decided

1. **Capabilities declare what they take, not only what they give.** Typed, with how
   often they run: once, or once per item. Routines next to their description in
   `RUNNERS`; skills in their front matter (`takes:` beside `result:`).
2. **The model picks, code wires.** The extractor names a step's capability (or says
   none fits). Plain code wires it from the contracts: which earlier result has the
   shape it takes, whether it runs per item, what its input is. When wiring cannot
   work, a question says why.
3. **When no routine fits, a model does it, said openly.** The step becomes an AI step
   following instructions written from the document, and the draft says so. No
   nearest-sounding routine.
4. **The document names the result; otherwise a question.** The extractor names the
   step whose result is what the process delivers. If the document does not say, the
   draft picks the last step that produces something and asks.
5. **A missing shape is added to the step meant to give it.** A link check or claim
   check needs the report's citations: the nearest earlier step that writes a report
   gains `citations[{line, claim, url}]`. With no report either, a question.
6. **Chains are added or moved by the draft.** Claim-support needs the pages read
   first: the draft adds "Read the cited pages" (or moves it) before it, marked "I
   suggested this", undoable.
7. **Existing steps are offered capabilities.** Triage proposes swaps when a draft is
   made; the chat does the same on request for existing drafts. Code keeps only the
   swaps whose wiring works; each is a question.

Settled without asking, open to change:

- A capability with a contract is used as it is (the step points at it, pinned); one
  without (deep-researcher, report-writer) stays a starting point for the step's own
  instructions. Editing a shared one from a step asks: for everyone, or a copy.
- Steps that follow their own instructions keep "everything before it" as the fallback
  input: a writer needs it. Capability steps are wired from their contracts.
- The validator checks every capability step's wiring against its contract, as #62
  does for what a nested run is given.

## Pieces, each a PR

Done: 1 is #64, 2 is #65, 3 is #66, 4 is the PR after it. In piece 2 the PDF routine's contract became
"the report" with no required field: it prints any report, using its longest text when
the title and Markdown are named otherwise, so demanding `body_md` refused reports it
prints. A routine that is wired keeps the rest of what it was given (the PDF also
prints follow-up reports); instructions run once per item are given exactly what they
take.

1. **Contracts and validation.** `takes` for the routines and claim-support; the
   validator checks a capability step's input (the key, the shape, once or per item);
   the catalogue shows the contracts to the extractor, triage and chat.
2. **Wiring.** The extractor may say none fits; code wires capability steps, adds
   missing shapes and chain steps.
3. **The result.** The extractor names what is delivered; otherwise the draft asks.
4. **Suggestions.** Triage and the chat propose swaps, checked by the wiring.

## How we will know

Redrafting online-researcher gives: write the report (with citations) → verify the
links (the report's citations) → read the cited pages → check each citation against
its page (once per citation) → add the sources at the end (a model step), and hands
back the final report.
