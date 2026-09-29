"""What a workflow hands back is what the document says it delivers, or a question. Run
8979b2e2 handed back the claim check's page_checks, not the report: a draft took the last
AI step. (plans/capability-contracts.md, piece 3)"""

from __future__ import annotations

from typing import Any

from tests.helpers import step
from tests.scripted import skill_answer
from tests.test_audit import DOC as SAMPLE_DOC
from tests.test_audit import scripted_auditor
from tests.test_wire_capabilities import DOC, extraction
from wf.activities import ModelResponse, ScriptedModel
from wf.audit import Auditor
from wf.audit.extract import EXTRACTION_INSTRUCTIONS, EXTRACTION_SCHEMA

QUESTION = "What should this workflow hand back?"


def drafted(ws, delivers: dict[str, Any] | None):
    def script(req):
        if req.tag.startswith("audit:skill:"):
            return skill_answer(req)
        return ModelResponse(output={**extraction(), "delivers": delivers}, decisions=[])

    auditor = Auditor(ws, ScriptedModel(script))
    return auditor, auditor.audit(DOC, name="online-researcher")


def test_the_extractor_is_asked_what_the_process_delivers():
    assert "delivers" in EXTRACTION_SCHEMA["required"]
    assert "Under delivers, name the step" in EXTRACTION_INSTRUCTIONS


def test_what_the_document_says_it_delivers_is_what_it_hands_back(ws):
    _, result = drafted(ws, {"step": "s3", "passage": None})
    assert result.definition["spec"]["outputs"] == {"result": "${steps.s3.output}"}
    assert not [f for f in result.open_findings() if f.question == QUESTION]


def test_when_the_document_does_not_say_it_is_asked(ws):
    auditor, result = drafted(ws, None)
    assert result.definition["spec"]["outputs"] == {"result": "${steps.s6.output}"}, (
        "the last step that produces something: the report with its sources"
    )
    f = next(f for f in result.open_findings() if f.question == QUESTION)
    labels = [o.label for o in f.options]
    assert labels[0] == "Yes, what “Add all sources at the very end” gives back"
    assert "No, what “Check the pages say what the citations claim” gives back" in labels
    other = next(o for o in f.options if "Write the report" in o.label)
    auditor.answer(result, f.id, other.value)
    assert result.definition["spec"]["outputs"] == {"result": "${steps.s3.output}"}


def test_a_step_the_document_names_that_is_not_there_is_asked_about(ws):
    _, result = drafted(ws, {"step": "nope", "passage": None})
    assert any(f.question == QUESTION for f in result.open_findings())


def test_a_step_that_sends_something_out_is_not_what_it_hands_back(ws):
    result = scripted_auditor(ws).audit(SAMPLE_DOC.read_text(), name="client-research")
    handed = result.definition["spec"]["outputs"]["result"]
    assert "steps.send" not in handed, "its result is only whether it went"
    assert step(result.definition, "export_pdf") and handed == "${steps.export_pdf.output}"
