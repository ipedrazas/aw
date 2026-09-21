from __future__ import annotations

from wf.schema import Workspace

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
from .render import PdfRenderer
from .search import FixtureSearch, NoSearch
from .send import RecordingSend


def default_activities(
    ws: Workspace, model: ModelActivity | None = None, *, model_guesses: bool = True
) -> Activities:
    """Real model calls, recorded tools, real PDF rendering, nothing sent anywhere."""
    model = model or AnthropicModel()
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
    "PdfRenderer",
    "RecordingModel",
    "RecordingSend",
    "RenderActivity",
    "ReplayModel",
    "ScriptedModel",
    "SearchActivity",
    "SearchResult",
    "SendActivity",
    "ToolCallRecord",
    "ToolSpec",
    "Usage",
    "cost_of",
    "default_activities",
    "envelope_schema",
    "run_with_policy",
]
