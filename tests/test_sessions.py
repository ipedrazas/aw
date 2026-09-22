"""Agentic sessions are persisted: every exchange with a model, under the work that
caused it. The run record says what the workflow decided; the session says what the
model was asked, which is the thing you need when a reasonable-looking decision is
wrong."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers import make_db
from tests.scripted import deep_research_script
from tests.test_api import client, wait_for  # noqa: F401  (pytest fixture)
from tests.test_audit import DOC, extraction_for_process_doc, passage_map
from wf.activities import (
    Activities,
    ActivityError,
    ActivityPolicy,
    FixtureLinkCheck,
    FixtureSearch,
    HeuristicGuesser,
    ModelResponse,
    PdfRenderer,
    RecordingSend,
    ScriptedModel,
    SessionRecorder,
    record_sessions,
)
from wf.audit import Auditor
from wf.audit.chat import chat
from wf.dryrun import DryRunner
from wf.interpret import RunConfig
from wf.store.sessions import SessionLog, session_file, session_root

TOPIC = {"topic": "Durable execution platforms for AI agents: who leads and why"}


def runner(ws, tmp_path: Path, script, db=None) -> tuple[DryRunner, SessionLog]:
    """The runner the product builds: one recorder around the model the steps share."""
    db = db or make_db()
    acts = Activities(
        model=record_sessions(ScriptedModel(script), db),
        search=FixtureSearch(ws),
        links=FixtureLinkCheck(ws),
        render=PdfRenderer(),
        send=RecordingSend(),
        guesser=HeuristicGuesser(),
        policy=ActivityPolicy(retries=0),
    )
    return (
        DryRunner(ws, acts, db, RunConfig(artifacts_dir=tmp_path / "artifacts")),
        SessionLog(db),
    )


def test_a_run_keeps_every_exchange_under_one_session(sample_ws, tmp_path, monkeypatch):
    monkeypatch.delenv("WF_SESSION_LOG", raising=False)
    r, sessions = runner(sample_ws, tmp_path, deep_research_script("accept"))
    result = r.run("deep-research", TOPIC)

    rows = sessions.list(run_id=result.run_id)
    assert len(rows) == 1, "one run, one session"
    session = rows[0]
    assert session["kind"] == "run" and session["mode"] == "dry"
    assert session["status"] == "done"
    assert session["calls"] > 0 and session["cost_usd"] > 0
    assert session["input_tokens"] > 0 and session["output_tokens"] > 0

    full = sessions.get(session["id"])
    calls = full["calls_detail"]
    assert [c["seq"] for c in calls] == list(range(1, len(calls) + 1))
    assert {c["step_id"] for c in calls} <= {s["step_id"] for s in r.report(result.run_id).steps}
    assert all(c["step_id"] for c in calls), "every call names the step that made it"

    plan = next(c for c in calls if c["step_id"] == "plan")
    assert "Plan the research brief" in plan["system"], "the instructions as the model got them"
    assert plan["input"] == TOPIC
    assert plan["output"]["questions"], "and the answer as it came back"
    assert plan["output_schema"]["type"] == "object"
    assert plan["decisions"], "the decisions the model recorded travel with the call"
    assert plan["status"] == "ok" and plan["duration_s"] >= 0

    research = next(c for c in calls if c["step_id"] == "research")
    assert research["tool_calls"], "a step that searched says what it searched for"
    searched = [t for t in research["tool_calls"] if t["name"] == "search"]
    assert searched and searched[0]["result"], "and what came back, not only a count of it"
    assert any(t.get("injection") for t in research["tool_calls"]), (
        "the page that tried to give instructions is named in the session too"
    )


def test_the_totals_add_up_to_what_the_run_spent(sample_ws, tmp_path, monkeypatch):
    monkeypatch.delenv("WF_SESSION_LOG", raising=False)
    r, sessions = runner(sample_ws, tmp_path, deep_research_script("accept"))
    result = r.run("deep-research", TOPIC)
    session = sessions.list(run_id=result.run_id)[0]
    calls = sessions.get(session["id"])["calls_detail"]
    assert round(sum(c["cost_usd"] for c in calls), 6) == session["cost_usd"]
    assert session["cost_usd"] == pytest.approx(result.spent_usd, abs=1e-6)


def test_meta_keeps_the_count_without_the_prompts(sample_ws, tmp_path, monkeypatch):
    monkeypatch.setenv("WF_SESSION_LOG", "meta")
    r, sessions = runner(sample_ws, tmp_path, deep_research_script("accept"))
    result = r.run("deep-research", TOPIC)

    session = sessions.list(run_id=result.run_id)[0]
    assert session["calls"] > 0, "the session is still there"
    for c in sessions.get(session["id"])["calls_detail"]:
        assert c["system"] is None and c["input"] is None and c["output"] is None
        assert c["model"] and c["input_tokens"] > 0, "what it cost is still recorded"
        for call in c["tool_calls"]:
            assert set(call) == {"name", "summary"}, "which tools ran, not what went through"
    research = next(
        c for c in sessions.get(session["id"])["calls_detail"] if c["step_id"] == "research"
    )
    assert research["tool_calls"], "a step that called tools still says that it did"


def test_off_writes_nothing_and_the_work_still_runs(sample_ws, tmp_path, monkeypatch):
    monkeypatch.setenv("WF_SESSION_LOG", "off")
    r, sessions = runner(sample_ws, tmp_path, deep_research_script("accept"))
    result = r.run("deep-research", TOPIC)
    assert result.status == "done", "recording is not a precondition for working"
    assert sessions.list(run_id=result.run_id) == []


def test_a_call_that_failed_is_recorded_before_it_is_raised(sample_ws, tmp_path, monkeypatch):
    monkeypatch.delenv("WF_SESSION_LOG", raising=False)

    def broken(req):
        if req.tag.startswith("plan"):
            raise ActivityError("the model declined this request")
        raise AssertionError("nothing should run after the first step failed")

    r, sessions = runner(sample_ws, tmp_path, broken)
    report = r.run("deep-research", TOPIC)
    assert report.status == "failed"

    session = sessions.list(run_id=report.run_id)[0]
    assert session["status"] == "done", "the run finished; it is the call that failed"
    (call,) = sessions.get(session["id"])["calls_detail"]
    assert call["status"] == "failed"
    assert "declined" in call["error"]
    assert call["output"] is None and call["system"], "what was asked is kept even so"


def test_an_audit_and_a_chat_turn_are_sessions_of_their_own(ws, monkeypatch):
    monkeypatch.delenv("WF_SESSION_LOG", raising=False)
    db = make_db()
    sessions = SessionLog(db)
    text = DOC.read_text()
    extracted = extraction_for_process_doc(passage_map(text))

    def script(req):
        if req.tag == "audit:chat":
            return ModelResponse(
                output={"reply": "Yes.", "edits": [], "answers": [], "point_to_finding": None}
            )
        return ModelResponse(output=extracted, decisions=[])

    model = record_sessions(ScriptedModel(script), db)
    result = Auditor(ws, model).audit(text)
    assert result.session_id, "the draft says which session read the document"

    audit = sessions.get(result.session_id)
    assert audit["kind"] == "audit" and audit["calls"] == 1
    assert audit["title"], "a session is titled by the document's own first line"
    assert audit["calls_detail"][0]["tag"] == "audit:extract"

    chat(model, result, [], "Is the brief step right?", audit_id="draft-1")
    turns = sessions.list(kind="chat")
    assert len(turns) == 1 and turns[0]["audit_id"] == "draft-1"
    assert turns[0]["title"] == "Is the brief step right?"


def test_a_call_nobody_claimed_is_still_kept(monkeypatch):
    monkeypatch.delenv("WF_SESSION_LOG", raising=False)
    db = make_db()
    sessions = SessionLog(db)
    model = record_sessions(ScriptedModel(lambda req: ModelResponse(output={"ok": True})), db)

    from wf.activities import ModelRequest

    model.complete(
        ModelRequest(tag="loose", model="a-model", system="do a thing", input={}, output_schema={})
    )
    loose = sessions.list(kind="other")
    assert len(loose) == 1 and loose[0]["calls"] == 1, "an unattached exchange is not lost"


def test_wrapping_twice_is_a_no_op(monkeypatch):
    db = make_db()
    once = record_sessions(ScriptedModel(lambda req: ModelResponse(output={})), db)
    assert isinstance(once, SessionRecorder)
    assert record_sessions(once, db) is once


def test_session_root_defaults_to_the_container_path(monkeypatch):
    monkeypatch.delenv("SESSION_ROOT", raising=False)
    monkeypatch.setattr(Path, "mkdir", lambda self, *a, **k: None)
    assert session_root() == Path("/app/var/sessions")


def test_session_root_is_read_from_the_environment_and_created(tmp_path, monkeypatch):
    root = tmp_path / "nested" / "sessions"
    assert not root.exists()
    monkeypatch.setenv("SESSION_ROOT", str(root))
    assert session_root() == root
    assert root.is_dir(), "the root is created the first time it is asked for"


def test_a_session_is_mirrored_to_its_own_file(sample_ws, tmp_path, monkeypatch):
    monkeypatch.delenv("WF_SESSION_LOG", raising=False)
    r, sessions = runner(sample_ws, tmp_path, deep_research_script("accept"))
    result = r.run("deep-research", TOPIC)
    session = sessions.list(run_id=result.run_id)[0]

    path = session_file(session["id"])
    assert path.parent == session_root()
    assert path.name == f"{session['id']}.log"
    on_disk = json.loads(path.read_text())
    assert on_disk["id"] == session["id"]
    assert on_disk["status"] == "done"
    assert len(on_disk["calls_detail"]) == session["calls"]


def test_a_session_survives_the_database_that_wrote_it(sample_ws, tmp_path, monkeypatch):
    """The database a run used is gone; SESSION_ROOT is not — a stand-in for a restart."""
    monkeypatch.delenv("WF_SESSION_LOG", raising=False)
    r, sessions = runner(sample_ws, tmp_path, deep_research_script("accept"))
    result = r.run("deep-research", TOPIC)
    session_id = sessions.list(run_id=result.run_id)[0]["id"]

    after_restart = SessionLog(make_db())
    with pytest.raises(KeyError):
        after_restart.get(session_id)  # a fresh database has never heard of it

    listed = after_restart.list_from_disk()
    assert session_id in {s["id"] for s in listed}
    loaded = after_restart.get_from_disk(session_id)
    assert loaded["id"] == session_id
    assert loaded["calls_detail"], "the calls it made are on disk too"


def test_loading_a_missing_session_from_disk_is_a_key_error(monkeypatch):
    sessions = SessionLog(make_db())
    with pytest.raises(KeyError):
        sessions.get_from_disk("does-not-exist")


def test_the_api_serves_the_sessions_of_a_run(client):  # noqa: F811  (the fixture)
    run_id = client.post("/api/workflows/deep-research/runs", json={"inputs": TOPIC}).json()[
        "run_id"
    ]
    wait_for(client, run_id)

    rows = client.get(f"/api/runs/{run_id}/sessions").json()
    assert len(rows) == 1 and rows[0]["kind"] == "run"

    detail = client.get(f"/api/sessions/{rows[0]['id']}").json()
    assert detail["calls"] == len(detail["calls_detail"]) > 0
    assert detail["calls_detail"][0]["system"], "the prompts are there by default"

    without = client.get(f"/api/sessions/{rows[0]['id']}?bodies=false").json()
    assert "system" not in without["calls_detail"][0], "and can be left out when they are large"

    assert [s["id"] for s in client.get("/api/sessions?kind=run").json()] == [rows[0]["id"]]
    assert client.get("/api/sessions/nope").status_code == 404
