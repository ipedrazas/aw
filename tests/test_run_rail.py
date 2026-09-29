"""On a long run page, the side rail and the banner take you to the step you want."""

from __future__ import annotations

from tests.test_api import client, wait_for  # noqa: F401 - the fixture
from tests.test_gates import only_plan_asks


def test_each_step_in_the_rail_links_to_its_card(client):  # noqa: F811
    r = client.post("/api/workflows/deep-research/runs", json={"case": "durable-execution"})
    run = wait_for(client, r.json()["run_id"])
    page = client.get(f"/runs/{run['id']}").text
    for s in run["steps"]:
        assert f'href="#step-{s["step_id"]}"' in page and f'id="step-{s["step_id"]}"' in page


def test_the_banner_of_a_run_waiting_for_an_ok_goes_to_the_form(client, ws):  # noqa: F811
    only_plan_asks(ws, {"policy": "always_ask"})
    r = client.post(
        "/api/workflows/deep-research/runs", json={"case": "durable-execution", "mode": "live"}
    )
    run = wait_for(client, r.json()["run_id"])
    assert run["status"] == "waiting", run["error"]
    page = client.get(f"/runs/{run['id']}").text
    assert '<a href="#ok-plan">Go to it</a>' in page and 'id="ok-plan"' in page
