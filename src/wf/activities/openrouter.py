"""The same model activity, asked through OpenRouter.

The vendor client speaks one API to one vendor. OpenRouter is a gateway that fronts
many vendors behind the chat-completions shape, so this is a second implementation of
``ModelActivity`` rather than a different client for the first one: the system prompt,
the JSON envelope, the data regions, the tool loop and its call limits are the same
code and the same rules, and only the wire shape differs. Which model answers is
whatever the environment names — ``vendor/model``, as the gateway writes it — so any
model it carries is reachable without touching this file.

Two things the gateway knows that we would otherwise guess. It says what the call
cost, having routed it, so the local price table is only a fallback for when it does
not; and it says which model actually answered, which is what the session records
when the name asked for was a family rather than a model.
"""

from __future__ import annotations

import json
from typing import Any

from wf.settings import openrouter_api_key, openrouter_base_url, openrouter_strict_schemas

from .base import ActivityError, ModelRequest, ModelResponse, ToolCallRecord, ToolSpec, Usage
from .models import build_system, cost_of, envelope_schema, summarise
from .safety import data_region, find_instructions

#: What the envelope is called on the wire. Some providers show the name back to the
#: model, so it says what it is.
SCHEMA_NAME = "wf_step_envelope"

#: Statuses worth another go. They are raised rather than turned into an
#: ``ActivityError`` so the activity policy retries them; everything else is the
#: gateway saying no, and asking again would only say it again.
RETRYABLE = frozenset({408, 409, 425, 429})

DEFAULT_TIMEOUT_S = 300.0


class OpenRouterModel:
    """A model activity that asks OpenRouter, in the shape OpenRouter reads."""

    def __init__(
        self,
        client: Any | None = None,
        max_tool_rounds: int = 60,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        self._client = client
        self.max_tool_rounds = max_tool_rounds
        self.timeout_s = timeout_s

    @property
    def client(self) -> Any:
        if self._client is None:
            import httpx

            key = openrouter_api_key()
            if not key:
                raise ActivityError(
                    "OPENROUTER_API_KEY is not set, so nothing can be asked of the gateway"
                )
            self._client = httpx.Client(
                base_url=openrouter_base_url(),
                timeout=self.timeout_s,
                headers={
                    # A bearer token, not an API key header: the gateway is not the vendor.
                    "Authorization": f"Bearer {key}",
                    "X-Title": "wf",  # what the account's activity list calls these calls
                },
            )
        return self._client

    def complete(self, request: ModelRequest) -> ModelResponse:
        strict = openrouter_strict_schemas()
        schema = envelope_schema(request.output_schema, request.decisions_required)
        tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                    "strict": strict,
                },
            }
            for t in request.tools
        ]
        executors = {t.name: t for t in request.tools}
        calls_left = {t.name: t.max_calls for t in request.tools}
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": build_system(request.system)},
            {"role": "user", "content": data_region("step input", request.input)},
        ]
        usage = Usage()
        billed = 0.0  # what the gateway says the rounds cost, when it says
        answered_by = ""
        tool_calls: list[ToolCallRecord] = []

        for _ in range(self.max_tool_rounds):
            body: dict[str, Any] = {
                "model": request.model,
                "max_tokens": request.max_tokens,
                "messages": messages,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": SCHEMA_NAME, "strict": strict, "schema": schema},
                },
            }
            if tools:
                body["tools"] = tools
            data = self._post(body)

            answered_by = data.get("model") or answered_by
            spent = data.get("usage") or {}
            usage.input_tokens += int(spent.get("prompt_tokens") or 0)
            usage.output_tokens += int(spent.get("completion_tokens") or 0)
            billed += float(spent.get("cost") or 0.0)

            choices = data.get("choices") or []
            if not choices:
                raise ActivityError("the gateway returned no answer")
            message = choices[0].get("message") or {}
            reason = choices[0].get("finish_reason")
            if reason == "content_filter":
                raise ActivityError("the model declined this request")
            if reason == "length":
                raise ActivityError("the model ran out of room before finishing its answer")

            calls = message.get("tool_calls") or []
            if calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": _text_of(message),
                        "tool_calls": calls,
                    }
                )
                for call in calls:
                    messages.append(self._run_tool(call, executors, calls_left, tool_calls))
                continue

            text = _text_of(message)
            if not text:
                raise ActivityError("the model returned no answer")
            try:
                answer = json.loads(text)
            except json.JSONDecodeError as e:
                raise ActivityError(f"the model's answer was not valid JSON: {e}") from e
            usage.cost_usd = (
                round(billed, 6)
                if billed
                else cost_of(request.model, usage.input_tokens, usage.output_tokens)
            )
            return ModelResponse(
                output=answer.get("output", {}),
                decisions=list(answer.get("decisions", [])),
                usage=usage,
                tool_calls=tool_calls,
                model=answered_by or request.model,
            )
        raise ActivityError("the model kept calling tools past the round limit")

    # -- one call ------------------------------------------------------------

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        """One round trip. A bad moment is raised as itself so it can be retried; a
        refusal is an ``ActivityError``, which the policy above does not retry."""
        response = self.client.post("/chat/completions", json=body)
        status = response.status_code
        if status in RETRYABLE or status >= 500:
            response.raise_for_status()
        if status >= 400:
            raise ActivityError(f"the gateway refused the request ({status}): {_detail(response)}")
        data = response.json()
        # A provider that fails mid-call is reported in the body of a 200.
        error = data.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else error
            raise ActivityError(f"the gateway could not answer: {message}")
        return data

    # -- one tool call --------------------------------------------------------

    def _run_tool(
        self,
        call: dict[str, Any],
        executors: dict[str, ToolSpec],
        calls_left: dict[str, int],
        recorded: list[ToolCallRecord],
    ) -> dict[str, Any]:
        """Run what the model asked for and write the result down twice: once for the
        model, as the next message, and once for the session, as a record."""
        function = call.get("function") or {}
        name = str(function.get("name") or "")
        spec = executors.get(name)

        def answer(content: str) -> dict[str, Any]:
            return {
                "role": "tool",
                "tool_call_id": call.get("id"),
                "name": name,
                "content": content,
            }

        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            text = f"{name} was called with arguments that are not valid JSON"
            recorded.append(ToolCallRecord(name, {}, text))
            return answer(f"Error: {text}")
        if not isinstance(arguments, dict):
            text = f"{name} was called with arguments that are not an object"
            recorded.append(ToolCallRecord(name, {}, text))
            return answer(f"Error: {text}")

        if spec is None or calls_left.get(name, 0) <= 0:
            text = (
                f"{name} is not available"
                if spec is None
                else f"{name} has reached its limit of {spec.max_calls} calls"
            )
            recorded.append(ToolCallRecord(name, arguments, text))
            return answer(f"Error: {text}")

        calls_left[name] -= 1
        try:
            result = spec.executor(arguments)
        except Exception as e:  # noqa: BLE001 - a failing tool is an answer, not a crash
            recorded.append(ToolCallRecord(name, arguments, f"error: {e}"))
            return answer(f"Error: {e}")

        source = arguments.get("url") or arguments.get("query") or name
        injection = find_instructions(json.dumps(result, ensure_ascii=False))
        recorded.append(ToolCallRecord(name, arguments, summarise(result), injection, result))
        return answer(data_region(str(source), result))


def _text_of(message: dict[str, Any]) -> str:
    """The text of an answer. Most providers send a string; some send the blocks."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


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
