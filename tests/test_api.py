"""The JSON API, driven end to end with the offline model."""

from __future__ import annotations

import time
from pathlib import Path

import jsonschema
import pytest
from fastapi.testclient import TestClient

from tests.helpers import make_db
from tests.scripted import deep_research_script, schema_filling_script
from tests.test_audit import DOC, extraction_for_process_doc, passage_map
from wf import settings
from wf.activities import ScriptedModel
from wf.api.app import AppState, create_app


@pytest.fixture
def client(ws, tmp_path: Path):
    """A client whose model plays deep research by tag and extracts the sample document faithfully."""
    extracted = extraction_for_process_doc(passage_map(DOC.read_text()))
    play = deep_research_script("go_deeper")
    fill = schema_filling_script({"review": {"verdict": "reject"}})

    def script(req):
        from wf.activities import ModelResponse

        if req.tag == "audit:extract":
            return ModelResponse(
                output=extracted,
                decisions=[
                    {
                        "decision": "Kept your nine steps.",
                        "reason": "Nothing had to be added.",
                        "alternatives": [],
                    }
                ],
            )
        if req.tag == "audit:chat":
            fid = req.input["open_questions"][0]["id"] if req.input["open_questions"] else None
            if req.input["message"].startswith("remove "):
                # the person, from a question's "Chat about this", says the step is not one
                return ModelResponse(
                    output={
                        "reply": f"Removed it. You asked about {req.input['about']['id']}.",
                        "edits": [
                            {
                                "path": f"steps.{req.input['message'].split()[1]}",
                                "value_json": "null",
                                "reason": "That is how it starts, not a step.",
                            }
                        ],
                        "answers": [],
                        "point_to_finding": None,
                    }
                )
            if req.input["message"].startswith("meanwhile "):
                # while the model thinks, the person answers a question on the right
                aid_ = state.audits.list()[0]["id"]
                result, _rec = state.audits.load(aid_)
                fid_ = req.input["message"].split()[1]
                f = next(x for x in result.findings if x.id == fid_)
                state.auditor.answer(result, fid_, f.options[0].value if f.options else "yes")
                state.audits.save(aid_, result, [], by="answer")
                return ModelResponse(
                    output={
                        "reply": "Here is my answer.",
                        "edits": [],
                        "answers": [],
                        "dismiss": [],
                        "point_to_finding": None,
                    }
                )
            if req.input["message"].startswith("close "):
                # the person says the question does not apply; nothing in the draft changes
                return ModelResponse(
                    output={
                        "reply": "Closed it.",
                        "edits": [],
                        "answers": [],
                        "dismiss": [
                            {
                                "finding_id": req.input["message"].split()[1],
                                "reason": "The topic comes from a form, so nobody waits.",
                            }
                        ],
                        "point_to_finding": None,
                    }
                )
            if req.input["message"].startswith("answer "):
                # the model writes prose into a question that only takes one of its choices
                return ModelResponse(
                    output={
                        "reply": "Recorded as an answer.",
                        "edits": [],
                        "answers": [
                            {
                                "finding_id": req.input["message"].split()[1],
                                "option_index": None,
                                "text": "Whatever the requester decides when we get there.",
                            }
                        ],
                        "point_to_finding": None,
                    }
                )
            return ModelResponse(
                output={
                    "reply": "Whichever you choose, and it is the first question on the right.",
                    "edits": [
                        {
                            "path": "metadata.description",
                            "value_json": '"Research a topic and send a sourced report."',
                            "reason": "You asked for a shorter description.",
                        }
                    ],
                    "answers": [],
                    "point_to_finding": fid,
                }
            )
        try:
            resp = play(req)
            jsonschema.validate(resp.output, req.output_schema)
            return resp
        except (AssertionError, jsonschema.ValidationError):
            return fill(req)

    state = AppState(ws, make_db(), ScriptedModel(script), tmp_path / "artifacts")
    from wf.activities import ActivityPolicy

    state.runner.activities.policy = ActivityPolicy(retries=0)
    return TestClient(create_app(state))


def wait_for(client: TestClient, run_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/runs/{run_id}").json()
        if data["status"] != "running":
            return data
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_health_and_workflow_listing(client):
    health = client.get("/healthz").json()
    assert health["ok"] is True
    assert health["provider"] == settings.provider(), "which gateway this one asks"
    wfs = client.get("/api/workflows").json()
    names = {w["name"] for w in wfs}
    assert "deep-research" in names
    dr = next(w for w in wfs if w["name"] == "deep-research")
    assert dr["step_count"] == 8 and dr["open_findings"] == 0


def test_workflow_view_is_in_plain_language(client):
    data = client.get("/api/workflows/deep-research").json()
    steps = data["steps"]
    assert [s["n"] for s in steps] == list(range(1, 9))
    titles = " ".join(s["title"] + " " + s["description"] for s in steps)
    assert "claude" not in titles.lower() and ".md" not in titles and "exa" not in titles.lower()
    assert steps[0]["origin"] == "I suggested this"
    assert steps[6]["trust"] == "Always asks you"
    assert steps[3]["does_not_check"] == [
        "Whether the page supports the claim",
        "How reliable or recent the source is",
    ]
    assert steps[6]["when"].startswith("Only if")
    assert steps[1]["technical"]["model"] == "claude-sonnet-5"
    assert data["summary"]["recursion"].startswith("Yes")
    assert "durable-execution" in data["cases"]


def test_run_a_workflow_against_a_case_and_read_the_report(client):
    r = client.post("/api/workflows/deep-research/runs", json={"case": "durable-execution"})
    assert r.status_code == 200
    run = wait_for(client, r.json()["run_id"])
    assert run["status"] == "done", run["error"]
    report = run["report"]
    assert report["case_name"] == "durable-execution"
    assert report["first_divergence"] is not None
    assert report["artifacts"][0]["simulated"] is True
    art = report["artifacts"][0]
    pdf = client.get(f"/api/runs/{run['id']}/artifacts/{art['id']}")
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    runs = client.get("/api/runs").json()
    assert runs[0]["id"] == run["id"] and runs[0]["steps"][0]["status"] == "done"
    page = client.get(f"/runs/{run['id']}").text
    check = next(s for s in run["steps"] if s["kind"] == "check")
    out = check["output"]
    assert f"{out['open_count']} of {out['total']} links open" in page
    assert "recorded fixtures" in page and "No model involved" in page
    # what each agent step was sent, and what it searched for, as it happened
    assert (
        "Instructions (system prompt)" in page
        and "&lt;data source=&#34;step input&#34;&gt;" in page
    )
    assert "What it searched and read" in page and 'pill-blue">search</span>' in page


def test_a_real_run_starts_from_the_page_and_says_so(client):
    r = client.post(
        "/api/workflows/deep-research/runs", json={"case": "durable-execution", "mode": "live"}
    )
    assert r.status_code == 200, r.text
    run = wait_for(client, r.json()["run_id"])
    assert run["mode"] == "live" and run["status"] in ("done", "waiting"), run["error"]
    page = client.get(f"/runs/{run['id']}").text
    assert "Real run" in page and "Dry run</span>" not in page
    assert 'value="live"' in client.get("/workflows/deep-research").text


def test_audit_answer_chat_undo_and_dry_run(client):
    r = client.post("/api/audits", json={"document": DOC.read_text(), "name": "client-research"})
    assert r.status_code == 200, r.text
    audit = r.json()
    aid = audit["id"]
    assert audit["counts"]["open"] > 5
    assert audit["chat"][0]["role"] == "assistant" and "steps" in audit["chat"][0]["text"]
    q_fields = [f["field"] for g in audit["questions"] for f in g["findings"]]
    assert "steps.review.output.continue_on.verdict.reject" in q_fields
    assert audit["diff"]["counts"]["stated"] > 0

    # answer a question with a plain control
    reject = next(
        f
        for g in audit["questions"]
        for f in g["findings"]
        if f["field"].endswith("verdict.reject")
    )
    stop = next(o for o in reject["options"] if o["value"].get("op") == "stop")
    a = client.post(
        f"/api/audits/{aid}/answer", json={"finding_id": reject["id"], "answer": stop["value"]}
    ).json()
    assert a["counts"]["answered"] == 1
    assert any(s["id"] == "review_reject_stop" for s in a["steps"])
    assert a["changes"][-1]["by"] == "answer" and a["changes"][-1]["reason"]

    # the chat edits the draft with a reason, and points at a question
    c = client.post(
        f"/api/audits/{aid}/chat", json={"message": "Make the description shorter"}
    ).json()
    assert c["summary"]["title"] == "Research a topic and send a sourced report."
    assert c["chat"][-1]["role"] == "assistant" and c["chat"][-1]["changes"]
    assert c["chat"][-1]["point_to_finding"]

    # and the change can be undone
    seq = c["changes"][-1]["seq"]
    u = client.post(f"/api/audits/{aid}/undo", json={"seq": seq}).json()
    assert u["summary"]["title"] != "Research a topic and send a sourced report."
    assert any(ch["undone"] for ch in u["changes"])

    # a dry run of the draft shows where it had to guess
    d = client.post(
        f"/api/audits/{aid}/dry-run", json={"topic": "Durable execution platforms for AI agents"}
    )
    run = wait_for(client, d.json()["run_id"])
    assert run["status"] == "done", [
        (s["step_id"], s["error"], [x["reason"] for x in s["decisions"] if x["kind"] == "control"])
        for s in run["steps"]
        if s["status"] == "failed"
    ] or run["error"]
    guessed = {g["field"] for g in run["report"]["guesses"]}
    assert "steps.send.requires_approval" in guessed
    assert "steps.check_links.does_not_check" in guessed
    assert run["report"]["artifacts"][0]["name"].startswith("SIMULATED-")
    assert client.get(f"/api/audits/{aid}").json()["runs"][0]["id"] == run["id"]

    # two runs can be diffed
    d2 = client.post(
        f"/api/audits/{aid}/dry-run", json={"topic": "Durable execution platforms for AI agents"}
    )
    run2 = wait_for(client, d2.json()["run_id"])
    diff = client.get(f"/api/runs/{run['id']}/diff/{run2['id']}").json()
    assert diff["run_a"] == run["id"] and diff["steps"]

    # saving writes the definition into the workspace
    s = client.post(f"/api/audits/{aid}/save").json()
    assert s["saved"] and s["workflow"] == "client-research"
    assert "client-research" in {w["name"] for w in client.get("/api/workflows").json()}


def test_a_finished_run_is_read_once_so_its_status_and_its_guesses_agree(client, monkeypatch):
    """The poll read the report and the status in two reads of the database. A guess
    recorded between them was missing from a run the same response called done."""
    audit = client.post(
        "/api/audits", json={"document": DOC.read_text(), "name": "client-research"}
    ).json()
    d = client.post(f"/api/audits/{audit['id']}/dry-run", json={"topic": "Durable execution"})
    run_id = d.json()["run_id"]
    expected = [g["field"] for g in wait_for(client, run_id)["report"]["guesses"]]
    assert "steps.send.requires_approval" in expected, "the last step had to guess"

    runner = client.app.state.wf.runner
    real = runner.snapshot
    seen: list[str] = []

    def mid_run_then_finished(rid: str) -> dict:
        """The first read lands while the last step is still going."""
        snap = real(rid)
        if not seen:
            seen.append(rid)
            return {
                **snap,
                "status": "running",
                "steps": [{**s, "decisions": []} for s in snap["steps"]],
            }
        return snap

    monkeypatch.setattr(runner, "snapshot", mid_run_then_finished)
    data = client.get(f"/api/runs/{run_id}").json()
    assert data["status"] == "running", "the status came from the read that was taken"
    assert [g["field"] for g in data["report"]["guesses"]] == []


def test_typed_answer_is_accepted_on_a_question_with_fixed_options(client):
    """When none of the offered choices is the real answer, free text still gets stored."""
    audit = client.post(
        "/api/audits", json={"document": DOC.read_text(), "name": "client-research"}
    ).json()
    aid = audit["id"]
    approval = next(
        f
        for g in audit["questions"]
        for f in g["findings"]
        if f["field"] == "steps.send.requires_approval"
    )
    assert approval["answer_kind"] == "choice"
    assert approval["options"], "the question offers fixed choices"

    a = client.post(
        f"/api/audits/{aid}/answer",
        json={"finding_id": approval["id"], "answer": "the account lead"},
    )
    assert a.status_code == 200, a.text
    body = a.json()
    assert body["counts"]["answered"] == 1
    step = next(s for s in body["steps"] if s["id"] == "send")
    assert step["requires_approval"] == "the account lead"

    # a fixed-option question still answers the normal way, no regression
    reject = next(
        f
        for g in audit["questions"]
        for f in g["findings"]
        if f["field"].endswith("verdict.reject")
    )
    stop = next(o for o in reject["options"] if o["value"].get("op") == "stop")
    r = client.post(
        f"/api/audits/{aid}/answer", json={"finding_id": reject["id"], "answer": stop["value"]}
    )
    assert r.status_code == 200, r.text
    assert any(s["id"] == "review_reject_stop" for s in r.json()["steps"])


def test_a_draft_can_be_deleted_and_its_sessions_are_kept(client):
    aid = client.post(
        "/api/audits", json={"document": DOC.read_text(), "name": "client-research"}
    ).json()["id"]
    sessions = client.get(f"/api/sessions?audit={aid}").json()
    assert sessions, "drafting asked a model something"
    page = client.get(f"/audits/{aid}")
    assert page.status_code == 200 and f'data-delete="/api/audits/{aid}"' in page.text

    assert client.delete(f"/api/audits/{aid}").json()["deleted"] == aid
    assert client.get(f"/api/audits/{aid}").status_code == 404
    assert aid not in {a["id"] for a in client.get("/api/audits").json()}
    assert client.delete(f"/api/audits/{aid}").status_code == 404
    # what the model was asked is a record of what happened, and outlives the draft
    assert [s["id"] for s in client.get(f"/api/sessions?audit={aid}").json()] == [
        s["id"] for s in sessions
    ]


def test_a_workflow_can_be_deleted_and_its_runs_are_kept(client, ws):
    r = client.post("/api/workflows/deep-research/runs", json={"case": "durable-execution"})
    run = wait_for(client, r.json()["run_id"])

    listing = client.get("/workflows")
    assert 'data-delete="/api/workflows/deep-research"' in listing.text

    body = client.delete("/api/workflows/deep-research").json()
    assert body["deleted"] == "deep-research"
    assert body["removed"] == ["definitions/deep-research.workflow.yaml"]
    assert body["runs_kept"] == 1
    assert not ws.definition_path("deep-research").exists()
    assert client.get("/api/workflows/deep-research").status_code == 404
    assert "deep-research" not in {w["name"] for w in client.get("/api/workflows").json()}
    assert client.get(f"/api/runs/{run['id']}").json()["status"] == "done"
    assert client.delete("/api/workflows/deep-research").status_code == 404


def test_deleting_a_workflow_takes_the_files_its_draft_wrote_and_nothing_else(client, ws):
    aid = client.post(
        "/api/audits", json={"document": DOC.read_text(), "name": "client-research"}
    ).json()["id"]
    assert client.post(f"/api/audits/{aid}/save").json()["saved"]
    assert ws.path("skills/client-research").is_dir()

    removed = client.delete("/api/workflows/client-research").json()["removed"]
    assert any(p.startswith("skills/client-research/") for p in removed)
    assert not ws.path("skills/client-research").exists()
    assert not ws.path("schemas/client-research").exists()
    # instruction files shared between definitions sit at the top of skills/ and stay
    assert ws.path("skills/deep-researcher.md").is_file()


def _missing_file_questions(findings) -> list:
    return [f for f in findings if "not in the workspace" in (f.get("detail") or "")]


def test_files_a_step_names_are_written_again_instead_of_asked_about(client, ws):
    """A workflow deleted under its draft, or renamed before renames moved references,
    leaves steps pointing at nothing. Those files are written back, not asked about."""
    import shutil

    aid = client.post(
        "/api/audits", json={"document": DOC.read_text(), "name": "client-research"}
    ).json()["id"]
    assert client.post(f"/api/audits/{aid}/save").json()["saved"]
    shutil.rmtree(ws.path("skills/client-research"))
    shutil.rmtree(ws.path("schemas/client-research"))

    audit = client.get(f"/api/audits/{aid}").json()
    assert not _missing_file_questions(
        audit["answered"] + [f for g in audit["questions"] for f in g["findings"]]
    )
    assert ws.path("skills/client-research").is_dir()

    shutil.rmtree(ws.path("schemas/client-research"))
    wf = client.get("/api/workflows/client-research").json()
    assert not _missing_file_questions(wf["findings"])
    assert ws.path("schemas/client-research").is_dir()


def test_a_name_that_is_a_path_is_not_a_definition(client, ws):
    assert client.delete("/api/workflows/..%2F..%2Fworkspace").status_code == 404
    assert ws.path("definitions").is_dir()


def test_a_workflow_can_be_renamed_and_its_runs_keep_the_old_name(client, ws):
    r = client.post("/api/workflows/deep-research/runs", json={"case": "durable-execution"})
    run = wait_for(client, r.json()["run_id"])

    listing = client.get("/workflows")
    assert 'data-rename="/api/workflows/deep-research/rename"' in listing.text

    body = client.post(
        "/api/workflows/deep-research/rename", json={"name": "deeper-research"}
    ).json()
    assert body == {
        "renamed": "deep-research",
        "name": "deeper-research",
        "changed": body["changed"],
        "commit": body["commit"],
        "drafts": 0,
    }
    assert not ws.definition_path("deep-research").exists()
    assert ws.definition_path("deeper-research").exists()
    assert client.get("/api/workflows/deep-research").status_code == 404
    names = {w["name"] for w in client.get("/api/workflows").json()}
    assert names == {"deeper-research"}
    assert (
        client.get("/api/workflows/deeper-research").json()["summary"]["name"] == "deeper-research"
    )
    # past runs are what happened: they keep saying the name the workflow had then
    assert client.get(f"/api/runs/{run['id']}").json()["status"] == "done"


def test_renaming_a_workflow_moves_the_files_its_draft_wrote(client, ws):
    aid = client.post(
        "/api/audits", json={"document": DOC.read_text(), "name": "client-research"}
    ).json()["id"]
    assert client.post(f"/api/audits/{aid}/save").json()["saved"]
    assert ws.path("skills/client-research").is_dir()

    changed = client.post(
        "/api/workflows/client-research/rename", json={"name": "client-research-v2"}
    ).json()["changed"]
    assert any(p.startswith("skills/client-research-v2/") for p in changed)
    assert not ws.path("skills/client-research").exists()
    assert ws.path("skills/client-research-v2").is_dir()
    # instruction files shared between definitions sit at the top of skills/ and stay
    assert ws.path("skills/deep-researcher.md").is_file()
    # the steps point at the files where they went, so nothing asks where they are
    wf = client.get("/api/workflows/client-research-v2").json()
    assert "yaml" in wf and "skills/client-research/" not in wf["yaml"]
    assert not [f for f in wf["findings"] if "not in the workspace" in (f["detail"] or "")]
    # and the draft it was saved from follows it
    audit = client.get(f"/api/audits/{aid}").json()
    assert audit["name"] == "client-research-v2"
    assert "skills/client-research/" not in audit["yaml"]


def test_renaming_to_a_name_already_taken_is_refused(client, ws):
    aid = client.post(
        "/api/audits", json={"document": DOC.read_text(), "name": "client-research"}
    ).json()["id"]
    assert client.post(f"/api/audits/{aid}/save").json()["saved"]

    r = client.post("/api/workflows/deep-research/rename", json={"name": "client-research"})
    assert r.status_code == 400
    assert ws.definition_path("deep-research").exists()


def test_renaming_a_missing_workflow_or_to_a_path_is_refused(client, ws):
    assert (
        client.post("/api/workflows/no-such-workflow/rename", json={"name": "x"}).status_code == 404
    )
    r = client.post("/api/workflows/deep-research/rename", json={"name": "../../workspace"})
    assert r.status_code == 400
    assert ws.definition_path("deep-research").exists()


def test_live_run_is_refused_while_questions_are_open(client):
    r = client.post("/api/audits", json={"document": DOC.read_text(), "name": "client-research"})
    assert r.status_code == 200
    r = client.post(
        "/api/workflows/client-research/runs", json={"mode": "live", "inputs": {"topic": "x" * 20}}
    )
    assert r.status_code == 409


def test_pages_render_or_say_they_are_missing(client):
    for path in ["/workflows", "/workflows/deep-research", "/audits/new", "/runs"]:
        r = client.get(path)
        assert r.status_code == 200, path


def test_chat_answer_that_does_not_fit_leaves_the_question_open(client):
    """The chat writing prose into a question with fixed choices is told so, not a 500."""
    audit = client.post(
        "/api/audits", json={"document": DOC.read_text(), "name": "client-research"}
    ).json()
    aid = audit["id"]
    budget = next(
        f for g in audit["questions"] for f in g["findings"] if f["field"] == "spec.budget"
    )

    c = client.post(f"/api/audits/{aid}/chat", json={"message": f"answer {budget['id']}"})
    assert c.status_code == 200, c.text
    body = c.json()
    assert "so the draft is unchanged" in body["chat"][-1]["text"]
    assert not body["chat"][-1]["changes"]
    assert any(f["id"] == budget["id"] for g in body["questions"] for f in g["findings"])

    # and the same answer through the control returns a reason, not a crash
    bad = client.post(
        f"/api/audits/{aid}/answer",
        json={"finding_id": budget["id"], "answer": "whatever it takes"},
    )
    assert bad.status_code == 400
    assert "so the draft is unchanged" in bad.json()["detail"]


def test_chat_about_a_question_can_remove_the_step_it_rests_on(client):
    """“Chat about this” sends the question along; the chat can take the step out, and undo it."""
    audit = client.post(
        "/api/audits", json={"document": DOC.read_text(), "name": "client-research"}
    ).json()
    aid = audit["id"]
    page = client.get(f"/audits/{aid}").text
    assert "Chat about this" in page and "Type your answer" not in page
    fid = audit["questions"][0]["findings"][0]["id"]

    c = client.post(f"/api/audits/{aid}/chat", json={"message": "remove export_pdf", "about": fid})
    assert c.status_code == 200, c.text
    body = c.json()
    assert fid in body["chat"][-1]["text"], "the chat was told which question"
    assert body["chat"][-1]["changes"][0]["path"] == "spec.steps"
    assert "id: export_pdf" not in body["yaml"]

    seq = next(ch["seq"] for ch in body["changes"] if ch["path"] == "spec.steps")
    u = client.post(f"/api/audits/{aid}/undo", json={"seq": seq})
    assert u.status_code == 200, u.text
    assert "id: export_pdf" in u.json()["yaml"]


def test_audit_page_serves_fresh_assets_and_a_box_for_no(client):
    """A changed app.js reaches the browser, and “No, I will answer this” has somewhere to answer."""
    audit = client.post(
        "/api/audits", json={"document": DOC.read_text(), "name": "client-research"}
    ).json()
    page = client.get(f"/audits/{audit['id']}").text
    assert "/static/app.js?v=" in page and "/static/app.css?v=" in page
    assert "chat-send" not in page and "Enter to send" in page
    if any(f["type"] == "assumption" for g in audit["questions"] for f in g["findings"]):
        assert "data-own" in page


@pytest.mark.parametrize(
    "path", ["steps.brief", "spec.steps.brief", "spec.steps.0", "spec.steps[0]"]
)
def test_removing_a_step_takes_its_questions_with_it(client, path):
    """However the chat names the step, it goes, and so does every question that rested on it."""
    from wf.api.app import _audit
    from wf.validate import Finding

    aid = client.post("/api/audits", json={"document": DOC.read_text(), "name": "p"}).json()["id"]
    st = client.app.state.wf
    result, _rec = _audit(st, aid)
    # a question the auditor raised about the step, as “we assumed…” questions are
    result.findings.append(
        Finding(
            type="assumption",
            step_id="brief",
            field="steps.brief.when",
            question="Is it?",
            raised_by="auditor",
        )
    )
    st.auditor.set_field(result, path, None, "That is how it starts, not a step.")
    assert "brief" not in [s["id"] for s in result.definition["spec"]["steps"]]
    assert not [f for f in result.open_findings() if f.step_id == "brief"]


def test_an_edit_that_does_not_go_in_is_said_in_the_chat(client):
    audit = client.post("/api/audits", json={"document": DOC.read_text(), "name": "p"}).json()
    fid = audit["questions"][0]["findings"][0]["id"]
    body = client.post(
        f"/api/audits/{audit['id']}/chat", json={"message": "remove no_such_step", "about": fid}
    ).json()
    assert "unchanged" in body["chat"][-1]["text"]
    assert not body["chat"][-1]["changes"]


def test_chat_can_close_a_question_that_does_not_apply(client):
    """Closed, not answered: the draft is unchanged, the question says why, and it can be reopened."""
    audit = client.post("/api/audits", json={"document": DOC.read_text(), "name": "p"}).json()
    aid, fid = audit["id"], audit["questions"][0]["findings"][0]["id"]
    body = client.post(f"/api/audits/{aid}/chat", json={"message": f"close {fid}"}).json()
    assert body["yaml"] == audit["yaml"], "closing a question does not touch the draft"
    assert fid not in [f["id"] for g in body["questions"] for f in g["findings"]]
    closed = next(f for f in body["answered"] if f["id"] == fid)
    assert closed["status"] == "dismissed" and "form" in closed["answer"]
    assert body["chat"][-1]["closed"][0]["finding_id"] == fid
    page = client.get(f"/audits/{aid}").text
    assert "Closed:" in page and "Doesn't apply: The topic comes from a form" in page

    # a later edit rebuilds the questions; the closed one stays closed
    client.post(f"/api/audits/{aid}/chat", json={"message": "shorter please"})
    again = client.get(f"/api/audits/{aid}").json()
    assert fid not in [f["id"] for g in again["questions"] for f in g["findings"]]

    # closing an unknown question does nothing
    body = client.post(f"/api/audits/{aid}/chat", json={"message": "close nope"}).json()
    assert body["chat"][-1]["closed"] == []


def test_an_answer_given_while_the_chat_thinks_is_kept(client):
    """Chatting and answering are separate: neither overwrites the other."""
    audit = client.post("/api/audits", json={"document": DOC.read_text(), "name": "p"}).json()
    f = next(f for g in audit["questions"] for f in g["findings"] if f["options"])
    body = client.post(
        f"/api/audits/{audit['id']}/chat", json={"message": f"meanwhile {f['id']}"}
    ).json()
    assert body["chat"][-1]["text"] == "Here is my answer."
    assert f["id"] in [a["id"] for a in body["answered"]], "the answer given meanwhile survived"


def test_a_step_that_runs_a_routine_gives_back_the_routines_shape(client):
    """A document's guess at a check's output does not outlive choosing the routine."""
    from wf.api.app import _audit
    from wf.interpret.registry import RUNNER_OUTPUT_SCHEMAS

    aid = client.post("/api/audits", json={"document": DOC.read_text(), "name": "p"}).json()["id"]
    st = client.app.state.wf
    result, _rec = _audit(st, aid)
    check = next(s for s in result.definition["spec"]["steps"] if s["kind"] == "check")
    rel = check["output"]["schema"]
    st.ws.save_schema(
        rel,
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"ok": {"type": "boolean"}},
        },
    )
    st.auditor.set_field(result, f"steps.{check['id']}.run", "checks.http_resolves", "Chosen.")
    assert st.ws.load_schema(rel) == RUNNER_OUTPUT_SCHEMAS["checks.http_resolves"]

    # a draft saved with the wrong shape is put right when it is saved again
    st.ws.save_schema(rel, {"type": "object", "properties": {}})
    assert client.post(f"/api/audits/{aid}/save").status_code == 200
    assert st.ws.load_schema(rel) == RUNNER_OUTPUT_SCHEMAS["checks.http_resolves"]


def test_a_topic_only_a_wait_reads_is_asked_about_and_fixed(client):
    """“Get my topic” waits, reads the topic and passes nothing on: the steps after it had no topic."""
    from wf.api.app import _audit

    aid = client.post("/api/audits", json={"document": DOC.read_text(), "name": "p"}).json()["id"]
    st = client.app.state.wf
    result, _rec = _audit(st, aid)
    steps = result.definition["spec"]["steps"]
    brief = steps[0]
    wait = {
        "id": "get_topic",
        "kind": "wait",
        "title": "Get my topic",
        "input": {"topic": "${inputs.topic}"},
    }
    brief.pop("input")
    st.auditor.set_field(result, "spec.steps", [wait, *steps], "As d6 had it.")
    st.audits.save(aid, result)

    f = next(f for f in result.open_findings() if f.field == "spec.inputs.topic.read_by")
    assert "Get my topic" in f.detail and "passes nothing on" in f.detail
    remove = next(o for o in f.options if isinstance(o.value, dict) and o.value.get("remove"))
    r = client.post(f"/api/audits/{aid}/answer", json={"finding_id": f.id, "answer": remove.value})
    assert r.status_code == 200, r.text
    result, _rec = _audit(st, aid)
    assert [s["id"] for s in result.definition["spec"]["steps"]][0] == "brief"
    assert result.definition["spec"]["steps"][0]["input"] == {"topic": "${inputs.topic}"}
    assert not [x for x in result.open_findings() if x.field.endswith(".read_by")]


def test_an_open_question_on_a_workflow_is_answered_on_its_page(client, ws):
    from tests.helpers import read_yaml, write_yaml

    rel = "definitions/deep-research.workflow.yaml"
    d = read_yaml(ws, rel)
    go = next(s for s in d["spec"]["steps"] if s["id"] == "go_deeper")
    go.pop("limits")
    write_yaml(ws, rel, d)

    wf = client.get("/api/workflows/deep-research").json()
    q = next(f for f in wf["findings"] if f["field"] == "steps.go_deeper.limits")
    assert q["status"] == "open" and q["answer_kind"] == "choice" and q["options"]
    page = client.get("/workflows/deep-research").text
    assert 'data-workflow-answers="deep-research"' in page and f'data-answer="{q["id"]}"' in page

    # words that set nothing are refused, not recorded as an answer
    r = client.post(
        "/api/workflows/deep-research/answer", json={"finding_id": q["id"], "answer": "not far"}
    )
    assert r.status_code == 400
    assert "steps.go_deeper.limits" in [
        f["field"] for f in client.get("/api/workflows/deep-research").json()["findings"]
    ]

    r = client.post(
        "/api/workflows/deep-research/answer",
        json={"finding_id": q["id"], "answer": q["options"][0]["value"]},
    )
    assert r.status_code == 200, r.text
    assert "steps.go_deeper.limits" not in [f["field"] for f in r.json()["findings"]]
    limits = next(s for s in read_yaml(ws, rel)["spec"]["steps"] if s["id"] == "go_deeper")[
        "limits"
    ]
    assert limits["max_depth"] == 1 and limits["max_fanout"] == 3


def test_limits_typed_in_words_are_read_as_numbers():
    from wf.audit.question import _parse_limits

    assert _parse_limits("2 levels, 3 at a time") == {"max_depth": 2, "max_fanout": 3}
    assert _parse_limits("just 1") == {"max_depth": 1}
