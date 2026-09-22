"""Model activities.

``AnthropicModel`` is the real thing. ``ScriptedModel`` answers from a function, for
tests. ``RecordingModel`` and ``ReplayModel`` capture and replay responses keyed by
request tag, which is how the determinism test runs the interpreter twice over the
same recorded step outputs.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from wf.settings import DEFAULT_CAREFUL, DEFAULT_PRICING, pricing

from .base import ActivityError, ModelRequest, ModelResponse, ToolCallRecord, Usage
from .safety import DATA_RULE, data_region, find_instructions


def cost_of(model: str, input_tokens: int, output_tokens: int) -> float:
    inp, out = pricing().get(model, DEFAULT_PRICING[DEFAULT_CAREFUL])
    return round((input_tokens * inp + output_tokens * out) / 1_000_000, 6)


DECISION_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["decision", "reason", "alternatives"],
        "properties": {
            "decision": {"type": "string", "description": "What you decided, one plain sentence."},
            "reason": {"type": "string", "description": "Why, in one or two plain sentences."},
            "alternatives": {
                "type": "array",
                "items": {"type": "string"},
                "description": "What you considered and did not do.",
            },
        },
    },
}


def envelope_schema(output_schema: dict[str, Any], decisions_required: bool) -> dict[str, Any]:
    """The output the model must produce: the step's output plus its decisions.

    Decisions are enforced by the schema, not parsed from prose.
    """
    out = dict(output_schema)
    out.pop("$schema", None)
    decisions = dict(DECISION_SCHEMA)
    if decisions_required:
        decisions = {**decisions, "minItems": 1}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["output", "decisions"],
        "properties": {"output": out, "decisions": decisions},
    }


def build_system(skill_body: str) -> str:
    return (
        f"{skill_body.strip()}\n\n"
        "## How to answer\n"
        'Reply with one JSON object: {"output": <the step\'s output>, "decisions": [...]}. '
        "Decisions are written for the person running this, in plain sentences: what you decided, why, and what else you considered.\n"
        f"{DATA_RULE}"
    )


class AnthropicModel:
    def __init__(self, client: Any | None = None, max_tool_rounds: int = 60):
        self._client = client
        self.max_tool_rounds = max_tool_rounds

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def complete(self, request: ModelRequest) -> ModelResponse:
        schema = envelope_schema(request.output_schema, request.decisions_required)
        tools = [
            {
                "name": t.name,
                "description": t.description,
                "strict": True,
                "input_schema": t.input_schema,
            }
            for t in request.tools
        ]
        executors = {t.name: t for t in request.tools}
        calls_left = {t.name: t.max_calls for t in request.tools}
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": data_region("step input", request.input)}
        ]
        usage = Usage()
        tool_calls: list[ToolCallRecord] = []

        for _ in range(self.max_tool_rounds):
            kwargs: dict[str, Any] = {
                "model": request.model,
                "max_tokens": request.max_tokens,
                "system": build_system(request.system),
                "messages": messages,
                "output_config": {"format": {"type": "json_schema", "schema": schema}},
            }
            if tools:
                kwargs["tools"] = tools
            response = self.client.messages.create(**kwargs)
            usage.input_tokens += getattr(response.usage, "input_tokens", 0)
            usage.output_tokens += getattr(response.usage, "output_tokens", 0)

            if response.stop_reason == "refusal":
                raise ActivityError("the model declined this request")
            if response.stop_reason == "max_tokens":
                raise ActivityError("the model ran out of room before finishing its answer")

            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    spec = executors.get(block.name)
                    if spec is None or calls_left.get(block.name, 0) <= 0:
                        text = (
                            f"{block.name} is not available"
                            if spec is None
                            else f"{block.name} has reached its limit of {spec.max_calls} calls"
                        )
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": text,
                                "is_error": True,
                            }
                        )
                        tool_calls.append(ToolCallRecord(block.name, dict(block.input), text))
                        continue
                    calls_left[block.name] -= 1
                    try:
                        result = spec.executor(dict(block.input))
                    except Exception as e:  # noqa: BLE001
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": f"Error: {e}",
                                "is_error": True,
                            }
                        )
                        tool_calls.append(
                            ToolCallRecord(block.name, dict(block.input), f"error: {e}")
                        )
                        continue
                    source = block.input.get("url") or block.input.get("query") or block.name
                    text = json.dumps(result, ensure_ascii=False)
                    injection = find_instructions(text)
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": data_region(str(source), result),
                        }
                    )
                    tool_calls.append(
                        ToolCallRecord(
                            block.name, dict(block.input), _summarise(result), injection, result
                        )
                    )
                messages.append({"role": "user", "content": results})
                continue

            text = next((b.text for b in response.content if b.type == "text"), None)
            if text is None:
                raise ActivityError("the model returned no answer")
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                raise ActivityError(f"the model's answer was not valid JSON: {e}") from e
            usage.cost_usd = cost_of(request.model, usage.input_tokens, usage.output_tokens)
            return ModelResponse(
                output=data.get("output", {}),
                decisions=list(data.get("decisions", [])),
                usage=usage,
                tool_calls=tool_calls,
                model=request.model,
            )
        raise ActivityError("the model kept calling tools past the round limit")


def _summarise(result: Any) -> str:
    if isinstance(result, list):
        return f"{len(result)} results"
    if isinstance(result, dict):
        if "title" in result:
            return str(result["title"])[:120]
        return ", ".join(list(result)[:5])
    return str(result)[:120]


class ScriptedModel:
    """Answers from a function of the request. Tools are executed if the script asks."""

    def __init__(self, script: Callable[[ModelRequest], ModelResponse]):
        self.script = script
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        resp = self.script(request)
        if not resp.model:
            resp.model = request.model
        return resp


class RecordingModel:
    def __init__(self, inner: Any):
        self.inner = inner
        self.records: list[dict[str, Any]] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        resp = self.inner.complete(request)
        self.records.append({"tag": request.tag, "response": resp.to_json()})
        return resp


class ReplayModel:
    """Replays recorded responses in order, checking the tag matches the request."""

    def __init__(self, records: list[dict[str, Any]]):
        self.records = list(records)
        self.i = 0

    def complete(self, request: ModelRequest) -> ModelResponse:
        if self.i >= len(self.records):
            raise ActivityError(f"no recorded response left for {request.tag}")
        rec = self.records[self.i]
        self.i += 1
        if rec["tag"] != request.tag:
            raise ActivityError(
                f"replay mismatch: expected {rec['tag']}, interpreter asked for {request.tag}"
            )
        return ModelResponse.from_json(rec["response"])
