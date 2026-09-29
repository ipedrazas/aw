"""The draft page's questions are called questions, with how far through them you are."""

from __future__ import annotations

from tests.test_api import client  # noqa: F401 - the fixture
from tests.test_audit import DOC


def test_the_questions_card_is_called_questions_and_shows_progress(client):  # noqa: F811
    audit = client.post(
        "/api/audits", json={"document": DOC.read_text(), "name": "client-research"}
    ).json()
    page = client.get(f"/audits/{audit['id']}").text
    assert "<h2>Questions</h2>" in page and "What you expect" not in page
    assert '<progress class="bar" value="' in page and 'id="questions"' in page
