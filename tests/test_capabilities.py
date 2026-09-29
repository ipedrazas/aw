"""What the system already knows how to do is not asked about.

A document that says "search the web" or "write the report" has said enough when the
system has its own way of doing that kind of step. The extractor is shown everything
the system can do; a step it matches to the system's own instructions starts from
them, and only a step nothing matches is asked "is its name enough to go on?"."""

from __future__ import annotations

from tests.scripted import _step
from tests.test_audit import DOC
from tests.test_other_workflows import auditor_for
from wf.audit.catalog import capabilities, known_instructions
from wf.audit.draft import build_draft

NAME_ENOUGH = "Is its name enough to go on?"


def research_step(**kw):
    return _step(
        id="search_web",
        title="Search the web in iterations of 40 searches",
        description="Search for sources on the topic.",
        tools=["search", "get_contents"],
        produces=[{"name": "findings", "type": "string", "enum": [], "passage": None}],
        **kw,
    )


def draft_of(step, ws):
    return build_draft(
        {"name": "online-research", "title": "Online research", "steps": [step]},
        [],
        instructions=known_instructions(ws),
    )


def test_the_catalogue_is_read_from_the_code_and_the_workspace(ws):
    caps = capabilities(ws)
    assert {t["name"] for t in caps["tools"]} == {"search", "get_contents"}
    assert all(t["what"] for t in caps["tools"]), "each tool says what it does"
    routines = {r["name"]: r for r in caps["routines"]}
    assert routines["checks.http_resolves"]["kind"] == "check"
    assert routines["checks.http_resolves"]["does"]
    own = {i["name"]: i for i in caps["instructions"]}
    assert own["deep-researcher"]["title"] == "Research"
    assert own["deep-researcher"]["what"].startswith("You answer the questions in the brief")
    assert "version:" not in own["deep-researcher"]["body"]
    assert not any("/" in n for n in own), "a workflow's generated instructions are its own"


def test_the_extractor_is_shown_what_the_system_can_do(ws):
    auditor, asked = auditor_for(ws)
    auditor.audit(DOC.read_text(), name="client-research")
    (req,) = asked
    can = req["what_the_system_can_do"]
    assert {t["name"] for t in can["tools"]} == {"search", "get_contents"}
    assert "checks.http_resolves" in {r["name"] for r in can["routines"]}
    assert "deep-researcher" in {i["name"] for i in can["instructions"]}
    assert all("body" not in i for i in can["instructions"]), "what each is for, not all of it"


def test_a_step_the_system_knows_how_to_do_is_not_asked_about(ws):
    draft = draft_of(research_step(uses="deep-researcher"), ws)

    assert not [a for a in draft.assumptions if a.question and NAME_ENOUGH in a.question]
    assert any("“Research”" in n and "does not say how" in n for n in draft.notes), (
        "the draft says what it started from"
    )
    body = draft.skills["skills/online-research/search_web.md"]
    assert "Do it the way the system does “Research”" in body
    assert "\n## How to work" not in body and "\n### How to work" in body, (
        "its headings sit under this step's own"
    )
    assert "`findings`" in body, "this step's fields, not the ones it started from"
    brief = draft.skill_briefs["skills/online-research/search_web.md"]
    assert brief["system_knows_how"]["title"] == "Research"
    assert not brief["document_says_how"]


def test_a_step_nothing_matches_is_still_asked_about(ws):
    for uses in (None, "no-such-instructions"):
        draft = draft_of(research_step(uses=uses), ws)
        (asked,) = [a for a in draft.assumptions if a.field == "steps.search_web.skill"]
        assert NAME_ENOUGH in asked.question
        assert "system_knows_how" not in draft.skill_briefs["skills/online-research/search_web.md"]


def test_a_step_the_document_explains_follows_the_document(ws):
    step = research_step(
        uses="deep-researcher",
        instructions={"summary": "Search trade press only.", "passage": "p1"},
    )
    draft = draft_of(step, ws)
    body = draft.skills["skills/online-research/search_web.md"]
    assert "Search trade press only." in body
    assert "the way the system does" not in body
    assert not draft.notes
