"""Which model does what, read from the environment.

Model names are deployment configuration, so they are named once here and nowhere
else in the code. Two of them are the levels of judgement a step runs at; the rest
are the models the system itself uses to read a document, answer in the chat and
fill a gap. Each falls back to a level of judgement rather than to a name of its
own, so moving `WF_QUICK_MODEL` moves everything that wanted quick judgement.

Every value is read on each call, not at import, so a process started before its
environment was set still sees it.
"""

from __future__ import annotations

import json
import os

DEFAULT_QUICK = "claude-sonnet-5"
DEFAULT_CAREFUL = "claude-opus-5"


# $ per million tokens, (input, output). A model that is not listed is costed as if
# it were the careful one, so an estimate for a model configured without a price errs
# high rather than low.
DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    DEFAULT_CAREFUL: (5.0, 25.0),
    DEFAULT_QUICK: (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def pricing() -> dict[str, tuple[float, float]]:
    """The price table, or the one in ``WF_MODEL_PRICING`` when it is set."""
    raw = os.environ.get("WF_MODEL_PRICING")
    if not raw:
        return DEFAULT_PRICING
    data = json.loads(raw)
    return {k: (float(v[0]), float(v[1])) for k, v in data.items()}


def quick_model() -> str:
    """Faster and cheaper. The default when a step does not say how much care it needs."""
    return os.environ.get("WF_QUICK_MODEL") or DEFAULT_QUICK


def careful_model() -> str:
    """Slower and costs more. Used where the document asks for care."""
    return os.environ.get("WF_CAREFUL_MODEL") or DEFAULT_CAREFUL


def model_for(judgement: str | None) -> str:
    """The model for a level of judgement. Anything but ``careful`` is quick."""
    return careful_model() if judgement == "careful" else quick_model()


def extraction_model() -> str:
    """Turns a process document into a draft. Careful work by default."""
    return os.environ.get("WF_EXTRACTION_MODEL") or careful_model()


def chat_model() -> str:
    """Answers in the chat that edits a draft."""
    return os.environ.get("WF_CHAT_MODEL") or quick_model()


def guess_model() -> str:
    """Fills a gap the document left, when guesses are allowed to ask a model."""
    return os.environ.get("WF_GUESS_MODEL") or quick_model()
