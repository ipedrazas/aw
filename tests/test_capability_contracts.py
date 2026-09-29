"""A capability says what it takes, not only what it gives back, and a step that names one
is held to it: the input it reads, the shape it needs, once or once per item. The fixes
offered are the earlier results with that shape. (plans/capability-contracts.md, piece 1)

Run 8979b2e2 is why: its link check opened every address in a whole pipeline, and its
claim check ran once, on everything, before any page was read."""

from __future__ import annotations

from typing import Any

from tests.helpers import read_yaml, step
from wf.audit.catalog import capabilities
from wf.audit.question import answer_definition
from wf.schema import load_workflow_dict
from wf.validate import validate

REL = "definitions/deep-research.workflow.yaml"


def changed(ws, sid: str, **change: Any) -> dict[str, Any]:
    d = read_yaml(ws, REL)
    s = step(d, sid)
    for k, v in change.items():
        if v is None:
            s.pop(k, None)
        else:
            s[k] = v
    return d


def asked(ws, d: dict[str, Any]):
    return validate(load_workflow_dict(d), ws).by_field()


def test_deep_research_gives_every_capability_what_it_takes(ws):
    f = asked(ws, read_yaml(ws, REL))
    assert not [k for k in f if ".input." in k or k.endswith(".for_each")], [
        (k, v.detail) for k, v in f.items()
    ]


def test_a_link_check_given_no_links_is_asked_where_they_come_from(ws):
    d = changed(ws, "check_links", input={"report": "${steps.write.output}"})
    f = asked(ws, d)["steps.check_links.input.sources"]
    assert (
        f.question == "Where does “Check the links” get the links to check, each with its address?"
    )
    assert "look for them in everything it is given" in f.detail
    assert f.options[0].label == "“Write the report”'s sources", "the nearest result shaped so"
    new, _ = answer_definition(d, f, f.options[0].value, ws)
    assert step(new, "check_links")["input"]["sources"] == "${steps.write.output.sources}"
    assert "steps.check_links.input.sources" not in asked(ws, new)


def test_a_link_check_given_the_wrong_shape_is_asked_too(ws):
    d = changed(ws, "check_links", input={"sources": "${steps.plan.output}"})
    f = asked(ws, d)["steps.check_links.input.sources"]
    assert f.type == "conflict" and "is not that" in f.detail


def test_claim_support_runs_once_per_citation(ws):
    d = changed(ws, "check_support", for_each=None, max_fanout=None)
    f = asked(ws, d)["steps.check_support.for_each"]
    assert (
        f.question
        == "What should “Check the pages say what the report says” run over, one citation at a time?"
    )
    assert "Once for each of “Read the cited pages”'s sources" in [o.label for o in f.options]
    pick = next(o for o in f.options if "Read the cited pages" in o.label)
    new, _ = answer_definition(d, f, pick.value, ws)
    assert step(new, "check_support")["for_each"] == "${steps.fetch_pages.output.sources}"


def test_and_is_given_each_citation_s_page(ws):
    d = changed(ws, "check_support", input={"claim": "${item.claim}"})
    f = asked(ws, d)["steps.check_support.input.page"]
    assert [o.value for o in f.options] == ["${item.page}"]


def test_a_pdf_needs_the_report_and_an_email_its_parts(ws):
    d = changed(ws, "assemble", input={"link_check": "${steps.check_links.output}"})
    f = asked(ws, d)["steps.assemble.input.report"]
    assert "What “Write the report” gives back" in [o.label for o in f.options]


def test_the_extractor_triage_and_chat_are_told_what_each_takes(ws):
    caps = capabilities(ws)
    routines = {r["name"]: r for r in caps["routines"]}
    assert routines["checks.http_resolves"]["takes"].startswith("the links to check")
    own = {i["name"]: i for i in caps["instructions"]}
    assert "once per citation" in own["claim-support"]["takes"]
    assert own["report-writer"]["takes"] is None


def test_a_new_version_of_the_instructions_keeps_what_they_take(ws):
    version, _ = ws.save_skill_version("skills/claim-support.md", "# Check\n\nCarefully.")
    skill = ws.load_skill(f"skills/claim-support.md@{version}")
    assert skill.takes and skill.takes["fields"] == ["claim", "page"]
    assert skill.result == "schemas/claim_support.json"
