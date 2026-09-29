"""The chat can answer questions about a workflow's runs — status, where a run
stopped and why, and what a step decided — fetched through a tool call when a
question needs it, rather than sent to the model on every turn (TAV-208)."""

from __future__ import annotations

from pathlib import Path

from tests.helpers import make_db
from tests.scripted import deep_research_script
from tests.test_api import client, wait_for  # noqa: F401 - the fixtures
from tests.test_audit import DOC
from wf.activities import (
    Activities,
    ActivityPolicy,
    FixtureLinkCheck,
    FixtureSearch,
    HeuristicGuesser,
    ModelResponse,
    PdfRenderer,
    RecordingSend,
    ScriptedModel,
    Usage,
)
from wf.audit.chat import _runs_tools, chat
from wf.audit.service import AuditResult
from wf.dryrun import DryRunner
from wf.interpret import RunConfig

TOPIC = {"topic": "Durable execution platforms for AI agents: who leads and why"}


def _runner(ws, tmp_path: Path, model) -> DryRunner:
    acts = Activities(
        model=model,
        search=FixtureSearch(ws),
        links=FixtureLinkCheck(ws),
        render=PdfRenderer(),
        send=RecordingSend(),
        guesser=HeuristicGuesser(),
        policy=ActivityPolicy(retries=0),
    )
    return DryRunner(ws, acts, make_db(), RunConfig(artifacts_dir=tmp_path / "artifacts"))


def _tools(runner: DryRunner, workflow: str = "deep-research") -> dict:
    return {t.name: t for t in _runs_tools(runner, workflow)}


def _result(name: str = "deep-research") -> AuditResult:
    return AuditResult(
        name=name, title=name, passages=[], definition={}, provenance={}, findings=[]
    )


def test_recent_runs_lists_the_workflow_s_runs_newest_first(sample_ws, tmp_path):
    runner = _runner(sample_ws, tmp_path, ScriptedModel(deep_research_script("accept")))
    report = runner.run("deep-research", TOPIC, mode="dry")
    assert report.status == "done"

    out = _tools(runner)["recent_runs"].executor({})
    assert out[0]["id"] == report.run_id
    assert out[0]["status"] == "done"
    assert {s["step_id"] for s in out[0]["steps"]} >= {"plan", "research", "write"}


def test_run_details_carries_what_a_step_decided(sample_ws, tmp_path):
    runner = _runner(sample_ws, tmp_path, ScriptedModel(deep_research_script("accept")))
    report = runner.run("deep-research", TOPIC, mode="dry")

    detail = _tools(runner)["run_details"].executor({"run_id": report.run_id})
    assert detail["status"] == "done"
    research = next(s for s in detail["steps"] if s["step_id"] == "research")
    assert any(
        d["kind"] == "decision" and "Stopped at 3 of 25 searches" in d["text"]
        for d in research["decisions"]
    ), "the step's own decision, in plain sentences, reaches the chat"


def test_run_details_says_where_a_run_paused_and_why(sample_ws, tmp_path):
    def pricey(req):
        resp = deep_research_script("accept")(req)
        return ModelResponse(
            output=resp.output,
            decisions=resp.decisions,
            usage=Usage(1, 1, 7.0),
            tool_calls=resp.tool_calls,
        )

    runner = _runner(sample_ws, tmp_path, ScriptedModel(pricey))
    report = runner.run("deep-research", TOPIC, mode="dry")
    assert report.status == "paused_budget"

    detail = _tools(runner)["run_details"].executor({"run_id": report.run_id})
    assert detail["status"] == "paused_budget"
    stopped = detail["stopped"]
    assert stopped["text"].startswith("Paused before") and stopped["reason"]


def test_run_details_on_an_unknown_run_says_so_instead_of_raising(sample_ws, tmp_path):
    runner = _runner(sample_ws, tmp_path, ScriptedModel(deep_research_script("accept")))
    out = _tools(runner)["run_details"].executor({"run_id": "no-such-run"})
    assert "error" in out


def test_the_chat_is_not_offered_the_runs_tools_without_a_runs_accessor():
    shown: list[list[str]] = []

    def script(req):
        shown.append([t.name for t in req.tools])
        return ModelResponse(output={"reply": "ok", "edits": [], "answers": [], "dismiss": []})

    chat(ScriptedModel(script), _result(), [], "hello")
    assert shown == [[]]


def test_the_chat_is_offered_the_runs_tools_when_a_runs_accessor_is_given(sample_ws, tmp_path):
    runner = _runner(sample_ws, tmp_path, ScriptedModel(deep_research_script("accept")))
    shown: list[set[str]] = []

    def script(req):
        shown.append({t.name for t in req.tools})
        assert "runs" not in req.input, "runs are fetched through a tool, not sent every turn"
        return ModelResponse(output={"reply": "ok", "edits": [], "answers": [], "dismiss": []})

    chat(ScriptedModel(script), _result(), [], "hello", runs=runner)
    assert shown == [{"recent_runs", "run_details"}]


def test_the_chat_can_answer_a_question_about_a_run_through_the_app(client):  # noqa: F811
    r = client.post("/api/workflows/deep-research/runs", json={"case": "durable-execution"})
    run = wait_for(client, r.json()["run_id"])
    assert run["status"] == "done", run["error"]

    audit = client.post(
        "/api/audits", json={"document": DOC.read_text(), "name": "deep-research"}
    ).json()
    body = client.post(
        f"/api/audits/{audit['id']}/chat", json={"message": "how did the last run go?"}
    ).json()
    reply = next(m for m in reversed(body["chat"]) if m["role"] == "assistant" and m["text"])
    assert reply["text"] == f"status={run['status']}"
