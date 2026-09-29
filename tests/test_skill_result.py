"""Instructions can come with the result they are written to give back, and a step
moved onto them is offered it, told first what that would take from the steps that
read it. That is what lets "use claim-support and Jev" be one choice rather than a
refusal."""

from __future__ import annotations

from typing import Any

import pytest

from tests.helpers import read_yaml, step
from tests.test_api import client  # noqa: F401  (pytest fixture)
from tests.test_audit import DOC
from tests.test_other_workflows import auditor_for
from wf import settings
from wf.activities import ModelResponse, ScriptedModel
from wf.audit import AnswerRejected
from wf.audit.catalog import capabilities, result_costs, skill_choices
from wf.audit.chat import chat
from wf.validate.readers import lost_by, reads_from

REL = "definitions/deep-research.workflow.yaml"
CLAIMS = "schemas/claim_support.json"
URL = "/api/workflows/deep-research/skill"


def test_instructions_name_their_result_in_their_front_matter(ws):
    assert ws.load_skill("skills/claim-support.md@1").result == CLAIMS
    assert ws.load_skill("skills/report-reviewer.md").result is None
    own = {i["name"]: i for i in capabilities(ws)["instructions"]}
    assert own["claim-support"]["result"] == CLAIMS


def test_an_edited_version_keeps_the_result_it_came_with(ws):
    version, _ = ws.save_skill_version("skills/claim-support.md", "# Check it\n\nMore carefully.")
    skill = ws.load_skill(f"skills/claim-support.md@{version}")
    assert skill.result == CLAIMS and "More carefully." in skill.body


def test_what_later_steps_read_from_a_step(ws):
    wf = ws.load_definition("deep-research")
    reads = reads_from(wf, "review")
    assert ("Go deeper, if you say so", "followup_topics") in reads
    assert ("Apply the reviewer's edits", None) in reads, "reads all of it"
    assert ("the workflow's result", "verdict") in reads
    lost = lost_by(wf, "review", ws.load_schema(CLAIMS))
    assert any("“followup topics”, which it would no longer give back" in s for s in lost)
    assert any("reads everything it gives back now" in s for s in lost)
    assert not any("the workflow's result" in s for s in lost), "claim support has a verdict too"


def test_a_step_is_offered_the_result_with_what_it_would_cost(ws):
    wf = ws.load_definition("deep-research")
    claims = next(
        c for c in skill_choices(ws, wf, wf.step("review")) if c["value"].startswith("skills/claim")
    )
    assert claims["result"] == CLAIMS and claims["loses"]
    already = skill_choices(ws, wf, wf.step("check_support"))
    assert not any("result" in c for c in already), "it gives that back already"


def test_the_page_offers_it(client):  # noqa: F811
    page = client.get("/workflows/deep-research").text
    assert f'data-result="{CLAIMS}"' in page
    assert "which it would no longer give back" in page


def test_choosing_instructions_and_their_result_is_one_change(client, ws):  # noqa: F811
    r = client.post(URL, json={"step": "review", "skill": "claim-support", "result": True})
    assert r.status_code == 200, r.text
    review = step(read_yaml(ws, REL), "review")
    assert review["skill"] == "skills/claim-support.md@1"
    assert review["output"]["schema"] == CLAIMS
    fields = {f["field"] for f in r.json()["findings"]}
    # what the later steps can no longer read, or branch on, is asked about
    assert "steps.go_deeper.for_each" in fields
    assert "steps.revise.when" in fields, "a branch on a value that is gone, found afterwards"


def test_choosing_instructions_alone_keeps_the_result(client, ws):  # noqa: F811
    before = step(read_yaml(ws, REL), "review")["output"]["schema"]
    r = client.post(URL, json={"step": "review", "skill": "claim-support", "result": False})
    assert r.status_code == 200, r.text
    assert step(read_yaml(ws, REL), "review")["output"]["schema"] == before


def test_instructions_with_no_result_cannot_bring_one(client):  # noqa: F811
    r = client.post(URL, json={"step": "plan", "skill": "report-writer", "result": True})
    assert r.status_code == 400 and "no result of its own" in r.json()["detail"]


def test_the_step_can_then_run_on_jev(client, ws, monkeypatch):  # noqa: F811
    """The request that started this: those instructions, and Jev."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.delenv("WF_STEP_MODELS", raising=False)
    model = "/api/workflows/deep-research/model"
    assert client.post(model, json={"step": "review", "model": "Jev"}).status_code == 400
    client.post(URL, json={"step": "review", "skill": "claim-support", "result": True})
    r = client.post(model, json={"step": "review", "model": "Jev"})
    assert r.status_code == 200, r.text
    assert step(read_yaml(ws, REL), "review")["model"] == settings.JEV


# -- drafts and the chat --------------------------------------------------------------


@pytest.fixture
def draft(ws):
    auditor, _ = auditor_for(ws)
    return auditor, auditor.audit(DOC.read_text(), name="client-research")


def test_a_result_that_does_not_exist_is_refused(draft):
    auditor, result = draft
    with pytest.raises(AnswerRejected, match="no result called"):
        auditor.set_field(result, "steps.review.output.schema", "schemas/nope.json", "x")
    auditor.set_field(result, "steps.review.output.schema", CLAIMS, "x")
    assert step(result.definition, "review")["output"]["schema"] == CLAIMS


def test_the_chat_is_told_what_switching_result_would_take(draft, ws):
    auditor, result = draft
    shown: list[dict[str, Any]] = []

    def script(req):
        shown.append(req.input)
        return ModelResponse(
            output={"reply": "ok", "edits": [], "answers": [], "dismiss": []}, decisions=[]
        )

    wf = result.workflow()
    chat(
        ScriptedModel(script),
        result,
        [],
        "use claim-support on the review",
        capabilities=capabilities(ws),
        result_costs=result_costs(ws, wf),
    )
    (req,) = shown
    own = {i["name"]: i for i in req["what_the_system_can_do"]["instructions"]}
    assert own["claim-support"]["result"] == CLAIMS
    costs = req["switching_result_would_take"]["review"]["skills/claim-support.md@1"]
    assert costs == lost_by(wf, "review", ws.load_schema(CLAIMS))
