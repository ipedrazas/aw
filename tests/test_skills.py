"""Instruction files are edited as new versions, and every earlier version stays readable."""

from __future__ import annotations

import shutil

from tests.helpers import read_yaml, step
from tests.test_api import client  # noqa: F401  (pytest fixture)
from wf.schema import WorkspaceError
from wf.validate import validate

DEF = "definitions/deep-research.workflow.yaml"
WRITER = "skills/report-writer.md"


def test_saving_a_new_version_keeps_the_one_it_replaces(ws):
    before = ws.load_skill(f"{WRITER}@2")
    version, written = ws.save_skill_version(WRITER, "# Write the report\n\nShorter, please.\n")
    assert version == 3
    assert written == ["skills/.versions/report-writer/2.md", WRITER]
    assert ws.skill_versions(WRITER) == [2, 3]

    now = ws.load_skill(f"{WRITER}@3")
    assert now.version == 3 and "Shorter, please." in now.body and now.file == WRITER
    # the kept copy is the old file, byte for byte, so its sha256 is what a run recorded
    kept = ws.load_skill(f"{WRITER}@2")
    assert kept.content == before.content and kept.sha256 == before.sha256
    assert kept.path == WRITER and kept.file == "skills/.versions/report-writer/2.md"


def test_front_matter_typed_into_the_editor_does_not_set_the_version(ws):
    version, _ = ws.save_skill_version(WRITER, "---\nversion: 9\n---\n# Write\n\nNew.\n")
    assert version == 3
    assert ws.path(WRITER).read_text().startswith("---\nversion: 3\n---\n# Write")


def test_a_step_pinned_to_a_kept_version_is_not_a_conflict(ws):
    ws.save_skill_version(WRITER, "# Write the report\n\nNew text.\n")
    # the definition still pins @2; that version is kept, so the step reads it as it was
    assert [
        f for f in validate(ws.load_definition("deep-research"), ws).findings if f.status == "open"
    ] == []

    # a file edited by hand, without keeping the old version, is still a conflict
    p = ws.path("skills/report-reviewer.md")
    p.write_text(p.read_text().replace("version: 3", "version: 4", 1))
    findings = validate(ws.load_definition("deep-research"), ws).findings
    assert [(f.type, f.field) for f in findings] == [("conflict", "steps.review.skill")]


def test_versions_go_with_the_directory_the_file_is_in(ws):
    rel = "skills/deep-research/plan.md"
    ws.save_skill(rel, 1, "# Plan\n\nOne.\n")
    ws.save_skill_version(rel, "# Plan\n\nTwo.\n")
    assert ws.exists("skills/deep-research/.versions/plan/1.md")
    shutil.rmtree(ws.path("skills/deep-research"))
    assert ws.skill_versions(rel) == []


def test_saving_a_version_of_a_file_that_is_not_there_is_refused(ws):
    try:
        ws.save_skill_version("skills/nothing.md", "# x\n")
    except WorkspaceError as e:
        assert "no instruction file" in str(e)
    else:
        raise AssertionError("expected a WorkspaceError")


def test_editing_a_step_s_instructions_from_its_workflow(client, ws):  # noqa: F811
    view = client.get(
        "/api/skill", params={"path": WRITER, "workflow": "deep-research", "step": "write"}
    ).json()
    assert view["latest"] == 2 and [v["version"] for v in view["versions"]] == [2]
    assert view["edit"]["pinned"] == 2 and view["edit"]["step_title"] == "Write the report"
    # the step earns its trust under the workflow's defaults, and an edit starts it again
    assert "starts this step's trust again" in view["edit"]["trust_resets"]
    assert view["used_by"] == [
        {
            "workflow": "deep-research",
            "step_id": "write",
            "step_title": "Write the report",
            "version": 2,
        }
    ]

    body = view["edit"]["body"].replace("Do not pad.", "Do not pad, and keep it under two pages.")
    r = client.post(
        "/api/workflows/deep-research/steps/write/skill", json={"body": body, "latest": 2}
    )
    assert r.status_code == 200, r.text
    assert r.json()["skill"] == f"{WRITER}@3" and r.json()["before"] == f"{WRITER}@2"
    assert step(read_yaml(ws, DEF), "write")["skill"] == f"{WRITER}@3"
    assert "under two pages" in ws.load_skill(f"{WRITER}@3").body
    assert "under two pages" not in ws.load_skill(f"{WRITER}@2").body

    # the workflow has nothing open because of it
    wf = client.get("/api/workflows/deep-research").json()
    assert not [f for f in wf["findings"] if f["field"] == "steps.write.skill"]

    # both versions are listed, and can be compared line by line
    view = client.get("/api/skill", params={"path": WRITER, "a": 2, "b": 3}).json()
    assert [v["version"] for v in view["versions"]] == [3, 2]
    ops = {r["op"] for r in view["diff"]["rows"]}
    assert {"+", "-"} <= ops
    assert any("under two pages" in r["text"] for r in view["diff"]["rows"] if r["op"] == "+")

    # an edit made from a page that is out of date is refused, and nothing is written
    r = client.post(
        "/api/workflows/deep-research/steps/write/skill", json={"body": "# Other\n", "latest": 2}
    )
    assert r.status_code == 409 and "version 3" in r.json()["detail"]
    assert ws.skill_versions(WRITER) == [2, 3]

    # saving what is already there makes no version
    r = client.post(
        "/api/workflows/deep-research/steps/write/skill", json={"body": body, "latest": 3}
    )
    assert r.status_code == 400 and "Nothing changed" in r.json()["detail"]


def test_a_step_with_its_own_trust_says_nothing_resets(client):  # noqa: F811
    view = client.get(
        "/api/skill",
        params={"path": "skills/research-brief.md", "workflow": "deep-research", "step": "plan"},
    ).json()
    # "plan" earns trust but names nothing that resets it
    assert view["edit"]["trust_resets"] is None


def test_the_skill_page_only_shows_instruction_files(client):  # noqa: F811
    for path in (
        DEF,
        "skills/../definitions/deep-research.workflow.yaml",
        "skills/.versions/x/1.md",
    ):
        assert client.get("/api/skill", params={"path": path}).status_code == 404
    assert client.get("/api/skill", params={"path": "skills/nothing.md"}).status_code == 404
    r = client.get(
        "/api/skill", params={"path": WRITER, "workflow": "deep-research", "step": "plan"}
    )
    assert r.status_code == 404 and "does not use" in r.json()["detail"]


def test_the_pages_link_to_the_instructions(client, ws):  # noqa: F811
    page = client.get("/workflows/deep-research").text
    assert (
        "/skill?path=skills%2Freport-writer.md&amp;v=2&amp;workflow=deep-research&amp;step=write"
        in page
    )

    page = client.get(
        "/skill", params={"path": WRITER, "workflow": "deep-research", "step": "write"}
    ).text
    assert 'data-skill-edit="/api/workflows/deep-research/steps/write/skill"' in page
    assert "Save as version 3" in page and "starts this step&#39;s trust again" in page

    client.post(
        "/api/workflows/deep-research/steps/write/skill",
        json={"body": "# Write the report\n\nNew.\n", "latest": 2},
    )
    page = client.get("/skill", params={"path": WRITER, "a": 2, "b": 3}).text
    assert 'class="textdiff"' in page and "Version 2 to version 3" in page


def test_a_run_links_to_the_version_it_used(client, ws):  # noqa: F811
    r = client.post("/api/workflows/deep-research/runs", json={"case": "durable-execution"})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    from tests.test_api import wait_for

    run = wait_for(client, run_id)
    write = next(s for s in run["steps"] if s["step_id"] == "write")
    assert write["instruction_ref"] == f"{WRITER}@2" and write["instruction_sha256"]

    # the instructions move on; the run still points at what it read
    client.post(
        "/api/workflows/deep-research/steps/write/skill",
        json={"body": "# Write the report\n\nNew.\n", "latest": 2},
    )
    page = client.get(f"/runs/{run_id}").text
    assert "/skill?path=skills%2Freport-writer.md&amp;v=2&amp;sha256=" in page
    view = client.get(
        "/api/skill", params={"path": WRITER, "v": 2, "sha256": write["instruction_sha256"]}
    ).json()
    assert view["shown"]["version"] == 2 and view["shown"]["matches_run"] is True
