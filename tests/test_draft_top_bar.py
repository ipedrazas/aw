"""A draft's top bar says what saving does, and whether there is anything to save."""

from __future__ import annotations

import subprocess

from tests.test_api import client  # noqa: F401 - the fixture
from tests.test_audit import DOC


def _git(ws, *args):
    subprocess.run(["git", *args], cwd=ws.root, check=True, capture_output=True)


def test_saving_is_named_and_a_change_after_it_shows(client, ws):  # noqa: F811
    _git(ws, "init", "-q")
    _git(ws, "add", "-A")
    _git(ws, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "start")
    audit = client.post(
        "/api/audits", json={"document": DOC.read_text(), "name": "client-research"}
    ).json()
    page = client.get(f"/audits/{audit['id']}").text
    assert "Save as a workflow</button>" in page and "Save draft" not in page
    menu = page[page.index('<details class="menu">') : page.index("</details>")]
    assert "Delete draft" in menu, "out of the way, not next to Save"

    assert client.post(f"/api/audits/{audit['id']}/save").json()["commit"]
    page = client.get(f"/audits/{audit['id']}").text
    assert "Update the workflow</button>" in page and "Every change saved" in page

    f = audit["questions"][0]["findings"][0]
    answer = f["options"][0]["value"] if f["options"] else "Every week."
    r = client.post(
        f"/api/audits/{audit['id']}/answer", json={"finding_id": f["id"], "answer": answer}
    )
    assert r.status_code == 200, r.text
    assert "Changed since it was saved" in client.get(f"/audits/{audit['id']}").text


def test_the_picture_shows_without_looking_for_it(client):  # noqa: F811
    audit = client.post("/api/audits", json={"document": DOC.read_text(), "name": "p"}).json()
    page = client.get(f"/audits/{audit['id']}").text
    assert '<div id="flowpic" style=' in page and "Hide the picture" in page
