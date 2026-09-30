"""A run's guesses and open questions lead to where they can be answered, and a run can
be run again to see the gaps closed."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.helpers import make_db
from tests.scripted import deep_research_script
from tests.test_api import client, wait_for  # noqa: F401 - the fixture
from tests.test_audit import DOC
from wf.activities import ScriptedModel
from wf.api.app import AppState, create_app


def _client(ws, tmp_path: Path) -> TestClient:
    state = AppState(ws, make_db(), ScriptedModel(deep_research_script("accept")), tmp_path / "a")
    return TestClient(create_app(state))


def test_a_guess_links_to_its_question_and_says_when_it_was_answered_since(client):  # noqa: F811
    audit = client.post(
        "/api/audits", json={"document": DOC.read_text(), "name": "client-research"}
    ).json()
    d = client.post(f"/api/audits/{audit['id']}/dry-run", json={"topic": "Durable execution"})
    run = wait_for(client, d.json()["run_id"])
    g = next(g for g in run["report"]["guesses"] if g["field"] == "steps.send.requires_approval")
    fid = g["finding_id"]
    page = client.get(f"/runs/{run['id']}").text
    assert f'#q-{fid}">Answer this now' in page
    assert 'data-rerun="client-research"' in page and "Run it again, as a dry run" in page

    wf = client.get("/api/workflows/client-research").json()
    f = next(f for f in wf["findings"] if f["id"] == fid)
    got = client.post(
        "/api/workflows/client-research/answer",
        json={"finding_id": fid, "answer": f["options"][0]["value"]},
    )
    assert got.status_code == 200, got.text
    page = client.get(f"/runs/{run['id']}").text
    assert f'#q-{fid}">Answer this now' not in page and "Answered since this run." in page
    assert "been answered since" in page


def test_running_it_again_starts_a_dry_run_with_the_same_case(ws, tmp_path):
    c = _client(ws, tmp_path)
    r = c.post("/api/workflows/deep-research/runs", json={"case": "durable-execution"})
    first = wait_for(c, r.json()["run_id"])
    page = c.get(f"/runs/{first['id']}").text
    assert "Run it again" not in page, "nothing to answer, so nothing to see closed"
