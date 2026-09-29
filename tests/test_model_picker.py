"""A model a step cannot run on is still in its dropdown, marked, and picking it says
why beside the dropdown, with what would make it possible, instead of a refusal the
person never sees."""

from __future__ import annotations

import html
import re

import pytest

from tests.test_api import client  # noqa: F401  (pytest fixture)

CLAIMS_TITLE = "Check the page says what the report says"


@pytest.fixture
def page(client, monkeypatch):  # noqa: F811
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.delenv("WF_STEP_MODELS", raising=False)
    return client.get("/workflows/deep-research").text


def jev_option(page: str, attr: str) -> str:
    m = re.search(rf"<select {attr}.*?</select>", page, re.S)
    assert m, attr
    (opt,) = re.findall(r'<option value="typesafe/jev-1\.13"[^>]*>[^<]*</option>', m.group(0))
    return html.unescape(opt)


def test_jev_is_marked_on_a_step_it_cannot_run_and_says_what_would_let_it(page):
    opt = jev_option(page, 'data-model-step="review"')
    assert "(not for this step)" in opt
    assert "Jev only answers pick-one and yes/no questions" in opt
    assert f"such as “{CLAIMS_TITLE}”" in opt, "the way to make it possible"


def test_no_instructions_let_a_step_that_searches_run_on_jev(page):
    opt = jev_option(page, 'data-model-step="research"')
    assert "cannot search or read pages" in opt and "such as" not in opt


def test_jev_is_a_plain_choice_where_it_can_run(page):
    opt = jev_option(page, 'data-model-step="check_support"')
    assert "data-cannot" not in opt and "not for this step" not in opt


def test_jev_is_marked_for_the_default_while_some_step_would_break(client, page):  # noqa: F811
    opt = jev_option(page, 'id="default-model" data-model-default')
    assert "data-cannot" not in opt, "every step names its own model, so none would break"
    url = "/api/workflows/deep-research/model"
    assert client.post(url, json={"step": None, "model": "Standard"}).status_code == 200
    assert client.post(url, json={"step": "plan", "model": None}).status_code == 200
    opt = jev_option(client.get("/workflows/deep-research").text, 'id="default-model"')
    assert "(not for every step)" in opt and "“Work out what to look for”" in opt


def test_what_happens_is_said_beside_each_dropdown(page):
    for attr in ('data-model-step="review"', 'data-skill-step="review"'):
        assert re.search(rf"<select {attr}.*?</select>\s*<div data-msg", page, re.S), attr
