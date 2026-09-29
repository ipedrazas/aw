"""The run page: what a person looks for first on it."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.helpers import make_db
from tests.scripted import deep_research_script
from tests.test_api import wait_for
from tests.test_pause import _no_gates
from wf.activities import ScriptedModel
from wf.api.app import AppState, create_app


def _client(ws, tmp_path: Path) -> TestClient:
    state = AppState(ws, make_db(), ScriptedModel(deep_research_script("accept")), tmp_path / "a")
    return TestClient(create_app(state))


def test_a_finished_real_run_shows_what_it_made_before_its_steps(ws, tmp_path):
    _no_gates(ws)
    client = _client(ws, tmp_path)
    r = client.post(
        "/api/workflows/deep-research/runs", json={"case": "durable-execution", "mode": "live"}
    )
    run = wait_for(client, r.json()["run_id"])
    assert run["status"] == "done", run["error"]
    page = client.get(f"/runs/{run['id']}").text
    assert "What it made is first" in page
    assert page.index("What it made") < page.index("Where it had to guess")
    assert page.count('id="artefacts"') == 1


def test_a_dry_run_still_shows_its_guesses_first(ws, tmp_path):
    client = _client(ws, tmp_path)
    r = client.post("/api/workflows/deep-research/runs", json={"case": "durable-execution"})
    run = wait_for(client, r.json()["run_id"])
    page = client.get(f"/runs/{run['id']}").text
    assert page.index("Where it had to guess") < page.index('id="artefacts"')
    assert 'href="#artefacts"' in page
