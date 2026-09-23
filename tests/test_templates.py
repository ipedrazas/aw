"""The predefined step templates: one per recognized step kind, and the rule that
steers a step misread as ``wait`` to the workflow's input instead."""

from __future__ import annotations

from wf.schema import StepKind
from wf.validate import STEP_TEMPLATES, is_workflow_input


def test_every_step_kind_has_a_template():
    assert set(STEP_TEMPLATES) == set(StepKind.__args__)
    for kind, template in STEP_TEMPLATES.items():
        assert template.kind == kind
        assert template.required  # every kind needs at least one field answered


def test_a_leading_wait_reading_nothing_is_the_workflow_input():
    """“First, get my topic” is not a step that waits for someone."""
    assert is_workflow_input({"kind": "wait", "reads_from": []})
    assert is_workflow_input({"kind": "wait"})


def test_a_wait_reading_an_earlier_step_is_a_real_wait():
    assert not is_workflow_input({"kind": "wait", "reads_from": ["brief"]})


def test_a_non_wait_step_is_never_treated_as_the_workflow_input():
    assert not is_workflow_input({"kind": "agent", "reads_from": []})
