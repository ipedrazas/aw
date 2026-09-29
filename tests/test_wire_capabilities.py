"""A draft wires the steps that use a capability from what it takes: the model picks, code
wires. The extraction here is the online-researcher's (run 8979b2e2), as a fixed
extractor gives it: "add the sources at the end" names no routine, and the claim check
uses the system's own claim-support. (plans/capability-contracts.md, piece 2)"""

from __future__ import annotations

from typing import Any

import pytest

from tests.helpers import step
from tests.scripted import _sourced, _step, skill_answer
from wf.activities import ModelResponse, ScriptedModel
from wf.audit import Auditor

DOC = """# Online research

Get my topic and create a strong prompt to guide the research.

Search the web in iterations of 40 searches.

Write a clear report that makes the information easy to digest, in Markdown.

Verify the links by making sure they work and match the citation.

Add all sources at the very end, with the discarded links after them as "Discarded".
"""


def produces(*fields: tuple[str, str]) -> list[dict[str, Any]]:
    return [_sourced(None, name=n, type=t, enum=[], description=f"The {n}.") for n, t in fields]


def extraction() -> dict[str, Any]:
    return {
        "name": "online-researcher",
        "title": "Online research",
        "description": "Research a topic online and write a sourced report.",
        "inputs": [
            _sourced(None, name="topic", type="string", required=True, description="The topic.")
        ],
        "steps": [
            _step(
                id="s1",
                title="Create a strong prompt",
                description="Create a strong prompt to guide the research on the topic.",
                reads_from=[],
                produces=produces(("prompt", "string")),
            ),
            _step(
                id="s2",
                title="Search the web",
                description="Search the web in iterations of 40 searches.",
                reads_from=["s1"],
                tools=["search", "get_contents"],
                produces=produces(("findings", "object")),
            ),
            _step(
                id="s3",
                title="Write the report",
                description="Write a clear report that makes the information easy to digest, in Markdown.",
                reads_from=["s1", "s2"],
                produces=produces(("report", "object")),
            ),
            _step(
                id="s4",
                kind="check",
                title="Verify the links work",
                description="Make sure the report's links work.",
                reads_from=["s1", "s2", "s3"],
                run="checks.http_resolves",
            ),
            _step(
                id="s5",
                title="Check the pages say what the citations claim",
                description="Make sure each link matches its citation.",
                reads_from=["s1", "s2", "s3", "s4"],
                uses="claim-support",
                produces=produces(("page_checks", "object")),
            ),
            _step(
                id="s6",
                kind="tool",
                title="Add all sources at the very end",
                description='Add all sources at the end, with discarded links after as "Discarded".',
                reads_from=["s3", "s4", "s5"],
                run=None,
                side_effects=_sourced(None, items=[]),
                produces=produces(("report", "object")),
            ),
        ],
    }


@pytest.fixture
def drafted(ws):
    def script(req):
        if req.tag.startswith("audit:skill:"):
            return skill_answer(req)
        return ModelResponse(output=extraction(), decisions=[])

    auditor = Auditor(ws, ScriptedModel(script))
    return auditor.audit(DOC, name="online-researcher")


def ids(result) -> list[str]:
    return [s["id"] for s in result.definition["spec"]["steps"]]


def notes(result) -> list[str]:
    return [e["decision"] for e in result.explanations]


def test_the_steps_are_in_the_order_the_capabilities_need(drafted):
    assert ids(drafted) == ["s1", "s2", "s3", "s4", "read_pages", "s5", "s6"]
    reader = step(drafted.definition, "read_pages")
    assert reader["run"] == "tools.fetch_pages"
    assert reader["origin"]["kind"] == "suggested", "marked as mine, with the reason"
    assert "I added “Read the cited pages”" in " ".join(notes(drafted))


def test_the_link_check_reads_the_report_s_citations(drafted, ws):
    report = step(drafted.definition, "s3")
    schema = ws.load_schema(report["output"]["schema"])
    assert schema["properties"]["sources"]["items"]["required"] == ["line", "claim", "url"]
    assert step(drafted.definition, "s4")["input"]["sources"] == "${steps.s3.output.sources}"
    body = ws.load_skill(report["skill"]).body
    assert "`sources`" in body, "the report's instructions say to list them"


def test_the_pages_read_are_the_ones_the_link_check_found(drafted):
    assert step(drafted.definition, "read_pages")["input"] == {
        "sources": "${steps.s4.output.sources}"
    }


def test_claim_support_is_used_as_it_is_once_per_citation(drafted):
    s5 = step(drafted.definition, "s5")
    assert s5["skill"] == "skills/claim-support.md@1"
    assert s5["output"] == {"schema": "schemas/claim_support.json"}
    assert s5["for_each"] == "${steps.read_pages.output.sources}"
    assert s5["input"] == {"claim": "${item.claim}", "page": "${item.page}"}


def test_a_step_no_routine_does_is_done_by_a_model_said_openly(drafted):
    s6 = step(drafted.definition, "s6")
    assert s6["kind"] == "agent" and "run" not in s6
    assert any(
        "No fixed routine does “Add all sources at the very end”" in n for n in notes(drafted)
    )


def test_nothing_is_left_to_guess_about_the_capabilities(drafted):
    asked = {f.field for f in drafted.open_findings()}
    wired = ("s4", "read_pages", "s5")
    assert not [f for f in asked if any(f.startswith(f"steps.{s}.input") for s in wired)]
    assert not [f for f in asked if f.endswith(".for_each") or f.endswith(".run")]
