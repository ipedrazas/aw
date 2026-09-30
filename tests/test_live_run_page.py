"""A running run's page: the parts that change are marked to be swapped in place, a
running step says since when, and what it has spent shows against its limit."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.helpers import make_db
from tests.scripted import deep_research_script
from tests.test_api import wait_for
from wf.activities import ScriptedModel
from wf.api.app import AppState, create_app

STATIC = Path(__file__).resolve().parents[1] / "src" / "wf" / "web" / "static"


def test_a_run_page_marks_what_changes_and_shows_spend_against_the_limit(ws, tmp_path):
    state = AppState(ws, make_db(), ScriptedModel(deep_research_script("accept")), tmp_path / "a")
    client = TestClient(create_app(state))
    r = client.post("/api/workflows/deep-research/runs", json={"case": "durable-execution"})
    run = wait_for(client, r.json()["run_id"])
    assert all(s["started_at"] for s in run["steps"]), "each step says when it started"
    page = client.get(f"/runs/{run['id']}").text
    for region in ("spend", "rail", "steps"):
        assert f'data-live="{region}"' in page
    assert '<progress class="bar spend-bar"' in page, "the sample has a spending limit"


def test_the_page_swaps_in_place_and_reloads_only_when_the_run_stops_running():
    js = (STATIC / "app.js").read_text()
    assert "DOMParser" in js and 'd.status !== "running"' in js and "function since()" in js
