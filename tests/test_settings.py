"""A workflow's settings page: how often its steps check with you, and its limits."""

from __future__ import annotations

from tests.helpers import read_yaml, write_yaml
from tests.test_api import client, wait_for  # noqa: F401 - the fixture
from tests.test_gates import only_plan_asks
from tests.test_retry import TOPIC, _go_deeper_runner

DEF = "definitions/deep-research.workflow.yaml"
URL = "/api/workflows/deep-research/settings"


def step_of(ws, sid):
    return next(s for s in read_yaml(ws, DEF)["spec"]["steps"] if s["id"] == sid)


def test_the_page_shows_each_step_and_how_far_it_has_got(client):  # noqa: F811
    page = client.get("/workflows/deep-research/settings")
    assert page.status_code == 200
    assert "Check until it has earned my trust" in page.text and "Check every time" in page.text
    data = client.get(URL).json()
    plan = next(s for s in data["steps"] if s["id"] == "plan")
    assert plan["progress"] == "0 of 3 OKs in a row so far."
    links = next(s for s in data["steps"] if s["id"] == "check_links")
    assert links["stops"] is False and links["label"] == "Doesn't stop"
    assert 'href="/workflows/deep-research/settings"' in client.get("/workflows/deep-research").text


def test_the_workflows_choice_keeps_what_the_page_does_not_show(client, ws):  # noqa: F811
    r = client.post(URL, json={"trust": {"policy": "auto"}})
    assert r.status_code == 200, r.text
    trust = read_yaml(ws, DEF)["spec"]["defaults"]["trust"]
    assert trust["policy"] == "auto"
    assert trust["reset_on"], "what resets a step's count is kept"
    assert step_of(ws, "plan")["trust"]["policy"] == "earned", "a step's own setting stays"

    client.post(URL, json={"reset_steps": True})
    assert "trust" not in step_of(ws, "plan"), "every step back on the workflow's"
    assert step_of(ws, "check_links")["trust"] == {"policy": "auto"}, (
        "a check is not a step that stops"
    )


def test_a_step_set_to_earn_it_counts_to_the_workflows_number(client, ws):  # noqa: F811
    client.post(URL, json={"trust": {"policy": "earned", "promote_after": 4}})
    client.post(URL, json={"step": "write", "trust": {"policy": "earned"}})
    assert step_of(ws, "write")["trust"]["promote_after"] == 4
    client.post(URL, json={"step": "write", "trust": None})
    assert "trust" not in step_of(ws, "write")


def test_settings_that_do_not_fit_change_nothing(client, ws):  # noqa: F811
    before = read_yaml(ws, DEF)
    for body in (
        {"step": "check_links", "trust": {"policy": "always_ask"}},
        {"trust": {"policy": "earned", "promote_after": 0}},
        {"trust": {"policy": "sometimes"}},
        {"budget": {"max_usd": -1, "max_minutes": 10}},
        {"step": "plan", "limits": {"max_depth": 1, "max_fanout": 1}},
        {"step": "nope", "trust": None},
    ):
        assert client.post(URL, json=body).status_code in (400, 404), body
    assert read_yaml(ws, DEF) == before


def test_the_spending_limit_and_how_far_follow_ups_go(client, ws):  # noqa: F811
    assert client.post(URL, json={"budget": {"max_usd": 3, "max_minutes": 15}}).status_code == 200
    budget = read_yaml(ws, DEF)["spec"]["budget"]
    assert budget["max_usd"] == 3 and budget["max_minutes"] == 15
    assert budget["shared_with_children"] is True, "what the page does not show is kept"

    r = client.post(URL, json={"step": "go_deeper", "limits": {"max_depth": 1, "max_fanout": 2}})
    assert r.status_code == 200, r.text
    lim = step_of(ws, "go_deeper")["limits"]
    assert (lim["max_depth"], lim["max_fanout"], lim["budget"]) == (1, 2, "inherit")


def test_an_ok_on_the_run_page_moves_the_count_on_the_settings_page(client, ws):  # noqa: F811
    only_plan_asks(ws, {"policy": "earned", "promote_after": 3})
    r = client.post(
        "/api/workflows/deep-research/runs", json={"case": "durable-execution", "mode": "live"}
    )
    run = wait_for(client, r.json()["run_id"])
    page = client.get(f"/runs/{run['id']}").text
    assert "so far 0." in page and "Change how often it checks with you" in page
    client.post(f"/api/runs/{run['id']}/ok", json={"ok": True})
    wait_for(client, run["id"])
    plan = next(s for s in client.get(URL).json()["steps"] if s["id"] == "plan")
    assert plan["progress"] == "1 of 3 OKs in a row so far."


def test_a_check_never_stops_a_run_whatever_its_trust_says(ws, tmp_path):
    d = read_yaml(ws, DEF)
    for s in d["spec"]["steps"]:
        s["trust"] = {"policy": "always_ask"} if s["kind"] == "check" else {"policy": "auto"}
    write_yaml(ws, DEF, d)
    runner = _go_deeper_runner(ws, tmp_path, followup_breaks=False)
    assert runner.run("deep-research", TOPIC, mode="live").status == "done"


def test_who_decides_whether_to_go_deeper(client, ws):  # noqa: F811
    page = client.get("/workflows/deep-research/settings").text
    assert "Who decides whether to go deeper?" in page and "Let the review decide" in page
    client.post(URL, json={"step": "go_deeper", "trust": {"policy": "auto"}})
    assert step_of(ws, "go_deeper")["trust"]["policy"] == "auto"
    gd = next(f for f in client.get(URL).json()["followups"] if f["id"] == "go_deeper")
    assert gd["asks"] == "auto" and gd["wait"] is None


def test_a_wait_that_asks_about_going_deeper_is_pointed_out(client, ws):  # noqa: F811
    from tests.test_api import _answered_wait_workflow

    _answered_wait_workflow(client, ws)
    gd = next(f for f in client.get(URL).json()["followups"] if f["id"] == "go_deeper")
    assert gd["wait"], "the step that asks you is named"
    assert "is a step of its own" in client.get("/workflows/deep-research/settings").text
