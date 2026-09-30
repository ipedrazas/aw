"""A step that searches further can be tuned on the workflow's settings, and its run
page shows each round: the topic, why, where it was found, and what it cost."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.helpers import make_db
from tests.test_api import wait_for
from tests.test_search_further import definition, scouting, script  # noqa: F401 - the fixture
from wf.activities import ScriptedModel
from wf.api.app import AppState, create_app
from wf.schema.loader import load_workflow_dict


def _client(ws, tmp_path: Path) -> TestClient:
    ws.save_definition(load_workflow_dict(definition()))
    state = AppState(ws, make_db(), ScriptedModel(script([])), tmp_path / "a")
    return TestClient(create_app(state))


def test_the_run_page_shows_each_round(scouting, tmp_path):  # noqa: F811
    client = _client(scouting, tmp_path)
    r = client.post("/api/workflows/scouting/runs", json={"inputs": {"topic": "Cells, in depth"}})
    run = wait_for(client, r.json()["run_id"])
    assert run["status"] == "done", run["error"]
    page = client.get(f"/runs/{run['id']}").text
    assert "Searching further · 4 topics followed, 2 rounds deep" in page
    assert "<strong>cell routers</strong>" in page and "found under “cells”" in page
    assert "cells matters" in page, "why it was followed"
    assert "First search" in page


def test_its_limits_are_set_on_the_settings_page(scouting, tmp_path):  # noqa: F811
    client = _client(scouting, tmp_path)
    page = client.get("/workflows/scouting/settings").text
    assert "Searching further: “Search the web”" in page and 'data-further="search"' in page
    r = client.post(
        "/api/workflows/scouting/settings",
        json={
            "step": "search",
            "search_further": {"levels": 1, "max_searches": 8, "max_topics": 2},
        },
    )
    assert r.status_code == 200, r.text
    sf = scouting.load_definition("scouting").spec.steps[0].search_further
    assert (sf.levels, sf.max_searches, sf.max_topics, sf.follow) == (1, 8, 2, "new_topics")
    bad = client.post(
        "/api/workflows/scouting/settings",
        json={"step": "write", "search_further": {"levels": 1, "max_searches": 8, "max_topics": 2}},
    )
    assert bad.status_code == 400
