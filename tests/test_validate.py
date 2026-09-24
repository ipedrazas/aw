"""Fixture-driven validator tests: the sample validates cleanly and each deliberate
breakage produces exactly the finding it should."""

from __future__ import annotations

import pytest

from tests.helpers import read_json, read_yaml, step, write_json, write_yaml
from wf.schema import Workspace
from wf.validate import validate

DEF = "definitions/deep-research.workflow.yaml"


def run(ws: Workspace):
    return validate(ws.load_definition("deep-research"), ws)


def test_sample_definition_validates_cleanly(sample_ws):
    result = run(sample_ws)
    assert result.ok, [(f.type, f.field, f.detail) for f in result.findings]


def test_removing_the_branch_condition_surfaces_the_unhandled_outcome(ws):
    data = read_yaml(ws, DEF)
    del step(data, "go_deeper")["when"]
    write_yaml(ws, DEF, data)
    result = run(ws)
    kinds = {(f.type, f.field) for f in result.findings}
    assert ("gap", "steps.review.output.continue_on.verdict.go_deeper") in kinds
    f = next(f for f in result.findings if f.field.endswith("verdict.go_deeper"))
    assert "go_deeper" in f.question and f.answer_kind == "choice"
    assert len(result.findings) == 1


def test_dropping_does_not_check_is_a_gap_with_the_right_question(ws):
    data = read_yaml(ws, DEF)
    del step(data, "check_links")["does_not_check"]
    write_yaml(ws, DEF, data)
    result = run(ws)
    assert [(f.type, f.field) for f in result.findings] == [
        ("gap", "steps.check_links.does_not_check")
    ]
    assert result.findings[0].question == "What does this check not tell you?"


def test_an_outcome_no_branch_handles_is_a_gap(ws):
    schema = read_json(ws, "schemas/review.json")
    schema["properties"]["verdict"]["enum"].append("reject")
    write_json(ws, "schemas/review.json", schema)
    result = run(ws)
    assert [(f.type, f.field) for f in result.findings] == [
        ("gap", "steps.review.output.continue_on.verdict.reject")
    ]
    f = result.findings[0]
    assert f.step_id == "review"
    assert "reject" in f.question
    labels = [o.label for o in f.options]
    assert any("Carry on" in lbl for lbl in labels)


def test_an_unbounded_fan_out_is_a_gap(ws):
    data = read_yaml(ws, DEF)
    del step(data, "go_deeper")["limits"]["max_fanout"]
    write_yaml(ws, DEF, data)
    result = run(ws)
    assert [(f.type, f.field) for f in result.findings] == [("gap", "steps.go_deeper.max_fanout")]
    assert result.findings[0].question == "How many of these can run at once?"


def test_a_branch_value_nothing_can_produce_is_unreachable(ws):
    data = read_yaml(ws, DEF)
    step(data, "revise")["when"] = '${steps.review.output.verdict == "rewrite"}'
    write_yaml(ws, DEF, data)
    result = run(ws)
    types = sorted((f.type, f.field) for f in result.findings)
    assert ("unreachable", "steps.revise.when") in types
    # and "revise" is now unhandled, which is the same real gap seen from the other side
    assert ("gap", "steps.review.output.continue_on.verdict.revise") in types
    assert len(result.findings) == 2


def test_reading_a_later_step_is_a_conflict(ws):
    data = read_yaml(ws, DEF)
    step(data, "write")["input"]["link_check"] = "${steps.check_links.output}"
    write_yaml(ws, DEF, data)
    result = run(ws)
    assert [(f.type, f.field) for f in result.findings] == [("conflict", "steps.write.input")]
    assert "runs later" in result.findings[0].detail


def test_reading_a_field_nothing_produces_is_a_conflict(ws):
    data = read_yaml(ws, DEF)
    step(data, "review")["input"]["score"] = "${steps.check_links.output.score}"
    write_yaml(ws, DEF, data)
    result = run(ws)
    assert [(f.type, f.field) for f in result.findings] == [("conflict", "steps.review.input")]
    assert "nothing produces" in result.findings[0].detail


def test_instruction_version_drift_is_a_conflict(ws):
    p = ws.path("skills/report-reviewer.md")
    p.write_text(p.read_text().replace("version: 3", "version: 4", 1))
    result = run(ws)
    assert [(f.type, f.field) for f in result.findings] == [("conflict", "steps.review.skill")]
    assert "version 4" in result.findings[0].detail


def test_missing_required_fields_become_gaps_with_questions(ws):
    data = read_yaml(ws, DEF)
    s = step(data, "assemble")
    del s["requires_approval"]
    del s["title"]
    write_yaml(ws, DEF, data)
    result = run(ws)
    fields = {f.field: f for f in result.findings}
    assert set(fields) == {"steps.assemble.requires_approval", "steps.assemble.title"}
    assert (
        fields["steps.assemble.requires_approval"].question
        == "Who signs this off before it leaves the system?"
    )
    assert fields["steps.assemble.requires_approval"].answer_kind == "choice"


def test_subworkflow_without_a_budget_is_a_gap(ws):
    data = read_yaml(ws, DEF)
    del data["spec"]["budget"]
    write_yaml(ws, DEF, data)
    result = run(ws)
    assert [(f.type, f.field) for f in result.findings] == [("gap", "spec.budget")]


def test_expression_outside_the_grammar_is_a_conflict(ws):
    data = read_yaml(ws, DEF)
    step(data, "revise")["when"] = "${len(steps.review.output.gaps) > 0}"
    write_yaml(ws, DEF, data)
    result = run(ws)
    kinds = {(f.type, f.field) for f in result.findings}
    assert ("conflict", "steps.revise.when") in kinds


def test_unknown_key_is_rejected_at_load(ws):
    data = read_yaml(ws, DEF)
    step(data, "plan")["skil"] = "typo"
    write_yaml(ws, DEF, data)
    with pytest.raises(Exception, match="skil"):
        ws.load_definition("deep-research")


def test_findings_are_ordered_by_what_they_unblock(ws):
    data = read_yaml(ws, DEF)
    del step(data, "plan")["skill"]  # everything downstream reads the brief
    del step(data, "assemble")["requires_approval"]  # nothing reads the PDF
    write_yaml(ws, DEF, data)
    ordered = run(ws).ordered()
    assert ordered[0].field == "steps.plan.skill"
    assert ordered[-1].field == "steps.assemble.requires_approval"


def test_a_check_with_no_routine_offers_the_routines_a_check_can_run(ws):
    data = read_yaml(ws, DEF)
    del step(data, "check_links")["run"]
    write_yaml(ws, DEF, data)
    result = run(ws)
    f = next(f for f in result.findings if f.field == "steps.check_links.run")
    assert f.type == "gap"
    # the routines are a closed set, so the question is a choice, not an empty box
    assert f.answer_kind == "choice"
    assert [o.value for o in f.options] == ["checks.http_resolves"]
    assert all(o.consequence for o in f.options)
    assert "Check the links" in f.question  # the question names the step it is about


def test_a_tool_with_no_routine_is_not_offered_a_checks_routine(ws):
    data = read_yaml(ws, DEF)
    del step(data, "assemble")["run"]
    write_yaml(ws, DEF, data)
    result = run(ws)
    f = next(f for f in result.findings if f.field == "steps.assemble.run")
    assert [o.value for o in f.options] == ["tools.render_pdf", "tools.send_email"]


def test_the_routines_the_validator_offers_are_the_ones_the_interpreter_has():
    from wf.interpret.registry import CHECKS, TOOLS
    from wf.validate import KNOWN_RUNNERS

    assert KNOWN_RUNNERS == set(CHECKS) | set(TOOLS)


def test_a_step_without_a_model_runs_on_the_workflows_default(ws):
    data = read_yaml(ws, DEF)
    del step(data, "plan")["model"]
    write_yaml(ws, DEF, data)
    assert "steps.plan.model" in [f.field for f in run(ws).findings]

    data["spec"]["defaults"]["model"] = "claude-sonnet-5"
    write_yaml(ws, DEF, data)
    result = run(ws)
    assert "steps.plan.model" not in [f.field for f in result.findings]
    wf = ws.load_definition("deep-research")
    assert wf.model_for(wf.step("plan")) == "claude-sonnet-5"
    # a step's own model still wins over the default
    assert wf.model_for(wf.step("write")) == "claude-opus-5"
