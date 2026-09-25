"""Typed findings and the field-to-question mapping.

A finding is the same object whether it is raised by the validator at audit time
or hit by the interpreter during a run. Each one carries the question to ask in
plain language, and where possible the answers to choose from, so the UI can use
radio buttons rather than free text.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, Field

from wf.schema import FindingType
from wf.settings import careful_model, quick_model

AnswerKind = Literal["choice", "multi", "text", "bool", "number", "list"]


class Option(BaseModel):
    value: Any
    label: str
    consequence: str | None = None


class Finding(BaseModel):
    id: str = ""
    type: FindingType
    step_id: str | None = None
    field: str  # dotted path inside the definition, e.g. steps.review.when
    question: str
    detail: str = ""  # one or two plain sentences on why it matters
    source_text: str | None = None  # the document's own words, when we have them
    answer_kind: AnswerKind = "text"
    options: list[Option] = Field(default_factory=list)
    answer: Any = None
    status: Literal["open", "answered", "dismissed"] = "open"
    unblocks: int = 0  # how many later steps depend on this being answered
    raised_by: Literal["validator", "auditor", "interpreter"] = "validator"

    def model_post_init(self, __context: Any) -> None:
        if not self.id:
            key = f"{self.type}|{self.field}|{self.question}"
            self.id = hashlib.sha1(key.encode()).hexdigest()[:10]


# -- question templates ------------------------------------------------------
# Keyed by the last meaningful part of the field path. ``{step}`` is the step title.

# Each question is about the person's work, in their words, and names the step it is
# about; the reason says what goes wrong for them, not which rule the definition breaks.
# A demo audience said the first version was about how the system works, and that it
# made them feel stupid.
QUESTIONS: dict[str, tuple[str, str]] = {
    "read_by": (
        "Which step should use the {input}?",
        "Nothing uses it yet, so it would be ignored.",
    ),
    "when": (
        "When should “{step}” happen?",
        "Your document says it only happens sometimes, but not when.",
    ),
    "does_not_check": (
        "What can “{step}” miss?",
        "For example, a link can open and still point to the wrong page. Saying what it "
        "misses stops a pass being read as more than it is.",
    ),
    "checks": (
        "What should “{step}” look for?",
        "Say what counts as a pass, so a pass always means the same thing.",
    ),
    "requires_approval": (
        "Should someone approve “{step}” before it goes out?",
        "It sends something outside the system.",
    ),
    "side_effects": (
        "Does “{step}” send or change anything outside the system?",
        "Anything that goes out can be held for someone to approve, but only if it is listed.",
    ),
    "deadline": (
        "How long should the run wait at “{step}”?",
        "Without a limit it could wait forever.",
    ),
    "on_timeout": (
        "If nobody answers “{step}” in time, what then?",
        "",
    ),
    "hands_on": (
        "What should “{step}” pass on from what it finds?",
        "To cite a source or check its link later, the steps after it need its address.",
    ),
    "follows": (
        "When should “{step}” go deeper, and into what?",
        "It comes after someone reviews the work, but nothing links the two, so it would "
        "run every time with nothing to look into.",
    ),
    "input": (
        "What does “{step}” work from?",
        "It is not given anything, so it would have to make up what it needs.",
    ),
    "tools": (
        "Should “{step}” search the web?",
        "Your document says it searches. Without search it can only answer from what the "
        "model already knows.",
    ),
    "limits": (
        "How much more research can “{step}” start?",
        "Each follow-up is another run, and costs money. A limit stops it running away.",
    ),
    "max_fanout": (
        "At most how many times should “{step}” run, one per item?",
        "It runs once for each item on a list, one after another. A limit keeps the cost known.",
    ),
    "budget": (
        "How much should one run be allowed to spend?",
        "It can start more research, so one limit covers everything it starts, in money and time.",
    ),
    "enum": (
        "What can “{step}” come back with?",
        "Later steps depend on it, so each possible outcome needs naming.",
    ),
    "unhandled": (
        "When “{step}” comes back with “{value}”, what should happen?",
        "Your document does not say.",
    ),
    "title": (
        "What do you call this step?",
        "",
    ),
    "shows_user": (
        "What do you want to see when “{step}” finishes?",
        "",
    ),
    "trust": (
        "Should “{step}” check with you before it carries on?",
        "",
    ),
    "model": (
        "Which model should run “{step}”?",
        "Standard is faster and cheaper, and enough for planning and research. Thorough is "
        "slower, costs more per run, and does better at writing and review. "
        "A default for every step can be set on the workflow page.",
    ),
    "skill": (
        "How should “{step}” be done?",
        "Your answer becomes the instructions it follows. You can change them later.",
    ),
    "output.schema": (
        "What does “{step}” produce?",
        "Later steps use it, so it needs to be clear what it is.",
    ),
    "run": (
        "What should “{step}” do?",
        "It needs no judgement, so it uses one of these built-in actions.",
    ),
    "workflow": (
        "Which of your workflows should “{step}” start?",
        "",
    ),
}


def plain_value(v: Any) -> str:
    """An outcome as a person would say it: “more research”, not “more_research”."""
    return str(v).replace("_", " ")


def question_for(field: str, step_title: str | None = None, **fmt: Any) -> tuple[str, str]:
    key = field.rsplit(".", 1)[-1]
    if field.endswith("output.schema"):
        key = "output.schema"
    if ".output.continue_on." in field:
        key = "unhandled"
    if ".output.enum." in field:
        key = "enum"
    q, d = QUESTIONS.get(key, (f"What should “{key.replace('_', ' ')}” be for “{{step}}”?", ""))
    ctx = {
        "step": step_title or "this step",
        "field": field,
        "field_name": field,
        "value": "",
        **{k: plain_value(v) if k == "value" else v for k, v in fmt.items()},
    }
    return q.format(**ctx), d.format(**ctx)


TRUST_OPTIONS = [
    Option(
        value={"policy": "always_ask"},
        label="Yes, every time",
        consequence="The run waits here for your OK.",
    ),
    Option(
        value={"policy": "earned", "promote_after": 3},
        label="Until I have said OK 3 times in a row",
        consequence="Then it carries on by itself. Any change to the step starts the count again.",
    ),
    Option(
        value={"policy": "auto"},
        label="No, it can carry on by itself",
        consequence="You see the result afterwards.",
    ),
]


def model_options() -> list[Option]:
    """The two models the deployment configured, named for what they are good at.

    "Standard" and "thorough" rather than "quick" and "careful": people read quick
    judgement as careless judgement, and neither is.
    """
    return [
        Option(
            value=quick_model(),
            label="Standard",
            consequence="Faster and cheaper. Enough for planning and research.",
        ),
        Option(
            value=careful_model(),
            label="Thorough",
            consequence="Slower and costs more. Better at writing and review.",
        ),
    ]


SHOWS_USER_OPTIONS = [
    Option(value=["output"], label="Its result"),
    Option(value=["decisions"], label="What it decided and why"),
    Option(value=["output", "decisions"], label="Both"),
]

ON_TIMEOUT_OPTIONS = [
    Option(value="remind", label="Remind and keep waiting"),
    Option(value="continue", label="Carry on without the answer"),
    Option(value="stop", label="Stop the run"),
    Option(value="escalate", label="Ask someone else"),
]

DEADLINE_OPTIONS = [
    Option(value="4h", label="4 hours"),
    Option(value="1d", label="1 day"),
    Option(value="3d", label="3 days"),
    Option(value="7d", label="A week"),
]

LIMITS_OPTIONS = [
    Option(
        value={"max_depth": 1, "max_fanout": 3, "budget": "inherit"},
        label="Up to 3 follow-ups",
        consequence="A follow-up cannot start more of its own. They share this run's spending limit.",
    ),
    Option(
        value={"max_depth": 2, "max_fanout": 3, "budget": "inherit"},
        label="Up to 3 follow-ups, and each can start 3 more",
        consequence="Goes further, and can cost several times as much. They share this run's spending limit.",
    ),
    Option(
        value={"max_depth": 1, "max_fanout": 1, "budget": "inherit"},
        label="One follow-up at most",
        consequence="The cheapest: one more piece of research per run.",
    ),
]

# -- the routines a check or a tool may name ---------------------------------
# Definitions cannot add code, so this is the whole set. Each one is described in the
# words of someone reading the question, not in the words of the code that runs it.

RUNNERS: dict[str, tuple[str, str, str]] = {
    # name: (step kind, label, what it does and what you get back)
    "checks.http_resolves": (
        "check",
        "Open every link and see which ones answer",
        "Gives back each source with the code its server returned, and a count of how many opened. It does not read the page, so it cannot tell you the link still says what it said.",
    ),
    "tools.fetch_pages": (
        "tool",
        "Read the pages a report cites",
        "Gives back the text of each page whose link opened, and lists the ones it could not read and why. Nothing leaves the system.",
    ),
    "tools.render_pdf": (
        "tool",
        "Turn the report into a PDF",
        "Gives back the file and its page count, and keeps the report's markdown beside it. Nothing leaves the system.",
    ),
    "tools.send_email": (
        "tool",
        "Send an email",
        "Gives back whether it was delivered. This one leaves the system, so it needs someone to approve it.",
    ),
}


def runner_options(kind: str | None = None) -> list[Option]:
    """The routines a step of this kind may name, as choices. All of them when kind is None."""
    return [
        Option(value=name, label=label, consequence=consequence)
        for name, (rkind, label, consequence) in sorted(RUNNERS.items())
        if kind is None or rkind == kind
    ]


def default_options(field: str) -> tuple[AnswerKind, list[Option]]:
    key = field.rsplit(".", 1)[-1]
    if key == "trust":
        return "choice", TRUST_OPTIONS
    if key == "model":
        return "choice", model_options()
    if key == "shows_user":
        return "choice", SHOWS_USER_OPTIONS
    if key == "on_timeout":
        return "choice", ON_TIMEOUT_OPTIONS
    if key == "deadline":
        return "choice", DEADLINE_OPTIONS
    if key == "run":
        return "choice", runner_options()
    if key == "requires_approval":
        return "choice", [
            Option(
                value=True,
                label="Yes, someone has to approve it",
                consequence="The run pauses here every time.",
            ),
            Option(
                value=False,
                label="No, it can go without approval",
                consequence="Nothing checks it before it leaves.",
            ),
        ]
    if key == "side_effects":
        return "choice", [
            Option(value="none", label="No, nothing goes out"),
            Option(value=["send_email"], label="It sends an email"),
            Option(value=["write_external"], label="It writes to another system"),
        ]
    if key == "limits":
        return "choice", LIMITS_OPTIONS
    if key in ("max_fanout", "max_depth"):
        return "number", []
    if key in ("does_not_check", "checks"):
        return "list", []
    return "text", []


def make_finding(
    type_: FindingType,
    field: str,
    *,
    step: Any = None,
    detail: str | None = None,
    source_text: str | None = None,
    options: list[Option] | None = None,
    answer_kind: AnswerKind | None = None,
    unblocks: int = 0,
    raised_by: Literal["validator", "auditor", "interpreter"] = "validator",
    **fmt: Any,
) -> Finding:
    title = getattr(step, "title", None) or getattr(step, "id", None)
    q, d = question_for(field, title, **fmt)
    kind, opts = default_options(field)
    if options is not None:
        opts = options
        kind = answer_kind or "choice"
    elif answer_kind:
        kind = answer_kind
    return Finding(
        type=type_,
        step_id=getattr(step, "id", None),
        field=field,
        question=q,
        detail=detail if detail is not None else d,
        source_text=source_text,
        answer_kind=kind,
        options=opts,
        unblocks=unblocks,
        raised_by=raised_by,
    )
