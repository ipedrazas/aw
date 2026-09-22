from __future__ import annotations

import os

from wf.schema import Workspace
from wf.settings import ANTHROPIC, OPENROUTER, PROVIDERS, provider

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
from .links import FixtureLinkCheck
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

        return FakeModel()
    name = provider()
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
    """Real model calls, recorded tools, real PDF rendering, nothing sent anywhere."""
    model = model or default_model()
    guesser: Guesser = ModelGuesser(model) if model_guesses else HeuristicGuesser()
    return Activities(
        model=model,
        search=FixtureSearch(ws),
        links=FixtureLinkCheck(ws),
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
