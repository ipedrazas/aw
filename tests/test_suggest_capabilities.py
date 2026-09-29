"""A step doing on its own instructions what the system already does is offered the
system's way: proposed by triage (a new draft) or the chat (when asked), offered only
when the same wiring a new draft gets works. The earlier online-research draft's step 7,
"check the links match the citation", ran on instructions of its own while claim-support
did that job. (plans/capability-contracts.md, piece 4)"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.helpers import make_db, step
from tests.scripted import skill_answer
from tests.test_wire_capabilities import DOC, extraction
from wf.activities import ModelResponse, ScriptedModel
from wf.api.app import AppState, create_app
from wf.audit import Auditor
from wf.audit.suggest import offers
from wf.audit.triage import can_settle

CLAIMS = "Check the page says what the report says"
QUESTION = f"Should “Check the pages say what the citations claim” use the system's own “{CLAIMS}”?"


def on_its_own() -> dict[str, Any]:
    """The extraction as the earlier draft had it: the link check and the claim check on
    instructions of their own."""
    x = extraction()
    for s in x["steps"]:
        if s["id"] == "s4":
            s.update(kind="agent", run=None, title="Verify the links work")
        if s["id"] == "s5":
            s["uses"] = None
    return x


def script(req, triage: list[dict[str, Any]] | None = None, suggest=None):
    if req.tag.startswith("audit:skill:"):
        return skill_answer(req)
    if req.tag == "audit:triage":
        return ModelResponse(
            output={"settle": [], "later": [], "ask": [], "capabilities": triage or []}
        )
    if req.tag == "audit:chat":
        return ModelResponse(
            output={
                "reply": "I looked.",
                "edits": [],
                "answers": [],
                "dismiss": [],
                "suggest": suggest or [],
            }
        )
    return ModelResponse(output={**on_its_own(), "delivers": None}, decisions=[])


@pytest.fixture
def drafted(ws):
    auditor = Auditor(ws, ScriptedModel(script))
    return auditor, auditor.audit(DOC, name="online-researcher")


def claims_for(sid: str) -> dict[str, str]:
    return {"step_id": sid, "name": "claim-support", "why": "It checks each citation's page."}


def test_a_proposal_that_wires_is_offered_saying_what_would_change(drafted, ws):
    _, result = drafted
    (f,) = offers(result.definition, ws, [claims_for("s5")])
    assert f.question == QUESTION
    assert f.detail.startswith("It checks each citation's page. What would change:")
    assert "I added “Read the cited pages”" in f.detail
    assert [o.label for o in f.options] == [f"Yes, use “{CLAIMS}”", "No, keep its own instructions"]
    assert not can_settle(f), "the chat proposed it; the person decides"


def test_a_proposal_that_does_not_wire_is_not_offered(drafted, ws):
    _, result = drafted
    assert offers(result.definition, ws, [claims_for("s1")]) == [], "nothing cited before it"
    assert offers(result.definition, ws, [{"step_id": "s3", "name": "report-writer"}]) == [], (
        "instructions that do not say what they take are a starting point, not a swap"
    )
    assert offers(result.definition, ws, [{"step_id": "s5", "name": "nope"}]) == []


def test_a_routine_can_be_offered_too(drafted, ws):
    _, result = drafted
    (f,) = offers(
        result.definition, ws, [{"step_id": "s4", "name": "checks.http_resolves", "why": ""}]
    )
    assert "Open every link and see which ones answer" in f.question


def test_yes_wires_it_as_a_new_draft_would_be(drafted, ws):
    auditor, result = drafted
    before = step(result.definition, "s3")["skill"]
    f = offers(result.definition, ws, [claims_for("s5")])[0]
    result.findings.append(f)
    auditor.answer(result, f.id, f.options[0].value)
    s5 = step(result.definition, "s5")
    assert s5["skill"] == "skills/claim-support.md@1"
    assert s5["for_each"].startswith("${steps.read_pages")
    assert "read_pages" in [s["id"] for s in result.definition["spec"]["steps"]]
    s3 = step(result.definition, "s3")
    assert s3["skill"] != before, (
        "the report's instructions, told to list its citations, are a new version"
    )
    assert "`sources`" in ws.load_skill(s3["skill"]).body
    assert "sources" in ws.load_schema(s3["output"]["schema"])["properties"]


# -- through the app ----------------------------------------------------------------


@pytest.fixture
def client(ws, tmp_path):
    turn: dict[str, Any] = {}
    state = AppState(
        ws,
        make_db(),
        ScriptedModel(lambda req: script(req, turn.get("triage"), turn.get("suggest"))),
        tmp_path / "artifacts",
    )
    return TestClient(create_app(state)), turn


def questions(view: dict[str, Any]) -> list[str]:
    return [f["question"] for g in view["questions"] for f in g["findings"]]


def test_triage_proposes_it_when_the_draft_is_made(client):
    c, turn = client
    turn["triage"] = [claims_for("s5"), claims_for("s1")]
    view = c.post("/api/audits", json={"document": DOC, "name": "online-researcher"}).json()
    assert QUESTION in questions(view)
    assert not any("“Create a strong prompt” use" in q for q in questions(view))


def test_the_chat_proposes_it_when_asked(client):
    c, turn = client
    aid = c.post("/api/audits", json={"document": DOC, "name": "online-researcher"}).json()["id"]
    turn["suggest"] = [claims_for("s5")]
    view = c.post(
        f"/api/audits/{aid}/chat", json={"message": "does the system already do any of this?"}
    ).json()
    assert QUESTION in questions(view)
    turn["suggest"] = [claims_for("s1")]
    view = c.post(f"/api/audits/{aid}/chat", json={"message": "and the prompt?"}).json()
    assert "nothing new is on the right" in view["chat"][-1]["text"]
