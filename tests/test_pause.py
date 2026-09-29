"""Pausing a real run, and carrying it on: it stops after the step it is on finishes,
never mid-step, and carries on from where it paused without running finished steps
again — the same way a run stopped at a gate carries on (#44). A run that hit its
spending limit carries on the same way too."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import make_db, read_yaml, write_yaml
from tests.scripted import deep_research_script
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
from wf.interpret import Interpreter, RunConfig
from wf.store import Run, StepRun
from wf.store.ledger import Ledger

DEF = "definitions/deep-research.workflow.yaml"
TOPIC = {"topic": "Durable execution platforms for AI agents: who leads and why"}


def _pausing_interpreter(ws, tmp_path: Path, db, *, pause_on: str) -> Interpreter:
    """An interpreter whose model asks, on another session, for the run to pause while
    ``pause_on`` is in progress — as a real pause request would land mid-step."""
    play = deep_research_script("accept")

    def script(req):
        if req.tag == pause_on:
            with db.session() as s:
                s.query(Run).filter_by(status="running").one().pause_requested = True
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
    return Interpreter(ws, acts, Ledger(db), RunConfig(artifacts_dir=tmp_path / "a"))


def test_pausing_stops_after_the_step_in_progress_and_never_mid_step(sample_ws, tmp_path):
    db = make_db()
    interp = _pausing_interpreter(sample_ws, tmp_path, db, pause_on="research")
    wf = sample_ws.load_definition("deep-research")

    r = interp.run(wf, TOPIC, "dry")
    assert r.status == "paused"
    assert any(t.event == "paused" and t.detail == "requested" for t in r.trace)

    with db.session() as s:
        steps = {
            row.step_id: row.status
            for row in s.query(StepRun).filter_by(run_id=r.run_id).order_by(StepRun.seq).all()
        }
    assert steps == {"plan": "done", "research": "done"}, (
        "the step in progress when it was asked to pause finished whole; nothing after it started"
    )


def test_carrying_a_paused_run_on_does_not_run_finished_steps_again(sample_ws, tmp_path):
    db = make_db()
    interp = _pausing_interpreter(sample_ws, tmp_path, db, pause_on="research")
    wf = sample_ws.load_definition("deep-research")
    r = interp.run(wf, TOPIC, "dry")
    assert r.status == "paused"

    run = interp.ledger.session.get(Run, r.run_id)
    done = interp.carry_on_paused(run, wf)
    assert done.status == "done", done.error
    assert any(d["text"].startswith("Carried on from") for d in done.decisions)

    with db.session() as s:
        ids = [
            row.step_id
            for row in s.query(StepRun).filter_by(run_id=r.run_id).order_by(StepRun.seq).all()
        ]
    assert ids.count("plan") == 1 and ids.count("research") == 1, (
        "the steps that finished before the pause are not run again"
    )


def test_only_a_paused_run_can_be_carried_on(sample_ws, tmp_path):
    db = make_db()
    interp = _pausing_interpreter(sample_ws, tmp_path, db, pause_on="__never_asked__")
    wf = sample_ws.load_definition("deep-research")
    done = interp.run(wf, TOPIC, "dry")
    assert done.status == "done"

    run = interp.ledger.session.get(Run, done.run_id)
    with pytest.raises(ValueError, match="not paused"):
        interp.carry_on_paused(run, wf)
    interp.ledger.close()


def test_a_run_that_hit_the_spending_limit_can_be_carried_on(ws, tmp_path):
    """The gap #44 left: only a run picked up after it broke could carry on before."""
    play = deep_research_script("accept")

    def pricey(req):
        resp = play(req)
        return ModelResponse(
            output=resp.output,
            decisions=resp.decisions,
            usage=Usage(1, 1, 7.0),
            tool_calls=resp.tool_calls,
        )

    db = make_db()
    acts = Activities(
        model=ScriptedModel(pricey),
        search=FixtureSearch(ws),
        links=FixtureLinkCheck(ws),
        render=PdfRenderer(),
        send=RecordingSend(),
        guesser=HeuristicGuesser(),
        policy=ActivityPolicy(retries=0),
    )
    interp = Interpreter(ws, acts, Ledger(db), RunConfig(artifacts_dir=tmp_path / "a"))
    wf = ws.load_definition("deep-research")
    r = interp.run(wf, TOPIC, "dry")
    assert r.status == "paused_budget"

    # someone raises the limit before asking it to carry on
    data = read_yaml(ws, DEF)
    data["spec"]["budget"]["max_usd"] = 1000
    write_yaml(ws, DEF, data)
    wf2 = ws.load_definition("deep-research")

    run = interp.ledger.session.get(Run, r.run_id)
    done = interp.carry_on_paused(run, wf2)
    assert done.status == "done", done.error


# -- through the API -------------------------------------------------------------


def _no_gates(ws) -> None:
    """Nothing asks for an OK; only the pause request stops this run."""
    d = read_yaml(ws, DEF)
    d["spec"]["defaults"]["trust"] = {"policy": "auto"}
    for s in d["spec"]["steps"]:
        s["trust"] = {"policy": "auto"}
    write_yaml(ws, DEF, d)


def test_the_run_page_offers_pause_only_on_a_running_real_run(tmp_path, sample_ws):
    from fastapi.testclient import TestClient

    from wf.api.app import AppState, create_app

    state = AppState(
        sample_ws, make_db(), ScriptedModel(deep_research_script("accept")), tmp_path / "art"
    )
    client = TestClient(create_app(state))

    ledger = Ledger(state.db)
    wf = sample_ws.load_definition("deep-research")
    wv = ledger.workflow_version(wf, "sha256:test")
    kw = dict(depth=0, parent=None, parent_step_run=None, budget_usd=None, case_name=None)
    live = ledger.start_run(wv, inputs=TOPIC, mode="live", title="Live", **kw)
    dry = ledger.start_run(wv, inputs=TOPIC, mode="dry", title="Dry", **kw)
    ledger.close()

    page = client.get(f"/runs/{live.id}").text
    assert f'data-pause="{live.id}"' in page and 'data-then="pause"' in page

    page = client.get(f"/runs/{dry.id}").text
    assert f'data-pause="{dry.id}"' not in page, "a dry run has nothing real to pause"

    assert client.post(f"/api/runs/{live.id}/pause").status_code == 200
    with state.db.session() as s:
        assert s.get(Run, live.id).pause_requested is True

    assert client.post(f"/api/runs/{dry.id}/pause").status_code == 409
    assert client.post("/api/runs/nope/pause").status_code == 404
    assert client.post(f"/api/runs/{live.id}/carry-on").status_code == 409, (
        "it is running, not paused"
    )


def test_the_run_page_offers_pause_and_carrying_on_does_not_repeat_finished_steps(ws, tmp_path):
    from fastapi.testclient import TestClient

    from tests.test_api import wait_for
    from wf.api.app import AppState, create_app

    _no_gates(ws)
    play = deep_research_script("accept")

    def script(req):
        if req.tag == "research":
            with state.db.session() as s:
                s.query(Run).filter_by(status="running").one().pause_requested = True
        return play(req)

    state = AppState(ws, make_db(), ScriptedModel(script), tmp_path / "artifacts")
    state.runner.activities.policy = ActivityPolicy(retries=0)
    client = TestClient(create_app(state))

    r = client.post(
        "/api/workflows/deep-research/runs", json={"case": "durable-execution", "mode": "live"}
    )
    run = wait_for(client, r.json()["run_id"])
    assert run["status"] == "paused", run["error"]
    steps = {s["step_id"]: s["status"] for s in run["steps"]}
    assert steps == {"plan": "done", "research": "done"}

    page = client.get(f"/runs/{run['id']}").text
    assert f'data-pause="{run["id"]}"' in page and 'data-then="carry-on"' in page

    assert client.post(f"/api/runs/{run['id']}/pause").status_code == 409, "already paused"
    assert client.post(f"/api/runs/{run['id']}/carry-on").status_code == 200
    done = wait_for(client, run["id"])
    assert done["status"] == "done", done["error"]
    ids = [s["step_id"] for s in done["steps"]]
    assert ids.count("research") == 1, "the step that finished before the pause is not run again"


def test_a_paused_budget_run_can_be_carried_on_through_the_api(ws, tmp_path):
    from fastapi.testclient import TestClient

    from tests.test_api import wait_for
    from wf.api.app import AppState, create_app

    play = deep_research_script("accept")

    def pricey(req):
        resp = play(req)
        return ModelResponse(
            output=resp.output,
            decisions=resp.decisions,
            usage=Usage(1, 1, 7.0),
            tool_calls=resp.tool_calls,
        )

    state = AppState(ws, make_db(), ScriptedModel(pricey), tmp_path / "artifacts")
    state.runner.activities.policy = ActivityPolicy(retries=0)
    client = TestClient(create_app(state))

    r = client.post("/api/workflows/deep-research/runs", json={"case": "durable-execution"})
    run = wait_for(client, r.json()["run_id"])
    assert run["status"] == "paused_budget"
    page = client.get(f"/runs/{run['id']}").text
    assert f'data-pause="{run["id"]}"' in page and 'data-then="carry-on"' in page

    data = read_yaml(ws, DEF)
    data["spec"]["budget"]["max_usd"] = 1000
    write_yaml(ws, DEF, data)

    assert client.post(f"/api/runs/{run['id']}/carry-on").status_code == 200
    done = wait_for(client, run["id"])
    assert done["status"] == "done", done["error"]
    said = [d["text"] + " " + d["reason"] for st in done["steps"] for d in st["decisions"]]
    assert any(t.startswith("Carried on from") and "at the spending limit" in t for t in said), (
        "it says it paused at the spending limit, not that you paused it"
    )
