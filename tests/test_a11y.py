"""What a screen reader or a keyboard needs from the pages."""

from __future__ import annotations

from pathlib import Path

from tests.test_api import client  # noqa: F401 - the fixture
from tests.test_audit import DOC

STATIC = Path(__file__).resolve().parents[1] / "src" / "wf" / "web" / "static"


def test_the_runs_filter_is_a_tablist(client):  # noqa: F811
    page = client.get("/runs").text
    assert 'role="tablist"' in page and 'role="tab" aria-selected="true"' in page


def test_the_chat_is_a_log_read_out_as_it_grows(client):  # noqa: F811
    audit = client.post("/api/audits", json={"document": DOC.read_text(), "name": "x-y"}).json()
    assert (
        'data-chat-log role="log" aria-live="polite"' in client.get(f"/audits/{audit['id']}").text
    )


def test_messages_are_live_regions_and_errors_are_a_class_not_an_inline_colour():
    js = (STATIC / "app.js").read_text()
    assert 'setAttribute("aria-live", "polite")' in js and "style.color" not in js
    css = (STATIC / "app.css").read_text()
    assert ".btn:focus-visible" in css and ".opt:has(input:checked)" in css
