"""A saved workflow's history: each version and what changed, and putting one back."""

from __future__ import annotations

import subprocess

from tests.test_api import client  # noqa: F401 - the fixture


def _git(ws, *args):
    subprocess.run(["git", *args], cwd=ws.root, check=True, capture_output=True)


def _repo(ws):
    _git(ws, "init", "-q")
    _git(ws, "add", "-A")
    _git(ws, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "start")


def test_a_change_shows_in_the_history_and_can_be_put_back(client, ws):  # noqa: F811
    _repo(ws)
    before = ws.definition_path("deep-research").read_text()
    r = client.post(
        "/api/workflows/deep-research/settings", json={"budget": {"max_usd": 2, "max_minutes": 5}}
    )
    assert r.status_code == 200 and r.json()["commit"], r.text
    h = client.get("/api/workflows/deep-research/history").json()
    assert len(h["versions"]) == 2 and h["versions"][0]["latest"]
    assert any(row["op"] == "+" and "max_usd: 2" in row["text"] for row in h["versions"][0]["rows"])

    page = client.get("/workflows/deep-research/history").text
    assert "Put this version back" in page and 'href="/workflows/deep-research/history"' in (
        client.get("/workflows/deep-research").text
    )

    first = h["versions"][1]["commit"]
    got = client.post("/api/workflows/deep-research/restore", json={"commit": first})
    assert got.status_code == 200, got.text
    assert ws.definition_path("deep-research").read_text() == before
    assert len(client.get("/api/workflows/deep-research/history").json()["versions"]) == 3


def test_only_its_own_versions_can_be_put_back(client, ws):  # noqa: F811
    _repo(ws)
    r = client.post("/api/workflows/deep-research/restore", json={"commit": "0" * 40})
    assert r.status_code == 404


def test_without_git_there_is_no_history(client):  # noqa: F811
    h = client.get("/api/workflows/deep-research/history").json()
    assert h["versions"] == [] and h["git"] is False
    assert "not kept in git" in client.get("/workflows/deep-research/history").text
