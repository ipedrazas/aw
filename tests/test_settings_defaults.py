"""A workflow's settings say what new workflows start from, and can go back to it."""

from __future__ import annotations

import json
import re

from tests.test_api import client  # noqa: F401 - the fixture

PAGE = "/workflows/deep-research/settings"


def _use_app(page: str) -> list[dict]:
    return [
        json.loads(v.replace("&#34;", '"')) for v in re.findall(r"data-use-app='([^']*)'", page)
    ]


def test_the_page_says_what_new_workflows_start_from(client):  # noqa: F811
    page = client.get(PAGE).text
    assert "New workflows start from “Check until it has earned my trust”" in page
    assert "Leave a box blank for no limit on it." in page
    assert page.count("<span data-msg") >= 3, "each Save says what happened beside it"


def test_use_that_here_sets_the_workflow_to_the_app_default(client):  # noqa: F811
    client.post("/api/settings", json={"trust": {"policy": "auto"}})
    client.post("/api/settings", json={"budget": {"max_usd": 3, "max_minutes": 10}})
    offers = _use_app(client.get(PAGE).text)
    assert {"trust": {"policy": "auto", "promote_after": None}} in offers
    budget = next(o for o in offers if "budget" in o)
    assert budget == {"budget": {"max_usd": 3.0, "max_minutes": 10.0}}

    for body in offers:
        r = client.post("/api/workflows/deep-research/settings", json=body)
        assert r.status_code == 200, r.text
    data = client.get("/api/workflows/deep-research/settings").json()
    assert data["trust"]["policy"] == "auto"
    assert data["budget"]["max_usd"] == 3 and data["budget"]["max_minutes"] == 10
    assert _use_app(client.get(PAGE).text) == [], "nothing left to take from the defaults"
