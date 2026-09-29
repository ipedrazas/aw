"""The form that starts a run asks for the workflow's own inputs, not a topic it assumes."""

from __future__ import annotations

from tests.helpers import read_yaml, write_yaml
from tests.test_api import client, wait_for  # noqa: F401 - the fixture
from wf.api.plain import run_fields, sends_outside, starts_more_work
from wf.interpret.interpreter import run_title
from wf.schema import Workspace

DEF = "definitions/deep-research.workflow.yaml"


def _with_inputs(ws: Workspace, inputs: dict) -> None:
    data = read_yaml(ws, DEF)
    data["spec"]["inputs"] = inputs
    write_yaml(ws, DEF, data)


def test_the_run_form_has_a_field_per_input_a_person_gives(client):  # noqa: F811
    page = client.get("/workflows/deep-research").text
    assert 'data-input="topic"' in page and "At least 10 characters" in page
    assert 'data-input="depth"' not in page, "the runtime sets depth itself"
    assert "Run with a topic" not in page and "What should it research?" not in page


def test_other_inputs_get_fields_of_their_kind(client, ws):  # noqa: F811
    _with_inputs(
        ws,
        {
            "topic": {"type": "string", "required": True, "min_length": 10},
            "audience": {"type": "string", "enum": ["board", "engineers"], "default": "board"},
            "max_pages": {"type": "integer", "description": "How long the report may be"},
            "depth": {"type": "integer", "default": 0, "internal": True},
        },
    )
    page = client.get("/workflows/deep-research").text
    assert '<option value="engineers">engineers</option>' in page
    assert 'data-input="max_pages" data-type="integer"' in page
    assert "Max pages (optional)" in page and "How long the report may be" in page


def test_a_run_is_named_after_the_first_text_it_was_given(ws):
    wf = ws.load_definition("deep-research")
    assert run_title(wf, {"topic": "Durable execution", "depth": 0}) == "Durable execution"
    assert run_title(wf, {"depth": 0}) == "deep-research"


def test_a_run_started_with_other_inputs_is_named_after_them(client, ws):  # noqa: F811
    _with_inputs(ws, {"question": {"type": "string", "required": True}})
    r = client.post(
        "/api/workflows/deep-research/runs",
        json={"inputs": {"question": "Which queues survive a restart?"}},
    )
    assert r.status_code == 200, r.text
    run = wait_for(client, r.json()["run_id"])
    assert run["title"] == "Which queues survive a restart?"


def test_what_the_page_says_a_real_run_does(ws):
    wf = ws.load_definition("deep-research")
    assert [f["name"] for f in run_fields(wf)] == ["topic"]
    assert starts_more_work(wf), "the sample can go deeper"
    assert not sends_outside(wf)


def test_a_finished_run_says_done_not_that_a_pdf_is_ready(client):  # noqa: F811
    r = client.post("/api/workflows/deep-research/runs", json={"case": "durable-execution"})
    wait_for(client, r.json()["run_id"])
    assert "PDF ready" not in client.get("/runs").text
