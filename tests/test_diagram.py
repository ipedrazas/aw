"""The workflow as a picture: the main line, and what happens when things do not go to plan."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from tests.helpers import read_yaml, write_yaml
from tests.test_api import client  # noqa: F401 - the fixture
from tests.test_audit import DOC, scripted_auditor
from wf.api.diagram import describe, flow, render_svg
from wf.api.plain import plain_summary
from wf.validate import validate

DEF = "definitions/deep-research.workflow.yaml"


def sample(ws):
    wf = ws.load_definition("deep-research")
    return wf, flow(wf, validate(wf, ws).findings, ws)


def row(model, sid):
    return next(r for r in model["rows"] if r["id"] == sid)


def test_each_outcome_of_the_review_says_where_it_leads(ws):
    _wf, m = sample(ws)
    assert [(o["value"], o["goes"]) for o in row(m, "review")["outcomes"]] == [
        ("accept", "Carries on"),
        ("revise", "8. Apply the reviewer's edits"),
        ("go deeper", "9. Go deeper, if you say so"),
    ]
    revise = row(m, "revise")
    assert revise["condition"] == "Only if “Review the report” says “revise”"
    assert revise["otherwise"] == "Otherwise skipped"


def test_where_a_person_comes_in_and_how_a_check_copes(ws):
    _wf, m = sample(ws)
    deeper = [b["text"] for b in row(m, "go_deeper")["badges"]]
    assert "Asks you before it starts" in deeper
    assert "Runs this whole workflow again, up to 2 levels deep" in deeper
    assert row(m, "go_deeper")["loops_back"]
    assert [b["text"] for b in row(m, "check_links")["badges"]] == [
        "If it fails: noted, and the run carries on"
    ]
    assert any("$12" in n for n in m["when_it_breaks"])


def test_a_draft_shows_what_is_not_decided_yet_as_dead_ends(ws):
    result = scripted_auditor(ws).audit(DOC.read_text(), name="client-research")
    m = flow(result.workflow(), result.findings, ws)
    outcomes = {o["value"]: o for o in row(m, "review")["outcomes"]}
    assert outcomes["reject"]["goes"] == "Not decided yet"
    assert outcomes["reject"]["tone"] == "question"
    more = row(m, "more_research")
    assert more["condition"] == "When this happens is not decided yet" and more["condition_open"]
    assert row(m, "review")["questions"] > 0


def test_the_picture_is_well_formed_and_says_it_in_words_too(ws):
    wf, m = sample(ws)
    svg = render_svg(m, "How it flows")
    root = ET.fromstring(svg)
    assert root.get("role") == "img" and root.get("aria-label") == "How it flows"
    text = " ".join(t.text or "" for t in root.iter("{http://www.w3.org/2000/svg}text"))
    for s in wf.spec.steps:
        assert (s.title or "")[:20] in text
    assert "again, one level deeper" in text
    words = describe(m)
    assert "If revise: 8. Apply the reviewer's edits." in words


def test_the_pages_show_it(client):  # noqa: F811
    page = client.get("/workflows/deep-research").text
    assert "How it flows" in page and '<svg class="dg"' in page
    audit = client.post("/api/audits", json={"document": DOC.read_text(), "name": "p"}).json()
    page = client.get(f"/audits/{audit['id']}").text
    assert 'id="flowpic"' in page and "Not decided yet" in page


def test_the_summary_says_what_the_run_does(ws):
    wf = ws.load_definition("deep-research")
    s = plain_summary(wf)
    assert s["budget_on_exceeded"] == "If either would be exceeded, the run stops."
    assert s["recursion"].endswith("and it asks you before it starts.")
    d = read_yaml(ws, DEF)
    next(x for x in d["spec"]["steps"] if x["id"] == "go_deeper")["trust"] = {"policy": "auto"}
    write_yaml(ws, DEF, d)
    s = plain_summary(ws.load_definition("deep-research"))
    assert s["recursion"].endswith("starts them by itself when the review asks for them.")
