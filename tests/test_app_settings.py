"""App-wide settings: what a new draft starts from, not what a saved workflow keeps."""

from __future__ import annotations

from tests.test_api import client, wait_for  # noqa: F401 - the fixture
from tests.test_audit import DOC, scripted_auditor
from wf.schema import Workspace

URL = "/api/settings"


def test_the_page_and_api_show_the_built_in_defaults(client):  # noqa: F811
    page = client.get("/settings")
    assert page.status_code == 200
    assert "Check until it has earned my trust" in page.text
    assert "Default model" in page.text
    data = client.get(URL).json()
    assert data["trust"]["policy"] == "earned" and data["trust"]["promote_after"] == 3
    assert data["model"] is None
    assert data["budget"] is None
    assert 'href="/settings"' in client.get("/workflows").text


def test_choosing_no_questions_at_all_is_what_a_new_draft_starts_from(client, ws):  # noqa: F811
    r = client.post(URL, json={"trust": {"policy": "auto"}})
    assert r.status_code == 200, r.text
    assert client.get(URL).json()["trust"]["policy"] == "auto"

    result = scripted_auditor(ws).audit(DOC.read_text(), name="brand-new")
    wf = result.workflow()
    assert wf.spec.defaults.trust.policy == "auto", "a new draft starts from the owner's choice"


def test_the_default_model_and_a_starting_budget_carry_to_a_new_draft(client, ws):  # noqa: F811
    assert client.post(URL, json={"model": "claude-opus-5"}).status_code == 200, "a real choice"
    r = client.post(URL, json={"budget": {"max_usd": 5, "max_minutes": 20}})
    assert r.status_code == 200, r.text
    data = client.get(URL).json()
    assert data["model"] == "claude-opus-5"
    assert data["budget"]["max_usd"] == 5 and data["budget"]["max_minutes"] == 20

    result = scripted_auditor(ws).audit(DOC.read_text(), name="brand-new-2")
    wf = result.workflow()
    assert wf.spec.defaults.model == "claude-opus-5"
    assert wf.spec.budget.max_usd == 5 and wf.spec.budget.max_minutes == 20


def test_an_existing_workflows_own_settings_keep_winning(client, ws):  # noqa: F811
    before = ws.load_definition("deep-research")
    assert client.post(URL, json={"trust": {"policy": "auto"}}).status_code == 200
    assert client.post(URL, json={"model": "claude-opus-5"}).status_code == 200
    assert client.post(URL, json={"budget": {"max_usd": 1, "max_minutes": 1}}).status_code == 200

    after = ws.load_definition("deep-research")
    assert after.spec.defaults.trust == before.spec.defaults.trust
    assert after.spec.defaults.model == before.spec.defaults.model
    assert after.spec.budget == before.spec.budget


def test_settings_persist_and_survive_a_reload(client, ws):  # noqa: F811
    assert client.post(URL, json={"trust": {"policy": "always_ask"}}).status_code == 200
    assert client.post(URL, json={"model": "claude-opus-5"}).status_code == 200
    r = client.post(URL, json={"budget": {"max_usd": 9, "max_minutes": 30}})
    assert r.status_code == 200, r.text

    reloaded = Workspace(ws.root).load_app_settings()
    assert reloaded.trust.policy == "always_ask"
    assert reloaded.model == "claude-opus-5"
    assert reloaded.budget.max_usd == 9 and reloaded.budget.max_minutes == 30


def test_settings_that_do_not_fit_change_nothing(client, ws):  # noqa: F811
    before = ws.load_app_settings()
    for body in (
        {"trust": {"policy": "earned", "promote_after": 0}},
        {"trust": {"policy": "sometimes"}},
        {"model": "not-a-real-model"},
        {"budget": {"max_usd": -1, "max_minutes": 10}},
        {},
    ):
        assert client.post(URL, json=body).status_code == 400, body
    assert ws.load_app_settings() == before
