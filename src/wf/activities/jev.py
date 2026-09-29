"""The same model activity, answered by a decisions model.

Jev (TypeSafe's first "System One" model) writes no text. It is handed the state and a
set of typed questions, and answers each with a value and a probability: a *choice*
among named options, or a *noul*, a yes or no with the probability of yes. OpenRouter
serves it on its own endpoint, ``/api/alpha/decisions``, and refuses it on
chat/completions, so it cannot be one more name behind ``OpenRouterModel``.

What it can be is one more implementation of ``ModelActivity``, so an agent step runs
on it the way it runs on Claude, with the step's model the only thing that changes.
The step's output schema is where the questions come from, read by the rules in
``wf.decisions`` (shared with the check that a step can run on it): a string with an
``enum`` is a choice, a boolean a yes or no, and a property named ``probabilities`` is
filled with Jev's probabilities rather than asked. A text model asked the same schema
states its own there, which is the difference the claim-support experiment measures.

Anything else in the schema is a question Jev cannot answer, and the step is told so
rather than handed a guess. Jev gives no reasons, so the response carries no decisions;
and it takes no instructions, so the step's skill is not sent.
"""

from __future__ import annotations

from typing import Any

from wf.decisions import PROBABILITIES, NotAQuestion, is_decisions_model
from wf.decisions import questions_from_schema as _questions
from wf.logs import ROOT, get_logger
from wf.settings import openrouter_api_key, openrouter_base_url

from .base import ActivityError, ModelActivity, ModelRequest, ModelResponse, Usage

logger = get_logger(f"{ROOT}.activities.jev")

#: Statuses worth another go, as for the chat gateway.
RETRYABLE = frozenset({408, 409, 425, 429})

DEFAULT_TIMEOUT_S = 60.0


def questions_from_schema(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The typed questions an output schema asks (``wf.decisions``), refused as an
    activity error when it asks one a decisions model cannot answer."""
    try:
        return _questions(schema)
    except NotAQuestion as e:
        raise ActivityError(str(e)) from e


def answer_from(
    schema: dict[str, Any], questions: dict[str, dict[str, Any]], answers: dict[str, Any]
) -> dict[str, Any]:
    """The step's output from Jev's answers, with its probabilities where the schema
    has room for them."""
    out: dict[str, Any] = {}
    probabilities: dict[str, Any] = {}
    for name, q in questions.items():
        a = answers.get(name)
        if not isinstance(a, dict):
            raise ActivityError(f"the decisions model did not answer “{name}”")
        if q["type"] == "choice":
            out[name] = a.get("choice")
            probabilities[name] = dict(a.get("probabilities") or {})
        else:
            p = float(a.get("noul") or 0.0)
            out[name] = p >= 0.5
            probabilities[name] = p
    if PROBABILITIES in (schema.get("properties") or {}):
        out[PROBABILITIES] = probabilities
    return out


class JevModel:
    """A model activity that asks OpenRouter's Decisions endpoint."""

    def __init__(self, client: Any | None = None, *, timeout_s: float = DEFAULT_TIMEOUT_S):
        self._client = client
        self.timeout_s = timeout_s

    @property
    def client(self) -> Any:
        if self._client is None:
            import httpx

            key = openrouter_api_key()
            if not key:
                raise ActivityError(
                    "OPENROUTER_API_KEY is not set, so the decisions model cannot be asked"
                )
            # The Decisions endpoint sits beside v1, not under it.
            base = openrouter_base_url().rstrip("/").removesuffix("/v1")
            self._client = httpx.Client(
                base_url=base,
                timeout=self.timeout_s,
                headers={"Authorization": f"Bearer {key}", "X-Title": "wf"},
            )
        return self._client

    def complete(self, request: ModelRequest) -> ModelResponse:
        if request.tools:
            raise ActivityError(
                f"{request.model} is a decisions model: it cannot use tools, so a step "
                "that searches or reads pages needs a text model"
            )
        questions = questions_from_schema(request.output_schema)
        body = {"model": request.model, "state": request.input, "questions": questions}
        response = self.client.post("/alpha/decisions", json=body)
        status = response.status_code
        if status >= 400:
            if status in RETRYABLE or status >= 500:
                response.raise_for_status()  # raised as itself, so the policy retries it
            raise ActivityError(
                f"the gateway refused the decisions request ({status}): {_detail(response)}"
            )
        data = response.json()
        spent = data.get("usage") or {}
        return ModelResponse(
            output=answer_from(request.output_schema, questions, data.get("answers") or {}),
            decisions=[],
            usage=Usage(
                input_tokens=int(spent.get("input_tokens") or 0),
                output_tokens=int(spent.get("output_tokens") or 0),
                cost_usd=round(float(spent.get("cost") or 0.0), 9),
            ),
            model=str(data.get("model") or request.model),
        )


class DecisionsRouter:
    """Sends a request for a decisions model to Jev, and every other one to the model
    the environment chose. One activity in, so recording, replay and everything else
    that wraps the model wraps both."""

    def __init__(self, inner: ModelActivity, decisions: ModelActivity | None = None):
        self.inner = inner
        self.decisions = decisions or JevModel()

    def complete(self, request: ModelRequest) -> ModelResponse:
        if is_decisions_model(request.model):
            logger.debug(
                "%s goes to the decisions endpoint",
                request.tag,
                extra={
                    "fields": {
                        "event": "model.decisions",
                        "tag": request.tag,
                        "model": request.model,
                    }
                },
            )
            return self.decisions.complete(request)
        return self.inner.complete(request)


def _detail(response: Any) -> str:
    """Why the gateway said no, in as few words as it gave."""
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - an error page is not always JSON
        return str(getattr(response, "text", ""))[:300]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return str(error or body)[:300]
