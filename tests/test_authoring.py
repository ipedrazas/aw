"""The conversation that writes a workflow (prototype): the skill leads, the code keeps
what the model may not decide."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from tests.helpers import make_db
from wf.activities import ModelRequest, ModelResponse, ScriptedModel
from wf.activities.fake import FakeModel
from wf.api.app import AppState, create_app
from wf.author import author_turn, protected_changes, references, skill

FINDINGS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "sources"],
    "properties": {
        "summary": {"type": "string"},
        "sources": {"type": "array", "items": {"type": "string"}},
    },
}
REPORT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "body_md", "sources"],
    "properties": {
        "title": {"type": "string"},
        "body_md": {"type": "string"},
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["line", "claim", "url"],
                "properties": {
                    "line": {"type": "integer"},
                    "claim": {"type": "string"},
                    "url": {"type": "string"},
                },
            },
        },
    },
}


def definition(**extra: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "apiVersion": "workflows.tavon.io/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "market-scan", "version": 1, "description": "Scan a market."},
        "spec": {
            "inputs": {"topic": {"type": "string", "required": True}},
            "steps": [
                {
                    "id": "research",
                    "kind": "agent",
                    "title": "Search the market",
                    "description": "Finds who sells what.",
                    "skill": "skills/market-scan/research.md",
                    "tools": {"search": {"max_calls": 10}},
                    "input": {"topic": "${inputs.topic}"},
                    "output": {"schema": "schemas/market-scan/findings.json"},
                },
                {
                    "id": "write",
                    "kind": "agent",
                    "title": "Write it up",
                    "description": "A short sourced report.",
                    "skill": "skills/market-scan/write.md",
                    "input": {"findings": "${steps.research.output}"},
                    "output": {"schema": "schemas/market-scan/report.json"},
                },
                {
                    "id": "check_links",
                    "kind": "check",
                    "title": "Check the links",
                    "description": "Marks any link that does not open.",
                    "run": "checks.http_resolves",
                    "input": {"sources": "${steps.write.output.sources}"},
                    "on_fail": "annotate",
                },
            ],
        },
    }
    d["spec"].update(extra)
    return d


def draft_args(d: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "definition_yaml": yaml.safe_dump(d or definition(), sort_keys=False),
        "instructions": [
            {"path": "skills/market-scan/research.md", "body": "# Search the market\n\nFind them."},
            {"path": "skills/market-scan/write.md", "body": "# Write it up\n\nWrite `title`."},
        ],
        "results": [
            {"path": "schemas/market-scan/findings.json", "schema_json": json.dumps(FINDINGS)},
            {"path": "schemas/market-scan/report.json", "schema_json": json.dumps(REPORT)},
        ],
    }


def call(req: ModelRequest, tool: str, args: dict[str, Any]) -> Any:
    return next(t for t in req.tools if t.name == tool).executor(args)


def agent(*writes: dict[str, Any], reply: str = "Here it is.", seen: list | None = None):
    """A model that writes each of ``writes`` in turn, keeping what came back."""

    def script(req: ModelRequest) -> ModelResponse:
        for w in writes:
            got = call(req, "write_draft", w)
            if seen is not None:
                seen.append(got)
        return ModelResponse(output={"reply": reply, "ready_to_try": bool(writes)})

    return ScriptedModel(script)


def test_the_skill_is_the_system_prompt_without_its_front_matter():
    meta, body = skill()
    assert meta["name"] == "author-workflow" and "description" in meta
    assert {"definition", "instructions"} <= set(references())
    model = agent()
    author_turn(model, None, [], "hello", None)  # type: ignore[arg-type]
    req = model.requests[0]
    assert req.system == body and not req.system.startswith("---")
    assert {t.name for t in req.tools} >= {"write_draft", "what_the_system_can_do"}


def test_a_turn_that_only_talks_leaves_no_draft(ws):
    out = author_turn(agent(reply="What is it for?"), ws, [], "I scan markets.", None)
    assert out.reply == "What is it for?"
    assert out.definition is None and out.drafts == 0
    assert not ws.definition_path("market-scan").exists()


def test_a_draft_that_loads_is_kept_with_its_files(ws):
    seen: list[Any] = []
    out = author_turn(agent(draft_args(), seen=seen), ws, [], "Draft it.", None)
    assert seen[0]["accepted"] is True
    assert out.drafts == 1 and out.definition is not None
    steps = {s["id"]: s for s in out.definition["spec"]["steps"]}
    # its own instructions, at the version just written
    assert steps["research"]["skill"] == "skills/market-scan/research.md@1"
    assert ws.load_skill("skills/market-scan/research.md@1") is not None
    assert ws.load_schema("schemas/market-scan/report.json") == REPORT
    # a routine gives back its own shape, written for it
    assert steps["check_links"]["output"]["schema"]
    assert ws.definition_path("market-scan").exists()
    assert set(out.wrote) >= {"skills/market-scan/write.md", "schemas/market-scan/findings.json"}


def test_a_draft_that_does_not_load_is_refused_with_the_reasons_and_can_be_fixed(ws):
    bad = definition()
    bad["spec"]["steps"][0]["colour"] = "blue"
    seen: list[Any] = []
    out = author_turn(agent(draft_args(bad), draft_args(), seen=seen), ws, [], "Draft it.", None)
    assert seen[0]["accepted"] is False
    assert any("colour" in e for e in seen[0]["errors"])
    assert seen[1]["accepted"] is True and out.drafts == 1


def test_files_go_only_under_the_workflow_s_own_folders(ws):
    before = ws.path("skills/report-writer.md").read_text()
    args = draft_args()
    args["instructions"].append({"path": "skills/report-writer.md", "body": "# Gone"})
    seen: list[Any] = []
    out = author_turn(agent(args, seen=seen), ws, [], "Draft it.", None)
    assert seen[0]["accepted"] is False and out.definition is None
    assert ws.path("skills/report-writer.md").read_text() == before


def test_a_draft_cannot_take_the_name_of_a_workflow_that_exists(ws):
    d = definition()
    d["metadata"]["name"] = "deep-research"
    seen: list[Any] = []
    author_turn(agent(draft_args(d), seen=seen), ws, [], "Draft it.", None)
    assert seen[0]["accepted"] is False
    assert "already a workflow" in seen[0]["errors"][0]


def test_a_change_to_what_only_the_person_decides_is_said_to_them(ws):
    first = author_turn(agent(draft_args()), ws, [], "Draft it.", None)
    assert first.check_these == []
    pricier = definition(budget={"max_usd": 50})
    pricier["spec"]["steps"][1]["model"] = "claude-opus-5"
    out = author_turn(agent(draft_args(pricier)), ws, [], "Make it better.", first.definition)
    assert "what a run may spend" in out.check_these
    assert any("Write it up" in c and "its model" in c for c in out.check_these)


def test_protected_changes_ignore_everything_else():
    a = definition()
    b = definition()
    b["spec"]["steps"][0]["title"] = "Another title"
    assert protected_changes(a, b) == []


def test_reading_tools_stay_inside_skills_and_schemas(ws):
    def script(req: ModelRequest) -> ModelResponse:
        assert "error" in call(req, "read_workspace_file", {"path": "definitions/../README.md"})
        assert "# Write the report" in call(
            req, "read_workspace_file", {"path": "skills/report-writer.md@2"}
        )
        assert "deep-research" in call(req, "read_workflow", {"name": "deep-research"})
        assert "steps" in call(req, "read_reference", {"name": "definition"})
        caps = call(req, "what_the_system_can_do", {})
        assert caps["routines"] and caps["models"]
        return ModelResponse(output={"reply": "ok", "ready_to_try": False})

    assert author_turn(ScriptedModel(script), ws, [], "hi", None).reply == "ok"


# -- through the app ----------------------------------------------------------------


@pytest.fixture
def app(ws, tmp_path: Path):
    turns: list[list[dict[str, Any]]] = []

    def script(req: ModelRequest) -> ModelResponse:
        writes = turns.pop(0) if turns else []
        for w in writes:
            call(req, "write_draft", w)
        return ModelResponse(
            output={"reply": f"Turn with {len(writes)} drafts.", "ready_to_try": False}
        )

    state = AppState(ws, make_db(), ScriptedModel(script), tmp_path / "artifacts")
    return TestClient(create_app(state)), turns


def test_a_conversation_keeps_its_draft_as_a_draft_of_the_usual_kind(app):
    client, turns = app
    conv = client.post("/api/author").json()
    assert conv["messages"] == [] and conv["draft"] is None

    turns.append([])
    view = client.post(
        f"/api/author/{conv['id']}/message", json={"message": "I scan markets."}
    ).json()
    assert view["draft"] is None and [m["role"] for m in view["messages"]] == ["user", "assistant"]

    turns.append([draft_args()])
    view = client.post(f"/api/author/{conv['id']}/message", json={"message": "Draft it."}).json()
    audit_id = view["draft"]["audit_id"]
    assert view["draft"]["name"] == "market-scan"
    assert [s["title"] for s in view["draft"]["steps"]][:2] == ["Search the market", "Write it up"]
    assert view["messages"][-1]["drafts"] == 1
    # the draft page, and everything it does, works on it
    assert client.get(f"/api/audits/{audit_id}").json()["name"] == "market-scan"

    turns.append([draft_args(definition(budget={"max_usd": 5}))])
    view = client.post(
        f"/api/author/{conv['id']}/message", json={"message": "Cap it at $5."}
    ).json()
    assert view["draft"]["audit_id"] == audit_id  # the same draft, updated
    assert view["messages"][-1]["check_these"] == ["what a run may spend"]

    page = client.get(f"/author/{conv['id']}")
    assert page.status_code == 200 and "Search the market" in page.text


def test_a_new_conversation_page_starts_one(app):
    client, _ = app
    r = client.get("/author/new", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/author/")
    page = client.get(r.headers["location"])
    assert page.status_code == 200 and "What would you like to hand over?" in page.text


def test_an_empty_message_is_refused(app):
    client, _ = app
    conv = client.post("/api/author").json()
    assert (
        client.post(f"/api/author/{conv['id']}/message", json={"message": " "}).status_code == 400
    )


def test_the_new_workflow_page_offers_the_conversation_only_when_turned_on(app, monkeypatch):
    client, _ = app
    assert "/author/new" not in client.get("/audits/new").text
    monkeypatch.setenv("WF_AUTHORING", "on")
    assert "/author/new" in client.get("/audits/new").text


def test_the_offline_model_says_it_cannot_talk(ws):
    out = author_turn(FakeModel(), ws, [], "hello", None)
    assert "offline" in out.reply and out.definition is None
