"""A step that relies on another process: asked about, and handed to a workflow they have."""

from __future__ import annotations

from typing import Any

from tests.scripted import extraction_for_process_doc, skill_answer
from tests.test_audit import DOC, passage_map
from wf.activities import ModelResponse, ScriptedModel
from wf.audit import Auditor
from wf.audit.catalog import known_workflows


def auditor_for(ws, change=None) -> tuple[Auditor, list[dict[str, Any]]]:
    """The sample document's faithful extraction, with ``change`` applied to it first.
    The extraction requests are kept, to see what the extractor was shown."""
    extracted = extraction_for_process_doc(passage_map(DOC.read_text()))
    if change:
        change(extracted)
    asked: list[dict[str, Any]] = []

    def script(req):
        if req.tag.startswith("audit:skill:"):
            return skill_answer(req)
        asked.append(req.input)
        return ModelResponse(output=extracted, decisions=[])

    return Auditor(ws, ScriptedModel(script)), asked


def step_of(extracted: dict[str, Any], sid: str) -> dict[str, Any]:
    return next(s for s in extracted["steps"] if s["id"] == sid)


def mentions_deep_research(extracted: dict[str, Any]) -> None:
    step_of(extracted, "research")["mentions"] = {
        "process": "our usual deep research",
        "workflow": "deep-research",
        "passage": None,
    }


def test_the_extractor_is_shown_the_workflows_they_already_have(ws):
    auditor, asked = auditor_for(ws)
    auditor.audit(DOC.read_text(), name="client-research")
    (req,) = asked
    assert [w["name"] for w in req["your_workflows"]] == ["deep-research"]
    assert req["your_workflows"][0]["steps"], "with its steps, to recognise it by"


def test_a_process_the_document_only_names_is_asked_about_not_broken_down(ws):
    auditor, _ = auditor_for(ws, mentions_deep_research)
    result = auditor.audit(DOC.read_text(), name="client-research")

    f = next(f for f in result.open_findings() if f.field == "steps.research.workflow")
    assert "our usual deep research" in f.question
    assert "deep-research" in f.detail, "the one it looks like is named"
    assert f.options[0].label == "It is my “deep-research” workflow"
    assert f.options[-1].value == {"keep": True}

    auditor.answer(result, f.id, f.options[0].value)
    step = result.workflow().step("research")
    assert step.kind == "subworkflow"
    assert step.workflow == "deep-research@4"
    assert step.with_ == {"topic": "${inputs.topic}"}, "passed its topic from this one's"
    assert step.skill is None and step.model is None and step.tools is None
    assert step.limits and step.limits.max_depth == 1
    assert not any(f.field == "steps.research.workflow" for f in result.open_findings())
    assert result.changes[-1].path == "spec.steps", "one change, so undo puts the step back"


def test_saying_the_document_is_enough_leaves_the_step_as_it_was(ws):
    auditor, _ = auditor_for(ws, mentions_deep_research)
    result = auditor.audit(DOC.read_text(), name="client-research")
    f = next(f for f in result.open_findings() if f.field == "steps.research.workflow")

    auditor.answer(result, f.id, {"keep": True})
    assert result.workflow().step("research").kind == "agent"
    assert f.status == "answered"


def test_the_chat_can_hand_a_step_to_a_workflow_and_the_question_goes(ws):
    auditor, _ = auditor_for(ws, mentions_deep_research)
    result = auditor.audit(DOC.read_text(), name="client-research")

    auditor.set_field(result, "steps.research.workflow", "deep-research", "You said so.")
    assert result.workflow().step("research").kind == "subworkflow"
    assert not any(f.field == "steps.research.workflow" for f in result.open_findings())


def test_what_the_other_workflow_needs_and_is_not_given_is_a_question(ws):
    """This workflow starts from a "subject"; deep research needs a "topic"."""

    def subject_not_topic(extracted: dict[str, Any]) -> None:
        extracted["inputs"][0]["name"] = "subject"

    auditor, _ = auditor_for(ws, subject_not_topic)
    result = auditor.audit(DOC.read_text(), name="client-research")
    auditor.set_field(result, "steps.research.workflow", "deep-research", "You said so.")

    f = next(f for f in result.open_findings() if f.field == "steps.research.with.topic")
    assert f.type == "gap"
    assert f.question.endswith("give “deep-research” as its “topic”?")
    assert [o.label for o in f.options] == ["This workflow's “subject”"]

    auditor.answer(result, f.id, f.options[0].value)
    assert result.workflow().step("research").with_ == {"topic": "${inputs.subject}"}
    assert not any(f.field == "steps.research.with.topic" for f in result.open_findings())


def test_a_step_cannot_be_handed_to_a_workflow_that_is_not_there(ws):
    auditor, _ = auditor_for(ws)
    result = auditor.audit(DOC.read_text(), name="client-research")
    try:
        auditor.set_field(result, "steps.research.workflow", "onboarding", "You said so.")
    except Exception as e:  # noqa: BLE001
        assert "no workflow called “onboarding”" in str(e)
    else:
        raise AssertionError("an unknown workflow was accepted")
    assert result.workflow().step("research").kind == "agent"


def test_the_known_workflows_leave_out_the_one_being_drafted(ws):
    assert [w["name"] for w in known_workflows(ws)] == ["deep-research"]
    assert known_workflows(ws, exclude="deep-research") == []
    (dr,) = known_workflows(ws)
    assert [i["name"] for i in dr["starts_from"]] == ["topic"], "internal inputs are not shown"


def test_a_workflow_runs_another_one_and_the_run_is_under_it(ws, tmp_path):
    """Not only itself: a workflow that hands a step to deep research starts it for real,
    and the deep research run is recorded under it."""
    from tests.helpers import write_yaml
    from tests.test_retry import TOPIC, _go_deeper_runner

    write_yaml(
        ws,
        "definitions/weekly-brief.workflow.yaml",
        {
            "apiVersion": "workflows.tavon.io/v1alpha1",
            "kind": "Workflow",
            "metadata": {"name": "weekly-brief", "version": 1, "description": "A brief."},
            "spec": {
                "inputs": {"topic": {"type": "string", "required": True}},
                "budget": {"max_usd": 5, "max_minutes": 30},
                "steps": [
                    {
                        "id": "research",
                        "kind": "subworkflow",
                        "title": "Research it",
                        "workflow": "deep-research@4",
                        "with": {"topic": "${inputs.topic}"},
                        "limits": {"max_depth": 1, "budget": "inherit"},
                        "trust": {"policy": "always_ask"},
                    }
                ],
            },
        },
    )
    runner = _go_deeper_runner(ws, tmp_path, followup_breaks=False)
    report = runner.run("weekly-brief", TOPIC)
    assert report.status == "done", report.error

    rows = runner.list_runs()
    assert [r["workflow"] for r in rows if r["level"] == 0] == ["weekly-brief"]
    child = next(r for r in rows if r["parent_run_id"] == report.run_id)
    assert child["workflow"] == "deep-research" and child["status"] == "done"
    assert child["level"] == 1
