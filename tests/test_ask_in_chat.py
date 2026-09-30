"""The list beside a draft sends a question to the chat, one place to answer it."""

from __future__ import annotations

from tests.test_api import client  # noqa: F401 - the fixture
from tests.test_chat_asks import asked, draft


def test_ask_me_in_the_chat_asks_that_question_now_and_puts_the_other_aside(client):  # noqa: F811
    audit = draft(client)
    first = audit["asking"]
    other = next(f["id"] for g in audit["questions"] for f in g["findings"] if f["id"] != first)
    page = client.get(f"/audits/{audit['id']}").text
    assert f'data-ask-in-chat="{other}"' in page and f'data-ask-in-chat="{first}"' not in page
    assert "The chat is asking this now" in page

    r = client.post(f"/api/audits/{audit['id']}/ask", json={"finding_id": other})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["asking"] == other and asked(got)[-1] == other
    skipped = [m["asks"] for m in got["chat"] if m.get("asks") and m["asks"].get("skipped")]
    assert [a["finding_id"] for a in skipped] == [first], "put aside, not lost"


def test_asking_the_question_already_asked_changes_nothing(client):  # noqa: F811
    audit = draft(client)
    r = client.post(f"/api/audits/{audit['id']}/ask", json={"finding_id": audit["asking"]})
    assert len(r.json()["chat"]) == len(audit["chat"])


def test_a_closed_question_cannot_be_asked(client):  # noqa: F811
    audit = draft(client)
    r = client.post(f"/api/audits/{audit['id']}/ask", json={"finding_id": "nope"})
    assert r.status_code == 409


def test_the_full_form_is_still_there_behind_answer_here(client):  # noqa: F811
    audit = draft(client)
    fid = audit["questions"][0]["findings"][0]["id"]
    page = client.get(f"/audits/{audit['id']}").text
    assert f'<div class="q-row" id="q-{fid}">' in page and f'id="q-list-{fid}"' in page
    assert "Answer here instead" in page
