from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfReader

from tests.helpers import make_db, read_yaml, step, write_yaml
from tests.scripted import deep_research_script
from wf.activities import (
    Activities,
    ActivityPolicy,
    FixtureLinkCheck,
    FixtureSearch,
    HeuristicGuesser,
    PdfRenderer,
    RecordingModel,
    RecordingSend,
    ReplayModel,
    ScriptedModel,
)
from wf.interpret import Interpreter, OpenFindings, RunConfig
from wf.store import Database, Decision, Run, StepRun
from wf.store.ledger import Ledger
from wf.validate import validate

DEF = "definitions/deep-research.workflow.yaml"


def make(ws, tmp_path: Path, model, guesser=None) -> tuple[Interpreter, Database]:
    db = make_db()
    acts = Activities(
        model=model,
        search=FixtureSearch(ws),
        links=FixtureLinkCheck(ws),
        render=PdfRenderer(),
        send=RecordingSend(),
        guesser=guesser or HeuristicGuesser(),
        policy=ActivityPolicy(retries=0),
    )
    interp = Interpreter(ws, acts, Ledger(db), RunConfig(artifacts_dir=tmp_path / "artifacts"))
    return interp, db


TOPIC = {"topic": "Durable execution platforms for AI agents: who leads and why"}


def test_deep_research_runs_end_to_end_and_records_everything(sample_ws, tmp_path):
    interp, db = make(sample_ws, tmp_path, ScriptedModel(deep_research_script("accept")))
    wf = sample_ws.load_definition("deep-research")
    result = interp.run(wf, TOPIC, "dry")
    assert result.status == "done", [d for d in result.decisions if d["kind"] == "control"]
    assert [t.as_tuple()[:2] for t in result.trace] == [
        ("plan", "run"),
        ("research", "run"),
        ("write", "run"),
        ("check_links", "run"),
        ("review", "run"),
        ("revise", "skip"),
        ("go_deeper", "skip"),
        ("assemble", "run"),
    ]
    with db.session() as s:
        run = s.query(Run).one()
        steps = s.query(StepRun).order_by(StepRun.seq).all()
        decisions = s.query(Decision).all()
    assert run.status == "done" and run.mode == "dry"
    assert [st.step_id for st in steps] == [
        "plan",
        "research",
        "write",
        "check_links",
        "review",
        "revise",
        "go_deeper",
        "assemble",
    ]
    agent_steps = [st for st in steps if st.kind == "agent" and st.status == "done"]
    assert all(st.model and st.instruction_ref and st.instruction_sha256 for st in agent_steps)
    assert any(d.kind == "decision" and d.step_id == "plan" for d in decisions)
    assert any(d.kind == "ignored" for d in decisions), (
        "the injected page must be recorded as ignored, not dropped"
    )
    assert run.spent_usd > 0
    assert result.outputs and result.outputs["verdict"] == "accept"


def test_branches_follow_the_verdict(ws, tmp_path):
    wf = ws.load_definition("deep-research")
    interp, _ = make(ws, tmp_path, ScriptedModel(deep_research_script("revise")))
    r = interp.run(wf, TOPIC, "dry")
    assert ("revise", "run") in [t.as_tuple()[:2] for t in r.trace]
    assert ("go_deeper", "skip") in [t.as_tuple()[:2] for t in r.trace]

    # the reviewer may propose at most 3 follow-ups (its output schema says so);
    # the step's own limit is what caps the fan-out
    data = read_yaml(ws, DEF)
    step(data, "go_deeper")["limits"]["max_fanout"] = 2
    write_yaml(ws, DEF, data)
    wf = ws.load_definition("deep-research")
    interp, db = make(ws, tmp_path, ScriptedModel(deep_research_script("go_deeper", followups=3)))
    r = interp.run(wf, TOPIC, "dry")
    events = [t.as_tuple()[:2] for t in r.trace]
    assert ("go_deeper", "fanout") in events
    fan = next(t for t in r.trace if t.event == "fanout")
    assert fan.detail == 2, "fan-out is capped by max_fanout"
    assert any("Running 2 of 3" in d["text"] for d in r.decisions)
    assert sum(1 for t in r.trace if t.event == "stub") == 2, (
        "dry runs show follow-ups, they do not start them"
    )


def test_the_output_schema_is_enforced_not_trusted(sample_ws, tmp_path):
    wf = sample_ws.load_definition("deep-research")
    interp, _ = make(
        sample_ws, tmp_path, ScriptedModel(deep_research_script("go_deeper", followups=5))
    )
    r = interp.run(wf, TOPIC, "dry")
    assert r.status == "failed"
    assert any(
        "did not match its declared shape" in d["reason"]
        for d in r.decisions
        if d["step_id"] == "review"
    )


def test_interpreter_is_deterministic_over_recorded_outputs(sample_ws, tmp_path):
    """Same definition, inputs and recorded step outputs -> identical control flow."""
    wf = sample_ws.load_definition("deep-research")
    recorder = RecordingModel(ScriptedModel(deep_research_script("go_deeper")))
    first, _ = make(sample_ws, tmp_path / "a", recorder)
    r1 = first.run(wf, TOPIC, "dry")
    second, _ = make(sample_ws, tmp_path / "b", ReplayModel(recorder.records))
    r2 = second.run(wf, TOPIC, "dry")
    assert r1.status == r2.status == "done"
    assert r1.control_trace == r2.control_trace
    flow1 = [
        (d["step_id"], d["kind"], d["text"])
        for d in r1.decisions
        if d["kind"] in ("control", "guess")
    ]
    flow2 = [
        (d["step_id"], d["kind"], d["text"])
        for d in r2.decisions
        if d["kind"] in ("control", "guess")
    ]
    assert flow1 == flow2


def test_dry_run_guesses_instead_of_failing_and_links_the_finding(ws, tmp_path):
    data = read_yaml(ws, DEF)
    del step(data, "assemble")["requires_approval"]
    del step(data, "go_deeper")["when"]
    write_yaml(ws, DEF, data)
    wf = ws.load_definition("deep-research")
    findings = validate(wf, ws).findings
    assert {f.field for f in findings} == {
        "steps.assemble.requires_approval",
        "steps.review.output.continue_on.verdict.go_deeper",
    }

    interp, db = make(ws, tmp_path, ScriptedModel(deep_research_script("go_deeper")))
    r = interp.run(wf, TOPIC, "dry", findings=findings)
    assert r.status == "done", r.error
    guessed = {g["field"]: g for g in r.guesses}
    assert set(guessed) == {
        "steps.assemble.requires_approval",
        "steps.review.output.continue_on.verdict.go_deeper",
    }
    by_id = {f.id for f in findings}
    assert all(g["finding_id"] in by_id for g in r.guesses)
    with db.session() as s:
        kinds = {d.kind for d in s.query(Decision).all()}
    assert "guess" in kinds


def test_live_mode_refuses_to_run_with_open_findings(ws, tmp_path):
    data = read_yaml(ws, DEF)
    del step(data, "check_links")["does_not_check"]
    write_yaml(ws, DEF, data)
    wf = ws.load_definition("deep-research")
    interp, _ = make(ws, tmp_path, ScriptedModel(deep_research_script()))
    with pytest.raises(OpenFindings):
        interp.run(wf, TOPIC, "live")


def test_artifact_is_marked_simulated_in_name_body_and_metadata(sample_ws, tmp_path):
    wf = sample_ws.load_definition("deep-research")
    interp, db = make(sample_ws, tmp_path, ScriptedModel(deep_research_script("accept")))
    r = interp.run(wf, TOPIC, "dry")
    assert [a["name"] for a in r.artifacts] == ["SIMULATED-report.pdf", "SIMULATED-report.md"]
    art, md = r.artifacts
    assert art["simulated"] is True and md["simulated"] is True
    assert Path(md["path"]).read_text().startswith("> **SIMULATED")
    reader = PdfReader(art["path"])
    assert "simulated" in (reader.metadata.title or "").lower()
    assert "simulated" in (reader.metadata.subject or "").lower()
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert "SIMULATED" in text
    with db.session() as s:
        from wf.store import Artifact

        recs = s.query(Artifact).all()
        steps = {s.get(StepRun, rec.step_run_id).step_id for rec in recs}
    assert sorted(r.media_type.split(";")[0] for r in recs) == ["application/pdf", "text/markdown"]
    assert all(rec.simulated and rec.sha256 for rec in recs) and steps == {"assemble"}


def test_budget_pauses_the_run(sample_ws, tmp_path):
    from wf.activities import ModelResponse, Usage

    def pricey(req):
        resp = deep_research_script("accept")(req)
        return ModelResponse(
            output=resp.output,
            decisions=resp.decisions,
            usage=Usage(1, 1, 7.0),
            tool_calls=resp.tool_calls,
        )

    wf = sample_ws.load_definition("deep-research")
    interp, _ = make(sample_ws, tmp_path, ScriptedModel(pricey))
    r = interp.run(wf, TOPIC, "dry")
    assert r.status == "paused_budget"
    assert any(t.event == "paused" for t in r.trace)
    assert any("spent $14.00 of a $12.00 limit" in d["text"] for d in r.decisions)
