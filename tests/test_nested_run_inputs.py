"""What a step gives the workflow it starts is checked before any run: only inputs that
workflow has, each of the kind it takes. At run time a name it lacks was dropped without
a word and a value of the wrong kind stopped the run it started.
(plans/search-further.md, piece 4)"""

from __future__ import annotations

from typing import Any

from tests.helpers import read_yaml, step, write_yaml
from tests.test_audit import DOC
from tests.test_other_workflows import auditor_for, mentions_deep_research
from wf.audit.question import answer_definition
from wf.schema import load_workflow_dict
from wf.validate import validate

REL = "definitions/deep-research.workflow.yaml"


def with_given(ws, given: dict[str, Any]) -> dict[str, Any]:
    d = read_yaml(ws, REL)
    step(d, "go_deeper")["with"] = given
    return d


def findings(ws, d: dict[str, Any]):
    return validate(load_workflow_dict(d), ws).by_field()


def test_what_deep_research_gives_itself_fits(ws):
    f = findings(ws, read_yaml(ws, REL))
    assert not [k for k in f if ".with" in k], "topic is text, and depth is the runtime's"


def test_a_name_the_started_workflow_does_not_take_is_said_and_can_be_renamed(ws):
    d = with_given(ws, {"subject": "${item.topic}"})
    f = findings(ws, d)["steps.go_deeper.with.subject"]
    assert f.type == "conflict" and "takes no input by that name" in f.detail
    assert [o.label for o in f.options] == ["It is “deep-research”'s “topic”", "Leave it out"]
    new, _ = answer_definition(d, f, f.options[0].value, ws)
    assert step(new, "go_deeper")["with"] == {"topic": "${item.topic}"}
    assert not [k for k in findings(ws, new) if ".with" in k]


def test_or_left_out(ws):
    d = with_given(ws, {"topic": "${item.topic}", "tone": "formal"})
    f = findings(ws, d)["steps.go_deeper.with.tone"]
    new, _ = answer_definition(d, f, {"op": "leave_out"}, ws)
    assert step(new, "go_deeper")["with"] == {"topic": "${item.topic}"}


def test_a_value_of_the_wrong_kind_is_said(ws):
    d = with_given(ws, {"topic": "${steps.review.output.followup_topics}"})
    f = findings(ws, d)["steps.go_deeper.with.topic"]
    assert "takes text as its “topic”" in f.detail and "gives it a list" in f.detail
    d = with_given(ws, {"topic": 42})
    assert "gives it a whole number" in findings(ws, d)["steps.go_deeper.with.topic"].detail


def test_what_cannot_be_told_before_a_run_is_not_guessed_at(ws):
    d = with_given(ws, {"topic": "More on ${item.topic}", "depth": "${inputs.depth + 1}"})
    assert not [k for k in findings(ws, d) if ".with" in k], "text with a value in it is text"


def test_a_value_outside_the_ones_it_takes_is_said(ws):
    child = read_yaml(ws, REL)
    child["metadata"]["name"] = "brief-only"
    child["spec"]["inputs"]["tone"] = {"type": "string", "enum": ["formal", "plain"]}
    ws.save_definition(load_workflow_dict(child))
    d = with_given(ws, {"topic": "${item.topic}", "tone": "chatty"})
    step(d, "go_deeper")["workflow"] = "brief-only@4"
    write_yaml(ws, REL, d)
    f = findings(ws, d)["steps.go_deeper.with.tone"]
    assert f.detail == "“brief-only” takes one of “formal”, “plain” as its “tone”, not “chatty”."
    assert [o.value for o in f.options] == ["formal", "plain"]


def test_a_step_handed_to_another_workflow_is_not_told_it_needs_a_list(ws):
    """#41 gave a handed-over step `with` and no list; the validator called that a
    conflict ("“with” fills in each item of a list")."""
    auditor, _ = auditor_for(ws, mentions_deep_research)
    result = auditor.audit(DOC.read_text(), name="client-research")
    f = next(f for f in result.open_findings() if f.field == "steps.research.workflow")
    auditor.answer(result, f.id, f.options[0].value)
    assert step(result.definition, "research")["with"] == {"topic": "${inputs.topic}"}
    assert "steps.research.with" not in {x.field for x in result.open_findings()}
