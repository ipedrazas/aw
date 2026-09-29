"""A document that says "go deeper" gets a searching step that searches further, with
the limits the document gave, the document's rule for what is worth following, and a
choice for how far when it gave no numbers. (plans/search-further.md, piece 2)"""

from __future__ import annotations

from typing import Any

from tests.helpers import step
from tests.test_audit import DOC
from tests.test_other_workflows import auditor_for, step_of
from wf.audit.extract import EXTRACTION_INSTRUCTIONS, EXTRACTION_SCHEMA
from wf.audit.triage import can_settle

HOW_FAR = "How far may “Do the research” go when it searches further?"
WHEN = "a related topic that could be worth investigating"


def deeper(**given: Any):
    def change(extracted: dict[str, Any]) -> None:
        step_of(extracted, "research")["search_further"] = {
            "levels": None,
            "max_searches": None,
            "max_topics": None,
            "when": None,
            "passage": None,
            **given,
        }

    return change


def draft(ws, **given: Any):
    auditor, _ = auditor_for(ws, deeper(**given))
    return auditor, auditor.audit(DOC.read_text(), name="client-research")


def test_the_extractor_is_told_going_deeper_is_not_a_step():
    props = EXTRACTION_SCHEMA["properties"]["steps"]["items"]["properties"]
    assert set(props["search_further"]["properties"]) >= {
        "levels",
        "max_searches",
        "max_topics",
        "when",
    }
    assert "Going deeper is not a step of its own" in EXTRACTION_INSTRUCTIONS


def test_the_numbers_and_the_rule_the_document_gave_are_used(ws):
    _, result = draft(ws, levels=2, max_searches=25, when=WHEN)
    research = step(result.definition, "research")
    assert research["search_further"] == {"levels": 2, "max_searches": 25, "max_topics": 3}
    assert research["tools"]["search"]["max_calls"] == 9, "a round leaves room for the rest"
    topics = ws.load_schema(research["output"]["schema"])["properties"]["new_topics"]
    assert WHEN in topics["description"]
    asked = {f.field for f in result.open_findings()}
    assert "steps.research.search_further" not in asked, "the document said how far"
    assert "steps.research.search_further.follow" not in asked, "and what is worth it"


def test_how_far_is_asked_as_a_choice_when_the_document_gives_no_numbers(ws):
    auditor, result = draft(ws, when=WHEN)
    f = next(f for f in result.open_findings() if f.field == "steps.research.search_further")
    assert f.question == HOW_FAR
    assert [o.label for o in f.options] == [
        "1 level deeper, 15 searches in all",
        "2 levels deeper, 25 searches in all",
        "3 levels deeper, 50 searches in all",
    ]
    assert not can_settle(f), "a spending limit is theirs to choose"
    auditor.answer(result, f.id, f.options[2].value)
    research = step(result.definition, "research")
    assert research["search_further"] == {"levels": 3, "max_searches": 50, "max_topics": 3}
    assert research["tools"]["search"]["max_calls"] == 13


def test_what_is_worth_following_is_asked_when_the_document_does_not_say(ws):
    _, result = draft(ws, levels=1, max_searches=15)
    f = next(f for f in result.open_findings() if f.field == "steps.research.search_further.follow")
    assert f.question == "When is a topic “Do the research” finds worth following?"
