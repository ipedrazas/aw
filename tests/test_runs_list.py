"""The runs list: filtered by workflow and kind, a page at a time, and compared within
one workflow."""

from __future__ import annotations

from tests.test_api import client, wait_for  # noqa: F401 - the fixture


def _dry(client):  # noqa: F811
    r = client.post("/api/workflows/deep-research/runs", json={"case": "durable-execution"})
    return wait_for(client, r.json()["run_id"])


def test_runs_filter_by_kind_and_workflow_in_the_address(client):  # noqa: F811
    run = _dry(client)
    assert f'href="/runs/{run["id"]}"' in client.get("/runs?mode=dry").text
    only_real = client.get("/runs?mode=live").text
    assert f'href="/runs/{run["id"]}"' not in only_real and "No runs match" in only_real
    assert '<option value="live" selected>' in only_real
    page = client.get("/runs?workflow=deep-research").text
    assert '<option value="deep-research" selected>' in page
    assert '<option value="/workflows/deep-research">' in page, "start a run of it from here"


def test_older_runs_are_on_the_next_page(client, monkeypatch):  # noqa: F811
    import wf.api.app as app

    monkeypatch.setattr(app, "RUNS_PAGE", 1)
    first, second = _dry(client), _dry(client)
    page = client.get("/runs").text
    assert f'href="/runs/{second["id"]}"' in page and f'href="/runs/{first["id"]}"' not in page
    assert 'href="/runs?page=2">Older' in page
    older = client.get("/runs?page=2").text
    assert f'href="/runs/{first["id"]}"' in older and 'href="/runs">Newer' in older


def test_compare_options_carry_their_workflow(client):  # noqa: F811
    _dry(client), _dry(client)
    page = client.get("/runs").text
    assert 'data-workflow="deep-research">' in page
    assert "Only runs of the same workflow can be compared." in page
