"""Times are marked up for the page to say them in words, where you are."""

from __future__ import annotations

from tests.test_api import client, wait_for  # noqa: F401 - the fixture


def test_runs_show_when_they_started_as_a_time_element(client):  # noqa: F811
    r = client.post("/api/workflows/deep-research/runs", json={"case": "durable-execution"})
    run = wait_for(client, r.json()["run_id"])
    tag = f'<time datetime="{run["started_at"]}" data-ago>'
    assert tag in client.get("/runs").text
    assert tag in client.get(f"/runs/{run['id']}").text
