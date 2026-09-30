"""The home page says what needs you; with nothing yet it goes to the workflows."""

from __future__ import annotations

from tests.test_api import client, wait_for  # noqa: F401 - the fixture
from tests.test_audit import DOC
from tests.test_gates import only_plan_asks


def test_with_nothing_yet_home_is_the_workflows(client):  # noqa: F811
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/workflows"


def test_home_puts_what_waits_on_you_first(client, ws):  # noqa: F811
    only_plan_asks(ws, {"policy": "always_ask"})
    r = client.post(
        "/api/workflows/deep-research/runs", json={"case": "durable-execution", "mode": "live"}
    )
    run = wait_for(client, r.json()["run_id"])
    audit = client.post("/api/audits", json={"document": DOC.read_text(), "name": "p"}).json()
    page = client.get("/").text
    assert "<h2>Waiting on you</h2>" in page and f'href="/runs/{run["id"]}"' in page
    assert f'href="/audits/{audit["id"]}"' in page
    assert page.index("Waiting on you</h2>") < page.index("Drafts with questions left")
    assert "This week" in page and 'class="brand" href="/"' in page
