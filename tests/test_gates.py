"""A real run stops after a step that checks with you, and carries on when you say OK."""

from __future__ import annotations

from typing import Any

from tests.helpers import read_yaml, write_yaml
from tests.test_api import client  # noqa: F401 - the fixture
from tests.test_retry import TOPIC, _go_deeper_runner

DEF = "definitions/deep-research.workflow.yaml"


def only_plan_asks(ws, trust: dict[str, Any]) -> None:
    """Every step runs on its own but the brief, which has ``trust``."""
    d = read_yaml(ws, DEF)
    d["spec"]["defaults"]["trust"] = {"policy": "auto"}
    for s in d["spec"]["steps"]:
        s["trust"] = trust if s["id"] == "plan" else {"policy": "auto"}
    write_yaml(ws, DEF, d)


def texts(runner, report) -> list[str]:
    """Every decision the run recorded, from the run's own record."""
    return [d["text"] for s in runner.snapshot(report.run_id)["steps"] for d in s["decisions"]]


def statuses(runner, run_id: str) -> dict[str, str]:
    return {s["step_id"]: s["status"] for s in runner.snapshot(run_id)["steps"]}


def test_a_real_run_stops_after_the_step_and_carries_on_when_you_say_ok(ws, tmp_path):
    only_plan_asks(ws, {"policy": "always_ask"})
    runner = _go_deeper_runner(ws, tmp_path, followup_breaks=False)
    first = runner.run("deep-research", TOPIC, mode="live")
    assert first.status == "waiting"
    snap = runner.snapshot(first.run_id)
    assert snap["gate"]["step_id"] == "plan" and not snap["gate"]["before"]
    assert list(statuses(runner, first.run_id)) == ["plan"], "nothing after it has run"
    assert any(t.startswith("Waiting for your OK on “") for t in texts(runner, first))

    done = runner.carry_on(first.run_id, ok=True, note="Good brief.")
    assert done.status == "done", done.error
    steps = runner.snapshot(first.run_id)["steps"]
    assert [s["step_id"] for s in steps].count("plan") == 1, "the brief is not written again"
    assert any("You said OK. You added: “Good brief.”" in t for t in texts(runner, done))
    assert runner.snapshot(first.run_id)["gate"] is None


def test_stopping_ends_the_run_there_and_keeps_what_finished(ws, tmp_path):
    only_plan_asks(ws, {"policy": "always_ask"})
    runner = _go_deeper_runner(ws, tmp_path, followup_breaks=False)
    first = runner.run("deep-research", TOPIC, mode="live")
    stopped = runner.carry_on(first.run_id, ok=False)
    assert stopped.status == "stopped"
    assert statuses(runner, first.run_id) == {"plan": "done"}


def test_a_step_stops_asking_once_it_has_earned_it_and_asks_again_when_it_changes(ws, tmp_path):
    only_plan_asks(ws, {"policy": "earned", "promote_after": 2})
    runner = _go_deeper_runner(ws, tmp_path, followup_breaks=False)
    for _ in range(2):
        r = runner.run("deep-research", TOPIC, mode="live")
        assert r.status == "waiting"
        assert "so far" in runner.snapshot(r.run_id)["steps"][0]["decisions"][-1]["reason"]
        runner.carry_on(r.run_id, ok=True)

    third = runner.run("deep-research", TOPIC, mode="live")
    assert third.status == "done", "two OKs in a row: it carries on by itself"
    assert "Carried on after “" in " ".join(texts(runner, third))

    # a change to its instructions starts the count again
    skill = read_yaml(ws, DEF)
    plan = next(s for s in skill["spec"]["steps"] if s["id"] == "plan")
    path = ws.path(plan["skill"].split("@")[0])
    path.write_text(path.read_text() + "\nKeep the brief to one page.\n")
    fourth = runner.run("deep-research", TOPIC, mode="live")
    assert fourth.status == "waiting"


def test_a_stop_starts_the_count_again(ws, tmp_path):
    only_plan_asks(ws, {"policy": "earned", "promote_after": 2})
    runner = _go_deeper_runner(ws, tmp_path, followup_breaks=False)
    runner.carry_on(runner.run("deep-research", TOPIC, mode="live").run_id, ok=True)
    runner.carry_on(runner.run("deep-research", TOPIC, mode="live").run_id, ok=False)
    runner.carry_on(runner.run("deep-research", TOPIC, mode="live").run_id, ok=True)
    assert runner.run("deep-research", TOPIC, mode="live").status == "waiting", "one OK since"


def test_a_dry_run_says_where_it_would_stop_and_carries_on(ws, tmp_path):
    only_plan_asks(ws, {"policy": "always_ask"})
    runner = _go_deeper_runner(ws, tmp_path, followup_breaks=False)
    r = runner.run("deep-research", TOPIC)
    assert r.status == "done", r.error
    assert any(
        "In a real run, this is where it would stop for your OK" in t for t in texts(runner, r)
    )


def test_follow_ups_do_not_stop_for_an_ok(ws, tmp_path):
    """Going deeper was approved in the run that started them."""
    d = read_yaml(ws, DEF)
    d["spec"]["defaults"]["trust"] = {"policy": "auto"}
    for s in d["spec"]["steps"]:
        # the parent's brief asks; so would every follow-up's, if follow-ups asked
        s["trust"] = {"policy": "always_ask"} if s["id"] == "plan" else {"policy": "auto"}
    write_yaml(ws, DEF, d)
    runner = _go_deeper_runner(ws, tmp_path, followup_breaks=False)
    r = runner.run("deep-research", TOPIC, mode="live")
    done = runner.carry_on(r.run_id, ok=True)
    assert done.status == "done", done.error
    children = [x for x in runner.list_runs() if x["parent_run_id"] == r.run_id]
    assert children and all(c["status"] == "done" for c in children)


def test_the_run_page_asks_for_the_ok_and_the_answer_carries_it_on(client, ws):  # noqa: F811
    from tests.test_api import wait_for

    only_plan_asks(ws, {"policy": "always_ask"})

    def start() -> dict:
        r = client.post(
            "/api/workflows/deep-research/runs", json={"case": "durable-execution", "mode": "live"}
        )
        assert r.status_code == 200, r.text
        return wait_for(client, r.json()["run_id"])

    run = start()
    assert run["status"] == "waiting" and run["gate"]["step_id"] == "plan"
    page = client.get(f"/runs/{run['id']}").text
    assert f'data-ok="{run["id"]}"' in page and "Waiting for your OK on" in page
    assert 'data-answer-wait="' not in page, "not the wait's question"

    assert client.post(f"/api/runs/{run['id']}/ok", json={"ok": True}).status_code == 200
    run = wait_for(client, run["id"])
    assert run["status"] == "done", run["error"]
    assert client.post(f"/api/runs/{run['id']}/ok", json={"ok": True}).status_code == 409

    run = start()
    r = client.post(f"/api/runs/{run['id']}/ok", json={"ok": False, "note": "Wrong topic."})
    assert r.json()["status"] == "stopped"
    assert client.get(f"/api/runs/{run['id']}").json()["status"] == "stopped"
    assert "You stopped this run." in client.get(f"/runs/{run['id']}").text


def only_going_deeper_asks(ws, trust: dict[str, Any]) -> None:
    d = read_yaml(ws, DEF)
    d["spec"]["defaults"]["trust"] = {"policy": "auto"}
    for s in d["spec"]["steps"]:
        s["trust"] = trust if s["id"] == "go_deeper" else {"policy": "auto"}
    write_yaml(ws, DEF, d)


def children_of(runner, run_id: str) -> list[dict[str, Any]]:
    return [x for x in runner.list_runs() if x["parent_run_id"] == run_id]


def test_going_deeper_asks_before_it_starts_anything(ws, tmp_path):
    """Afterwards the money is spent: the OK is about what it would start."""
    only_going_deeper_asks(ws, {"policy": "always_ask"})
    runner = _go_deeper_runner(ws, tmp_path, followup_breaks=False)
    r = runner.run("deep-research", TOPIC, mode="live")
    assert r.status == "waiting"
    gate = runner.snapshot(r.run_id)["gate"]
    assert gate["step_id"] == "go_deeper" and gate["before"]
    assert gate["text"] == "Waiting for your OK before starting “Go deeper, if you say so”."
    assert gate["reason"].startswith("It would start 1 follow-up: “")
    assert "go_deeper" not in statuses(runner, r.run_id) and not children_of(runner, r.run_id)

    done = runner.carry_on(r.run_id, ok=True)
    assert done.status == "done", done.error
    assert statuses(runner, r.run_id)["go_deeper"] == "done"
    assert children_of(runner, r.run_id), "the follow-up started once you said yes"
    assert any("You said OK to starting it." in t for t in texts(runner, r))


def test_saying_no_to_going_deeper_starts_nothing(ws, tmp_path):
    only_going_deeper_asks(ws, {"policy": "always_ask"})
    runner = _go_deeper_runner(ws, tmp_path, followup_breaks=False)
    r = runner.run("deep-research", TOPIC, mode="live")
    assert runner.carry_on(r.run_id, ok=False).status == "stopped"
    assert not children_of(runner, r.run_id)


def test_when_the_review_decides_follow_ups_start_without_asking(ws, tmp_path):
    only_going_deeper_asks(ws, {"policy": "auto"})
    runner = _go_deeper_runner(ws, tmp_path, followup_breaks=False)
    r = runner.run("deep-research", TOPIC, mode="live")
    assert r.status == "done", r.error
    assert children_of(runner, r.run_id)


def test_a_dry_run_says_it_would_ask_before_going_deeper(ws, tmp_path):
    only_going_deeper_asks(ws, {"policy": "always_ask"})
    runner = _go_deeper_runner(ws, tmp_path, followup_breaks=False)
    r = runner.run("deep-research", TOPIC)
    assert r.status == "done", r.error
    assert any("would ask you before starting “" in t for t in texts(runner, r))


def test_the_run_page_asks_whether_to_start_it(client, ws):  # noqa: F811
    from tests.test_api import wait_for

    only_going_deeper_asks(ws, {"policy": "always_ask"})
    r = client.post(
        "/api/workflows/deep-research/runs", json={"case": "durable-execution", "mode": "live"}
    )
    run = wait_for(client, r.json()["run_id"])
    assert run["status"] == "waiting" and run["gate"]["before"], run["error"]
    page = client.get(f"/runs/{run['id']}").text
    assert "Start it?" in page and "Change who decides whether to go deeper" in page
