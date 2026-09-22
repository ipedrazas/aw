"""Which model does what, and through whom, read from the environment.

Model names are deployment configuration, so they are named once here and nowhere
else in the code. Two of them are the levels of judgement a step runs at; the rest
are the models the system itself uses to read a document, answer in the chat and
fill a gap. Each falls back to a level of judgement rather than to a name of its
own, so moving `WF_QUICK_MODEL` moves everything that wanted quick judgement.

A name is only half of it: the other half is the provider it is sent to. Anthropic
is one vendor's models under their own names; OpenRouter is a gateway in front of
many vendors, where a model is written `vendor/model`. Nothing above this module
knows which is in use — a step asks for careful judgement and gets whatever the
environment names — so pointing the whole system at a different model is two
variables, not a code change.

Every value is read on each call, not at import, so a process started before its
environment was set still sees it.
"""

from __future__ import annotations

import json
import os

DEFAULT_QUICK = "claude-sonnet-5"
DEFAULT_CAREFUL = "claude-opus-5"

ANTHROPIC = "anthropic"
OPENROUTER = "openrouter"
PROVIDERS = (ANTHROPIC, OPENROUTER)

#: The gateway's own address, and the same two models reached through it. OpenRouter
#: names a model for the vendor it comes from, so the defaults are these two under the
#: vendor whose API the other provider is.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_QUICK = f"{ANTHROPIC}/{DEFAULT_QUICK}"
OPENROUTER_CAREFUL = f"{ANTHROPIC}/{DEFAULT_CAREFUL}"


# $ per million tokens, (input, output). A model that is not listed is costed as if
# it were the careful one, so an estimate for a model configured without a price errs
# high rather than low.
DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    DEFAULT_CAREFUL: (5.0, 25.0),
    DEFAULT_QUICK: (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    OPENROUTER_CAREFUL: (5.0, 25.0),
    OPENROUTER_QUICK: (2.0, 10.0),
}


def pricing() -> dict[str, tuple[float, float]]:
    """The price table, or the one in ``WF_MODEL_PRICING`` when it is set."""
    raw = os.environ.get("WF_MODEL_PRICING")
    if not raw:
        return DEFAULT_PRICING
    data = json.loads(raw)
    return {k: (float(v[0]), float(v[1])) for k, v in data.items()}


def provider() -> str:
    """Where the model names are sent: ``anthropic`` or ``openrouter``.

    ``WF_MODEL_PROVIDER`` says it outright. Failing that, a checkout that has only an
    OpenRouter key in its environment means OpenRouter: having to set a second
    variable to explain the first would be ceremony.
    """
    named = (os.environ.get("WF_MODEL_PROVIDER") or "").strip().lower()
    if named:
        return named
    if os.environ.get("OPENROUTER_API_KEY") and not os.environ.get("ANTHROPIC_API_KEY"):
        return OPENROUTER
    return ANTHROPIC


def quick_model() -> str:
    """Faster and cheaper. The default when a step does not say how much care it needs."""
    named = os.environ.get("WF_QUICK_MODEL")
    if named:
        return named
    return OPENROUTER_QUICK if provider() == OPENROUTER else DEFAULT_QUICK


def careful_model() -> str:
    """Slower and costs more. Used where the document asks for care."""
    named = os.environ.get("WF_CAREFUL_MODEL")
    if named:
        return named
    return OPENROUTER_CAREFUL if provider() == OPENROUTER else DEFAULT_CAREFUL


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


def openrouter_base_url() -> str:
    """Where the gateway lives. ``OPENROUTER_BASE_URL`` moves it, for a proxy in front."""
    return os.environ.get("OPENROUTER_BASE_URL") or OPENROUTER_BASE_URL


def openrouter_api_key() -> str:
    """The one key. OpenRouter authenticates with a bearer token, not an API key header."""
    return os.environ.get("OPENROUTER_API_KEY", "")


def openrouter_strict_schemas() -> bool:
    """Whether to ask the gateway to enforce the output schema rather than suggest it.

    Off by default: strict enforcement is a property of the provider that ends up
    serving the call, several of them reject schemas that use `minItems` or `maximum`,
    and a request refused for that reason is worse than an answer we validate here.
    Turn it on (`WF_OPENROUTER_STRICT=1`) when the models in use all support it.
    """
    return (os.environ.get("WF_OPENROUTER_STRICT") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
