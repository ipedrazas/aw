from __future__ import annotations

from pathlib import Path

from tests.scripted import extraction_for_process_doc
from wf.activities import ModelResponse, ScriptedModel
from wf.audit import Auditor, ingest
from wf.validate import make_finding, validate

DOC = (
    Path(__file__).resolve().parents[1] / "workspace" / "process-docs" / "deep-research-process.md"
)


def passage_map(text: str) -> dict[str, str]:
    passages = ingest(text)
    phrases = [
        "A client sends us a topic",
        "turns the topic into a set of questions",
        "The researcher searches the web",
        "keeps going until each question",
        "The writer takes the brief",
        "Every factual claim gets a citation",
        "makes sure the links work",
        "A reviewer who wasn't involved",
        "The reviewer decides whether more research",
        "exported to PDF",
    ]
    out = {}
    for ph in phrases:
        out[ph] = next(p.id for p in passages if ph in p.text)
    return out


def scripted_auditor(ws) -> Auditor:
    text = DOC.read_text()
    extracted = extraction_for_process_doc(passage_map(text))

    def script(req):
        assert req.tag == "audit:extract"
        assert "<data" in str(req.input) or req.input.get("passages"), "the document enters as data"
        return ModelResponse(
            output=extracted,
            decisions=[
                {
                    "decision": "Kept your nine steps as written.",
                    "reason": "Nothing had to be added for the process to run.",
                    "alternatives": [],
                }
            ],
        )

    return Auditor(ws, ScriptedModel(script))


def test_ingest_splits_into_addressable_passages():
    passages = ingest(DOC.read_text())
    body = [p for p in passages if p.kind != "heading"]
    assert len(body) >= 12
    assert all(p.id.startswith("p") and p.start_line > 0 for p in body)
    assert "Constructed example" not in " ".join(p.text for p in passages), (
        "HTML comments are not passages"
    )
    assert any(p.kind == "list" for p in passages)


def test_audit_finds_the_real_gaps_in_the_process_document(ws):
    auditor = scripted_auditor(ws)
    result = auditor.audit(DOC.read_text(), name="client-research")
    wf = result.workflow()
    assert [s.id for s in wf.spec.steps] == [
        "brief",
        "research",
        "write",
        "check_links",
        "review",
        "more_research",
        "make_edits",
        "export_pdf",
        "send",
    ]
    assert ws.exists("definitions/client-research.workflow.yaml")
    assert ws.exists("skills/client-research/review.md") and ws.exists(
        "schemas/client-research/review.json"
    )

    by = {(f.type, f.field): f for f in result.open_findings()}

    # (e) the writer fills the link status column before the links are checked
    conflict = by[("conflict", "steps.write.input")]
    assert "runs later" in conflict.detail
    assert conflict.source_text and "writer" in conflict.source_text.lower()

    # (c) "make sure the links work" is unscoped
    dnc = by[("gap", "steps.check_links.does_not_check")]
    assert dnc.question == "What does this check not tell you?"
    assert "makes sure the links work" in (dnc.source_text or "")

    # (d) "send the final report to the client" has no owner
    approval = by[("gap", "steps.send.requires_approval")]
    assert approval.question == "Who signs this off before it leaves the system?"
    assert "send the final report" in (approval.source_text or "")

    # (b) rejection is mentioned but never handled
    reject = by[("gap", "steps.review.output.continue_on.verdict.reject")]
    assert "reject" in reject.question

    # (a) the reviewer decides, with no stated criteria
    when = by[("gap", "steps.more_research.when")]
    assert when.question == "What decides which way this goes?"
    assert "The reviewer decides whether more research is needed" in (when.source_text or "")
    assert any("more_research" in o.label for o in when.options)

    # recursion without limits or a budget
    assert ("gap", "steps.more_research.limits") in by
    assert ("gap", "spec.budget") in by

    # assumptions are marked, not hidden
    assumed = [f for f in result.open_findings() if f.type == "assumption"]
    assert any(f.field == "steps.brief.model" for f in assumed)
    assert all(f.options and f.options[0].label.startswith("Yes") for f in assumed)

    # ordered by what they unblock: the branch and the budget come before the send approval
    fields = [f.field for f in result.open_findings()]
    assert fields.index("spec.budget") < fields.index("steps.send.requires_approval")

    # every finding has a question in plain language and no field names leak into it
    for f in result.open_findings():
        assert f.question.endswith("?")
        assert "steps." not in f.question


def test_answers_write_back_and_close_findings(ws):
    auditor = scripted_auditor(ws)
    result = auditor.audit(DOC.read_text(), name="client-research")
    fid = {f.field: f.id for f in result.open_findings()}

    changes = auditor.answer(
        result,
        fid["steps.check_links.does_not_check"],
        "Whether the page supports the claim\nHow reliable the source is",
    )
    assert changes[0].after == ["Whether the page supports the claim", "How reliable the source is"]
    auditor.answer(result, fid["steps.send.requires_approval"], "the account lead")
    auditor.answer(
        result,
        fid["steps.more_research.when"],
        {"step": "review", "field": "verdict", "equals": "more_research"},
    )
    auditor.answer(result, fid["steps.review.output.continue_on.verdict.reject"], {"op": "stop"})
    auditor.answer(
        result, fid["steps.review.output.continue_on.verdict.approved"], {"op": "continue"}
    )
    auditor.answer(result, fid["steps.more_research.limits"], {"max_depth": 1, "max_fanout": 3})
    auditor.answer(result, fid["spec.budget"], {"max_usd": 12, "max_minutes": 45})
    auditor.answer(result, fid["steps.write.input"], {"op": "remove_ref", "ref": "check_links"})

    wf = result.workflow()
    assert wf.step("send").requires_approval == "the account lead"
    assert wf.step("more_research").when == '${steps.review.output.verdict == "more_research"}'
    assert wf.step("review").output.continue_on == {"verdict": ["approved"]}
    assert wf.step("review_reject_stop").kind == "wait"
    assert wf.spec.budget and wf.spec.budget.max_usd == 12
    assert "check_links" not in (wf.step("write").input or {})

    open_fields = {f.field for f in result.open_findings() if f.type != "assumption"}
    assert "steps.check_links.does_not_check" not in open_fields
    assert "steps.send.requires_approval" not in open_fields
    assert "steps.review.output.continue_on.verdict.reject" not in open_fields
    assert "steps.write.input" not in open_fields
    # what is left are the fields nobody has answered yet
    assert open_fields <= {"steps.more_research.max_fanout", "steps.brief.output.schema"} | {
        f for f in open_fields if f.startswith("steps.more_research")
    }
    # the saved definition on disk validates the same way
    fresh = validate(ws.load_definition("client-research"), ws)
    assert {f.field for f in fresh.findings if f.type != "assumption"} == open_fields

    # every change is recorded with a reason, so it can be shown and undone
    assert all(c.reason for c in result.changes)
    assert len(result.changes) >= 8


def test_document_diff_shows_stated_assumed_and_open(ws):
    auditor = scripted_auditor(ws)
    result = auditor.audit(DOC.read_text(), name="client-research")
    diff = result.diff()
    statuses = {(s["id"], f["key"]): f["status"] for s in diff["steps"] for f in s["fields"]}
    assert statuses[("check_links", "checks")] == "stated"
    assert statuses[("check_links", "does_not_check")] == "open"
    assert statuses[("brief", "model")] == "assumed"
    assert diff["counts"]["stated"] > 0 and diff["counts"]["open"] > 0
    produced = {p["id"]: p["produced"] for p in diff["passages"]}
    assert any(produced.values()), "passages point at what they produced"


def test_the_two_levels_of_judgement_are_configurable(ws, monkeypatch):
    monkeypatch.setenv("WF_QUICK_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("WF_CAREFUL_MODEL", "claude-sonnet-5")
    auditor = scripted_auditor(ws)
    result = auditor.audit(DOC.read_text(), name="client-research")
    models = {s.id: s.model for s in result.workflow().spec.steps if s.model}
    assert set(models.values()) == {"claude-haiku-4-5", "claude-sonnet-5"}
    assert models["review"] == "claude-sonnet-5", "a careful step takes the careful model"

    offered = make_finding("gap", "steps.review.model").options
    assert [o.value for o in offered] == ["claude-haiku-4-5", "claude-sonnet-5"], (
        "the answers offered are the models the deployment configured"
    )
