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
environment was set still sees it. ``model_plan`` is the whole of it in one dict:
what each task will be asked of and through whom, which is what the log prints when
the app loads.
"""

from __future__ import annotations

import json
import os
from typing import Any

DEFAULT_QUICK = "claude-sonnet-5"
DEFAULT_CAREFUL = "claude-opus-5"

#: How much one answer may run to before the model is cut off. Not a model limit —
#: the models above allow several times this — but the most any one call here should
#: need, and a cap on what a model that will not stop can cost.
DEFAULT_MAX_OUTPUT_TOKENS = 32000

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


def skill_model() -> str:
    """Writes the instructions for each step of a draft. It follows the extraction, since
    it is the same careful reading of the same document."""
    return os.environ.get("WF_SKILL_MODEL") or extraction_model()


def skills_mode() -> str:
    """``written`` asks a model to write each step's instructions from the document;
    ``template`` lays them out from the draft alone, with no call. ``WF_SKILLS`` names it;
    written is the default."""
    named = (os.environ.get("WF_SKILLS") or "").strip().lower()
    return "template" if named in {"template", "off"} else "written"


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


def max_output_tokens() -> int:
    """The ceiling on one answer, in tokens. ``WF_MAX_OUTPUT_TOKENS`` moves it.

    This is deployment configuration for the same reason a model name is: how much a
    model may write before it is cut off is a property of the model, and which model
    answers is the environment's to say. The default is what the models named above
    carry comfortably; a model with a smaller ceiling of its own needs this lowered,
    and one asked for a long answer — reading a whole document into a draft is the
    long one — may want it raised. Every answer is streamed, so a large number here
    costs nothing until it is used and does not risk a timeout while it is.

    An answer that hits the ceiling is not truncated quietly: it is the activity error
    that says the model ran out of room.
    """
    named = (os.environ.get("WF_MAX_OUTPUT_TOKENS") or "").strip()
    if not named:
        return DEFAULT_MAX_OUTPUT_TOKENS
    try:
        value = int(named)
    except ValueError:
        return DEFAULT_MAX_OUTPUT_TOKENS
    return value if value > 0 else DEFAULT_MAX_OUTPUT_TOKENS


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


# -- the whole picture ---------------------------------------------------------

#: The tasks that name a model, in the order they are announced: the key, what the
#: task is in plain words, and the variable that names it outright.
TASKS: tuple[tuple[str, str, str], ...] = (
    ("quick", "a step that wants quick judgement", "WF_QUICK_MODEL"),
    ("careful", "a step that wants careful judgement", "WF_CAREFUL_MODEL"),
    ("extraction", "reading a process document into a draft", "WF_EXTRACTION_MODEL"),
    ("skill", "writing the instructions for each step of a draft", "WF_SKILL_MODEL"),
    ("chat", "the chat that edits a draft", "WF_CHAT_MODEL"),
    ("guess", "filling a gap the document left", "WF_GUESS_MODEL"),
)


def model_for_task(task: str) -> str:
    """The model one of the ``TASKS`` will be asked of."""
    answer = {
        "quick": quick_model,
        "careful": careful_model,
        "extraction": extraction_model,
        "skill": skill_model,
        "chat": chat_model,
        "guess": guess_model,
    }[task]
    return answer()


def link_check_mode() -> str:
    """``live`` asks each server (recorded fixtures still answer the URLs they hold);
    ``recorded`` answers from the fixtures alone, for tests and a machine with no network.
    ``WF_LINK_CHECK`` names it; live is the default."""
    named = (os.environ.get("WF_LINK_CHECK") or "").strip().lower()
    return "recorded" if named in {"recorded", "fixtures", "off"} else "live"


def search_mode() -> str:
    """``exa`` searches the web and fetches pages through Exa; ``recorded`` answers from
    the fixtures. ``WF_SEARCH`` names it; failing that, a set ``EXA_API_KEY`` means Exa."""
    named = (os.environ.get("WF_SEARCH") or "").strip().lower()
    if named in {"exa", "live"}:
        return "exa"
    if named in {"recorded", "fixtures", "off"}:
        return "recorded"
    return "exa" if os.environ.get("EXA_API_KEY") else "recorded"


def offline() -> bool:
    """Whether the offline model answers everything, so no name above is asked at all."""
    return bool(os.environ.get("WF_FAKE_MODEL"))


def provider_named_by() -> str:
    """Which variable decided the provider, for when the answer is a surprise."""
    if (os.environ.get("WF_MODEL_PROVIDER") or "").strip():
        return "WF_MODEL_PROVIDER"
    if os.environ.get("OPENROUTER_API_KEY") and not os.environ.get("ANTHROPIC_API_KEY"):
        return "OPENROUTER_API_KEY"
    return "default"


def api_key_variable() -> str:
    """The variable the chosen provider authenticates with."""
    return "OPENROUTER_API_KEY" if provider() == OPENROUTER else "ANTHROPIC_API_KEY"


def model_plan() -> dict[str, Any]:
    """Every model this process will ask for, through whom, and at what price.

    The values, not the variables. This is what the log prints when the app loads, so
    that a gateway nobody meant to use, or a careful model that is quietly the quick
    one, is visible before the first call rather than after the bill. No key is in
    here — only whether one is set.
    """
    where = provider()
    prices = pricing()
    fallback = prices.get(DEFAULT_CAREFUL) or DEFAULT_PRICING[DEFAULT_CAREFUL]
    plan: dict[str, Any] = {
        "provider": where,
        "provider_named_by": provider_named_by(),
        "api_key_variable": api_key_variable(),
        "api_key_set": bool(os.environ.get(api_key_variable())),
        "offline": offline(),
        "max_output_tokens": max_output_tokens(),
        "pricing_from": "WF_MODEL_PRICING" if os.environ.get("WF_MODEL_PRICING") else "default",
        "search": search_mode(),
        "link_check": link_check_mode(),
        "tasks": [],
    }
    if where == OPENROUTER:
        plan["base_url"] = openrouter_base_url()
        plan["strict_schemas"] = openrouter_strict_schemas()
    for task, what, variable in TASKS:
        name = model_for_task(task)
        price = prices.get(name)
        plan["tasks"].append(
            {
                "task": task,
                "what": what,
                "model": name,
                "named_by": variable if os.environ.get(variable) else "default",
                # $ per million tokens, in and out. A model with no price of its own is
                # costed as the careful one, which is what ``cost_of`` does.
                "price_per_mtok": list(price or fallback),
                "priced": price is not None,
            }
        )
    return plan
