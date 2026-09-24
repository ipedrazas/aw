"""Picking a run up after it broke, once the cause is fixed: the steps that finished keep
their results and are not asked again, the step that broke runs again whole, and the
run's record says what happened. And a model name that could never work is a question
before the run, not a failure halfway through it."""

from __future__ import annotations

from pathlib import Path

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
    ModelRequest,
    ModelResponse,
    PdfRenderer,
    RecordingSend,
    ScriptedModel,
    Usage,
)
from wf.dryrun import DryRunner
from wf.interpret import RunConfig
from wf.store import StepRun
from wf.validate import validate

DEF = "definitions/deep-research.workflow.yaml"
TOPIC = {"topic": "Durable execution platforms for AI agents: who leads and why"}
JEV = "typesafe/jev-1.13"


class Jev:
    """A decisions model that refuses until it is fixed."""

    def __init__(self) -> None:
        self.fixed = False

    def complete(self, request: ModelRequest) -> ModelResponse:
        if not self.fixed:
            raise ActivityError(
                "the gateway refused the decisions request (400): not a valid model ID"
            )
        return ModelResponse(
            output={
                "verdict": "supports",
                "supports": True,
                "probabilities": {
                    "verdict": {
                        "supports": 1.0,
                        "partly": 0.0,
                        "not_supported": 0.0,
                        "contradicts": 0.0,
                        "unreadable": 0.0,
                    },
                    "supports": 0.94,
                },
            },
            usage=Usage(560, 77, 0.000024),
            model="typesafe/jev-1.13-20260917",
        )


def setup(ws, tmp_path: Path) -> tuple[DryRunner, Jev, list[str]]:
    data = read_yaml(ws, DEF)
    step(data, "check_support")["model"] = JEV
    write_yaml(ws, DEF, data)
    asked: list[str] = []
    play = deep_research_script("accept")

    def script(req: ModelRequest) -> ModelResponse:
        asked.append(req.tag)
        return play(req)

    jev = Jev()
    acts = Activities(
        model=DecisionsRouter(ScriptedModel(script), jev),
        search=FixtureSearch(ws),
        links=FixtureLinkCheck(ws),
        render=PdfRenderer(),
        send=RecordingSend(),
        guesser=HeuristicGuesser(),
        policy=ActivityPolicy(retries=0),
    )
    return DryRunner(ws, acts, make_db(), RunConfig(artifacts_dir=tmp_path / "a")), jev, asked


def broken_run(ws, tmp_path: Path) -> tuple[DryRunner, Jev, list[str], str]:
    runner, jev, asked = setup(ws, tmp_path)
    report = runner.run(
        "deep-research",
        TOPIC,
        expectation=[{"step": "review", "field": "output.verdict", "equals": "accept"}],
    )
    assert report.status == "failed"
    return runner, jev, asked, report.run_id


def test_a_run_that_broke_is_picked_up_at_the_step_that_broke(ws, tmp_path):
    runner, jev, asked, run_id = broken_run(ws, tmp_path)
    assert asked == ["plan", "research", "write"], "it broke at check_support, before review"
    before = runner.snapshot(run_id)
    assert [s["step_id"] for s in before["steps"]][-1] == "check_support"
    assert before["expectations"][0]["matched"] is False

    jev.fixed = True
    asked.clear()
    report = runner.retry(run_id)

    assert report.status == "done", report.error
    assert "plan" not in asked and "research" not in asked and "write" not in asked, (
        "the steps that finished are not asked again"
    )
    assert asked[0] == "review"
    snap = runner.snapshot(run_id)
    ids = [s["step_id"] for s in snap["steps"]]
    assert ids.count("check_support") == 1, "one row per step: its latest attempt"
    row = next(s for s in snap["steps"] if s["step_id"] == "check_support")
    assert row["status"] == "done" and row["attempts"] == 2
    texts = [d["text"] for d in row["decisions"]]
    assert any("could not finish" in t for t in texts), "the attempt that broke stays on record"
    assert any(t.startswith("Picked up from “Check the pages") for t in texts)
    assert snap["expectations"][0]["matched"] is True, "scored again against where it ended"

    with runner.db.session() as s:
        statuses = {
            r.status
            for r in s.query(StepRun).filter_by(run_id=run_id, step_id="check_support").all()
            if r.fanout_index is None
        }
    assert statuses == {"retried", "done"}


def test_the_record_says_when_the_definition_changed_under_the_run(ws, tmp_path):
    runner, jev, _asked, run_id = broken_run(ws, tmp_path)
    data = read_yaml(ws, DEF)
    step(data, "plan")["title"] = "Work out what to look for, and what not to"
    write_yaml(ws, DEF, data)
    jev.fixed = True

    runner.retry(run_id)

    row = next(s for s in runner.snapshot(run_id)["steps"] if s["step_id"] == "check_support")
    said = next(d for d in row["decisions"] if d["text"].startswith("Picked up"))
    assert "has changed since the run began" in said["reason"]
    assert "“Work out what to look for, and what not to”" in said["reason"], (
        "an earlier step that changed is named: its result is from before the change"
    )


def test_only_a_run_that_broke_can_be_picked_up(ws, tmp_path):
    runner, jev, _asked = setup(ws, tmp_path)
    jev.fixed = True
    done = runner.run("deep-research", TOPIC)
    assert done.status == "done"
    with pytest.raises(ValueError, match="did not stop on an error"):
        runner.retry(done.run_id)


# -- the model name -----------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["claude-sonnet-5", "typesafe/jev-1.13", "~typesafe/jev-latest", "anthropic/claude-haiku-4.5"],
)
def test_a_model_name_in_the_right_shape_is_not_questioned(ws, name):
    data = read_yaml(ws, DEF)
    step(data, "check_support")["model"] = name
    write_yaml(ws, DEF, data)
    found = validate(ws.load_definition("deep-research"), ws).findings
    assert not [f for f in found if f.field.endswith(".model")]


@pytest.mark.parametrize("name", ["-typesafe/jev-1.13", "typesafe/ jev", "claude sonnet", "/jev"])
def test_a_model_name_that_could_never_work_is_a_question_before_the_run(ws, name):
    data = read_yaml(ws, DEF)
    step(data, "check_support")["model"] = name
    write_yaml(ws, DEF, data)
    found = validate(ws.load_definition("deep-research"), ws).findings
    f = next(f for f in found if f.field == "steps.check_support.model")
    assert f.type == "conflict" and f.status == "open"
    assert f.question == "Which model should “Check the pages say what the report says” run on?"
    assert f"“{name}” is not a model name" in (f.detail or "")
    assert f.options, "it offers the models this deployment runs"


def test_the_workflows_default_model_is_checked_too(ws):
    data = read_yaml(ws, DEF)
    data["spec"]["defaults"]["model"] = "-claude-sonnet-5"
    write_yaml(ws, DEF, data)
    found = validate(ws.load_definition("deep-research"), ws).findings
    f = next(f for f in found if f.field == "defaults.model")
    assert f.question.startswith("Which model should steps that name no model")


def test_the_run_page_picks_a_broken_run_up_again(ws, tmp_path):
    from fastapi.testclient import TestClient

    from tests.test_api import wait_for
    from wf.api.app import AppState, create_app

    data = read_yaml(ws, DEF)
    step(data, "check_support")["model"] = JEV
    write_yaml(ws, DEF, data)
    jev = Jev()
    state = AppState(
        ws,
        make_db(),
        DecisionsRouter(ScriptedModel(deep_research_script("accept")), jev),
        tmp_path / "artifacts",
    )
    state.runner.activities.policy = ActivityPolicy(retries=0)
    client = TestClient(create_app(state))

    r = client.post("/api/workflows/deep-research/runs", json={"case": "durable-execution"})
    run = wait_for(client, r.json()["run_id"])
    assert run["status"] == "failed"
    page = client.get(f"/runs/{run['id']}").text
    assert "Pick up from where it broke" in page and "Skip it and carry on" in page

    jev.fixed = True
    assert client.post(f"/api/runs/{run['id']}/retry").status_code == 200
    again = wait_for(client, run["id"])
    assert again["status"] == "done", again["error"]
    page = client.get(f"/runs/{run['id']}").text
    assert "Pick up from where it broke" not in page
    assert "Run 2 times: it broke, and was picked up again." in page


# -- carrying on without the step that broke ----------------------------------------


def test_a_step_that_broke_can_be_skipped_and_the_run_carries_on(ws, tmp_path):
    runner, jev, asked, run_id = broken_run(ws, tmp_path)
    asked.clear()

    report = runner.retry(run_id, skip=True)

    assert report.status == "done", report.error
    assert asked[0] == "review", "it carries on from the step after the one that broke"
    assert "check_support" not in " ".join(asked) and not jev.fixed
    row = next(s for s in runner.snapshot(run_id)["steps"] if s["step_id"] == "check_support")
    assert row["status"] == "skipped" and row["attempts"] == 1
    broke = next(d for d in row["decisions"] if d["text"].endswith("could not finish."))
    assert "not a valid model ID" in broke["reason"], "why it broke is kept"
    texts = [d["text"] for d in row["decisions"]]
    assert (
        "Skipped “Check the pages say what the report says” after it broke, and carried on without it."
        in texts
    )


def _go_deeper_runner(ws, tmp_path: Path, followup_breaks: bool) -> DryRunner:
    """Deep research whose reviewer asks for follow-ups, which are started for real."""
    play = deep_research_script("go_deeper", followups=1)

    def script(req: ModelRequest) -> ModelResponse:
        if followup_breaks and req.tag == "plan" and "Follow-up" in str(req.input.get("topic")):
            raise ActivityError("the model declined this request")
        return play(req)

    acts = Activities(
        model=ScriptedModel(script),
        search=FixtureSearch(ws),
        links=FixtureLinkCheck(ws),
        render=PdfRenderer(),
        send=RecordingSend(),
        guesser=HeuristicGuesser(),
        policy=ActivityPolicy(retries=0),
    )
    config = RunConfig(artifacts_dir=tmp_path / "a", subworkflows="run")
    return DryRunner(ws, acts, make_db(), config)


def test_a_follow_up_that_broke_says_where_and_why_and_can_be_skipped(ws, tmp_path):
    runner = _go_deeper_runner(ws, tmp_path, followup_breaks=True)
    report = runner.run("deep-research", TOPIC)
    assert report.status == "failed"

    row = next(s for s in runner.snapshot(report.run_id)["steps"] if s["step_id"] == "go_deeper")
    broke = next(d for d in row["decisions"] if d["text"].endswith("could not finish."))
    assert broke["reason"].startswith("follow-up run “Follow-up 1” stopped: ")
    assert (
        "“Work out what to look for” could not finish: the model declined this request"
        in (broke["reason"])
    ), "the parent says which of the follow-up's steps broke, and why"
    (child,) = row["follow_ups"]
    assert child["title"] == "Follow-up 1" and child["status"] == "failed"
    assert f"(run {child['id'][:8]})" in broke["reason"]

    done = runner.retry(report.run_id, skip=True)
    assert done.status == "done", done.error
    assert [s["status"] for s in done.steps if s["step_id"] == "assemble"] == ["done"], (
        "the report is put together without the follow-up"
    )
