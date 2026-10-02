"""Two things the second round of feedback found: a run with no step that makes a file
said it produced nothing, though it wrote the report; and the chat agreed to add a step
it had no way to add."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers import read_yaml, write_yaml
from tests.scripted import deep_research_script
from tests.test_audit import DOC, scripted_auditor
from tests.test_interpreter import TOPIC, make
from wf.activities import ScriptedModel
from wf.audit import AnswerRejected
from wf.store import Artifact

DEF = "definitions/deep-research.workflow.yaml"


def without_the_pdf(ws) -> None:
    d = read_yaml(ws, DEF)
    d["spec"]["steps"] = [s for s in d["spec"]["steps"] if s["id"] != "assemble"]
    write_yaml(ws, DEF, d)


def test_a_finished_run_keeps_its_report_as_a_file(ws, tmp_path: Path):
    without_the_pdf(ws)
    interp, db = make(ws, tmp_path, ScriptedModel(deep_research_script("accept")))
    result = interp.run(ws.load_definition("deep-research"), TOPIC, "dry")
    assert result.status == "done"
    assert [a["name"] for a in result.artifacts] == ["SIMULATED-report.md", "SIMULATED-result.json"]
    md = Path(result.artifacts[0]["path"]).read_text()
    report = result.outputs["report"]
    assert report["body_md"].strip().splitlines()[-1] in md
    assert "## Sources" in md and report["sources"][0]["url"] in md
    # what else it hands back, together, not a file for one word
    assert json.loads(Path(result.artifacts[1]["path"]).read_text()) == {"verdict": "accept"}
    with db.session() as s:
        kept = s.query(Artifact).all()
    assert {a.meta["step"] for a in kept} == {"write", "review"}
    assert all(a.simulated for a in kept)


def test_a_run_that_made_its_own_files_gets_nothing_more(sample_ws, tmp_path: Path):
    interp, _db = make(sample_ws, tmp_path, ScriptedModel(deep_research_script("accept")))
    result = interp.run(sample_ws.load_definition("deep-research"), TOPIC, "dry")
    assert [a["name"] for a in result.artifacts] == ["SIMULATED-report.pdf", "SIMULATED-report.md"]


def test_the_chat_can_add_a_step_where_it_was_asked_and_it_can_be_undone(ws):
    auditor = scripted_auditor(ws)
    result = auditor.audit(DOC.read_text(), name="client-research")
    ids = [s["id"] for s in result.definition["spec"]["steps"]]
    writer = next(s["id"] for s in result.definition["spec"]["steps"] if "write" in s["id"])
    ch = auditor.set_field(
        result,
        "steps.publish",
        {
            "after": writer,
            "kind": "tool",
            "title": "Put the report in a PDF",
            "description": "Makes the PDF and keeps the markdown beside it.",
            "run": "tools.render_pdf",
            "input": {"report": f"${{steps.{writer}.output}}"},
            "side_effects": "none",
        },
        "You asked for the report as a PDF.",
    )
    steps = result.definition["spec"]["steps"]
    at = [s["id"] for s in steps].index("publish")
    assert steps[at - 1]["id"] == writer
    assert steps[at]["origin"]["by"] == "system"
    assert "after" not in steps[at]
    # the routine's result shape is written for it
    assert steps[at]["output"]["schema"]
    assert ch.path == "spec.steps"

    auditor.set_field(result, ch.path, ch.before, "Undid it.")
    assert [s["id"] for s in result.definition["spec"]["steps"]] == ids


def test_a_step_that_cannot_be_a_step_is_refused(ws):
    auditor = scripted_auditor(ws)
    result = auditor.audit(DOC.read_text(), name="client-research")
    before = [s["id"] for s in result.definition["spec"]["steps"]]
    with pytest.raises(AnswerRejected):
        auditor.set_field(result, "steps.Publish It", {"kind": "tool"}, "Bad name.")
    with pytest.raises(AnswerRejected):
        auditor.set_field(result, "steps.publish", {"kind": "tool", "colour": "blue"}, "No.")
    assert [s["id"] for s in result.definition["spec"]["steps"]] == before
