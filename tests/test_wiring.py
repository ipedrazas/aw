"""A workflow whose steps read nothing, or search with nothing, looks like it runs and
does not: a real run of one wrote a report on a topic nobody asked for. These are the
questions and fallbacks that stop that."""

from __future__ import annotations

from pathlib import Path

from wf.activities import FixtureLinkCheck
from wf.audit.question import AnswerRejected, answer_definition
from wf.interpret.registry import RunnerContext, http_resolves, render_pdf
from wf.schema import load_workflow_dict
from wf.validate import validate


def _wf() -> dict:
    return {
        "metadata": {"name": "t", "version": 1},
        "spec": {
            "inputs": {"topic": {"type": "string", "required": True}},
            "steps": [
                {
                    "id": "search",
                    "kind": "agent",
                    "title": "Search the web",
                    "input": {"topic": "${inputs.topic}"},
                },
                {"id": "write", "kind": "agent", "title": "Write a sourced report"},
                {
                    "id": "links",
                    "kind": "check",
                    "title": "Verify links",
                    "run": "checks.http_resolves",
                },
            ],
        },
    }


def _open(d: dict, ws) -> dict:
    return {f.field: f for f in validate(load_workflow_dict(d), ws).findings if f.status == "open"}


def test_a_later_step_that_reads_nothing_is_asked_what_it_starts_from(ws):
    found = _open(_wf(), ws)
    q = found["steps.write.input"]
    assert q.question == "What does this step start from?"
    everything = q.options[0].value
    assert everything == {"topic": "${inputs.topic}", "search": "${steps.search.output}"}
    assert "steps.links.input" in found
    assert "steps.search.input" not in found, "the first step reads the topic already"

    d, _ = answer_definition(_wf(), q, everything, ws)
    assert "steps.write.input" not in _open(d, ws)


def test_a_step_that_says_it_searches_and_cannot_is_asked(ws):
    q = _open(_wf(), ws)["steps.search.tools"]
    assert q.question == "Can this step search the web?"
    d, _ = answer_definition(_wf(), q, q.options[0].value, ws)
    step = next(s for s in d["spec"]["steps"] if s["id"] == "search")
    assert set(step["tools"]) == {"search", "get_contents"}
    assert "steps.search.tools" not in _open(d, ws)
    assert "steps.write.tools" not in _open(_wf(), ws), "writing is not searching"
    from wf.validate import says_it_searches

    assert not says_it_searches(
        "Write a sourced report", "with sources drawn from the search results"
    )
    assert says_it_searches("Research", "Look up recent sources online")


def test_an_answer_that_sets_nothing_is_refused(ws):
    q = _open(_wf(), ws)["steps.write.input"]
    try:
        answer_definition(_wf(), q, "whatever came before", ws)
    except AnswerRejected:
        return
    raise AssertionError("prose that sets no input was accepted")


def _ctx(ws, tmp_path: Path, notes: list) -> RunnerContext:
    from types import SimpleNamespace

    acts = SimpleNamespace(links=FixtureLinkCheck(ws), render=None)
    return RunnerContext(
        activities=acts,  # type: ignore[arg-type]
        mode="dry",
        run_id="r",
        step_id="links",
        artifacts_dir=tmp_path,
        note=lambda text, reason: notes.append(text),
    )


def test_the_link_check_finds_the_links_in_whatever_it_is_handed(ws, tmp_path):
    notes: list[str] = []
    report = (
        "# Title\n\nFirst claim (https://docs.temporal.io/evaluate/durable-execution-for-agents).\n"
        "Dead one: https://research.example.com/reports/agent-pilots-to-production-2026, "
        "and again https://docs.temporal.io/evaluate/durable-execution-for-agents"
    )
    out = http_resolves(
        _ctx(ws, tmp_path, notes),
        {"topic": "x", "write": {"report": report, "report_links": ["1. Section"]}},
    )
    assert out["total"] == 2 and out["open_count"] == 1
    assert [s["line"] for s in out["sources"]] == [3, 4]
    assert out["sources"][0]["url"].endswith("durable-execution-for-agents")
    assert any("Found 2 links" in n for n in notes)


def test_a_list_of_sources_is_still_taken_as_given(ws, tmp_path):
    out = http_resolves(
        _ctx(ws, tmp_path, []),
        {
            "sources": [
                {"line": 7, "claim": "c", "url": "https://docs.n8n.io/flow-logic/error-handling/"}
            ]
        },
    )
    assert out["sources"][0]["line"] == 7 and out["sources"][0]["status"] == 301


def test_the_pdf_is_made_from_the_report_whatever_it_is_called(tmp_path):
    seen = {}

    class Render:
        def render_pdf(self, **kw):
            seen.update(kw)
            return {"artifact": kw["out_path"], "pages": 1, "simulated": kw["simulated"]}

    from types import SimpleNamespace

    ctx = RunnerContext(
        activities=SimpleNamespace(render=Render()),  # type: ignore[arg-type]
        mode="live",
        run_id="r",
        step_id="pdf",
        artifacts_dir=tmp_path,
        note=lambda *a: None,
    )
    render_pdf(ctx, {"topic": "celld", "write": {"report": "# celld\n\n" + "words " * 50}})
    assert seen["title"] == "celld" and seen["report"]["body_md"].startswith("# celld")
