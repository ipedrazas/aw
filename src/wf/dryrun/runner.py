"""Dry runs: a definition executed against a past case, reporting guess points first,
findings second, and the simulated artefact last."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from wf.activities import (
    Activities,
    ModelActivity,
    default_activities,
    default_model,
    record_sessions,
)
from wf.interpret import Interpreter, RunConfig, RunResult
from wf.schema import Mode, Workflow, Workspace
from wf.store import Artifact, Database, Decision, Expectation, Gate, Run, StepRun
from wf.store.ledger import Ledger
from wf.validate import Finding, validate

from .diff import RunDiff, diff_runs
from .divergence import ExpectationResult, evaluate_expectations, first_divergence


class DryRunReport(BaseModel):
    run_id: str
    workflow: str
    title: str
    mode: str
    status: str
    error: str | None = None
    spent_usd: float = 0.0
    guesses: list[dict[str, Any]] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    expectations: list[ExpectationResult] = Field(default_factory=list)
    first_divergence: ExpectationResult | None = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    case_name: str | None = None

    def render_text(self) -> str:
        lines = [f"Run {self.run_id[:8]}: {self.title} [{self.mode}] {self.status}", ""]
        if self.error:
            lines += [f"Error: {self.error}", ""]
        for s in self.steps:
            if s.get("status") == "failed":
                why = s.get("error") or next(
                    (d["reason"] for d in s.get("decisions", []) if d["kind"] == "control"), ""
                )
                lines += [f"Step \u201c{s.get('title')}\u201d failed: {why}", ""]
        lines.append(f"## Where it had to guess ({len(self.guesses)})")
        for g in self.guesses:
            lines.append(f"- [{g['step_id']}] {g['text']} — {g['reason']}")
            lines.append(f"    question: {g['question']}")
        lines.append("")
        if self.expectations:
            lines.append("## Against what you expected")
            for e in self.expectations:
                mark = "same" if e.matched else "DIFFERENT"
                lines.append(
                    f"- [{e.step}] {e.field}: expected {e.expected!r}, got {e.actual!r} ({mark})"
                )
            if self.first_divergence:
                lines.append(f"First step to go a different way: {self.first_divergence.step}")
            lines.append("")
        lines.append(f"## Open questions ({len([f for f in self.findings if f.status == 'open'])})")
        for f in self.findings:
            if f.status == "open":
                lines.append(f"- [{f.type}] {f.step_id or 'workflow'}: {f.question}")
        lines.append("")
        lines.append("## Artefacts")
        for a in self.artifacts:
            lines.append(f"- {a['name']} (simulated: {a['simulated']}) {a['path']}")
        lines.append("")
        lines.append(f"Spent ${self.spent_usd:.2f}.")
        return "\n".join(lines)


@dataclass
class DryRunner:
    ws: Workspace
    activities: Activities
    db: Database
    config: RunConfig = field(default_factory=RunConfig)

    @classmethod
    def from_env(
        cls,
        ws: Workspace,
        model: ModelActivity | None = None,
        db: Database | None = None,
        *,
        model_guesses: bool = True,
    ) -> DryRunner:
        db = db or Database()
        # Wrapped once, here: the guesser and the auditor are given the same model, so
        # every exchange any of them has lands in the session that is open.
        recorded = record_sessions(model or default_model(), db)
        acts = default_activities(ws, recorded, model_guesses=model_guesses)
        artifacts = Path(os.environ.get("WF_ARTIFACTS_DIR", "var/artifacts"))
        return cls(ws, acts, db, RunConfig(artifacts_dir=artifacts))

    # -- running ------------------------------------------------------------

    def run_case(self, name: str, case: str, mode: Mode = "dry") -> DryRunReport:
        data = self.ws.load_yaml(f"cases/{case}.case.yaml")
        if not data:
            raise FileNotFoundError(f"no case named {case!r} in {self.ws.root / 'cases'}")
        return self.run(
            name,
            dict(data.get("inputs", {})),
            mode=mode,
            expectation=list(data.get("expectation", [])),
            case_name=case,
            title=data.get("title"),
        )

    def run(
        self,
        name: str,
        inputs: dict[str, Any],
        *,
        mode: Mode = "dry",
        expectation: list[dict[str, Any]] | None = None,
        case_name: str | None = None,
        title: str | None = None,
        findings: list[Finding] | None = None,
    ) -> DryRunReport:
        wf = self.ws.load_definition(name)
        return self.run_workflow(
            wf,
            inputs,
            mode=mode,
            expectation=expectation,
            case_name=case_name,
            title=title,
            findings=findings,
        )

    def run_workflow(
        self,
        wf: Workflow,
        inputs: dict[str, Any],
        *,
        mode: Mode = "dry",
        expectation: list[dict[str, Any]] | None = None,
        case_name: str | None = None,
        title: str | None = None,
        findings: list[Finding] | None = None,
    ) -> DryRunReport:
        findings = findings if findings is not None else validate(wf, self.ws).findings
        ledger = Ledger(self.db)
        try:
            interp = Interpreter(self.ws, self.activities, ledger, self.config)
            result = interp.run(
                wf,
                inputs,
                mode,
                findings=findings,
                expectation=expectation,
                case_name=case_name,
                title=title,
            )
            self._record_expectations(ledger, result, expectation or [])
        finally:
            ledger.close()
        return self.report(result.run_id, findings=findings, result=result)

    def retry(self, run_id: str, *, skip: bool = False) -> DryRunReport:
        """Pick up a run that broke, at the step that broke, on the definition as it is
        now; or, with ``skip``, carry it on without that step."""
        ledger = Ledger(self.db)
        try:
            run = ledger.session.get(Run, run_id)
            if run is None:
                raise KeyError(run_id)
            wf = self.ws.load_definition(run.workflow_name)
            interp = Interpreter(self.ws, self.activities, ledger, self.config)
            result = interp.skip(run, wf) if skip else interp.retry(run, wf)
            self.rescore_expectations(ledger, result)
        finally:
            ledger.close()
        return self.report(run_id, result=result)

    def carry_on(self, run_id: str, *, ok: bool, note: str = "") -> DryRunReport:
        """Answer the gate a real run stopped at: carry on, or stop the run there."""
        ledger = Ledger(self.db)
        try:
            run = ledger.session.get(Run, run_id)
            if run is None:
                raise KeyError(run_id)
            wf = self.ws.load_definition(run.workflow_name)
            interp = Interpreter(self.ws, self.activities, ledger, self.config)
            result = interp.carry_on(run, wf, ok=ok, note=note)
        finally:
            ledger.close()
        return self.report(run_id, result=result)

    def rescore_expectations(self, ledger: Ledger, result: RunResult) -> None:
        """Score a run's expectations again, against where it ended this time."""
        rows = ledger.session.query(Expectation).filter_by(run_id=result.run_id).all()
        for row in rows:
            (res,) = evaluate_expectations(
                result.state,
                [
                    {
                        "step": row.step_id,
                        "field": row.field,
                        "equals": (row.equals or {}).get("value"),
                    }
                ],
            )
            ledger.set_expectation_result(row, res.matched, res.actual)

    def _record_expectations(
        self, ledger: Ledger, result: RunResult, expectation: list[dict[str, Any]]
    ) -> None:
        if not expectation:
            return
        results = evaluate_expectations(result.state, expectation)
        rows = ledger.session.query(Expectation).filter_by(run_id=result.run_id).all()
        for row, res in zip(rows, results, strict=False):
            ledger.set_expectation_result(row, res.matched, res.actual)

    # -- reading back ----------------------------------------------------------------

    def snapshot(self, run_id: str) -> dict[str, Any]:
        with self.db.session() as s:
            run = s.get(Run, run_id)
            if run is None:
                raise KeyError(run_id)
            steps = s.query(StepRun).filter_by(run_id=run_id).order_by(StepRun.seq).all()
            decisions = s.query(Decision).filter_by(run_id=run_id).order_by(Decision.seq).all()
            artifacts = s.query(Artifact).filter_by(run_id=run_id).all()
            expectations = s.query(Expectation).filter_by(run_id=run_id).all()
            gate = (
                s.query(Gate)
                .filter_by(run_id=run_id, status="waiting")
                .order_by(Gate.created_at.desc())
                .first()
            )
            by_step: dict[str, list[dict[str, Any]]] = {}
            for d in decisions:
                by_step.setdefault(d.step_id, []).append(
                    {
                        "kind": d.kind,
                        "text": d.text,
                        "reason": d.reason,
                        "alternatives": d.alternatives,
                        "finding_id": d.finding_id,
                        "field": d.field,
                        "value": (d.value or {}).get("value"),
                    }
                )
            # One row per step, in the order the steps first ran: its latest attempt. A
            # step picked up again after it broke keeps the attempt that broke in its
            # decisions, and says how many attempts there were.
            # the follow-up runs each step started, so the step can say how each one went
            step_of = {st.id: st.step_id for st in steps}
            follow_ups: dict[str, list[dict[str, Any]]] = {}
            for child in (
                s.query(Run).filter_by(parent_run_id=run_id).order_by(Run.started_at).all()
            ):
                follow_ups.setdefault(step_of.get(child.parent_step_run_id or "", ""), []).append(
                    {
                        "id": child.id,
                        "title": child.title,
                        "status": child.status,
                        "error": child.error,
                    }
                )
            latest = _latest_attempts(steps)
            attempts: dict[str, int] = {}
            for st in steps:
                if st.fanout_index is None:
                    attempts[st.step_id] = attempts.get(st.step_id, 0) + 1
            step_rows = []
            for st in latest:
                step_rows.append(
                    {
                        "step_id": st.step_id,
                        "title": st.title,
                        "kind": st.kind,
                        "status": st.status,
                        "output": st.output,
                        "input": st.input,
                        "cost_usd": st.cost_usd,
                        "duration_s": st.duration_s,
                        "model": st.model,
                        "instruction_ref": st.instruction_ref,
                        "instruction_commit": st.instruction_commit,
                        "instruction_sha256": st.instruction_sha256,
                        "tool_calls": st.tool_calls,
                        "error": st.error,
                        "attempts": attempts.get(st.step_id, 1),
                        "follow_ups": follow_ups.get(st.step_id, []),
                        "decisions": by_step.get(st.step_id, []),
                    }
                )
            return {
                "id": run.id,
                "workflow": run.workflow_name,
                "title": run.title,
                "mode": run.mode,
                "status": run.status,
                "error": run.error,
                "cost": run.spent_usd,
                "spent_minutes": run.spent_minutes,
                "budget_usd": run.budget_usd,
                "depth": run.depth,
                "inputs": run.inputs,
                "outputs": run.outputs,
                "case_name": run.case_name,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                "steps": step_rows,
                # the step a real run stopped after, for someone's OK
                "gate": _gate_view(gate, steps, by_step)
                if gate and run.status == "waiting"
                else None,
                "artifacts": [
                    {
                        "id": a.id,
                        "name": a.name,
                        "path": a.path,
                        "simulated": a.simulated,
                        "media_type": a.media_type,
                        "sha256": a.sha256,
                    }
                    for a in artifacts
                ],
                "expectations": [
                    {
                        "step": e.step_id,
                        "field": e.field,
                        "expected": (e.equals or {}).get("value"),
                        "actual": (e.actual or {}).get("value"),
                        "matched": e.matched,
                        "why": e.why,
                    }
                    for e in expectations
                ],
            }

    def report(
        self,
        run_id: str,
        findings: list[Finding] | None = None,
        result: RunResult | None = None,
        snap: dict[str, Any] | None = None,
    ) -> DryRunReport:
        # A caller that has already read the run takes its snapshot rather than
        # reading again: status and guesses then come from the same moment, and a
        # report cannot be one step behind the status shown beside it.
        snap = snap if snap is not None else self.snapshot(run_id)
        guesses = [
            {
                "step_id": s["step_id"],
                "step_title": s["title"],
                **{
                    k: d[k]
                    for k in ("text", "reason", "alternatives", "finding_id", "field", "value")
                },
                "question": _question_for(findings, d.get("finding_id")),
            }
            for s in snap["steps"]
            for d in s["decisions"]
            if d["kind"] == "guess"
        ]
        if findings is None:
            findings = []
            with self.db.session() as s:
                from wf.store import FindingRecord

                for rec in s.query(FindingRecord).filter_by(run_id=run_id).all():
                    findings.append(Finding.model_validate(rec.payload))
        exps = [
            ExpectationResult(
                step=e["step"],
                field=e["field"],
                expected=e["expected"],
                actual=e["actual"],
                matched=bool(e["matched"]),
                why=e["why"] or "",
            )
            for e in snap["expectations"]
        ]
        order = [s["step_id"] for s in snap["steps"]]
        return DryRunReport(
            run_id=run_id,
            workflow=snap["workflow"],
            title=snap["title"],
            mode=snap["mode"],
            status=snap["status"],
            error=snap["error"],
            spent_usd=snap["cost"],
            guesses=guesses,
            findings=findings,
            expectations=exps,
            first_divergence=first_divergence(exps, order),
            artifacts=snap["artifacts"],
            steps=snap["steps"],
            case_name=snap["case_name"],
        )

    def diff(self, run_a: str, run_b: str) -> RunDiff:
        return diff_runs(self.snapshot(run_a), self.snapshot(run_b))

    def list_runs(self, workflow: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """The latest runs, newest first, each followed by the follow-ups it started.

        A follow-up sits under its parent however long after it started, with ``level``
        saying how far down, and every run's ``total_cost`` is its own cost plus its
        follow-ups', which is what starting it actually cost. ``cost`` stays its own, so
        adding up ``cost`` over the list counts nothing twice.
        """
        with self.db.session() as s:
            q = s.query(Run).order_by(Run.started_at.desc())
            if workflow:
                q = q.filter_by(workflow_name=workflow)
            runs = q.limit(limit).all()
            # follow-ups the limit or the filter left out still belong under their parent
            have = {r.id for r in runs}
            frontier = list(have)
            while frontier:
                more = s.query(Run).filter(Run.parent_run_id.in_(frontier)).all()
                more = [r for r in more if r.id not in have]
                have.update(r.id for r in more)
                runs.extend(more)
                frontier = [r.id for r in more]
            runs = _as_tree(runs)
            out = []
            for r, level in runs:
                steps = s.query(StepRun).filter_by(run_id=r.id).order_by(StepRun.seq).all()
                statuses = [
                    {"step_id": st.step_id, "status": st.status} for st in _latest_attempts(steps)
                ]
                out.append(
                    {
                        "id": r.id,
                        "workflow": r.workflow_name,
                        "title": r.title,
                        "mode": r.mode,
                        "status": r.status,
                        "cost": r.spent_usd,
                        "depth": r.depth,
                        "parent_run_id": r.parent_run_id,
                        "level": level,
                        "case_name": r.case_name,
                        "started_at": r.started_at.isoformat() if r.started_at else None,
                        "steps": statuses,
                    }
                )
            _add_totals(out)
            return out


def _as_tree(runs: list[Run]) -> list[tuple[Run, int]]:
    """Runs whose parent is not in the list first, newest first, each followed by its
    follow-ups in the order they started, depth first."""
    ids = {r.id for r in runs}
    children: dict[str, list[Run]] = {}
    roots: list[Run] = []
    for r in runs:
        if r.parent_run_id and r.parent_run_id in ids:
            children.setdefault(r.parent_run_id, []).append(r)
        else:
            roots.append(r)
    never = datetime.min

    def started(r: Run) -> datetime:
        return (r.started_at or never).replace(tzinfo=None)

    out: list[tuple[Run, int]] = []

    def walk(r: Run, level: int) -> None:
        out.append((r, level))
        for c in sorted(children.get(r.id, []), key=started):
            walk(c, level + 1)

    for r in sorted(roots, key=started, reverse=True):
        walk(r, 0)
    return out


def _add_totals(rows: list[dict[str, Any]]) -> None:
    """Each row's own cost plus everything below it, filled in from the bottom up."""
    by_id = {r["id"]: r for r in rows}
    for r in rows:
        r["total_cost"] = r["cost"] or 0.0
        r["followups"] = 0
    for r in reversed(rows):
        parent = by_id.get(r["parent_run_id"] or "")
        if parent is not None:
            parent["total_cost"] += r["total_cost"]
            parent["followups"] += 1 + r["followups"]


def _gate_view(
    gate: Gate, steps: list[StepRun], by_step: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    """The OK a run is waiting for. ``before`` when the step has not started: it asks
    before starting follow-up research, and says what it would start."""
    asked = [
        d for d in by_step.get(gate.step_id, []) if d["text"].startswith("Waiting for your OK")
    ]
    return {
        "step_id": gate.step_id,
        "before": not any(s.step_id == gate.step_id for s in steps),
        "text": asked[-1]["text"] if asked else "",
        "reason": asked[-1]["reason"] if asked else "",
    }


def _latest_attempts(steps: list[StepRun]) -> list[StepRun]:
    """Each step's last top-level record, in the order the steps first appeared."""
    latest: dict[str, StepRun] = {}
    for st in steps:
        # a key set again keeps its first place, so the order is when the step first ran
        if st.fanout_index is None or st.step_id not in latest:
            latest[st.step_id] = st
    return list(latest.values())


def _question_for(findings: list[Finding] | None, finding_id: str | None) -> str | None:
    if not findings or not finding_id:
        return None
    return next((f.question for f in findings if f.id == finding_id), None)
