"""Pages name a step by its title, the way the person wrote it, not by its id."""

from __future__ import annotations

from tests.test_api import client, wait_for  # noqa: F401 - the fixture
from tests.test_audit import DOC


def test_questions_and_run_squares_name_the_step_by_its_title(client):  # noqa: F811
    audit = client.post(
        "/api/audits", json={"document": DOC.read_text(), "name": "client-research"}
    ).json()
    d = client.post(f"/api/audits/{audit['id']}/dry-run", json={"topic": "Durable execution"})
    run = wait_for(client, d.json()["run_id"])

    wf = client.get("/api/workflows/client-research").json()
    q = next(f for g in wf["questions"] for f in g["findings"] if f["step_id"])
    assert q["step_title"] != q["step_id"], "the sample's steps have titles of their own"
    page = client.get("/workflows/client-research").text
    assert f"Step: {q['step_title']}" in page and f"Step: {q['step_id']}<" not in page

    runs = client.get("/runs").text
    assert f'title="{run["steps"][0]["step_id"]}: ' not in runs
