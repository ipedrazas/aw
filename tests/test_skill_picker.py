"""The workflow page chooses a step's instructions the way it chooses its model: from
the instructions written for the step and the system's own, never a path typed in."""

from __future__ import annotations

import re

from tests.helpers import read_yaml, step
from tests.test_api import client  # noqa: F401  (pytest fixture)
from wf import settings

REL = "definitions/deep-research.workflow.yaml"
URL = "/api/workflows/deep-research/skill"


def select_for(page: str, attr: str, sid: str) -> str:
    m = re.search(rf'<select {attr}="{sid}".*?</select>', page, re.S)
    assert m, f"no {attr} for {sid}"
    return m.group(0)


def test_each_agent_step_offers_the_system_s_own_instructions(client, ws):  # noqa: F811
    wf = client.get("/api/workflows/deep-research").json()
    review = {c["label"]: c["value"] for c in wf["skill_choices"]["review"]}
    assert review["Check the page says what the report says"] == "skills/claim-support.md@1"
    assert "check_links" not in wf["skill_choices"], "a check follows no instructions"

    page = client.get("/workflows/deep-research").text
    picker = select_for(page, "data-skill-step", "review")
    current = step(read_yaml(ws, REL), "review")["skill"]
    assert f'<option value="{current}" selected>' in picker
    assert 'data-skill-step="check_links"' not in page


def test_a_step_is_moved_onto_other_instructions_by_name(client, ws):  # noqa: F811
    r = client.post(URL, json={"step": "review", "skill": "claim-support"})
    assert r.status_code == 200, r.text
    assert step(read_yaml(ws, REL), "review")["skill"] == "skills/claim-support.md@1"

    shown = next(s for s in r.json()["steps"] if s["id"] == "review")
    assert shown["technical"]["skill"] == "skills/claim-support.md@1", "the page shows it at once"


def test_instructions_that_are_not_there_are_refused(client, ws):  # noqa: F811
    before = step(read_yaml(ws, REL), "review")["skill"]
    r = client.post(URL, json={"step": "review", "skill": "skills/nope.md"})
    assert r.status_code == 400 and "skills/claim-support.md@1" in r.json()["detail"]
    assert step(read_yaml(ws, REL), "review")["skill"] == before
    assert (
        client.post(URL, json={"step": "check_links", "skill": "claim-support"}).status_code == 400
    )
    assert client.post(URL, json={"step": "nope", "skill": "claim-support"}).status_code == 404


def test_the_instructions_written_for_a_step_stay_on_its_list(client, ws):  # noqa: F811
    ws.save_skill("skills/deep-research/review.md", 2, "# Review, our way\n\nRead it twice.")
    client.post(URL, json={"step": "review", "skill": "claim-support"})
    choices = client.get("/api/workflows/deep-research").json()["skill_choices"]["review"]
    assert choices[0] == {
        "value": "skills/deep-research/review.md@2",
        "label": "Its own instructions",
    }, "so a step moved off them can go back"


def test_a_step_on_the_default_model_is_not_offered_it_twice(client):  # noqa: F811
    client.post(
        "/api/workflows/deep-research/model", json={"step": None, "model": settings.quick_model()}
    )
    client.post("/api/workflows/deep-research/model", json={"step": "plan", "model": None})
    page = client.get("/workflows/deep-research").text
    plan = select_for(page, "data-model-step", "plan")
    assert "Default (Standard)" in plan
    assert f'value="{settings.quick_model()}"' not in plan
    assert f'value="{settings.careful_model()}"' in plan
