"""The models and instructions a step can be set to: one list, read by the page, the
chat and the edit that checks them, so none of them offers what another refuses.

A person asked the chat for "skills/claim-support.md@1 and typesafe/jev-1.13" on a step
and was told neither existed: the chat only knew of two models and saw the system's
own instructions by title. These are the pieces that make that request work, or say
exactly why it cannot."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.helpers import make_db, read_yaml, step, write_yaml
from tests.scripted import extraction_for_process_doc, skill_answer
from tests.test_audit import DOC, passage_map
from tests.test_other_workflows import auditor_for
from wf import settings
from wf.activities import ActivityError, ModelResponse, ScriptedModel
from wf.api.app import AppState, create_app
from wf.audit import AnswerRejected
from wf.audit.catalog import capabilities
from wf.audit.chat import chat
from wf.schema import load_workflow_dict
from wf.validate import validate
from wf.validate.models import model_choices, models_steps_cannot_use

JEV = settings.JEV
CLAIMS = "schemas/claim_support.json"


@pytest.fixture
def with_jev(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.delenv("WF_STEP_MODELS", raising=False)


@pytest.fixture
def draft(ws, with_jev):
    auditor, _ = auditor_for(ws)
    return auditor, auditor.audit(DOC.read_text(), name="client-research")


# -- the list --------------------------------------------------------------------


def test_standard_and_thorough_are_always_there(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("WF_STEP_MODELS", raising=False)
    models = settings.step_models()
    assert [m["label"] for m in models] == ["Standard", "Thorough"]
    assert [m["value"] for m in models] == [settings.quick_model(), settings.careful_model()]
    assert all(m["kind"] == "text" and m["good_for"] for m in models)


def test_jev_is_on_the_list_when_it_can_be_reached(with_jev):
    jev = settings.step_models()[-1]
    assert jev["value"] == JEV and jev["label"] == "Jev" and jev["kind"] == "decisions"


def test_a_deployment_names_its_own_models(monkeypatch):
    monkeypatch.setenv(
        "WF_STEP_MODELS",
        '[{"value": "anthropic/claude-haiku-4-5", "label": "Quick"}, "typesafe/jev-2"]',
    )
    extra = {m["value"]: m for m in settings.step_models()[2:]}
    assert extra["anthropic/claude-haiku-4-5"]["label"] == "Quick"
    assert extra["anthropic/claude-haiku-4-5"]["kind"] == "text"
    assert extra["typesafe/jev-2"]["kind"] == "decisions", "a decisions model is told by its name"


def test_the_system_s_own_instructions_carry_the_reference_a_step_names(ws):
    own = {i["name"]: i for i in capabilities(ws)["instructions"]}
    assert own["claim-support"]["ref"] == "skills/claim-support.md@1"


# -- an edit is checked --------------------------------------------------------------


def test_a_step_is_put_on_a_model_by_its_label(draft):
    auditor, result = draft
    ch = auditor.set_field(result, "steps.write.model", "thorough", "Asked in the chat.")
    assert ch.after == settings.careful_model()
    assert step(result.definition, "write")["model"] == settings.careful_model()


def test_a_model_the_system_does_not_run_is_refused_with_the_ones_it_does(draft):
    auditor, result = draft
    with pytest.raises(AnswerRejected, match="Standard, Thorough or Jev"):
        auditor.set_field(result, "steps.write.model", "made-up/model", "x")
    assert step(result.definition, "write").get("model") != "made-up/model"


def test_jev_is_refused_for_a_step_that_searches(draft):
    auditor, result = draft
    with pytest.raises(AnswerRejected, match="cannot search or read pages"):
        auditor.set_field(result, "steps.research.model", JEV, "x")


def test_jev_is_refused_for_a_step_whose_result_is_not_only_questions(draft):
    auditor, result = draft
    with pytest.raises(AnswerRejected, match="“missing” is neither"):
        auditor.set_field(result, "steps.review.model", JEV, "x")
    assert step(result.definition, "review").get("model") != JEV, "the draft is unchanged"


def test_jev_runs_a_step_whose_result_is_questions(draft):
    auditor, result = draft
    auditor.set_field(result, "steps.review.output.schema", CLAIMS, "x")
    auditor.set_field(result, "steps.review.model", "typesafe/jev-1.13", "x")
    assert step(result.definition, "review")["model"] == JEV


def test_jev_cannot_be_the_default_while_a_step_that_writes_uses_it(draft):
    auditor, result = draft
    with pytest.raises(AnswerRejected, match="Jev"):
        auditor.set_field(result, "spec.defaults.model", JEV, "x")


def test_a_step_follows_the_system_s_own_instructions_by_name_or_reference(draft):
    auditor, result = draft
    auditor.set_field(result, "steps.review.skill", "claim-support", "x")
    assert step(result.definition, "review")["skill"] == "skills/claim-support.md@1"
    auditor.set_field(result, "steps.write.skill", "skills/report-writer.md", "x")
    assert step(result.definition, "write")["skill"].startswith("skills/report-writer.md@")


def test_instructions_that_do_not_exist_are_refused_with_the_ones_that_do(draft):
    auditor, result = draft
    with pytest.raises(AnswerRejected, match=r"skills/claim-support\.md@1"):
        auditor.set_field(result, "steps.review.skill", "skills/nope.md@1", "x")


def test_undoing_puts_back_instructions_the_draft_wrote_itself(draft):
    auditor, result = draft
    own = step(result.definition, "review")["skill"]
    auditor.set_field(result, "steps.review.skill", "claim-support", "x")
    auditor.set_field(result, "steps.review.skill", own, "Undid it.")
    assert step(result.definition, "review")["skill"] == own


# -- the validator asks the same --------------------------------------------------


def test_a_saved_step_on_jev_that_cannot_answer_is_asked_about(ws, with_jev):
    rel = "definitions/deep-research.workflow.yaml"
    data = read_yaml(ws, rel)
    step(data, "review")["model"] = JEV
    write_yaml(ws, rel, data)
    findings = validate(load_workflow_dict(read_yaml(ws, rel)), ws).by_field()
    f = findings["steps.review.model"]
    assert f.type == "conflict" and "Jev only answers" in f.detail
    assert [o.label for o in f.options] == ["Standard", "Thorough"], "only models that write"


def test_jev_shows_its_result_not_its_reasons(ws, with_jev):
    rel = "definitions/deep-research.workflow.yaml"
    data = read_yaml(ws, rel)
    step(data, "check_support")["model"] = JEV
    step(data, "check_support")["shows_user"] = ["decisions"]
    write_yaml(ws, rel, data)
    findings = validate(load_workflow_dict(read_yaml(ws, rel)), ws).by_field()
    assert "steps.check_support.model" not in findings, "its result is questions"
    assert "gives no reasons" in findings["steps.check_support.shows_user"].detail


# -- the chat is shown it -----------------------------------------------------------


def test_the_chat_sees_every_model_the_references_and_what_each_step_cannot_use(draft, ws):
    auditor, result = draft
    shown: list[dict[str, Any]] = []

    def script(req):
        shown.append(req.input)
        return ModelResponse(
            output={"reply": "ok", "edits": [], "answers": [], "dismiss": []}, decisions=[]
        )

    chat(
        ScriptedModel(script),
        result,
        [],
        "Step 7 should use skills/claim-support.md@1 and typesafe/jev-1.13",
        capabilities=capabilities(ws),
        model_limits=models_steps_cannot_use(result.workflow(), ws),
    )
    (req,) = shown
    can = req["what_the_system_can_do"]
    assert JEV in {m["value"] for m in can["models"]}
    assert "skills/claim-support.md@1" in {i["ref"] for i in can["instructions"]}
    limits = req["models_a_step_cannot_use"]
    assert "cannot search" in limits["research"]["Jev"]
    assert "Standard" not in limits["research"], "only the ones it cannot use"


def test_the_workflow_s_own_models_stay_on_the_list(ws):
    wf = load_workflow_dict(read_yaml(ws, "definitions/deep-research.workflow.yaml"))
    assert "claude-opus-5" in {c["value"] for c in model_choices(wf)}


# -- through the app ----------------------------------------------------------------


@pytest.fixture
def app(ws, tmp_path, with_jev):
    """The app on the sample document, with the chat's turns scripted by the test."""
    extracted = extraction_for_process_doc(passage_map(DOC.read_text()))
    turns: list[dict[str, Any]] = []

    def script(req):
        if req.tag.startswith("audit:skill:"):
            return skill_answer(req)
        if req.tag == "audit:extract":
            return ModelResponse(output=extracted, decisions=[])
        if req.tag == "audit:chat":
            return ModelResponse(output=turns.pop(0), decisions=[])
        raise ActivityError(f"{req.tag} is not scripted")

    state = AppState(ws, make_db(), ScriptedModel(script), tmp_path / "artifacts")
    return TestClient(create_app(state)), turns


def edit(path: str, value: str) -> dict[str, str]:
    return {"path": path, "value_json": value, "reason": "Asked in the chat."}


def test_a_new_result_and_jev_for_it_are_judged_together(app):
    """The chat may name the model before the result it needs; it is applied last."""
    client, turns = app
    aid = client.post(
        "/api/audits", json={"document": DOC.read_text(), "name": "client-research"}
    ).json()["id"]
    turns.append(
        {
            "reply": "Done.",
            "edits": [
                edit("steps.review.model", f'"{JEV}"'),
                edit("steps.review.skill", '"skills/claim-support.md@1"'),
                edit("steps.review.output.schema", f'"{CLAIMS}"'),
            ],
            "answers": [],
            "dismiss": [],
        }
    )
    r = client.post(f"/api/audits/{aid}/chat", json={"message": "use jev and claim-support"})
    assert r.status_code == 200, r.text
    said = r.json()["chat"][-1]
    assert [c["path"] for c in said["changes"]][-1] == "steps.review.model"
    assert said["text"] == "Done.", "nothing was refused"


def test_a_refused_model_is_said_in_the_chat(app):
    client, turns = app
    aid = client.post(
        "/api/audits", json={"document": DOC.read_text(), "name": "client-research"}
    ).json()["id"]
    turns.append(
        {
            "reply": "I put it on Jev.",
            "edits": [edit("steps.review.model", f'"{JEV}"')],
            "answers": [],
            "dismiss": [],
        }
    )
    said = client.post(f"/api/audits/{aid}/chat", json={"message": "use jev"}).json()["chat"][-1]
    assert not said["changes"]
    assert "Jev only answers pick-one and yes/no questions" in said["text"]


def test_the_workflow_page_takes_a_label_and_refuses_what_a_step_cannot_run_on(ws, app):
    client, _ = app
    url = "/api/workflows/deep-research/model"
    r = client.post(url, json={"step": "review", "model": JEV})
    assert r.status_code == 400 and "Jev only answers" in r.json()["detail"]
    r = client.post(url, json={"step": "check_support", "model": "Jev"})
    assert r.status_code == 200, r.text
    wf = read_yaml(ws, "definitions/deep-research.workflow.yaml")
    assert step(wf, "check_support")["model"] == JEV
    r = client.post(url, json={"step": "review", "model": "made-up/model"})
    assert r.status_code == 400 and "Standard, Thorough" in r.json()["detail"]
