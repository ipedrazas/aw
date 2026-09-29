"""The nav says where you are, and that a run is waiting for you, from any page."""

from __future__ import annotations

from tests.test_api import client, wait_for  # noqa: F401 - the fixture
from tests.test_gates import only_plan_asks
from wf.api.app import _section


def test_each_page_marks_its_part_of_the_nav(client):  # noqa: F811
    assert 'href="/runs" class="active" aria-current="page"' in client.get("/runs").text
    page = client.get("/workflows/deep-research/settings").text
    assert 'href="/workflows" class="active"' in page, "a workflow's settings are the workflow's"
    assert 'href="/settings" class="active"' not in page
    assert [_section(p) for p in ("/", "/audits/new", "/skill", "/settings")] == [
        "workflows",
        "workflows",
        "skills",
        "settings",
    ]


def test_a_run_waiting_for_you_shows_on_every_page(client, ws):  # noqa: F811
    assert 'class="badge"' not in client.get("/workflows").text
    only_plan_asks(ws, {"policy": "always_ask"})
    r = client.post(
        "/api/workflows/deep-research/runs", json={"case": "durable-execution", "mode": "live"}
    )
    assert wait_for(client, r.json()["run_id"])["status"] == "waiting"
    for url in ("/workflows", "/skills", "/settings"):
        assert 'title="1 run waiting for you">1<' in client.get(url).text
