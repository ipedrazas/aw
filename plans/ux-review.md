# UX review of the web app

Written 2026-09-29, from a read of every template in `src/wf/web/templates`, `app.js`
and `app.css`, after PRs #40 to #69. It builds on the demo feedback of 2026-09-25: the
chat was the hit, and people expected to see the workflow drawn.

The voice is plain and consistent, which is the app's strongest asset. The problems are
in the structure: too many ways to do the same thing on one page, a full reload after
nearly every action, copy written for the deep-research sample, and results placed at
the bottom of the page.

Suggestions are grouped by priority. Each has the file it touches and what "done" means.

---

## P1: quick wins (small, low risk)

### 1. Generic copy: stop assuming deep-research
The app now holds more than one workflow, but some copy still describes the sample.
- `_macros.html:9`: `status_pill("done")` says **"Done: PDF ready"** for every run,
  even when the workflow makes no PDF. It should say "Done", and add "PDF ready" only
  when the run has a PDF artefact.
- `workflow.html:25`: the run form's placeholder says "What should it research?".
- `app.js:69`: the real-run confirm says "Follow-up research starts if the reviewer
  asks for it". That only fits workflows with a follow-up step.
- `app.js:67`: the run form always sends `{inputs: {topic}}`. Build the form from the
  workflow's declared inputs (name, description, type) instead of one fixed `topic` box.
  This is a behaviour fix as well as a UX fix.

### 2. Put the result at the top of a finished run
In `run.html` the order is: guesses, expectations, open questions, every step, and then
**Artefacts** (line 186). For a real run the PDF is what people came for, and it sits
below every step card.
- When `status == done`, show the artefacts in the finished banner, or in a card right
  under it.
- Dry runs keep "guesses first", because guesses are the point of a dry run.

### 3. Let people act on guesses and open questions from the run page
A guess is the product's main idea ("found by running it rather than reading it"), but
on the run page it leads nowhere.
- Give each guess and each open question (`run.html:67-103`) an **"Answer this now"**
  link to the matching question on the workflow page (`#q-<id>`), or to the draft.
- After an answer, offer "Run the same case again" so people can see the gap closed.
  This completes the loop: guess, answer, re-run, compare.

### 4. Link the steps in the run's side rail
`run.html:12-19`: the rail steps are plain text. Turn each one into an anchor to its step
card. For a waiting run, the banner should have a **"Go to it"** link to the form it
mentions (the "Is this OK?" form is placed partway down the page).

### 5. Mark the current page in the nav, and say when something needs you
- `base.html:17-20`: no nav link is marked active, and the brand text "Workflows" is the
  same as the first nav link.
- Add a count badge to **Runs** when any run is `waiting`. Right now the only place a
  run waiting on a gate shows up is the "Needs you" tab on `/runs`. A run that stopped
  for your OK should be visible from every page.

### 6. Show the step title, not its id
`workflow.html:109` shows `Step: {{ f.step_id }}` under each question, and
`run.html:101` does the same. Use the step title, as the rest of the UI does.

### 7. Rename "What you expect" on the draft page
`audit.html:127`: the heading "What you expect" sits above a list of questions. Call it
**"Questions"** (or "What the draft still needs to know") and keep the
"N of M answered" count, shown as a progress bar.

### 8. Show times in a readable form
`runs.html:29` shows `started_at[:16]` as raw UTC ISO text. Use relative time
("12 min ago") with the full local time in a `title`. Do the same on the run page.

### 9. Accessibility basics
- `data-msg` elements (every "Saving…" and every error) need `role="status"` /
  `aria-live="polite"`, and errors `role="alert"`. `say()` (`app.js:20`) sets the colour
  inline. Use a `.msg-error` class instead.
- The chat log needs `role="log"` and `aria-live="polite"`.
- `app.css` has no `:focus-visible` style for `.btn`, `.opt` or `.tabs button`, and no
  hover state on `.btn` except `.btn-danger`.
- Radio cards (`.opt`) look the same when selected. Add
  `.opt:has(input:checked){border-color:var(--blue);background:var(--blue-bg)}`.
- The run-list filter `.tabs` should use `role="tablist"`/`aria-selected`, or be links
  with `?status=` so a filter survives a reload and can be shared.

---

## P2: medium (a PR each)

### 10. Stop reloading the whole page after every action
Almost every action ends in `location.reload()`: answering, changing a model, saving a
setting, chatting, run polling (`app.js:161,178,209,272,354,391,430,455,467,479`). The
cost:
- You lose your scroll position on long pages. Answering question 7 of 12 on the draft
  page sends you back to the top.
- The workflow page and settings page flash on every dropdown change.
- A running run reloads every time a step changes. Open `<details>` survive this (through
  sessionStorage), but scroll position and text selection do not.

Minimum fix: save `scrollY` to sessionStorage before a reload and restore it afterwards,
in the same way `<details>` state is kept now. A better fix: have the endpoints return the
updated fragment (the question list, the steps table, one run step card) and swap it in
place. No build step is needed; a small `swap(selector, html)` helper, or htmx from a CDN,
would do.

### 11. Replace `alert`/`confirm`/`prompt` with in-page dialogs
Native dialogs are used for delete, rename, model and instruction changes, real runs and
errors (`app.js:21,40,48,69,150,174,193,290,296`). The worst one is `takesResult`
(`app.js:193`): the choice rests on "OK: give back theirs as well. Cancel: keep what it
gives back now." Buttons that read OK and Cancel should not carry two real options.
- Use one `<dialog>` helper with labelled buttons ("Use their result too" / "Keep the
  current result").
- Rename: edit the name in place, with the slug rules shown as you type.
- Show errors inline beside the thing that failed, as the model dropdown already does.

### 12. Clean up the workflow page header
`workflow.html:10-19` has six actions of equal weight: the tech switch, Settings, Open
YAML, Rename, Delete workflow, Run with a topic.
- The primary action is **Run**. Settings stays a secondary button. Put YAML, Rename and
  Delete in a "More" menu (`<details class="menu">`).
- Disable **"Run it for real"** while questions are open, and explain why next to the
  button, linked to `#questions`. At the moment the server refuses it (`OpenFindings`)
  only after the click.
- The default-model picker (`workflow.html:34`) floats between the run form and the
  diagram with no card around it. Move it into Settings, or into the steps table header.

### 13. Show recent runs on the workflow page, and link it to its draft
- The workflow page has no list of its own runs. You have to go to `/runs` and search.
  Add a "Recent runs" card (last 5, with status, case/topic, cost and a link to compare).
- The workflow page has no link back to the draft chat that made it, and the saved draft
  has no link to the workflow it was saved as (the top bar only says "Saved"). Add both:
  **"Edit in chat"** on the workflow page and **"Open the workflow"** on a saved draft.
  People liked the chat most, so it should be one click away from any saved workflow.

### 14. One place to answer questions on the draft page
On the draft page the same question can appear in three places: asked in the chat
(`audit.html:50-70`), in the right-hand list (`:130-132`), and under "Your document
beside the definition". Each has its own answer form, and answering in one reloads all
three.
- Make the chat the main way to answer (the demo showed people like it). Turn the
  right-hand card into a compact checklist: each question as one line with its status,
  where clicking asks it in the chat (`data-chat-about`) instead of opening a second form.
- Keep the full forms for people who prefer them, collapsed behind "Answer here instead".

### 15. Make the top-bar actions on the draft page clear
`audit.html:7-11`: **Delete draft**, **Save draft**, **Save and try it on a topic**.
- "Save draft" writes the workflow file. Label it for what it does: "Save as workflow
  *name*" the first time, and "Update workflow *name*" after that. Show "unsaved changes"
  when the draft differs from what was saved.
- Move Delete into a menu. A red button next to Save is easy to hit by mistake.
- Show the diagram by default, as the workflow page does, rather than behind "Picture"
  (`:103`). People asked to see the workflow drawn, and they have to find a toggle to
  see it.

### 16. Settings pages: save the same way everywhere, and show inherited defaults
- On `settings.html` each section has its own Save button, the per-step dropdown saves on
  change (`:40`), and the only feedback line is at the top of the page (`:12`), which is
  off screen when you save the spending limit. Put the message beside the button that was
  pressed (as the model dropdown does now), and choose one saving style per page.
- A workflow's settings do not show the app-wide default they could fall back to. Show
  "App default: *X*" and a "Use the app default" link for each setting.
- Explain empty fields: what does a blank spending limit mean?
- **The settings are out of date after search_further (#59-#62).** "Going deeper"
  (`settings.html:76`, `app.py:1374`) only lists `subworkflow` steps. A step using
  `search_further` (levels, max_searches, max_topics) cannot be changed here, and the
  run page does not show its rounds (no template mentions `further`). Add a "Searching
  further" card for those steps, and show the rounds on the run page ("Round 2: 3 topics,
  7 searches", with the topics that were followed and the ones that were skipped).

### 17. Runs list
- Filter by workflow (and dry/real), and keep filters in the URL.
- "Start a run" (`runs.html:18`) sends people to `/workflows`. Make it a small picker, or
  remove it.
- The compare form (`:45`) lets you choose runs of different workflows. Only offer runs of
  the same workflow as the first run chosen.
- Add paging once there are more than about 50 runs. The page renders every run and its
  step squares.

---

## P3: larger pieces

### 18. Stream the chat and draft creation
- "Make the draft" can take a minute (`app.js` shows "This can take a minute…" and a
  spinner). Show the stages as they happen: reading, drafting steps, writing
  instructions, finding questions. The server already knows each stage.
- Chat replies arrive only after the whole turn is done, followed by a full reload.
  Stream the reply (SSE) and add the change and "answered for you" notes as they arrive.
  This matters most for slower models.

### 19. Live run page
Swap polling plus reload (`app.js:340-358`) for SSE, or for polling that swaps fragments.
Show per-step elapsed time, the live search log for the running step, and spend against
the budget as a bar in the top bar. Also let people stop a dry run: today only real runs
can be paused (`run.html:30`).

### 20. Undo on a saved workflow
Draft changes can be undone one by one (`audit.html` "Changes"). Changes to a saved
workflow's model, instructions or settings cannot, although the definition has versions.
Add a "History" panel to the workflow page with a diff of each version and "Restore".

### 21. Home page
`/` goes to the workflow list. A home page that answers "what needs me?" would help:
runs waiting on you, drafts with open questions, recent runs, and spend this week.

### 22. Responsive layout
The top bar (`app.css .topbar`) does not wrap: at under ~700px the four links, the
offline pill and the primary button overflow. The steps table on the workflow page has
five columns with dropdowns and does not reflow. Use a collapsible nav and stacked table
rows on narrow screens.

---

## Suggested order

1. P1 items 1–9 in one or two small PRs: copy, run-page order, links, a11y.
2. #10 scroll restoration (the cheap version) straight after. It removes the most
   noticeable irritation.
3. #16's search_further settings and run view: a gap left by features that already
   shipped.
4. #13 and #14 together: they settle how chat, draft and workflow relate to each other.
5. #11, #12, #15, #17.
6. P3 when there is demo feedback to pull it forward.

## Not recommended

- A drag-and-drop workflow editor. The demo showed people expect to *see* the workflow,
  and the read-only diagram (#46) covers that. Editing through chat is what worked, and a
  second editor would split the model of how changes happen (each with a reason, each
  undoable).
- A front-end framework or build step. Everything above can be done with fragments and a
  little plain JS, in keeping with `app.js` ("no build step").
