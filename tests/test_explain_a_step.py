"""“How should this step be done?” is answered in the person's own words, and those
words become the step's instructions. They used to be written into the field that
names an instruction file, and every explanation was refused as not fitting."""

from __future__ import annotations

import pytest

from tests.helpers import step
from tests.test_audit import DOC
from tests.test_other_workflows import auditor_for

HOW = (
    "If while doing a search we find a topic related to the original topic that could "
    "be worth investigating, we use that discovered topic to do more searches"
)


@pytest.fixture
def asked(ws):
    """A draft whose “Review the report” has no instructions, so it is asked how."""
    auditor, _ = auditor_for(ws)
    result = auditor.audit(DOC.read_text(), name="client-research")
    auditor.set_field(result, "steps.review.skill", None, "You said you would explain.")
    f = next(f for f in result.open_findings() if f.field == "steps.review.skill")
    return auditor, result, f


def test_the_question_says_what_kind_of_answer_it_wants(asked):
    _, _, f = asked
    assert f.question == "How should “Review the report” be done?"
    assert "someone doing it for the first time" in f.detail


def test_an_explanation_becomes_the_step_s_instructions(asked, ws):
    auditor, result, f = asked
    (ch,) = auditor.answer(result, f.id, HOW)
    ref = step(result.definition, "review")["skill"]
    assert ch.after == ref and ref.startswith("skills/client-research/review.md@")
    body = ws.load_skill(ref).body
    assert body.startswith("# Review the report")
    assert HOW in body, "their words, as they wrote them"
    assert "`verdict`" in body, "with what the step has to produce"
    assert "<data>" in body and "Decisions" in body, "and what the runtime relies on"
    assert f.status == "answered"
    assert "steps.review.skill" not in {x.field for x in result.open_findings()}


def test_what_the_step_had_before_is_kept(asked, ws):
    auditor, result, f = asked
    before = ws.load_skill("skills/client-research/review.md")
    auditor.answer(result, f.id, HOW)
    ref = step(result.definition, "review")["skill"]
    assert ref == f"skills/client-research/review.md@{before.version + 1}"
    old = ws.load_skill(f"skills/client-research/review.md@{before.version}")
    assert old.body == before.body, "the earlier version is still there to go back to"


def test_naming_the_system_s_own_instructions_uses_them(asked):
    auditor, result, f = asked
    auditor.answer(result, f.id, "claim-support")
    assert step(result.definition, "review")["skill"] == "skills/claim-support.md@1"
