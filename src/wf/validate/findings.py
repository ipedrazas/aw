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

QUESTIONS: dict[str, tuple[str, str]] = {
    "read_by": (
        "Which step starts from the {input} you type?",
        "No step reads it, so every step works without it.",
    ),
    "when": (
        "What decides which way this goes?",
        "“{step}” is a branch, but nothing says what makes it run.",
    ),
    "does_not_check": (
        "What does this check not tell you?",
        "Every check states what it does not verify, so a pass is not read as more than it is.",
    ),
    "checks": (
        "What exactly does this check look at?",
        "The check needs a stated test, so a pass means one thing.",
    ),
    "requires_approval": (
        "Who signs this off before it leaves the system?",
        "“{step}” does something outside the system and nobody is named to approve it.",
    ),
    "side_effects": (
        "What does this action change or send outside the system?",
        "Anything that leaves the system has to be declared before it can be gated.",
    ),
    "deadline": (
        "How long do you wait, and then what?",
        "“{step}” waits for someone. Without a deadline it can wait forever.",
    ),
    "on_timeout": (
        "How long do you wait, and then what?",
        "When the wait runs out, something has to happen.",
    ),
    "limits": (
        "How far can this go, and how much can it spend?",
        "“{step}” can start more work. It needs a depth, a fan-out and a budget.",
    ),
    "max_fanout": (
        "How many of these can run at once?",
        "“{step}” runs once per item in a list. The list needs a ceiling.",
    ),
    "budget": (
        "How much may one run spend, in money and time?",
        "This workflow can start more work, so it needs one budget for the whole tree.",
    ),
    "enum": (
        "Which outcomes are possible here?",
        "Later steps branch on this, so every outcome has to be listed.",
    ),
    "unhandled": (
        "What happens when {field_name} is “{value}”?",
        "The process can produce this outcome, but no step handles it.",
    ),
    "title": (
        "What would you call this step?",
        "Steps are shown in your words, not the system's.",
    ),
    "shows_user": (
        "What should you see when this step finishes?",
        "Each step says what it shows you: its result, its decisions, or both.",
    ),
    "trust": (
        "Should this step wait for you, run on its own, or earn that over time?",
        "Nothing runs unattended by default.",
    ),
    "model": (
        "How much judgement does this step need?",
        "This decides which model runs it.",
    ),
    "skill": (
        "What are the instructions for this step?",
        "A judgement step runs from an instruction file you can read and change.",
    ),
    "output.schema": (
        "What does this step produce?",
        "Later steps read fields from this output, so its shape has to be declared.",
    ),
    "run": (
        "Which routine does “{step}” run?",
        "A check or a tool does not use a model. It runs one of the routines below, and "
        "those are the only ones the system has.",
    ),
    "workflow": (
        "Which workflow does this start?",
        "A sub-workflow step names the workflow it runs.",
    ),
    "input": (
        "What does this step work from?",
        "The step needs to name what it reads.",
    ),
}


def question_for(field: str, step_title: str | None = None, **fmt: Any) -> tuple[str, str]:
    key = field.rsplit(".", 1)[-1]
    if field.endswith("output.schema"):
        key = "output.schema"
    if ".output.continue_on." in field:
        key = "unhandled"
    if ".output.enum." in field:
        key = "enum"
    q, d = QUESTIONS.get(key, (f"What should “{key}” be?", ""))
    ctx = {
        "step": step_title or "this step",
        "field": field,
        "field_name": field,
        "value": "",
        **fmt,
    }
    return q.format(**ctx), d.format(**ctx)


TRUST_OPTIONS = [
    Option(
        value={"policy": "always_ask"},
        label="Always ask me",
        consequence="Pauses every run at this step.",
    ),
    Option(
        value={"policy": "earned", "promote_after": 3},
        label="Ask me until I have accepted it 3 times in a row",
        consequence="Runs on its own once it has earned it. Any change to the step starts the count again.",
    ),
    Option(
        value={"policy": "auto"},
        label="Run on its own",
        consequence="You see the result afterwards.",
    ),
]


def model_options() -> list[Option]:
    """The two levels of judgement, named by whatever the deployment configured."""
    return [
        Option(
            value=quick_model(),
            label="Quick judgement",
            consequence="Faster and cheaper. Good for planning and research.",
        ),
        Option(
            value=careful_model(),
            label="Careful judgement",
            consequence="Slower and costs more. Good for writing and review.",
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
        label="One level deeper, up to 3 at a time",
        consequence="Follow-ups cannot start follow-ups of their own. They share this run's budget.",
    ),
    Option(
        value={"max_depth": 2, "max_fanout": 3, "budget": "inherit"},
        label="Two levels deeper, up to 3 at a time",
        consequence="A follow-up can go one level further. They share this run's budget.",
    ),
    Option(
        value={"max_depth": 1, "max_fanout": 1, "budget": "inherit"},
        label="One follow-up, one level deeper",
        consequence="The cheapest: at most one more piece of research per run.",
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
    "tools.render_pdf": (
        "tool",
        "Turn the report into a PDF",
        "Gives back the file and its page count. Nothing leaves the system.",
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
            Option(value="none", label="Nothing leaves the system"),
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
