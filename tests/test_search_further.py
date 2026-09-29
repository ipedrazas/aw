"""Going deeper is searching further, in the same run: a searching step names the topics
worth following, and the runtime runs it again on them, round by round, within limits
it enforces, as one result the later steps read unchanged. (plans/search-further.md)"""

from __future__ import annotations

from typing import Any

import pytest

from tests.test_interpreter import make
from wf.activities import ModelResponse, ScriptedModel
from wf.activities.base import ToolCallRecord, Usage
from wf.audit.question import answer_definition
from wf.interpret import Interpreter
from wf.interpret.interpreter import fingerprint
from wf.schema import load_workflow_dict
from wf.store import Decision, StepRun
from wf.validate import validate, with_sources, with_topics

SCHEMA = "schemas/scouting/search.json"
REPORT = "schemas/scouting/report.json"
FOLLOW = "Closely related to the question and not already covered."


def definition(**further: Any) -> dict[str, Any]:
    return {
        "apiVersion": "workflows.tavon.io/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "scouting", "version": 1},
        "spec": {
            "inputs": {"topic": {"type": "string", "required": True}},
            "defaults": {"model": "claude-sonnet-5", "trust": {"policy": "auto"}},
            "steps": [
                {
                    "id": "search",
                    "kind": "agent",
                    "title": "Search the web",
                    "shows_user": ["output"],
                    "input": {"topic": "${inputs.topic}"},
                    "tools": {"search": {"max_calls": 5}},
                    "search_further": {"levels": 2, "max_searches": 25, "max_topics": 3, **further},
                    "output": {"schema": SCHEMA},
                },
                {
                    "id": "write",
                    "kind": "agent",
                    "title": "Write the report",
                    "shows_user": ["output"],
                    "input": {"sources": "${steps.search.output.sources}"},
                    "output": {"schema": REPORT},
                },
            ],
        },
    }


@pytest.fixture
def scouting(ws):
    ws.save_schema(SCHEMA, with_topics(with_sources(None), "new_topics", FOLLOW))
    ws.save_schema(REPORT, {"type": "object", "properties": {"cited": {"type": "integer"}}})
    return ws


def src(url: str) -> dict[str, str]:
    return {"url": url, "title": url, "published": "2026-09-01", "notes": "…"}


#: What each round finds: its sources, the topics it names, and how many searches.
ROUNDS = {
    None: (["a", "b"], ["cells", "sandboxes", "costs", "history"], 3),
    "cells": (["b", "c"], ["cell routers"], 4),
    "sandboxes": (["d"], [], 2),
    "costs": (["e"], [], 2),
    "cell routers": (["f"], ["too deep"], 1),
}


def script(asked: list[Any]):
    def answer(req):
        if req.tag.startswith("write"):
            return ModelResponse(output={"cited": len(req.input["sources"])}, decisions=[])
        further = req.input.get("further")
        asked.append((further, req))
        found, topics, searches = ROUNDS[further["topic"] if further else None]
        return ModelResponse(
            output={
                "sources": [src(f"https://{u}.example") for u in found],
                "new_topics": [{"topic": t, "why": f"{t} matters"} for t in topics],
            },
            decisions=[],
            tool_calls=[ToolCallRecord("search", {"query": "q"}, "s")] * searches,
            usage=Usage(cost_usd=0.01 * searches),
            model="claude-sonnet-5",
        )

    return answer


def run(ws, tmp_path, **further):
    wf = load_workflow_dict(definition(**further))
    ws.save_definition(wf)
    asked: list[Any] = []
    interp, db = make(ws, tmp_path, ScriptedModel(script(asked)))
    result = interp.run(wf, {"topic": "Cell-based architectures"}, "dry")
    return result, asked, db


def said(db) -> list[str]:
    with db.session() as s:
        return [d.text for d in s.query(Decision).order_by(Decision.seq)]


# -- the loop -------------------------------------------------------------------------


def test_topics_are_searched_round_by_round_into_one_result(scouting, tmp_path):
    result, asked, db = run(scouting, tmp_path)
    assert result.status == "done", said(db)
    followed = [(f["topic"], f["level"], f["from"]) for f, _ in asked if f]
    assert followed == [
        ("cells", 1, None),
        ("sandboxes", 1, None),
        ("costs", 1, None),
        ("cell routers", 2, "cells"),
    ]
    with db.session() as s:
        rows = s.query(StepRun).filter_by(step_id="search").order_by(StepRun.seq).all()
    parent, *rounds = rows
    assert parent.fanout_index is None and [r.fanout_index for r in rounds] == [0, 1, 2, 3, 4]
    urls = [x["url"] for x in parent.output["sources"]]
    assert urls == [f"https://{u}.example" for u in "abcdef"], "joined, each address once"


def test_the_later_step_reads_what_every_round_found(scouting, tmp_path):
    _, asked, db = run(scouting, tmp_path)
    with db.session() as s:
        write = s.query(StepRun).filter_by(step_id="write").one()
    assert len(write.input["sources"]) == 6


def test_a_follow_up_is_told_what_to_search_and_what_it_already_has(scouting, tmp_path):
    _, asked, _ = run(scouting, tmp_path)
    first, req = asked[0]
    assert first is None and "## Searching further" in req.system
    block, req = next((f, r) for f, r in asked if f and f["topic"] == "cell routers")
    assert req.input["topic"] == "Cell-based architectures", "its usual input, unchanged"
    assert block["why"] == "cell routers matters"
    assert "https://c.example" in block["already_found"], "found by the round before"


def test_limits_are_kept_and_said(scouting, tmp_path):
    _, _, db = run(scouting, tmp_path)
    text = said(db)
    assert "Did not follow “history” (round 1)." in text, "3 topics a round"
    assert "Did not follow “too deep”." in text, "2 rounds at most"
    assert "Searched further on “cells” (round 1)." in text


def test_the_total_search_cap_stops_it_and_each_round_is_given_what_is_left(scouting, tmp_path):
    _, asked, db = run(scouting, tmp_path, max_searches=8)
    caps = [(f["topic"] if f else None, r.tools[0].max_calls) for f, r in asked]
    # 3 used by the first round, 4 by "cells", then 1 left for "sandboxes"
    assert caps == [(None, 5), ("cells", 5), ("sandboxes", 1)]
    # the script does not hold to the cap it is given, as a model's tool loop would
    assert "Stopped searching further after 9 searches." in said(db)


def test_no_topics_means_one_round(scouting, tmp_path, monkeypatch):
    monkeypatch.setitem(ROUNDS, None, (["a"], [], 1))
    _, asked, _ = run(scouting, tmp_path)
    assert [f for f, _ in asked] == [None]


def test_a_resumed_run_reads_the_joined_result_and_the_trail(scouting, tmp_path):
    _, _, db = run(scouting, tmp_path)
    with db.session() as s:
        rows = s.query(StepRun).order_by(StepRun.seq).all()
        state = Interpreter._read_back(load_workflow_dict(definition()), rows)
    assert len(state["search"]["output"]["sources"]) == 6
    assert [t["topic"] for t in state["search"]["further"]] == [
        "cells",
        "sandboxes",
        "costs",
        "cell routers",
    ]


# -- the definition -------------------------------------------------------------------


def findings(ws, **change: Any):
    d = definition()
    d["spec"]["steps"][0].update(change)
    return validate(load_workflow_dict(d), ws).by_field()


def test_a_step_that_cannot_search_cannot_search_further(scouting):
    f = findings(scouting, tools=None)["steps.search.search_further"]
    assert "cannot search" in f.detail


def test_a_total_below_one_round_is_asked_about(scouting):
    f = findings(scouting, search_further={"max_searches": 3})
    assert "only 3 times in all" in f["steps.search.search_further.max_searches"].detail


def test_the_rule_for_following_a_topic_is_asked_and_becomes_its_description(scouting):
    scouting.save_schema(SCHEMA, with_sources(None))
    d = definition()
    f = validate(load_workflow_dict(d), scouting).by_field()["steps.search.search_further.follow"]
    assert f.question == "When is a topic “Search the web” finds worth following?"
    new_def, _ = answer_definition(
        d, f, "Only if it names a product we have not covered.", scouting
    )
    topics = scouting.load_schema(SCHEMA)["properties"]["new_topics"]
    assert "Only if it names a product we have not covered." in topics["description"]
    assert (
        "steps.search.search_further.follow"
        not in validate(load_workflow_dict(new_def), scouting).by_field()
    )


def test_later_steps_can_read_each_round(scouting):
    d = definition()
    d["spec"]["steps"][1]["input"]["topics"] = "${steps.search.further[*].topic}"
    assert "steps.write.input" not in validate(load_workflow_dict(d), scouting).by_field()
    d["spec"]["steps"][1]["input"]["topics"] = "${steps.write.further[*].topic}"
    d["spec"]["steps"].append({**d["spec"]["steps"][1], "id": "again"})
    f = validate(load_workflow_dict(d), scouting).by_field()["steps.again.input"]
    assert "does not search further" in f.detail


def test_changing_the_limits_starts_the_count_of_oks_again(scouting):
    a = load_workflow_dict(definition())
    b = load_workflow_dict(definition(levels=3))
    assert fingerprint(scouting, a, a.step("search")) != fingerprint(scouting, b, b.step("search"))
    plain = definition()
    del plain["spec"]["steps"][1]["input"]
    w = load_workflow_dict(plain)
    assert fingerprint(scouting, w, w.step("write")), "a step without it is fingerprinted as before"


# -- what the step shows ------------------------------------------------------------


def test_the_step_shows_what_all_its_rounds_spent_and_searched(scouting, tmp_path):
    """Its own record stood for it with no cost, model or searches; a real dry run hid
    about $0.31 of $0.51 that way (run 8979b2e2)."""
    from wf.dryrun.runner import _parts_of

    _, _, db = run(scouting, tmp_path)
    with db.session() as s:
        rows = s.query(StepRun).order_by(StepRun.seq).all()
    parent, *rounds = [r for r in rows if r.step_id == "search"]
    assert len(parent.tool_calls) == sum(len(r.tool_calls) for r in rounds) == 3 + 4 + 2 + 2 + 1
    assert parent.cost_usd == pytest.approx(sum(r.cost_usd for r in rounds))
    assert parent.model == rounds[0].model and parent.instruction_ref == rounds[0].instruction_ref
    parts = _parts_of(parent, rows)
    assert [(p["index"], p.get("topic"), p["searches"]) for p in parts] == [
        (0, None, 3),
        (1, "cells", 4),
        (2, "sandboxes", 2),
        (3, "costs", 2),
        (4, "cell routers", 1),
    ]
    assert _parts_of(rows[-1], rows) == [], "a step that ran once has no parts"


def test_a_fan_out_shows_what_its_items_did(sample_ws, tmp_path):
    from tests.scripted import deep_research_script
    from tests.test_interpreter import TOPIC

    interp, db = make(sample_ws, tmp_path, ScriptedModel(deep_research_script("accept")))
    interp.run(sample_ws.load_definition("deep-research"), TOPIC, "dry")
    with db.session() as s:
        parent, *items = (
            s.query(StepRun).filter_by(step_id="check_support").order_by(StepRun.seq).all()
        )
    assert items and parent.fanout_index is None
    assert parent.instruction_ref == "skills/claim-support.md@1" == items[0].instruction_ref
    assert parent.cost_usd == pytest.approx(sum(i.cost_usd for i in items))
