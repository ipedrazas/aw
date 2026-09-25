# How this works

This is what the chat knows about the system itself. It answers from here when someone
asks how something works, and says so plainly when something is not possible yet.
Keep it true to the code: when behaviour changes, change this.

## What you can ask the chat

- About your draft: what a step does, why it is there, what a question means, what
  would happen if you answered it one way or the other.
- To change the draft: rename, reword, remove or reorder a step, change what a step
  is given, answer a question in your own words, or close a question that does not apply.
- About your other workflows: which ones exist, what they do, and whether a step in
  this one could hand its work to one of them.
- About how the system works: anything on this page.

The chat cannot see past runs or their results yet, and it cannot start a run. Runs
are on the Runs page; a draft is tried with "Save and try it on a topic" on the draft page.

## The draft and its questions

You describe how you work, in your own words or by pasting a document. The draft on
the right is built from what you wrote: each step says where in your text it came from.
Whatever your text does not say becomes a question, rather than a guess. Some things
the system had to assume are asked as "We assumed ... Is that right?".

Questions can be answered by picking a choice, or in the chat. If a question does not
make sense, say so: it may rest on a wrong reading of your text, and the chat can fix
the step instead. Every change has a reason next to it and can be undone.

A draft can be tried in a dry run while questions are still open: where it meets one, it
guesses and says so. A real run needs every question answered.

## Kinds of step

- A step that uses judgement: planning, searching, writing, reviewing. A model does it,
  following the instructions written for that step.
- A check: a mechanical test with no judgement, such as whether the links in a report
  open. The system has a fixed set of these; a workflow cannot add its own code.
- A tool: something that acts, such as making the PDF. Anything that would send
  something out of the system needs someone to approve it first.
- A wait: the run stops until a person does something.
- Handing work to another workflow: the step starts a whole workflow, gives it what
  it needs, and uses what it produces. A workflow can start itself again, which is how
  "go deeper" research works, up to a set depth.

## Conditions, repeats and order

- A step can run only in some cases, for example only when the review says "go deeper".
  Every outcome of a decision has to be accounted for: either something happens for it,
  or the draft says out loud that nothing more is needed. An outcome nobody mentioned
  becomes a question.
- A step can run once for each item in a list, such as once per follow-up topic, with
  a limit on how many. The items run one after another, not at the same time.
- Steps run in the order they are listed. Going back to an earlier step when a
  condition is not met (a loop) is not possible yet; the usual way round it today is a
  step that fixes the work, followed by another check.
- Steps do not run in parallel yet.

## Models

Each step that uses judgement runs on one of two models:

- Standard: faster and cheaper. Enough for planning and research.
- Thorough: slower and costs more. Better at writing and review.

A workflow has a default, and a single step can be moved to the other one from the
workflow page. Both are careful; the difference is how much effort and cost goes into
each answer. The draft only asks about a step's model when your text singles it out.

## Runs

- A dry run tries the workflow without doing anything for real: where it had to guess,
  it says so and why. Follow-up research is shown, not started.
- A real run ("Run it for real") uses real judgement and real search, starts follow-up
  research, and sends nothing anywhere.
- Every step records what it decided and why, what it was sent, and every search it made.
- A run stops at a step that needs approval, at a wait, or when it reaches its spending
  limit. There is no pause button yet.
- A step can check with you before the run carries on: every time, until you have said
  OK a number of times in a row, or never. A real run stops after that step; you look
  at what it produced, then say OK or stop the run there. Any change to the step (its
  instructions, model, tools, or what it produces) starts the count of OKs again. A dry
  run only says where it would have stopped, and follow-up research does not stop,
  because you already said to go deeper.
- A run that broke can be picked up from the step that broke, once the cause is fixed,
  without paying again for the steps that finished. A step that cannot be fixed can be
  skipped, and the run carries on without it.
- Follow-up runs sit under the run that started them on the Runs page. A run's cost
  includes its follow-ups.

## Instructions

Each judgement step has written instructions: what the step is for, what good looks
like, and what to record. They are written from your text when the draft is made, and
can be edited from the workflow page; every earlier version is kept. There is no
shared catalogue of instructions yet, beyond the ones that come with the sample.

## Search

Search looks at recent web pages and news. Limiting a step to particular sites is not
possible yet.

## Connecting to your own systems

Not yet. Today a workflow can search the web, read pages, check links and make a PDF.
Reading from systems such as a database, documents or an issue tracker would be added
as new tools; anything that writes back to them would need someone to approve it.
