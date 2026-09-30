"""A draft is made in the background, and the page is told each stage as it starts."""

from __future__ import annotations

import time

from tests.test_api import client  # noqa: F401 - the fixture
from tests.test_audit import DOC, scripted_auditor


def _wait(client, job_id: str) -> dict:  # noqa: F811
    deadline = time.time() + 20
    while time.time() < deadline:
        job = client.get(f"/api/audits/jobs/{job_id}").json()
        if job["stage"] in ("done", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError("the draft was not made")


def test_a_draft_made_in_the_background_ends_with_the_draft(client):  # noqa: F811
    job = client.post("/api/audits/jobs", json={"document": DOC.read_text(), "name": "bg"}).json()
    assert job["stage"] == "read" and job["audit_id"] is None
    done = _wait(client, job["id"])
    assert done["stage"] == "done", done["error"]
    audit = client.get(f"/api/audits/{done['audit_id']}").json()
    assert audit["name"] == "bg" and audit["questions"]


def test_the_stages_are_said_in_order(ws):
    seen: list[str] = []
    scripted_auditor(ws).audit(DOC.read_text(), name="p", progress=seen.append)
    assert seen[0] == "extract" and seen[-1] == "check"
    assert seen == sorted(seen, key=["extract", "skills", "check"].index)


def test_a_short_document_is_refused_before_anything_starts(client):  # noqa: F811
    assert client.post("/api/audits/jobs", json={"document": "too short"}).status_code == 400
    assert client.get("/api/audits/jobs/nope").status_code == 404


def test_the_new_draft_page_lists_the_stages(client):  # noqa: F811
    page = client.get("/audits/new").text
    assert 'data-stage="extract"' in page and 'data-stage="triage"' in page
