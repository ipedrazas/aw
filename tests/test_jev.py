"""Jev, the decisions model, behind the same model activity as Claude: the questions
come from the step's output schema, the answers go back into it, and a step that names
a ``typesafe/`` model is the only one that reaches the Decisions endpoint."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.helpers import make_db, read_yaml, step, write_yaml
from tests.scripted import deep_research_script
from wf.activities import (
    Activities,
    ActivityError,
    ActivityPolicy,
    DecisionsRouter,
    FixtureLinkCheck,
    FixtureSearch,
    HeuristicGuesser,
    JevModel,
    ModelRequest,
    ModelResponse,
    PdfRenderer,
    RecordingSend,
    ScriptedModel,
    ToolSpec,
)
from wf.activities.jev import questions_from_schema
from wf.activities.models import envelope_schema, for_text_model
from wf.interpret import Interpreter, RunConfig
from wf.interpret.registry import RunnerContext, fetch_pages
from wf.store import StepRun
from wf.store.ledger import Ledger

DEF = "definitions/deep-research.workflow.yaml"
JEV = "typesafe/jev-1.13"
VERDICTS = ["supports", "partly", "not_supported", "contradicts", "unreadable"]


def claim_support_schema(ws) -> dict[str, Any]:
    return ws.load_schema("schemas/claim_support.json")


def decisions_answer(verdict: str = "partly", p_yes: float = 0.03) -> dict[str, Any]:
    """What the Decisions endpoint returns, in the shape its docs and our first call show."""
    return {
        "id": "gen-dec-1",
        "model": "typesafe/jev-1.13-20260917",
        "provider": "TypeSafe",
        "answers": {
            "verdict": {
                "type": "choice",
                "choice": verdict,
                "confidence": 0.99,
                "probabilities": {v: (1.0 if v == verdict else 0.0) for v in VERDICTS},
            },
            "supports": {"type": "noul", "noul": p_yes},
        },
        "usage": {"input_tokens": 567, "output_tokens": 77, "cost": 0.000023814},
    }


def jev(handler) -> JevModel:
    client = httpx.Client(
        base_url="https://openrouter.ai/api", transport=httpx.MockTransport(handler)
    )
    return JevModel(client=client)


def request(ws, **kw: Any) -> ModelRequest:
    return ModelRequest(
        tag="check_support[0]",
        model=JEV,
        system="These instructions are not sent to a decisions model.",
        input={"claim": "Journey times fell by 7 minutes, and fares were cut.", "page": "..."},
        output_schema=claim_support_schema(ws),
        **kw,
    )


# -- the questions, from the schema ----------------------------------------------


def test_the_questions_are_read_from_the_output_schema(sample_ws):
    q = questions_from_schema(claim_support_schema(sample_ws))
    assert set(q) == {"verdict", "supports"}, "probabilities is filled, not asked"
    assert q["verdict"]["type"] == "choice"
    assert q["verdict"]["instructions"] == "Does the page support the claim?"
    assert list(q["verdict"]["criteria"]) == VERDICTS
    assert q["verdict"]["criteria"]["partly"].startswith("The page supports part")
    assert q["supports"]["type"] == "noul"
    assert set(q["supports"]["criteria"]) == {"true", "false"}


def test_a_question_jev_cannot_answer_is_refused_not_guessed():
    schema = {"type": "object", "properties": {"reason": {"type": "string"}}}
    with pytest.raises(ActivityError, match="“reason”"):
        questions_from_schema(schema)


def test_a_text_model_reads_the_same_criteria_in_its_descriptions(sample_ws):
    shown = for_text_model(claim_support_schema(sample_ws))
    assert "x-criteria" not in json.dumps(shown), "our keys are not sent to a text model"
    verdict = shown["properties"]["verdict"]["description"]
    assert verdict.startswith("Does the page support the claim?")
    assert "- partly: The page supports part of the claim" in verdict
    assert "- true: Every part of the claim" in shown["properties"]["supports"]["description"]
    # and the envelope every text model is asked in is built from it
    env = envelope_schema(claim_support_schema(sample_ws), decisions_required=False)
    assert "x-criteria" not in json.dumps(env)


# -- the call ---------------------------------------------------------------------


def test_jev_is_asked_on_the_decisions_endpoint_and_answers_in_the_steps_shape(sample_ws):
    seen: list[httpx.Request] = []

    def served(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return httpx.Response(200, json=decisions_answer("partly", 0.03))

    resp = jev(served).complete(request(sample_ws))

    assert seen[0].url.path == "/api/alpha/decisions"
    body = json.loads(seen[0].content)
    assert body["model"] == JEV
    assert body["state"]["claim"].startswith("Journey times fell")
    assert set(body["questions"]) == {"verdict", "supports"}
    assert "system" not in body and "messages" not in body, "Jev takes no instructions"

    assert resp.output["verdict"] == "partly"
    assert resp.output["supports"] is False
    assert resp.output["probabilities"]["verdict"]["partly"] == 1.0
    assert resp.output["probabilities"]["supports"] == 0.03
    assert resp.decisions == [], "Jev gives no reasons, and none are made up for it"
    assert resp.usage.cost_usd == pytest.approx(0.000023814)
    assert resp.model == "typesafe/jev-1.13-20260917"

    import jsonschema

    jsonschema.validate(resp.output, claim_support_schema(sample_ws))


def test_a_refusal_is_final_and_a_busy_gateway_is_retried(sample_ws):
    refused = jev(lambda r: httpx.Response(400, json={"error": {"message": "bad question"}}))
    with pytest.raises(ActivityError, match="bad question"):
        refused.complete(request(sample_ws))

    busy = jev(lambda r: httpx.Response(429, json={"error": {"message": "slow down"}}))
    with pytest.raises(httpx.HTTPStatusError):  # not an ActivityError: the policy retries it
        busy.complete(request(sample_ws))


def test_a_step_with_tools_cannot_run_on_a_decisions_model(sample_ws):
    tool = ToolSpec("search", "Search.", {"type": "object"}, lambda i: [])
    with pytest.raises(ActivityError, match="cannot use tools"):
        jev(lambda r: httpx.Response(200)).complete(request(sample_ws, tools=[tool]))


def test_only_a_decisions_model_is_routed_to_jev(sample_ws):
    asked: list[str] = []

    def inner(req: ModelRequest) -> ModelResponse:
        asked.append(req.model)
        return ModelResponse(output={})

    router = DecisionsRouter(
        ScriptedModel(inner), jev(lambda r: httpx.Response(200, json=decisions_answer()))
    )
    router.complete(request(sample_ws))
    assert asked == []
    router.complete(ModelRequest("plan", "claude-sonnet-5", "", {}, {"type": "object"}))
    assert asked == ["claude-sonnet-5"]


# -- the pages it reads -----------------------------------------------------------


def test_fetch_pages_reads_what_opened_and_says_what_it_could_not(sample_ws, tmp_path):
    notes: list[tuple[str, str]] = []
    ctx = RunnerContext(
        activities=Activities(
            model=ScriptedModel(lambda r: ModelResponse(output={})),
            search=FixtureSearch(sample_ws),
            links=FixtureLinkCheck(sample_ws),
            render=PdfRenderer(),
            send=RecordingSend(),
            guesser=HeuristicGuesser(),
        ),
        mode="dry",
        run_id="r",
        step_id="fetch_pages",
        artifacts_dir=tmp_path,
        note=lambda text, reason: notes.append((text, reason)),
    )
    recorded = "https://docs.temporal.io/evaluate/durable-execution-for-agents"
    out = fetch_pages(
        ctx,
        {
            "sources": [
                {"line": 3, "claim": "A", "url": recorded, "link_opens": True, "status": 200},
                {"line": 5, "claim": "B", "url": "https://gone.example", "link_opens": False},
                {
                    "line": 7,
                    "claim": "C",
                    "url": "https://not-recorded.example",
                    "link_opens": True,
                },
            ]
        },
    )
    assert [s["url"] for s in out["sources"]] == [recorded]
    assert out["sources"][0]["page"].startswith("Temporal describes durable execution")
    assert {u["url"]: u["reason"] for u in out["unread"]} == {
        "https://gone.example": "the link did not open",
        "https://not-recorded.example": "This page is not in the recorded fixtures.",
    }
    assert (out["read_count"], out["total"]) == (1, 3)
    assert notes and "2 of 3" in notes[0][0]


# -- the step, end to end ---------------------------------------------------------


def test_deep_research_checks_every_page_it_read_on_jev(ws, tmp_path: Path):
    data = read_yaml(ws, DEF)
    step(data, "check_support")["model"] = JEV
    write_yaml(ws, DEF, data)

    states: list[dict[str, Any]] = []

    def served(r: httpx.Request) -> httpx.Response:
        states.append(json.loads(r.content)["state"])
        return httpx.Response(200, json=decisions_answer("supports", 0.94))

    db = make_db()
    model = DecisionsRouter(ScriptedModel(deep_research_script("accept")), jev(served))
    acts = Activities(
        model=model,
        search=FixtureSearch(ws),
        links=FixtureLinkCheck(ws),
        render=PdfRenderer(),
        send=RecordingSend(),
        guesser=HeuristicGuesser(),
        policy=ActivityPolicy(retries=0),
    )
    interp = Interpreter(ws, acts, Ledger(db), RunConfig(artifacts_dir=tmp_path / "artifacts"))
    result = interp.run(
        ws.load_definition("deep-research"),
        {"topic": "Durable execution platforms for AI agents: who leads and why"},
        "dry",
    )
    assert result.status == "done", [d for d in result.decisions if d["kind"] == "control"]

    checked = next(t for t in result.trace if t.step_id == "check_support").detail
    assert checked > 0 and len(states) == checked, "one Decisions call per page read"
    assert all(s["claim"] and s["page"] for s in states), "each call carries the claim and its page"

    with db.session() as s:
        items = (
            s.query(StepRun)
            .filter(StepRun.step_id == "check_support", StepRun.fanout_index.isnot(None))
            .all()
        )
    assert len(items) == checked
    assert all(i.model == "typesafe/jev-1.13-20260917" for i in items)
    assert all(i.output["verdict"] == "supports" for i in items)
    assert all(i.output["probabilities"]["supports"] == 0.94 for i in items)
