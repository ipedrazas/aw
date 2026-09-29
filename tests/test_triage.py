"""The chat decides which questions to ask: it answers what it can, leaves what can
wait, and asks the rest in the order and the words it chose."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.helpers import make_db
from tests.scripted import extraction_for_process_doc, schema_filling_script, skill_answer
from tests.test_audit import DOC, passage_map
from wf.activities import ModelResponse, ScriptedModel
from wf.api.app import AppState, create_app
from wf.audit.asking import DONE_BUT_LATER, ask_first, move_on, next_question
from wf.audit.triage import can_settle, triage
from wf.validate import Finding, Option

NAME_ENOUGH = "Is its name enough to go on?"
LEAD = "Before anything else: should the report be written before or after its links are checked?"


def by_question(req, words: str) -> str:
    return next(q["id"] for q in req.input["open_questions"] if words in q["question"])


def sorting(req) -> dict[str, Any]:
    """What a chat that read the sample draft would decide."""
    return {
        "settle": [
            {
                "finding_id": by_question(req, NAME_ENOUGH),
                "option_index": 0,
                "reason": "The system already knows how to make edits a reviewer asked for.",
            },
            # it may not decide what a run spends: this one is asked instead
            {
                "finding_id": by_question(req, "allowed to spend"),
                "option_index": 0,
                "reason": "Five dollars is plenty.",
            },
        ],
        "later": [
            {
                "finding_id": by_question(req, "What can “Check the links” miss?"),
                "reason": "It does not change what a run does.",
            }
        ],
        "ask": [
            {"finding_id": by_question(req, "“Write the report” uses what"), "text": LEAD},
            {"finding_id": "not-a-question", "text": "Ignored."},
        ],
    }


@pytest.fixture
def client(ws, tmp_path: Path):
    extracted = extraction_for_process_doc(passage_map(DOC.read_text()))
    fill = schema_filling_script()
    seen: list[Any] = []

    def script(req):
        if req.tag.startswith("audit:skill:"):
            return skill_answer(req)
        if req.tag == "audit:extract":
            return ModelResponse(output=extracted, decisions=[])
        if req.tag == "audit:triage":
            seen.append(req)
            return ModelResponse(output=sorting(req))
        return fill(req)

    state = AppState(ws, make_db(), ScriptedModel(script), tmp_path / "artifacts")
    c = TestClient(create_app(state))
    c.triage_requests = seen  # type: ignore[attr-defined]
    return c


def draft(client) -> dict[str, Any]:
    return client.post("/api/audits", json={"document": DOC.read_text(), "name": "p"}).json()


def finding_by(audit, words: str) -> dict[str, Any]:
    everything = [f for g in audit["questions"] for f in g["findings"]] + audit["answered"]
    return next(f for f in everything if words in f["question"])


def test_the_chat_is_shown_what_it_may_settle(client):
    draft(client)
    (req,) = client.triage_requests
    may = {q["question"]: q["can_settle"] for q in req.input["open_questions"]}
    assert may[next(q for q in may if NAME_ENOUGH in q)] is True
    assert not any(v for q, v in may.items() if "spend" in q or "approve" in q), (
        "never money or approvals"
    )
    assert req.input["what_the_system_can_do"]["tools"], "it knows what the system does"
    assert req.input["document"], "and what they wrote"


def test_it_answers_what_it_can_and_says_so(client):
    audit = draft(client)
    settled = audit["chat"][0]["settled"]
    assert [s["question"] for s in settled] == [finding_by(audit, NAME_ENOUGH)["question"]]
    assert settled[0]["said"] == "Yes, the name says it"
    assert "system already knows" in settled[0]["reason"]
    assert "One question I could answer" in audit["chat"][0]["text"]
    assert finding_by(audit, NAME_ENOUGH)["status"] == "answered"
    assert finding_by(audit, "allowed to spend")["status"] == "open", "it may not decide that"

    page = client.get(f"/audits/{audit['id']}").text
    assert "Answered for you:" in page and "The chat answered: Yes, the name says it" in page


def test_it_asks_first_what_it_chose_in_its_own_words(client):
    audit = draft(client)
    first = next(m for m in audit["chat"] if m.get("asks"))
    assert first["asks"]["finding_id"] == finding_by(audit, "“Write the report” uses what")["id"]
    assert first["text"] == LEAD
    assert LEAD in client.get(f"/audits/{audit['id']}").text


def test_what_can_wait_is_left_until_the_rest_is_answered(client):
    audit = draft(client)
    later = finding_by(audit, "What can “Check the links” miss?")
    assert "One more can wait until you have tried it." in audit["chat"][0]["text"]
    for _ in range(40):
        if audit["asking"] is None:
            break
        assert audit["asking"] != later["id"]
        f = next(x for x in [*audit["asked"].values()] if x["id"] == audit["asking"])
        audit = client.post(f"/api/audits/{audit['id']}/skip", json={"finding_id": f["id"]}).json()
    assert later["id"] not in {m["asks"]["finding_id"] for m in audit["chat"] if m.get("asks")}


def test_the_chat_can_choose_what_to_ask_next(ws, tmp_path):
    extracted = extraction_for_process_doc(passage_map(DOC.read_text()))
    fill = schema_filling_script()

    def script(req):
        if req.tag.startswith("audit:skill:"):
            return skill_answer(req)
        if req.tag == "audit:extract":
            return ModelResponse(output=extracted, decisions=[])
        if req.tag == "audit:chat":
            about = req.input["about"]["id"]
            approve = by_question(req, "approve")
            return ModelResponse(
                output={
                    "reply": "Noted.",
                    "edits": [],
                    "answers": [{"finding_id": about, "option_index": 0, "text": None}],
                    "dismiss": [],
                    "point_to_finding": None,
                    "ask_next": {"finding_id": approve, "text": "You mentioned the client."},
                }
            )
        return fill(req)

    c = TestClient(create_app(AppState(ws, make_db(), ScriptedModel(script), tmp_path / "a")))
    audit = draft(c)
    after = c.post(
        f"/api/audits/{audit['id']}/chat",
        json={"message": "the first one, and the client must sign off", "about": audit["asking"]},
    ).json()
    assert after["chat"][-1]["text"] == "You mentioned the client."
    assert "approve" in after["asked"][after["asking"]]["question"]


# -- the rules, without the app ---------------------------------------------------


def finding(fid: str, type_: str = "assumption", field: str = "") -> Finding:
    return Finding(
        id=fid,
        type=type_,
        step_id=fid,
        field=field or f"steps.{fid}.output.schema",
        question=f"What does {fid} hand on?",
        answer_kind="choice",
        options=[
            Option(value={"keep": True}, label="Yes"),
            Option(value={"keep": False}, label="No, I will explain"),
        ],
    )


@pytest.mark.parametrize(
    ("f", "may"),
    [
        (finding("a"), True),
        (finding("b", type_="gap"), False),
        (finding("c", field="steps.a.model"), False),
        (finding("d", field="steps.send.requires_approval"), False),
        (finding("e", field="spec.budget"), False),
        (finding("f", field="steps.more.limits"), False),
    ],
)
def test_what_the_chat_may_settle(f, may):
    assert can_settle(f) is may


class Result:
    name = title = "demo"
    definition: dict[str, Any] = {"spec": {"steps": []}}

    def __init__(self, findings):
        self.findings = findings

    def open_findings(self):
        return [f for f in self.findings if f.status == "open"]


def test_the_model_is_held_to_what_it_may_do():
    fs = [finding("a"), finding("b"), finding("c", type_="gap"), finding("d")]
    out = {
        "settle": [
            {"finding_id": "a", "option_index": 1, "reason": "No."},  # "I will explain" is theirs
            {"finding_id": "b", "option_index": 0, "reason": "Yes."},
            {"finding_id": "c", "option_index": 0, "reason": "A gap."},  # not an assumption
            {"finding_id": "zz", "option_index": 0, "reason": "Not a question."},
        ],
        "later": [{"finding_id": "b", "reason": "Already settled."}],
        "ask": [{"finding_id": "d", "text": "D, in my words."}],
    }
    model = ScriptedModel(lambda req: ModelResponse(output=out))
    t = triage(model, Result(fs))
    assert [(s["finding_id"], s["value"]) for s in t.settle] == [("b", {"keep": True})]
    assert t.order == ["d"] and t.lead == {"d": "D, in my words."}
    assert t.later == {}, "one list per question"


def test_the_queue_follows_the_plan():
    fs = [
        finding("a", field="steps.a.skill"),
        finding("b", field="steps.b.output.schema"),
        finding("c", field="steps.c.title"),
    ]
    plan = {"order": ["c", "a"], "lead": {"c": "C first."}, "later": {"b": "It can wait."}}
    chat = [{"role": "assistant", "text": "Hello.", "plan": plan}]

    chat = move_on(fs, chat)
    assert chat[-1]["asks"]["finding_id"] == "c" and chat[-1]["text"] == "C first."
    fs[2].status = "answered"
    chat = move_on(fs, chat)
    assert chat[-1]["asks"]["finding_id"] == "a" and chat[-1]["text"] == ""
    fs[0].status = "answered"
    chat = move_on(fs, chat)
    assert chat[-1]["text"] == DONE_BUT_LATER, "b waits"

    moved = ask_first(chat, "b", "B now.")
    assert move_on(fs, moved)[-1]["asks"]["finding_id"] == "b"
    assert next_question(fs, moved)[0].id == "b"
    assert moved[0]["plan"]["lead"]["b"] == "B now." and "b" not in moved[0]["plan"]["later"]


def test_a_question_asked_of_several_steps_is_put_the_way_the_chat_put_any_of_them():
    fs = [finding("a"), finding("b")]  # the same question of two steps: asked once
    chat = [{"role": "assistant", "text": "Hello.", "plan": {"order": ["b"], "lead": {"b": "B."}}}]
    asked = move_on(fs, chat)[-1]
    assert asked["asks"] == {"finding_id": "a", "similar": ["b"]}
    assert asked["text"] == "B."
