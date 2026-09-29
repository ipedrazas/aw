"""A draft with a separate "go deeper" step after the search is offered, once, to fold it
into the searching step, with the limits read from its own words. The shape is the
online-research draft's: step 3 "Go deeper" held its "25 searches, 2 levels" as prose.
(plans/search-further.md, piece 3)"""

from __future__ import annotations

from typing import Any

import pytest

from tests.helpers import step
from tests.scripted import _sourced, _step
from tests.test_audit import DOC
from tests.test_other_workflows import auditor_for
from wf.audit.fold import candidates, suggestions
from wf.audit.triage import can_settle

SAYS = "if we need to go deeper, limit it to 25 searches and keep it to a maximum of 2 levels deep"
QUESTION = "Should “Do the research” search further by itself, instead of “Go deeper”?"


def with_go_deeper(extracted: dict[str, Any]) -> None:
    steps = extracted["steps"]
    i = next(n for n, s in enumerate(steps) if s["id"] == "research")
    steps.insert(
        i + 1,
        _step(
            id="go_deeper",
            title="Go deeper",
            description=SAYS,
            reads_from=["research"],
            tools=["search"],
            produces=[
                _sourced(
                    None,
                    name="findings",
                    type="list",
                    enum=[],
                    description="What the deeper searches found.",
                )
            ],
        ),
    )
    write = next(s for s in steps if s["id"] == "write")
    write["reads_from"] = [r if r != "research" else "go_deeper" for r in write["reads_from"]]


@pytest.fixture
def drafted(ws):
    auditor, _ = auditor_for(ws, with_go_deeper)
    return auditor, auditor.audit(DOC.read_text(), name="client-research")


def offer(result):
    return next(f for f in result.findings if f.question == QUESTION)


def test_the_draft_offers_to_fold_it_with_the_limits_it_gave(drafted):
    _, result = drafted
    f = offer(result)
    assert f.status == "open" and f.step_id == "research"
    assert f.source_text == SAYS, "its own words, beside the question"
    assert [o.label for o in f.options] == [
        "Yes, fold “Go deeper” into it: 2 levels deeper, 25 searches in all",
        "No, keep them separate",
    ]
    assert not can_settle(f), "it changes what the run may spend; theirs to say"


def test_yes_folds_it_in_and_the_later_steps_read_the_search(drafted):
    auditor, result = drafted
    assert "steps.go_deeper" in str(step(result.definition, "write")["input"])
    f = offer(result)
    auditor.answer(result, f.id, f.options[0].value)
    ids = [s["id"] for s in result.definition["spec"]["steps"]]
    assert "go_deeper" not in ids
    research = step(result.definition, "research")
    assert research["search_further"] == {"levels": 2, "max_searches": 25, "max_topics": 3}
    assert research["tools"]["search"]["max_calls"] == 9
    # the key keeps its name, which the write step's instructions may use; what it reads
    # is the search, which now holds what going deeper found
    write_input = step(result.definition, "write")["input"]
    assert write_input["go_deeper"] == "${steps.research.output}"
    assert "steps.go_deeper" not in str(write_input)
    assert offer(result).status == "answered"
    asked = {x.field for x in result.open_findings()}
    assert "steps.research.search_further.follow" in asked, "then: when is a topic worth it?"


def test_no_keeps_both_and_is_not_asked_again(drafted):
    auditor, result = drafted
    f = offer(result)
    auditor.answer(result, f.id, {"keep": True})
    auditor.set_field(result, "steps.write.title", "Write it up", "A later change.")
    assert "go_deeper" in [s["id"] for s in result.definition["spec"]["steps"]]
    assert [x.status for x in result.findings if x.question == QUESTION] == ["answered"]


def test_a_draft_that_lost_the_offer_gets_it_back_on_its_next_change(drafted):
    auditor, result = drafted
    result.findings = [f for f in result.findings if f.question != QUESTION]
    auditor.set_field(result, "steps.write.title", "Write it up", "A later change.")
    assert offer(result).status == "open"


def test_nothing_is_offered_where_nothing_goes_deeper_on_its_own(sample_ws):
    wf = sample_ws.load_definition("deep-research")
    definition = wf.model_dump(by_alias=True, exclude_none=True)
    assert candidates(definition) == [], "its go_deeper starts follow-up runs; not this shape"
    assert suggestions(definition) == []


def test_what_it_hands_back_follows_the_step_it_was_folded_into(drafted):
    auditor, result = drafted
    result.definition["spec"]["outputs"] = {"result": "${steps.go_deeper.output}"}
    f = offer(result)
    auditor.answer(result, f.id, f.options[0].value)
    assert result.definition["spec"]["outputs"] == {"result": "${steps.research.output}"}
