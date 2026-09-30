"""A workflow shows its own recent runs, and it and its draft link to each other."""

from __future__ import annotations

from tests.test_api import client, wait_for  # noqa: F401 - the fixture
from tests.test_audit import DOC


def test_the_workflow_page_lists_its_recent_runs(client):  # noqa: F811
    assert "Not run yet" in client.get("/workflows/deep-research").text
    ids = []
    for _ in range(2):
        r = client.post("/api/workflows/deep-research/runs", json={"case": "durable-execution"})
        ids.append(wait_for(client, r.json()["run_id"])["id"])
    page = client.get("/workflows/deep-research").text
    assert all(f'href="/runs/{i}"' in page for i in ids)
    assert f'href="/runs/{ids[1]}/diff/{ids[0]}"' in page, "compare an older run with the latest"
    assert 'href="/runs?workflow=deep-research"' in page
    only = client.get("/runs?workflow=deep-research").text
    assert "Show every run" in only


def test_a_saved_draft_and_its_workflow_link_to_each_other(client):  # noqa: F811
    audit = client.post(
        "/api/audits", json={"document": DOC.read_text(), "name": "client-research"}
    ).json()
    assert "Open the workflow" not in client.get(f"/audits/{audit['id']}").text
    client.post(f"/api/audits/{audit['id']}/save")
    assert (
        'href="/workflows/client-research">Open the workflow'
        in client.get(f"/audits/{audit['id']}").text
    )
    page = client.get("/workflows/client-research").text
    assert f'href="/audits/{audit["id"]}">Edit it in the chat' in page
    assert "Edit it in the chat" not in client.get("/workflows/deep-research").text
