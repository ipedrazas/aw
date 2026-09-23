from __future__ import annotations

import os

from wf.logs import ROOT, get_logger
from wf.schema import Workspace
from wf.settings import (
    ANTHROPIC,
    OPENROUTER,
    PROVIDERS,
    link_check_mode,
    provider,
    provider_named_by,
)

from .base import (
    Activities,
    ActivityError,
    ActivityPolicy,
    Guess,
    Guesser,
    LinkCheckActivity,
    LinkStatus,
    ModelActivity,
    ModelRequest,
    ModelResponse,
    RenderActivity,
    SearchActivity,
    SearchResult,
    SendActivity,
    ToolCallRecord,
    ToolSpec,
    Usage,
    run_with_policy,
)
from .guess import HeuristicGuesser, ModelGuesser
from .links import FixtureLinkCheck, LiveLinkCheck
from .models import (
    AnthropicModel,
    RecordingModel,
    ReplayModel,
    ScriptedModel,
    cost_of,
    envelope_schema,
)
from .openrouter import OpenRouterModel
from .render import PdfRenderer
from .search import FixtureSearch, NoSearch
from .send import RecordingSend
from .session import SessionRecorder, record_sessions

logger = get_logger(f"{ROOT}.activities")


def default_model() -> ModelActivity:
    """The model the environment asks for.

    Three answers, in the order they are asked for: the offline model when
    ``WF_FAKE_MODEL`` is set, since a checkout with no key should still walk; the
    gateway when the provider says so; the vendor otherwise. Everything that needs a
    model comes here rather than naming one, which is why adding a provider is a file
    beside this one and a line in it.
    """
    if os.environ.get("WF_FAKE_MODEL"):
        from .fake import FakeModel

        logger.debug(
            "the offline model will answer every call (WF_FAKE_MODEL is set)",
            extra={"fields": {"event": "model.activity", "provider": "offline"}},
        )
        return FakeModel()
    name = provider()
    logger.debug(
        "model calls go to %s, named by %s",
        name,
        provider_named_by(),
        extra={
            "fields": {
                "event": "model.activity",
                "provider": name,
                "named_by": provider_named_by(),
            }
        },
    )
    if name == OPENROUTER:
        return OpenRouterModel()
    if name == ANTHROPIC:
        return AnthropicModel()
    raise ActivityError(
        f"WF_MODEL_PROVIDER={name!r} is not a provider this knows; "
        f"it is one of {', '.join(PROVIDERS)}"
    )


def default_activities(
    ws: Workspace, model: ModelActivity | None = None, *, model_guesses: bool = True
) -> Activities:
    """Real model calls, recorded search, real link checks, real PDF rendering, nothing
    sent anywhere. ``WF_LINK_CHECK=recorded`` keeps the link check off the network."""
    model = model or default_model()
    guesser: Guesser = ModelGuesser(model) if model_guesses else HeuristicGuesser()
    return Activities(
        model=model,
        search=FixtureSearch(ws),
        links=LiveLinkCheck(ws) if link_check_mode() == "live" else FixtureLinkCheck(ws),
        render=PdfRenderer(),
        send=RecordingSend(),
        guesser=guesser,
    )


__all__ = [
    "Activities",
    "ActivityError",
    "ActivityPolicy",
    "AnthropicModel",
    "FixtureLinkCheck",
    "FixtureSearch",
    "Guess",
    "Guesser",
    "HeuristicGuesser",
    "LinkCheckActivity",
    "LinkStatus",
    "LiveLinkCheck",
    "ModelActivity",
    "ModelGuesser",
    "ModelRequest",
    "ModelResponse",
    "NoSearch",
    "OpenRouterModel",
    "PdfRenderer",
    "RecordingModel",
    "RecordingSend",
    "RenderActivity",
    "ReplayModel",
    "ScriptedModel",
    "SearchActivity",
    "SearchResult",
    "SendActivity",
    "SessionRecorder",
    "ToolCallRecord",
    "ToolSpec",
    "Usage",
    "cost_of",
    "default_activities",
    "default_model",
    "envelope_schema",
    "record_sessions",
    "run_with_policy",
]
