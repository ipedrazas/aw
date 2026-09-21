from __future__ import annotations

from pathlib import Path

from tests.helpers import make_db
from tests.scripted import deep_research_script, schema_filling_script
from tests.test_audit import DOC, scripted_auditor
from wf.activities import (
    Activities,
    ActivityPolicy,
    FixtureLinkCheck,
    FixtureSearch,
    HeuristicGuesser,
    PdfRenderer,
    RecordingSend,
    ScriptedModel,
)
from wf.dryrun import DryRunner
from wf.interpret import RunConfig


def runner(ws, tmp_path: Path, script) -> DryRunner:
    acts = Activities(
        model=ScriptedModel(script),
        search=FixtureSearch(ws),
        links=FixtureLinkCheck(ws),
        render=PdfRenderer(),
        send=RecordingSend(),
        guesser=HeuristicGuesser(),
        policy=ActivityPolicy(retries=0),
    )
    return DryRunner(ws, acts, make_db(), RunConfig(artifacts_dir=tmp_path / "artifacts"))


def test_dry_run_against_a_past_case_reports_the_first_divergence(sample_ws, tmp_path):
    r = runner(sample_ws, tmp_path, deep_research_script("go_deeper"))
    report = r.run_case("deep-research", "durable-execution")
    assert report.status == "done", report.error
    assert report.case_name == "durable-execution"
    assert {e.step for e in report.expectations} == {"review", "check_links", "go_deeper"}
    verdict = next(e for e in report.expectations if e.step == "review")
    assert verdict.expected == "accept" and verdict.actual == "go_deeper" and not verdict.matched
    # check_links comes first in the definition and also differs, so it is the first divergence
    assert report.first_divergence is not None
    order = [s["step_id"] for s in report.steps]
    assert all(
        order.index(report.first_divergence.step) <= order.index(e.step)
        for e in report.expectations
        if not e.matched
    )
    assert report.artifacts and report.artifacts[0]["simulated"]
    text = report.render_text()
    assert (
        text.index("Where it had to guess") < text.index("Open questions") < text.index("Artefacts")
    )


def test_two_runs_diff_at_the_level_of_decisions(sample_ws, tmp_path):
    topic = {"topic": "Durable execution platforms for AI agents: who leads and why"}
    r = runner(sample_ws, tmp_path, deep_research_script("accept"))
    a = r.run("deep-research", topic)
    a2 = r.run("deep-research", topic)
    same = r.diff(a.run_id, a2.run_id)
    assert same.first_divergence is None and same.same_control_flow

    # same definition, same fixtures; only the reviewer's judgement differs
    r.activities.model = ScriptedModel(deep_research_script("go_deeper"))
    b = r.run("deep-research", topic)
    diff = r.diff(a.run_id, b.run_id)
    assert diff.first_divergence == "review"
    review = next(s for s in diff.steps if s.step_id == "review")
    assert review.summary_a["verdict"] == "accept" and review.summary_b["verdict"] == "go_deeper"
    assert not diff.same_control_flow
    go = next(s for s in diff.steps if s.step_id == "go_deeper")
    assert go.status_a == "skipped" and go.status_b == "done"
    for sid in ("plan", "research", "write", "check_links"):
        s = next(x for x in diff.steps if x.step_id == sid)
        assert s.status_a == s.status_b == "done"


def test_an_audited_draft_dry_runs_and_guesses_at_the_unstated_branch(ws, tmp_path):
    result = scripted_auditor(ws).audit(DOC.read_text(), name="client-research")
    r = runner(ws, tmp_path, schema_filling_script({"review": {"verdict": "reject"}}))
    report = r.run_workflow(
        result.workflow(),
        {"topic": "Durable execution platforms for AI agents"},
        findings=result.findings,
    )
    assert report.status == "done", report.error
    fields = {g["field"] for g in report.guesses}
    assert "steps.review.output.continue_on.verdict.reject" in fields, (
        "the unstated rejection branch is a guess point"
    )
    assert "steps.check_links.does_not_check" in fields
    assert "steps.send.requires_approval" in fields
    assert "steps.more_research.when" in fields
    assert all(g["finding_id"] and g["question"] for g in report.guesses), (
        "every guess names its finding"
    )
    # nothing was sent: the send step was recorded, not done
    send = next(s for s in report.steps if s["step_id"] == "send")
    assert send["output"] == {"recorded": True, "delivered": False}
    assert any(
        "Nothing was sent" in d["text"] or "Did not run" in d["text"] for d in send["decisions"]
    )
    assert report.artifacts and report.artifacts[0]["name"].startswith("SIMULATED-")
