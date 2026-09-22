"""OpenRouter is a second way of asking, not a second set of rules.

The gateway is never called here. A stub stands in for it, which is enough to say what
goes up, what comes back, and which failures are worth trying again.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from wf import settings
from wf.activities import ActivityError, ModelRequest, OpenRouterModel, ToolSpec, default_model
from wf.activities.fake import FakeModel
from wf.activities.models import AnthropicModel

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict"],
    "properties": {"verdict": {"type": "string"}},
}


class Stream:
    """What the gateway sends back, as httpx hands a streamed answer over.

    A status and a run of lines, plus the short body a refusal has instead of a stream.
    """

    def __init__(self, lines: list[str] | None = None, *, status: int = 200, body: Any = None):
        self.lines = lines or []
        self.status_code = status
        self.body = body
        self.text = json.dumps(body) if body is not None else ""

    def __enter__(self) -> Stream:
        return self

    def __exit__(self, *_: Any) -> bool:
        return False

    def read(self) -> None:
        """Pull in the body of a refusal, which is not streamed."""

    def json(self) -> Any:
        if self.body is None:
            raise ValueError("the stream is not a JSON document")
        return self.body

    def iter_lines(self) -> Any:
        yield from self.lines

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise ConnectionResetError(f"HTTP {self.status_code}")


class Gateway:
    """A stub of the one method the model uses, keeping what it was asked."""

    def __init__(self, *replies: Stream):
        self.replies = list(replies)
        self.asked: list[dict[str, Any]] = []

    def stream(self, method: str, path: str, json: dict[str, Any]) -> Stream:  # noqa: A002
        assert (method, path) == ("POST", "/chat/completions")
        self.asked.append(json)
        return self.replies.pop(0)


def event(**chunk: Any) -> str:
    return "data: " + json.dumps(chunk)


def in_pieces(text: str, every: int = 7) -> list[str]:
    """A long answer does not arrive in one go, so neither does it here."""
    return [text[i : i + every] for i in range(0, len(text), every)] or [""]


def answer(
    content: str, *, model: str = "openai/some-model", cost: float | None = None, **usage: Any
) -> Stream:
    spent = {
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
    }
    if cost is not None:
        spent["cost"] = cost
    return Stream(
        [
            event(model=model, choices=[{"delta": {"role": "assistant"}}]),
            ": OPENROUTER PROCESSING",  # the gateway saying it is still there
            *[event(choices=[{"delta": {"content": piece}}]) for piece in in_pieces(content)],
            event(choices=[{"delta": {}, "finish_reason": "stop"}]),
            event(choices=[], usage=spent),  # what it cost comes last, on its own
            "data: [DONE]",
        ]
    )


def envelope(output: dict[str, Any], decisions: list[dict[str, Any]] | None = None) -> str:
    return json.dumps({"output": output, "decisions": decisions or []})


def request(**kw: Any) -> ModelRequest:
    return ModelRequest(
        tag=kw.pop("tag", "step:review"),
        model=kw.pop("model", "openai/some-model"),
        system=kw.pop("system", "Review the draft."),
        input=kw.pop("input", {"draft": "a draft"}),
        output_schema=kw.pop("output_schema", SCHEMA),
        **kw,
    )


def test_the_question_carries_the_schema_and_the_answer_is_the_envelope():
    gateway = Gateway(
        answer(
            envelope(
                {"verdict": "accept"},
                [{"decision": "kept it", "reason": "clean", "alternatives": []}],
            )
        )
    )
    resp = OpenRouterModel(gateway).complete(request())

    sent = gateway.asked[0]
    assert sent["model"] == "openai/some-model"
    assert [m["role"] for m in sent["messages"]] == ["system", "user"]
    assert '<data source="step input">' in sent["messages"][1]["content"]
    schema = sent["response_format"]["json_schema"]["schema"]
    assert schema["required"] == ["output", "decisions"]
    assert schema["properties"]["output"]["properties"]["verdict"]["type"] == "string"
    assert "tools" not in sent, "no tools offered, none declared"

    assert resp.output == {"verdict": "accept"}
    assert resp.decisions[0]["decision"] == "kept it"


def test_the_model_that_answered_is_the_one_recorded():
    """A name can route to a provider's own; the session should say who answered."""
    gateway = Gateway(answer(envelope({"verdict": "accept"}), model="openai/some-model:exact"))
    resp = OpenRouterModel(gateway).complete(request(model="openai/some-model"))
    assert resp.model == "openai/some-model:exact"


def test_what_the_gateway_says_it_cost_is_what_is_kept():
    gateway = Gateway(
        answer(
            envelope({"verdict": "accept"}), cost=0.0123, prompt_tokens=1000, completion_tokens=200
        )
    )
    resp = OpenRouterModel(gateway).complete(request())
    assert (resp.usage.input_tokens, resp.usage.output_tokens) == (1000, 200)
    assert resp.usage.cost_usd == 0.0123, "the gateway routed it, so the gateway knows"


def test_without_a_reported_cost_the_price_table_estimates_it(monkeypatch):
    monkeypatch.delenv("WF_MODEL_PRICING", raising=False)
    gateway = Gateway(answer(envelope({"verdict": "accept"}), prompt_tokens=1_000_000))
    resp = OpenRouterModel(gateway).complete(request(model=settings.OPENROUTER_QUICK))
    assert resp.usage.cost_usd == settings.DEFAULT_PRICING[settings.OPENROUTER_QUICK][0]


def test_a_tool_call_is_run_and_answered_in_the_shape_the_gateway_reads():
    searched: list[dict[str, Any]] = []
    tool = ToolSpec(
        name="search",
        description="Search the web.",
        input_schema={
            "type": "object",
            "required": ["query"],
            "properties": {"query": {"type": "string"}},
        },
        executor=lambda i: searched.append(i) or [{"title": "A page", "url": "https://x.example"}],
        max_calls=1,
    )
    # The call arrives in pieces, which is the whole of what streaming changes here:
    # the id and the name first, then the arguments a few characters at a time.
    asked_for_tool = Stream(
        [
            event(
                model="openai/some-model",
                choices=[
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "search", "arguments": '{"query": '},
                                }
                            ]
                        }
                    }
                ],
            ),
            event(
                choices=[
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '"durable execution"}'},
                                }
                            ]
                        }
                    }
                ]
            ),
            event(choices=[{"delta": {}, "finish_reason": "tool_calls"}]),
            event(choices=[], usage={"prompt_tokens": 10, "completion_tokens": 5}),
            "data: [DONE]",
        ]
    )
    gateway = Gateway(asked_for_tool, answer(envelope({"verdict": "accept"})))
    resp = OpenRouterModel(gateway).complete(request(tools=[tool]))

    assert searched == [{"query": "durable execution"}]
    second = gateway.asked[1]["messages"]
    assert second[2]["role"] == "assistant" and second[2]["tool_calls"]
    assert second[3]["role"] == "tool" and second[3]["tool_call_id"] == "call_1"
    assert '<data source="durable execution">' in second[3]["content"]
    assert gateway.asked[0]["tools"][0]["function"]["name"] == "search"

    assert [c.name for c in resp.tool_calls] == ["search"]
    assert resp.tool_calls[0].result == [{"title": "A page", "url": "https://x.example"}]


def test_a_tool_past_its_limit_is_told_so_rather_than_run():
    runs: list[dict[str, Any]] = []
    tool = ToolSpec(
        name="search",
        description="Search the web.",
        input_schema={"type": "object", "properties": {}},
        executor=lambda i: runs.append(i),
        max_calls=0,
    )
    call = Stream(
        [
            event(
                choices=[
                    {
                        "finish_reason": "tool_calls",
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "search", "arguments": "{}"},
                                }
                            ]
                        },
                    }
                ]
            ),
            "data: [DONE]",
        ]
    )
    gateway = Gateway(call, answer(envelope({"verdict": "accept"})))
    resp = OpenRouterModel(gateway).complete(request(tools=[tool]))
    assert runs == [], "the limit is ours to keep, not the model's"
    assert "limit" in gateway.asked[1]["messages"][-1]["content"]
    assert "limit" in resp.tool_calls[0].summary


@pytest.mark.parametrize(
    ("reason", "says"),
    [("content_filter", "declined"), ("length", "ran out of room")],
)
def test_an_answer_that_never_arrives_says_why(reason: str, says: str):
    stopped = Stream(
        [
            event(choices=[{"delta": {"content": '{"outp'}}]),
            event(choices=[{"delta": {}, "finish_reason": reason}]),
            "data: [DONE]",
        ]
    )
    with pytest.raises(ActivityError, match=says):
        OpenRouterModel(Gateway(stopped)).complete(request())


def test_prose_instead_of_json_is_an_activity_error():
    with pytest.raises(ActivityError, match="not valid JSON"):
        OpenRouterModel(Gateway(answer("Sure! Here is my review."))).complete(request())


def test_a_refusal_is_final_and_a_bad_moment_is_not():
    """An ActivityError is not retried; anything else is, which is the difference
    between the gateway saying no and the gateway having a bad minute."""
    refused = Stream(status=402, body={"error": {"message": "no credits"}})
    with pytest.raises(ActivityError, match="refused"):
        OpenRouterModel(Gateway(refused)).complete(request())

    # A provider that falls over partway through says so in a chunk, not a status.
    fell_over = Stream(
        [
            event(choices=[{"delta": {"content": "{"}}]),
            event(error={"message": "upstream fell over"}),
        ]
    )
    with pytest.raises(ActivityError, match="could not answer"):
        OpenRouterModel(Gateway(fell_over)).complete(request())

    with pytest.raises(Exception) as caught:
        OpenRouterModel(Gateway(Stream(status=503, body={}))).complete(request())
    assert not isinstance(caught.value, ActivityError), "a 503 is worth asking again"


def test_the_answer_is_streamed_and_what_it_cost_is_asked_for():
    """Streaming is not for showing the answer arriving; it is so a long one may be
    asked for at all. A buffered answer has to land inside one read."""
    gateway = Gateway(answer(envelope({"verdict": "accept"})))
    OpenRouterModel(gateway).complete(request())

    sent = gateway.asked[0]
    assert sent["stream"] is True
    assert sent["stream_options"] == {"include_usage": True}
    assert sent["usage"] == {"include": True}, "a stream says what it cost only if asked"


def test_how_much_room_an_answer_has_is_the_environments_to_say(monkeypatch):
    """Which model is behind a name here is configuration, and their ceilings differ."""
    monkeypatch.delenv("WF_MAX_OUTPUT_TOKENS", raising=False)
    assert request().max_tokens == settings.DEFAULT_MAX_OUTPUT_TOKENS

    monkeypatch.setenv("WF_MAX_OUTPUT_TOKENS", "100000")
    gateway = Gateway(answer(envelope({"verdict": "accept"})))
    OpenRouterModel(gateway).complete(request())
    assert gateway.asked[0]["max_tokens"] == 100_000

    monkeypatch.setenv("WF_MAX_OUTPUT_TOKENS", "as much as it likes")
    assert settings.max_output_tokens() == settings.DEFAULT_MAX_OUTPUT_TOKENS, "not a number"


def test_no_key_is_said_plainly_rather_than_dialled(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ActivityError, match="OPENROUTER_API_KEY"):
        OpenRouterModel().complete(request())


# -- the vendor, asked the same way -------------------------------------------


class Vendor:
    """A stub of the vendor client, down to the one call the model makes of it."""

    def __init__(self, text: str):
        self.reply = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=10, output_tokens=20),
        )
        self.asked: list[dict[str, Any]] = []
        self.messages = self

    def stream(self, **kwargs: Any) -> Vendor:
        self.asked.append(kwargs)
        return self

    def __enter__(self) -> Vendor:
        return self

    def __exit__(self, *_: Any) -> bool:
        return False

    def get_final_message(self) -> Any:
        return self.reply


def test_the_vendor_is_streamed_too_and_the_answer_waited_for(monkeypatch):
    """Both providers stream for the same reason, and neither shows it to anyone."""
    monkeypatch.delenv("WF_MAX_OUTPUT_TOKENS", raising=False)
    vendor = Vendor(envelope({"verdict": "accept"}))
    resp = AnthropicModel(vendor).complete(request(model=settings.DEFAULT_CAREFUL))

    assert vendor.asked[0]["max_tokens"] == settings.DEFAULT_MAX_OUTPUT_TOKENS
    assert resp.output == {"verdict": "accept"}
    assert (resp.usage.input_tokens, resp.usage.output_tokens) == (10, 20)


# -- which provider, and which names ------------------------------------------


def clear(monkeypatch) -> None:
    for var in ("WF_MODEL_PROVIDER", "WF_FAKE_MODEL", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_the_provider_is_named_or_inferred_from_the_key_that_is_there(monkeypatch):
    clear(monkeypatch)
    assert settings.provider() == settings.ANTHROPIC, "the vendor unless told otherwise"

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-x")
    assert settings.provider() == settings.OPENROUTER, "one key in the environment says enough"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    assert settings.provider() == settings.ANTHROPIC, "two keys, so it has to be said"

    monkeypatch.setenv("WF_MODEL_PROVIDER", "OpenRouter")
    assert settings.provider() == settings.OPENROUTER, "saying it wins, however it is cased"


def test_the_defaults_are_the_same_models_under_the_name_the_provider_uses(monkeypatch):
    clear(monkeypatch)
    for var in ("WF_QUICK_MODEL", "WF_CAREFUL_MODEL", "WF_EXTRACTION_MODEL", "WF_GUESS_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("WF_MODEL_PROVIDER", "openrouter")

    assert settings.quick_model() == settings.OPENROUTER_QUICK
    assert settings.careful_model() == settings.OPENROUTER_CAREFUL
    assert settings.extraction_model() == settings.OPENROUTER_CAREFUL, "still careful work"

    monkeypatch.setenv("WF_QUICK_MODEL", "openai/gpt-5")
    assert settings.quick_model() == "openai/gpt-5", "any model the gateway carries"
    assert settings.guess_model() == "openai/gpt-5"


def test_the_provider_decides_which_model_activity_is_built(monkeypatch):
    clear(monkeypatch)
    assert isinstance(default_model(), AnthropicModel)

    monkeypatch.setenv("WF_MODEL_PROVIDER", "openrouter")
    assert isinstance(default_model(), OpenRouterModel)

    monkeypatch.setenv("WF_FAKE_MODEL", "1")
    assert isinstance(default_model(), FakeModel), "offline beats every provider"

    monkeypatch.delenv("WF_FAKE_MODEL")
    monkeypatch.setenv("WF_MODEL_PROVIDER", "some-other-gateway")
    with pytest.raises(ActivityError, match="not a provider"):
        default_model()
