"""The write API the interpreter uses. One session per run; commits after each write
so the UI can read a run while it is still going."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from wf.schema import Step, Workflow, dump_workflow

from .db import Database
from .records import (
    Artifact,
    Decision,
    Expectation,
    FindingRecord,
    Run,
    StepRun,
    WorkflowVersion,
    now,
)

SCHEMA_VERSION = "workflows.tavon.io/v1alpha1"


class Ledger:
    def __init__(self, db: Database):
        self.db = db
        self.session: Session = db.Session()
        self._seq = 0
        self._dseq = 0

    def close(self) -> None:
        self.session.close()

    def _commit(self) -> None:
        self.session.commit()

    # -- workflow versions -------------------------------------------------------

    def workflow_version(self, wf: Workflow, commit: str | None) -> WorkflowVersion:
        text = dump_workflow(wf)
        digest = hashlib.sha256(text.encode()).hexdigest()
        existing = (
            self.session.query(WorkflowVersion)
            .filter_by(
                name=wf.metadata.name,
                version=wf.metadata.version,
                commit=commit or f"sha256:{digest}",
            )
            .first()
        )
        if existing:
            return existing
        wv = WorkflowVersion(
            name=wf.metadata.name,
            version=wf.metadata.version,
            commit=commit or f"sha256:{digest}",
            schema_version=SCHEMA_VERSION,
            definition_yaml=text,
        )
        self.session.add(wv)
        self._commit()
        return wv

    # -- runs ------------------------------------------------------------

    def start_run(
        self,
        wv: WorkflowVersion,
        *,
        inputs: dict[str, Any],
        mode: str,
        depth: int,
        parent: Run | None,
        parent_step_run: StepRun | None,
        budget_usd: float | None,
        title: str,
        case_name: str | None,
    ) -> Run:
        run = Run(
            workflow_version_id=wv.id,
            workflow_name=wv.name,
            title=title,
            mode=mode,
            inputs=inputs,
            depth=depth,
            parent_run_id=parent.id if parent else None,
            parent_step_run_id=parent_step_run.id if parent_step_run else None,
            budget_usd=budget_usd,
            case_name=case_name,
        )
        self.session.add(run)
        self._commit()
        return run

    def finish_run(
        self,
        run: Run,
        *,
        status: str,
        outputs: dict[str, Any] | None,
        spent_usd: float,
        spent_minutes: float,
        error: str | None = None,
    ) -> None:
        run.status = status
        run.outputs = outputs
        run.spent_usd = round(spent_usd, 6)
        run.spent_minutes = round(spent_minutes, 3)
        run.error = error
        run.finished_at = now()
        self._commit()

    def update_spend(self, run: Run, spent_usd: float, spent_minutes: float) -> None:
        run.spent_usd = round(spent_usd, 6)
        run.spent_minutes = round(spent_minutes, 3)
        self._commit()

    # -- steps -----------------------------------------------------------

    def start_step(self, run: Run, step: Step, *, fanout_index: int | None, input: Any) -> StepRun:
        self._seq += 1
        sr = StepRun(
            run_id=run.id,
            seq=self._seq,
            step_id=step.id,
            title=step.title or step.id,
            kind=step.kind,
            fanout_index=fanout_index,
            input=input
            if isinstance(input, dict)
            else ({"value": input} if input is not None else None),
        )
        self.session.add(sr)
        self._commit()
        return sr

    def finish_step(
        self,
        sr: StepRun,
        *,
        status: str,
        output: Any = None,
        cost_usd: float = 0.0,
        error: str | None = None,
        instruction_ref: str | None = None,
        instruction_commit: str | None = None,
        instruction_sha256: str | None = None,
        model: str | None = None,
        tool_calls: list[Any] | None = None,
    ) -> None:
        sr.status = status
        sr.output = output
        sr.cost_usd = round(cost_usd, 6)
        sr.error = error
        sr.instruction_ref = instruction_ref
        sr.instruction_commit = instruction_commit
        sr.instruction_sha256 = instruction_sha256
        sr.model = model
        sr.tool_calls = tool_calls or []
        sr.finished_at = now()
        sr.duration_s = (sr.finished_at - sr.started_at).total_seconds() if sr.started_at else 0.0
        self._commit()

    # -- decisions, artifacts, findings, expectations ------------------------------

    def decision(
        self,
        run: Run,
        step_run: StepRun | None,
        step_id: str,
        *,
        kind: str,
        text: str,
        reason: str = "",
        alternatives: list[Any] | None = None,
        finding_id: str | None = None,
        field: str | None = None,
        value: Any = None,
    ) -> Decision:
        self._dseq += 1
        d = Decision(
            run_id=run.id,
            step_run_id=step_run.id if step_run else None,
            step_id=step_id,
            seq=self._dseq,
            kind=kind,
            text=text,
            reason=reason,
            alternatives=alternatives or [],
            finding_id=finding_id,
            field=field,
            value={"value": value} if value is not None else None,
        )
        self.session.add(d)
        self._commit()
        return d

    def artifact(
        self,
        run: Run,
        step_run: StepRun,
        *,
        name: str,
        path: str,
        media_type: str,
        simulated: bool,
        meta: dict[str, Any],
    ) -> Artifact:
        p = Path(path)
        sha = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""
        a = Artifact(
            run_id=run.id,
            step_run_id=step_run.id,
            name=name,
            path=path,
            media_type=media_type,
            simulated=simulated,
            sha256=sha,
            meta=meta,
        )
        self.session.add(a)
        self._commit()
        return a

    def finding(self, run: Run, finding: Any) -> FindingRecord:
        rec = FindingRecord(
            finding_id=finding.id,
            source="run",
            run_id=run.id,
            type=finding.type,
            step_id=finding.step_id,
            field=finding.field,
            question=finding.question,
            source_text=finding.source_text,
            answer={"value": finding.answer} if finding.answer is not None else None,
            status=finding.status,
            payload=finding.model_dump(mode="json"),
        )
        self.session.add(rec)
        self._commit()
        return rec

    def expectations(self, run: Run, items: list[dict[str, Any]]) -> list[Expectation]:
        out = []
        for it in items:
            e = Expectation(
                run_id=run.id,
                step_id=it["step"],
                field=it["field"],
                equals={"value": it.get("equals")},
                why=it.get("why", ""),
            )
            self.session.add(e)
            out.append(e)
        self._commit()
        return out

    def set_expectation_result(self, e: Expectation, matched: bool, actual: Any) -> None:
        e.matched = matched
        e.actual = {"value": actual}
        self._commit()
