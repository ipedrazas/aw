"""The interpreter walks a validated definition and executes it through activities.

It is deterministic on this side of the activity boundary: given the same
definition, inputs and recorded step outputs it makes the same sequence of
control-flow decisions. Nothing here calls a model or the network directly.

In ``dry`` mode an unanswered required field does not fail the run. The interpreter
asks the guesser, records a ``guess`` decision linked to the finding, and continues.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from wf.activities import Activities, ActivityError, ModelRequest, ToolSpec, run_with_policy
from wf.expr import EvalError, ExprError, render
from wf.expr.template import _TEMPLATE
from wf.logs import ROOT, get_logger
from wf.schema import Mode, Step, Workflow, Workspace
from wf.settings import quick_model
from wf.store import repo
from wf.store.ledger import Ledger
from wf.store.records import Run, StepRun, WorkflowVersion
from wf.store.sessions import session_span, step_span
from wf.validate import Finding, validate

from .context import BudgetTracker, OpenFindings, TraceEvent
from .registry import CHECKS, TOOLS, RunnerContext

logger = get_logger(f"{ROOT}.interpret")

SEARCH_TOOL = ToolSpec(
    name="search",
    description="Search the web. Returns up to max_results results with url, title, snippet and publication date.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["query", "max_results"],
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
        },
    },
    executor=lambda _: None,
)
CONTENTS_TOOL = ToolSpec(
    name="get_contents",
    description="Fetch the text of a page found by search. Returns title, text and publication date.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["url"],
        "properties": {"url": {"type": "string"}},
    },
    executor=lambda _: None,
)


@dataclass
class RunConfig:
    artifacts_dir: Path = Path("var/artifacts")
    # how a subworkflow step behaves: "run" starts children, "stub" records what would start
    subworkflows: str = "auto"  # auto = run in live, stub in dry
    author: tuple[str, str] | None = None


@dataclass
class RunResult:
    run_id: str
    status: str
    state: dict[str, Any]
    outputs: dict[str, Any] | None
    trace: list[TraceEvent]
    decisions: list[dict[str, Any]]
    spent_usd: float
    spent_minutes: float
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    guesses: list[dict[str, Any]] = field(default_factory=list)
    children: list[RunResult] = field(default_factory=list)
    error: str | None = None

    @property
    def control_trace(self) -> list[tuple[str, str, str]]:
        return [t.as_tuple() for t in self.trace]


class Interpreter:
    def __init__(
        self, ws: Workspace, activities: Activities, ledger: Ledger, config: RunConfig | None = None
    ):
        self.ws = ws
        self.acts = activities
        self.ledger = ledger
        self.config = config or RunConfig()

    # -- entry point ---------------------------------------------------------------

    def run(
        self,
        wf: Workflow,
        inputs: dict[str, Any],
        mode: Mode,
        *,
        findings: list[Finding] | None = None,
        expectation: list[dict[str, Any]] | None = None,
        case_name: str | None = None,
        title: str | None = None,
        depth: int = 0,
        parent: Run | None = None,
        parent_step_run: StepRun | None = None,
        budget: BudgetTracker | None = None,
    ) -> RunResult:
        if findings is None:
            findings = validate(wf, self.ws).findings
        open_findings = [f for f in findings if f.status == "open"]
        if mode == "live" and open_findings:
            raise OpenFindings(open_findings)

        inputs = self._prepare_inputs(wf, inputs)
        if budget is None:
            b = wf.spec.budget
            budget = BudgetTracker(b.max_usd if b else None, b.max_minutes if b else None)
        commit = repo.file_commit(
            self.ws.root, str(self.ws.definition_path(wf.metadata.name).relative_to(self.ws.root))
        )
        wv = self.ledger.workflow_version(wf, commit)
        run = self.ledger.start_run(
            wv,
            inputs=inputs,
            mode=mode,
            depth=depth,
            parent=parent,
            parent_step_run=parent_step_run,
            budget_usd=budget.max_usd,
            title=title or str(inputs.get("topic") or wf.metadata.name),
            case_name=case_name,
        )
        return self.run_existing(
            run,
            wf,
            inputs,
            mode,
            findings=findings,
            expectation=expectation,
            budget=budget,
            depth=depth,
        )

    def run_existing(
        self,
        run: Run,
        wf: Workflow,
        inputs: dict[str, Any],
        mode: Mode,
        *,
        findings: list[Finding] | None = None,
        expectation: list[dict[str, Any]] | None = None,
        budget: BudgetTracker | None = None,
        depth: int = 0,
    ) -> RunResult:
        """Execute against a run record that already exists (the API creates it first so
        the id can be handed back before the work starts).

        Everything a model is asked while this run walks belongs to one session, so the
        run and the transcript behind it can be read side by side afterwards. A child
        run opens a session of its own.
        """
        with session_span("run", name=wf.metadata.name, title=run.title, run_id=run.id, mode=mode):
            return self._walk(
                run,
                wf,
                inputs,
                mode,
                findings=findings,
                expectation=expectation,
                budget=budget,
                depth=depth,
            )

    def resume(self, run: Run, wf: Workflow, answer: dict[str, Any]) -> RunResult:
        """Carry on a run that stopped at a wait, with the person's answer as the wait's
        output. The steps before it are not run again: their results are read back from
        the run, so what the answer is about is what the rest of the run works from."""
        rows = (
            self.ledger.session.query(StepRun).filter_by(run_id=run.id).order_by(StepRun.seq).all()
        )
        waiting = next((r for r in rows if r.status == "waiting"), None)
        if waiting is None:
            raise ValueError("This run is not waiting for anyone.")
        self._continue_numbering(run, rows)
        steps = self._read_back(wf, [r for r in rows if r.id != waiting.id])
        self.ledger.finish_step(waiting, status="done", output=answer)
        steps[waiting.step_id] = {"status": "done", "output": answer, "outputs": None}
        run.status, run.finished_at, run.error = "running", None, None
        self.ledger._commit()
        topics = [t for t in answer.get("topics") or [] if t]
        said = (
            "You asked it to go deeper into: " + "; ".join(topics) + "."
            if answer.get("go_deeper") and topics
            else "You said to finish the report without going deeper."
        )
        if answer.get("note"):
            said += f" You added: “{answer['note']}”"
        b = wf.spec.budget
        budget = BudgetTracker(b.max_usd if b else None, b.max_minutes if b else None)
        budget.spent_usd = run.spent_usd or 0.0
        with session_span(
            "run", name=wf.metadata.name, title=run.title, run_id=run.id, mode=run.mode
        ):
            return self._walk(
                run,
                wf,
                dict(run.inputs or {}),
                run.mode,
                budget=budget,
                depth=run.depth or 0,
                resume=_Resume(
                    steps=steps,
                    start_at=wf.step_index(waiting.step_id) + 1,
                    spent_usd=run.spent_usd or 0.0,
                    note_on=waiting,
                    note_step_id=waiting.step_id,
                    said=said,
                    why="Your answer to the wait. The run carried on from here.",
                ),
            )

    def retry(self, run: Run, wf: Workflow, *, claimed: bool = False) -> RunResult:
        """Pick up a run that broke, at the step that broke, once the cause is fixed.

        The steps that finished are not run again: their results are read back from the
        run, as after a wait. The step that broke runs again whole, every item of it if
        it runs over a list, and so does everything after it. It runs on the definition
        as it is now, since that is where the fix is; an earlier step whose definition
        changed since is named in the run's record, because its result is from before.

        ``claimed`` says the caller has already marked the broken run as running, so two
        requests cannot both pick it up.
        """
        return self._pick_up(run, wf, claimed=claimed, skip=False)

    def skip(self, run: Run, wf: Workflow, *, claimed: bool = False) -> RunResult:
        """Carry a run that broke on without the step that broke.

        For a step whose result the rest can do without, when what broke cannot be fixed
        from here: a follow-up that failed, a source that will not answer. The step is
        marked skipped, its error kept, and the run goes on from the step after it, as
        it would had the step's condition not been met; the steps before it are not run
        again.
        """
        return self._pick_up(run, wf, claimed=claimed, skip=True)

    def _pick_up(self, run: Run, wf: Workflow, *, claimed: bool, skip: bool) -> RunResult:
        if run.status != "failed" and not claimed:
            raise ValueError("This run did not stop on an error, so there is nothing to pick up.")
        rows = (
            self.ledger.session.query(StepRun).filter_by(run_id=run.id).order_by(StepRun.seq).all()
        )
        latest: dict[str, StepRun] = {}
        for r in rows:
            if r.fanout_index is None:
                latest[r.step_id] = r
        start_at = next(
            (
                i
                for i, s in enumerate(wf.spec.steps)
                if s.id not in latest or latest[s.id].status not in ("done", "skipped")
            ),
            len(wf.spec.steps),
        )
        if start_at == len(wf.spec.steps):
            raise ValueError("Every step of this run finished, so there is nothing to pick up.")
        step = wf.spec.steps[start_at]
        broke = latest.get(step.id)

        self._continue_numbering(run, rows)
        # the attempt that broke stays in the record: as the one that was retried, or as
        # the step that was skipped, with its error
        for r in rows:
            if r.step_id == step.id and r.status in ("failed", "running"):
                r.status = "skipped" if skip else "retried"
        earlier = {s.id for s in wf.spec.steps[:start_at]}
        steps = self._read_back(wf, [r for r in rows if r.step_id in earlier])

        if skip:
            steps[step.id] = {"status": "skipped", "output": None, "outputs": None}
            said = f"Skipped “{step.title or step.id}” after it broke, and carried on without it."
            why = (
                "You chose to. A step after it that reads its result gets nothing from it. "
                "The steps before it were not run again."
            )
        else:
            said = f"Picked up from “{step.title or step.id}”" + (
                ", after it broke." if broke is not None else ", the first step without a result."
            )
            why = (
                "The steps before it were not run again: their results are read back from this run."
            )
        commit = repo.file_commit(
            self.ws.root, str(self.ws.definition_path(wf.metadata.name).relative_to(self.ws.root))
        )
        now = self.ledger.workflow_version(wf, commit)
        if now.id != run.workflow_version_id:
            why += " It runs on the definition as it is now, which has changed since the run began."
            then = self.ledger.session.get(WorkflowVersion, run.workflow_version_id)
            changed = _changed_steps(then, wf, earlier)
            if changed:
                why += (
                    " These earlier steps changed too, and their results are from before: "
                    + ", ".join(f"“{t}”" for t in changed)
                    + "."
                )

        run.status, run.finished_at, run.error = "running", None, None
        self.ledger._commit()
        b = wf.spec.budget
        budget = BudgetTracker(b.max_usd if b else None, b.max_minutes if b else None)
        budget.spent_usd = run.spent_usd or 0.0
        with session_span(
            "run", name=wf.metadata.name, title=run.title, run_id=run.id, mode=run.mode
        ):
            return self._walk(
                run,
                wf,
                dict(run.inputs or {}),
                run.mode,
                budget=budget,
                depth=run.depth or 0,
                resume=_Resume(
                    steps=steps,
                    start_at=start_at + 1 if skip else start_at,
                    spent_usd=run.spent_usd or 0.0,
                    note_on=broke,
                    note_step_id=step.id,
                    said=said,
                    why=why,
                ),
            )

    def _continue_numbering(self, run: Run, rows: list[StepRun]) -> None:
        """New records follow the run's existing ones, so the order they happened in holds."""
        from wf.store.records import Decision

        self.ledger._seq = max((r.seq for r in rows), default=0)
        self.ledger._dseq = max(
            (d.seq for d in self.ledger.session.query(Decision).filter_by(run_id=run.id)),
            default=0,
        )

    @staticmethod
    def _read_back(wf: Workflow, rows: list[StepRun]) -> dict[str, Any]:
        """The run's state for these step records, as the walk that made them left it.
        The last record of a step is the one that counts."""
        steps: dict[str, Any] = {}
        for r in rows:
            if r.fanout_index is not None:
                continue
            fans_out = any(s.id == r.step_id and s.for_each for s in wf.spec.steps)
            steps[r.step_id] = (
                {"status": r.status, "output": None, "outputs": r.output}
                if fans_out
                else {"status": r.status, "output": r.output, "outputs": None}
            )
        return steps

    def _walk(
        self,
        run: Run,
        wf: Workflow,
        inputs: dict[str, Any],
        mode: Mode,
        *,
        findings: list[Finding] | None = None,
        expectation: list[dict[str, Any]] | None = None,
        budget: BudgetTracker | None = None,
        depth: int = 0,
        resume: _Resume | None = None,
    ) -> RunResult:
        if findings is None:
            findings = validate(wf, self.ws).findings
        open_findings = [f for f in findings if f.status == "open"]
        if mode == "live" and open_findings:
            raise OpenFindings(open_findings)
        if budget is None:
            b = wf.spec.budget
            budget = BudgetTracker(b.max_usd if b else None, b.max_minutes if b else None)
        if resume is None:
            for f in open_findings:
                self.ledger.finding(run, f)
            if expectation:
                self.ledger.expectations(run, expectation)

        state: dict[str, Any] = {
            "inputs": inputs,
            "steps": dict(resume.steps) if resume else {},
        }
        result = RunResult(
            run_id=run.id,
            status="running",
            state=state,
            outputs=None,
            trace=[],
            decisions=[],
            spent_usd=0.0,
            spent_minutes=0.0,
        )
        ctx = _Ctx(
            wf=wf,
            run=run,
            mode=mode,
            state=state,
            findings=open_findings,
            budget=budget,
            result=result,
            depth=depth,
            own_spend=resume.spent_usd if resume else 0.0,
            approved=resume.approved if resume else None,
        )
        if resume is not None:
            self._decide(
                ctx,
                resume.note_on,
                resume.note_step_id,
                kind="control",
                text=resume.said,
                reason=resume.why,
            )

        status = "done"
        error: str | None = None
        try:
            for step in wf.spec.steps[resume.start_at if resume else 0 :]:
                over = budget.exceeded()
                if over:
                    self._decide(
                        ctx,
                        None,
                        step.id,
                        kind="control",
                        text=f"Paused before “{step.title or step.id}”: {over}.",
                        reason="The spending limit protects the whole run, including any follow-ups.",
                    )
                    ctx.result.trace.append(TraceEvent(step.id, "paused", "budget"))
                    status = "paused_budget"
                    break
                outcome = self._run_step(ctx, step)
                if outcome in ("waiting", "paused", "failed"):
                    status = outcome
                    break
                if self._gate(ctx, step):
                    status = "waiting"
                    break
            if status == "done":
                result.outputs = self._workflow_outputs(wf, state)
        except ActivityError as e:
            status, error = "failed", str(e)
        except Exception as e:  # noqa: BLE001 - the run must record how it broke
            status, error = "failed", f"{type(e).__name__}: {e}"

        result.status = status
        result.error = error
        result.spent_usd = budget.spent_usd if depth == 0 else ctx.own_spend
        result.spent_minutes = budget.spent_minutes
        self.ledger.finish_run(
            run,
            status=status,
            outputs=result.outputs,
            spent_usd=ctx.own_spend,
            spent_minutes=budget.spent_minutes,
            error=error,
        )
        self.last_result = result
        return result

    # -- steps -------------------------------------------------------------

    def _run_step(self, ctx: _Ctx, step: Step) -> str:
        # when
        if step.when is not None:
            try:
                cond = bool(render(step.when, ctx.state))
            except (ExprError, EvalError) as e:
                f = self._finding(ctx, step, "when")
                g = self._guess(ctx, step, f, extra=f"The condition could not be read: {e}")
                cond = bool(g.value)
            if not cond:
                ctx.state["steps"][step.id] = {"status": "skipped", "output": None, "outputs": None}
                ctx.result.trace.append(TraceEvent(step.id, "skip"))
                sr = self.ledger.start_step(ctx.run, step, fanout_index=None, input=None)
                self.ledger.finish_step(sr, status="skipped")
                self._decide(
                    ctx,
                    sr,
                    step.id,
                    kind="control",
                    text=f"Skipped “{step.title or step.id}”.",
                    reason=f"Its condition was not met: {step.when}",
                )
                return "done"
        else:
            f = self._finding(ctx, step, "when")
            if f is not None:
                g = self._guess(ctx, step, f)
                if not g.value:
                    ctx.state["steps"][step.id] = {
                        "status": "skipped",
                        "output": None,
                        "outputs": None,
                    }
                    ctx.result.trace.append(TraceEvent(step.id, "skip"))
                    sr = self.ledger.start_step(ctx.run, step, fanout_index=None, input=None)
                    self.ledger.finish_step(sr, status="skipped")
                    return "done"

        # a step that starts follow-up research asks before it spends anything
        if self._gate_before(ctx, step):
            return "waiting"

        # for_each
        if step.for_each is not None:
            try:
                items = render(step.for_each, ctx.state)
            except (ExprError, EvalError):
                items = None
            items = list(items) if isinstance(items, list) else []
            cap = step.effective_max_fanout
            if cap is None:
                f = self._finding(ctx, step, "max_fanout")
                cap = int(self._guess(ctx, step, f).value) if f else 3
            if len(items) > cap:
                self._decide(
                    ctx,
                    None,
                    step.id,
                    kind="control",
                    text=f"Running {cap} of {len(items)} items for “{step.title or step.id}”.",
                    reason=f"The limit for this step is {cap} at a time.",
                    alternatives=[f"Raise the limit to {len(items)}"],
                )
                items = items[:cap]
            ctx.result.trace.append(TraceEvent(step.id, "fanout", len(items)))
            # one parent record for the fan-out; each item gets its own child record
            parent = self.ledger.start_step(
                ctx.run, step, fanout_index=None, input={"items": items}
            )
            outputs: list[Any] = []
            for i, item in enumerate(items):
                ctx.state["item"] = item
                status, out = self._execute(ctx, step, fanout_index=i)
                ctx.state.pop("item", None)
                if status != "done":
                    ctx.state["steps"][step.id] = {
                        "status": status,
                        "output": None,
                        "outputs": outputs,
                    }
                    self.ledger.finish_step(parent, status=status, output=outputs)
                    return status
                outputs.append(out)
            ctx.state["steps"][step.id] = {"status": "done", "output": None, "outputs": outputs}
            self.ledger.finish_step(parent, status="done", output=outputs)
            return "done"

        ctx.result.trace.append(TraceEvent(step.id, "run"))
        status, out = self._execute(ctx, step, fanout_index=None)
        ctx.state["steps"][step.id] = {"status": status, "output": out, "outputs": None}
        if status == "done":
            self._check_unhandled_outcomes(ctx, step, out)
        return status

    def _execute(self, ctx: _Ctx, step: Step, *, fanout_index: int | None) -> tuple[str, Any]:
        try:
            rendered_input = render(step.input, ctx.state) if step.input is not None else None
        except (ExprError, EvalError) as e:
            rendered_input = None
            self._decide(
                ctx,
                None,
                step.id,
                kind="control",
                text=f"Could not read part of the input for “{step.title or step.id}”.",
                reason=str(e),
            )
        sr = self.ledger.start_step(ctx.run, step, fanout_index=fanout_index, input=rendered_input)
        with step_span(step_run_id=sr.id, step_id=step.id, fanout_index=fanout_index):
            return self._execute_body(ctx, step, sr, rendered_input, fanout_index)

    def _execute_body(
        self,
        ctx: _Ctx,
        step: Step,
        sr: StepRun,
        rendered_input: Any,
        fanout_index: int | None,
    ) -> tuple[str, Any]:
        try:
            if step.kind == "agent":
                out, meta = self._agent(ctx, step, sr, rendered_input or {}, fanout_index)
            elif step.kind == "check":
                out, meta = self._check(ctx, step, sr, rendered_input or {})
            elif step.kind == "tool":
                out, meta = self._tool(ctx, step, sr, rendered_input or {})
            elif step.kind == "subworkflow":
                out, meta = self._subworkflow(ctx, step, sr, fanout_index)
            elif step.kind == "wait":
                out, meta = self._wait(ctx, step, sr)
            else:  # pragma: no cover
                raise ActivityError(f"unknown step kind {step.kind}")
        except ActivityError as e:
            self.ledger.finish_step(sr, status="failed", error=str(e))
            self._decide(
                ctx,
                sr,
                step.id,
                kind="control",
                text=f"“{step.title or step.id}” could not finish.",
                reason=str(e),
                alternatives=["Retry", "Skip this step"],
            )
            ctx.result.trace.append(TraceEvent(step.id, "failed"))
            return "failed", None
        status = meta.pop("status", "done")
        self.ledger.finish_step(sr, status=status, output=out, **meta)
        if status == "waiting":
            ctx.result.trace.append(TraceEvent(step.id, "wait"))
        return status, out

    # -- kinds -------------------------------------------------------------

    def _agent(
        self, ctx: _Ctx, step: Step, sr: StepRun, input: dict[str, Any], fanout_index: int | None
    ) -> tuple[Any, dict[str, Any]]:
        skill = self.ws.load_skill(step.skill) if step.skill else None
        if skill is None:
            f = self._finding(ctx, step, "skill")
            if f is not None:
                self._guess(ctx, step, f, sr=sr)
            system = f"# {step.title or step.id}\n\n{step.description or ''}\n\nDo what the title says, and no more."
        else:
            system = skill.body
        model = ctx.wf.model_for(step)
        origin = "the step names it" if step.model else "the workflow's default"
        if model is None:
            f = self._finding(ctx, step, "model")
            model = str(self._guess(ctx, step, f, sr=sr).value) if f else quick_model()
            origin = "guessed, the step names none" if f else "the quick default"
        logger.debug(
            "%s runs on %s (%s)",
            step.id,
            model,
            origin,
            extra={
                "fields": {
                    "event": "step.model",
                    "run": ctx.result.run_id,
                    "workflow": ctx.wf.metadata.name,
                    "step": step.id,
                    "kind": step.kind,
                    "model": model,
                    "origin": origin,
                }
            },
        )
        schema = (
            self.ws.load_schema(step.output.schema_)
            if step.output and step.output.schema_
            else None
        )
        if schema is None:
            f = self._finding(ctx, step, "output.schema")
            if f is not None:
                self._guess(ctx, step, f, sr=sr)
            schema = {
                "type": "object",
                "properties": {"result": {"type": "string"}},
                "required": ["result"],
                "additionalProperties": False,
            }

        tools = self._tools_for(step)
        tag = step.id if fanout_index is None else f"{step.id}[{fanout_index}]"
        req = ModelRequest(
            tag=tag,
            model=model,
            system=system,
            input=input,
            output_schema=schema,
            tools=tools,
            decisions_required=ctx.wf.decision_log_for(step) == "required",
        )
        resp = run_with_policy(self.acts.policy, lambda: self.acts.model.complete(req))
        try:
            jsonschema.validate(resp.output, schema)
        except jsonschema.ValidationError as e:
            raise ActivityError(
                f"the step's output did not match its declared shape: {e.message}"
            ) from e

        for d in resp.decisions:
            self._decide(
                ctx,
                sr,
                step.id,
                kind="decision",
                text=str(d.get("decision", "")),
                reason=str(d.get("reason", "")),
                alternatives=list(d.get("alternatives", [])),
            )
        if req.decisions_required and not resp.decisions:
            self._decide(
                ctx,
                sr,
                step.id,
                kind="control",
                text="This step recorded no decisions, although it was asked to.",
                reason="Its output was kept; the missing explanations are a gap in this run's trail.",
            )
        for call in resp.tool_calls:
            if call.injection:
                src = call.input.get("url") or call.input.get("query") or call.name
                self._decide(
                    ctx,
                    sr,
                    step.id,
                    kind="ignored",
                    text=f"Ignored instructions found in {src}.",
                    reason=f"The page contained text addressed to the model: “{call.injection}”. It was read as data only.",
                )

        cost = resp.usage.cost_usd
        ctx.spend(cost)
        self.ledger.update_spend(ctx.run, ctx.own_spend, ctx.budget.spent_minutes)
        meta = {
            "cost_usd": cost,
            "instruction_ref": step.skill,
            "instruction_commit": repo.file_commit(self.ws.root, skill.file or skill.path)
            if skill
            else None,
            "instruction_sha256": skill.sha256 if skill else None,
            "model": resp.model or model,
            "tool_calls": [
                {"name": c.name, "input": c.input, "summary": c.summary} for c in resp.tool_calls
            ],
        }
        return resp.output, meta

    def _tools_for(self, step: Step) -> list[ToolSpec]:
        out: list[ToolSpec] = []
        for name, perm in (step.tools or {}).items():
            base = name.split(".")[-1]
            if base == "search":
                out.append(
                    ToolSpec(
                        SEARCH_TOOL.name,
                        SEARCH_TOOL.description,
                        SEARCH_TOOL.input_schema,
                        lambda i: [
                            r.__dict__
                            for r in self.acts.search.search(
                                i["query"], int(i.get("max_results", 5))
                            )
                        ],
                        perm.max_calls,
                    )
                )
            elif base in ("get_contents", "fetch"):
                out.append(
                    ToolSpec(
                        CONTENTS_TOOL.name,
                        CONTENTS_TOOL.description,
                        CONTENTS_TOOL.input_schema,
                        lambda i: self.acts.search.get_contents(i["url"]),
                        perm.max_calls,
                    )
                )
        return out

    def _runner_ctx(self, ctx: _Ctx, step: Step, sr: StepRun) -> RunnerContext:
        return RunnerContext(
            activities=self.acts,
            mode=ctx.mode,
            run_id=ctx.run.id,
            step_id=step.id,
            artifacts_dir=self.config.artifacts_dir,
            note=lambda text, reason: self._decide(
                ctx, sr, step.id, kind="control", text=text, reason=reason
            ),
            keep=lambda path, media, simulated: self._keep(ctx, step, sr, path, media, simulated),
        )

    def _keep(
        self, ctx: _Ctx, step: Step, sr: StepRun, path: str, media: str, simulated: bool
    ) -> None:
        art = self.ledger.artifact(
            ctx.run,
            sr,
            name=Path(path).name,
            path=str(path),
            media_type=media,
            simulated=simulated,
            meta={"step": step.id, "mode": ctx.mode},
        )
        ctx.result.artifacts.append(
            {"id": art.id, "name": art.name, "path": art.path, "simulated": simulated}
        )

    def _check(
        self, ctx: _Ctx, step: Step, sr: StepRun, input: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        fn = CHECKS.get(step.run or "")
        if fn is None:
            f = self._finding(ctx, step, "run")
            if f is not None:
                self._guess(ctx, step, f, sr=sr)
                return None, {}
            raise ActivityError(f"no check routine named {step.run!r}")
        if not step.does_not_check:
            f = self._finding(ctx, step, "does_not_check")
            if f is not None:
                self._guess(ctx, step, f, sr=sr)
        out = run_with_policy(self.acts.policy, lambda: fn(self._runner_ctx(ctx, step, sr), input))
        self._validate_output(step, out)
        if step.does_not_check:
            self._decide(
                ctx,
                sr,
                step.id,
                kind="control",
                text=f"“{step.title or step.id}” checked: {'; '.join(step.checks or [])}.",
                reason="It does not check: " + "; ".join(step.does_not_check) + ".",
            )
        return out, {}

    def _tool(
        self, ctx: _Ctx, step: Step, sr: StepRun, input: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        side_effects = step.side_effects
        if side_effects is None:
            f = self._finding(ctx, step, "side_effects")
            side_effects = self._guess(ctx, step, f, sr=sr).value if f else "none"
        approval = step.requires_approval
        if approval is None:
            f = self._finding(ctx, step, "requires_approval")
            approval = self._guess(ctx, step, f, sr=sr).value if f else False
        leaves_system = side_effects not in (None, "none", [])
        if approval and ctx.mode == "live":
            self._decide(
                ctx,
                sr,
                step.id,
                kind="control",
                text=f"Waiting for approval before “{step.title or step.id}”.",
                reason="This step needs someone to sign it off. Approval gates are not automated in this phase, so the run stops here.",
            )
            return None, {"status": "waiting"}
        if leaves_system and ctx.mode != "live":
            self._decide(
                ctx,
                sr,
                step.id,
                kind="control",
                text=f"Did not run “{step.title or step.id}” for real.",
                reason=f"It would {', '.join(side_effects) if isinstance(side_effects, list) else side_effects} outside the system. In a dry run that is recorded, not done.",
                value=input,
            )
            ctx.result.trace.append(TraceEvent(step.id, "stub"))
            return {"recorded": True, "delivered": False}, {}
        fn = TOOLS.get(step.run or "")
        if fn is None:
            f = self._finding(ctx, step, "run")
            if f is not None:
                self._guess(ctx, step, f, sr=sr)
                return None, {}
            raise ActivityError(f"no tool routine named {step.run!r}")
        out = run_with_policy(self.acts.policy, lambda: fn(self._runner_ctx(ctx, step, sr), input))
        self._validate_output(step, out)
        return out, {}

    def _subworkflow(
        self, ctx: _Ctx, step: Step, sr: StepRun, fanout_index: int | None
    ) -> tuple[Any, dict[str, Any]]:
        child_wf = (
            self.ws.resolve_workflow_ref(step.workflow or "", current=ctx.wf)
            if step.workflow
            else None
        )
        if child_wf is None:
            raise ActivityError(f"no workflow matches {step.workflow!r}")
        limits = step.limits
        if limits is None or limits.max_depth is None:
            f = self._finding(ctx, step, "limits")
            g = self._guess(ctx, step, f, sr=sr) if f else None
            max_depth = int(g.value.get("max_depth", 1)) if g and isinstance(g.value, dict) else 1
        else:
            max_depth = limits.max_depth
        child_inputs = render(step.with_, ctx.state) if step.with_ else {}
        child_depth = ctx.depth + 1
        title = str(child_inputs.get("topic") or child_wf.metadata.name)
        if child_depth > max_depth:
            self._decide(
                ctx,
                sr,
                step.id,
                kind="control",
                text=f"Did not start “{title}”: it would be {child_depth} levels deep and the limit is {max_depth}.",
                reason="Depth is a declared limit, enforced before anything starts.",
            )
            ctx.result.trace.append(TraceEvent(step.id, "stub", "depth"))
            return None, {}
        mode_stub = self.config.subworkflows == "stub" or (
            self.config.subworkflows == "auto" and ctx.mode != "live"
        )
        if mode_stub:
            remaining = ctx.budget.remaining_usd
            self._decide(
                ctx,
                sr,
                step.id,
                kind="control",
                text=f"Would start a follow-up run: “{title}”, one level deeper ({child_depth} of {max_depth}).",
                reason="In a dry run, follow-ups are shown, not started."
                + (
                    f" About ${remaining:.2f} of the budget is left for them."
                    if remaining is not None
                    else ""
                ),
                value=child_inputs,
            )
            ctx.result.trace.append(TraceEvent(step.id, "stub", "subworkflow"))
            return None, {}
        shared = (
            ctx.budget
            if (limits and limits.budget == "inherit")
            or (ctx.wf.spec.budget and ctx.wf.spec.budget.shared_with_children)
            else None
        )
        child = self.run(
            child_wf,
            child_inputs,
            ctx.mode,
            depth=child_depth,
            parent=ctx.run,
            parent_step_run=sr,
            budget=shared,
            title=title,
        )
        ctx.result.children.append(child)
        if child.status != "done":
            raise ActivityError(
                f"follow-up run “{title}” stopped: {why_it_stopped(child)} (run {child.run_id[:8]})"
            )
        return child.outputs, {"cost_usd": child.spent_usd}

    def _wait(self, ctx: _Ctx, step: Step, sr: StepRun) -> tuple[Any, dict[str, Any]]:
        deadline = step.deadline
        if deadline is None:
            f = self._finding(ctx, step, "deadline")
            deadline = str(self._guess(ctx, step, f, sr=sr).value) if f else "1d"
        on_timeout = step.on_timeout
        if on_timeout is None:
            f = self._finding(ctx, step, "on_timeout")
            on_timeout = str(self._guess(ctx, step, f, sr=sr).value) if f else "continue"
        not_asked = {"go_deeper": False, "topics": [], "note": ""}
        if ctx.mode != "live":
            self._decide(
                ctx,
                sr,
                step.id,
                kind="control",
                text=f"Would wait up to {deadline} for “{step.title or step.id}”, then {on_timeout}.",
                reason="A dry run does not wait for people. It continued as if the wait had ended.",
            )
            ctx.result.trace.append(TraceEvent(step.id, "stub", "wait"))
            return {
                "waited": False,
                "deadline": deadline,
                "on_timeout": on_timeout,
                **not_asked,
            }, {}
        if ctx.depth > 0:
            self._decide(
                ctx,
                sr,
                step.id,
                kind="control",
                text=f"Did not stop for “{step.title or step.id}”: this is a follow-up run.",
                reason="You were asked once, on the run that started it. A follow-up finishes and hands its report back.",
            )
            return {"waited": False, **not_asked}, {}
        self._decide(
            ctx,
            sr,
            step.id,
            kind="control",
            text=f"Waiting for “{step.title or step.id}” (up to {deadline}).",
            reason="The run stops here until you answer. Your answer is what the steps after it work from.",
        )
        return None, {"status": "waiting"}

    # -- helpers -------------------------------------------------------------

    def _validate_output(self, step: Step, out: Any) -> None:
        if step.output and step.output.schema_ and out is not None:
            schema = self.ws.load_schema(step.output.schema_)
            if schema:
                try:
                    jsonschema.validate(out, schema)
                except jsonschema.ValidationError as e:
                    raise ActivityError(
                        f"the step's output did not match its declared shape: {e.message}"
                    ) from e

    def _finding(self, ctx: _Ctx, step: Step, key: str) -> Finding | None:
        want = f"steps.{step.id}.{key}"
        for f in ctx.findings:
            if f.field == want and f.status == "open":
                return f
        return None

    def _guess(
        self,
        ctx: _Ctx,
        step: Step,
        finding: Finding | None,
        *,
        sr: StepRun | None = None,
        extra: str | None = None,
    ) -> Any:
        if finding is None:
            from wf.validate import make_finding

            finding = make_finding(
                "gap", f"steps.{step.id}.when", step=step, raised_by="interpreter"
            )
        if ctx.mode == "live":
            raise OpenFindings([finding])
        g = self.acts.guesser.guess(finding=finding, step=step, state=ctx.state, mode=ctx.mode)
        reason = g.reason if not extra else f"{extra}. {g.reason}"
        self._decide(
            ctx,
            sr,
            step.id,
            kind="guess",
            text=g.text,
            reason=reason,
            alternatives=g.alternatives,
            finding_id=finding.id,
            field=finding.field,
            value=g.value,
        )
        ctx.result.trace.append(TraceEvent(step.id, "guess", (finding.field, g.value)))
        ctx.result.guesses.append(
            {
                "step_id": step.id,
                "field": finding.field,
                "finding_id": finding.id,
                "question": finding.question,
                "value": g.value,
                "text": g.text,
                "reason": reason,
            }
        )
        return g

    def _check_unhandled_outcomes(self, ctx: _Ctx, step: Step, out: Any) -> None:
        prefix = f"steps.{step.id}.output.continue_on."
        for f in ctx.findings:
            if not f.field.startswith(prefix) or f.status != "open":
                continue
            rest = f.field[len(prefix) :]
            field_name, _, value = rest.rpartition(".")
            actual = out.get(field_name) if isinstance(out, dict) else None
            if actual is not None and str(actual) == value:
                self._guess(ctx, step, f)

    # -- gates -------------------------------------------------------------

    def fingerprint(self, wf: Workflow, step: Step) -> str:
        return fingerprint(self.ws, wf, step)

    def _gate(self, ctx: _Ctx, step: Step) -> bool:
        """Whether the run stops after ``step`` for someone's OK, saying why either way.
        A step that starts follow-up research asks before it starts instead."""
        if not stops_for_ok(step) or asks_before(step):
            return False
        if ctx.state["steps"].get(step.id, {}).get("status") != "done":
            return False
        return self._stop_for_ok(ctx, step, self._latest_record(ctx.run, step.id), before=None)

    def _gate_before(self, ctx: _Ctx, step: Step) -> bool:
        """Whether the run stops before ``step`` starts follow-up research, for someone's OK
        on what it would start. Asked once: the OK carries the run on into the step."""
        if not asks_before(step):
            return False
        if ctx.approved == step.id:
            ctx.approved = None
            return False
        try:
            items = render(step.for_each, ctx.state) if step.for_each else None
        except (ExprError, EvalError):
            items = None
        names = [
            str(i.get("topic") or i) if isinstance(i, dict) else str(i)
            for i in (items if isinstance(items, list) else [])
        ]
        cap = step.effective_max_fanout
        if cap is not None:
            names = names[:cap]
        what = (
            f"It would start {len(names)} follow-up{'s' if len(names) != 1 else ''}: "
            + "; ".join(f"“{n}”" for n in names)
            + "."
            if names
            else "It would start more research."
        )
        return self._stop_for_ok(ctx, step, None, before=what)

    def _stop_for_ok(
        self, ctx: _Ctx, step: Step, sr: StepRun | None, *, before: str | None
    ) -> bool:
        """Whether this step's trust stops the run here, recording why either way.

        Only a real run stops, and only at the top: a follow-up was approved when
        someone chose to go deeper, and a dry run shows where it would have stopped."""
        trust = ctx.wf.trust_for(step)
        if trust is None or trust.policy == "auto":
            return False
        name = step.title or step.id
        went_on = (
            f"Started “{name}” without asking."
            if before
            else f"Carried on after “{name}” without asking."
        )
        fp = self.fingerprint(ctx.wf, step)
        if trust.policy == "earned":
            needed = trust.promote_after or 3
            oks = self.ledger.oks_in_a_row(ctx.wf.metadata.name, step.id, fp)
            if oks >= needed:
                self._decide(
                    ctx,
                    sr,
                    step.id,
                    kind="control",
                    text=went_on,
                    reason=f"You have said OK to it {oks} times in a row since it last changed.",
                )
                return False
            why = (
                f"It checks with you until you have said OK {needed} times in a row; "
                f"so far {oks}. Any change to the step starts the count again."
            )
        else:
            why = "It checks with you every time."
        if ctx.depth > 0:
            self._decide(
                ctx,
                sr,
                step.id,
                kind="control",
                text=went_on,
                reason="A follow-up does not stop for your OK: you approved going deeper in the run that started it.",
            )
            return False
        if ctx.mode != "live":
            self._decide(
                ctx,
                sr,
                step.id,
                kind="control",
                text=(
                    f"In a real run, this is where it would ask you before starting “{name}”."
                    if before
                    else f"In a real run, this is where it would stop for your OK on “{name}”."
                ),
                reason=f"{before} {why}" if before else why,
            )
            return False
        self.ledger.open_gate(ctx.run, step.id, sr, fp)
        self._decide(
            ctx,
            sr,
            step.id,
            kind="control",
            text=(
                f"Waiting for your OK before starting “{name}”."
                if before
                else f"Waiting for your OK on “{name}” before carrying on."
            ),
            reason=f"{before} {why}" if before else why,
        )
        ctx.result.trace.append(TraceEvent(step.id, "gate"))
        return True

    def _latest_record(self, run: Run, step_id: str) -> StepRun | None:
        return (
            self.ledger.session.query(StepRun)
            .filter_by(run_id=run.id, step_id=step_id, fanout_index=None)
            .order_by(StepRun.seq.desc())
            .first()
        )

    def carry_on(self, run: Run, wf: Workflow, *, ok: bool, note: str = "") -> RunResult:
        """Someone answered the gate a run stopped at: carry on from the next step, or
        stop the run there. The steps before it keep their results either way."""
        gate = self.ledger.pending_gate(run.id)
        if gate is None:
            raise ValueError("This run is not waiting for your OK.")
        self.ledger.decide_gate(gate, accepted=ok, note=note)
        rows = (
            self.ledger.session.query(StepRun).filter_by(run_id=run.id).order_by(StepRun.seq).all()
        )
        self._continue_numbering(run, rows)
        step = wf.step(gate.step_id)
        name = (step.title if step else None) or gate.step_id
        first = step is not None and asks_before(step)
        said = (
            ("You said OK to starting it." if first else "You said OK.")
            if ok
            else "You stopped the run here."
        ) + (f" You added: “{note}”" if note else "")
        note_on = next((r for r in reversed(rows) if r.id == gate.step_run_id), None)
        if not ok:
            self.ledger.decision(
                run, note_on, gate.step_id, kind="control", text=said, reason=f"After “{name}”."
            )
            self.ledger.finish_run(
                run,
                status="stopped",
                outputs=run.outputs,
                spent_usd=run.spent_usd or 0.0,
                spent_minutes=run.spent_minutes or 0.0,
            )
            return RunResult(
                run_id=run.id,
                status="stopped",
                state={"steps": self._read_back(wf, rows)},
                outputs=run.outputs,
                trace=[],
                decisions=[],
                spent_usd=run.spent_usd or 0.0,
                spent_minutes=run.spent_minutes or 0.0,
            )
        steps = self._read_back(wf, rows)
        run.status, run.finished_at, run.error = "running", None, None
        self.ledger._commit()
        b = wf.spec.budget
        budget = BudgetTracker(b.max_usd if b else None, b.max_minutes if b else None)
        budget.spent_usd = run.spent_usd or 0.0
        with session_span(
            "run", name=wf.metadata.name, title=run.title, run_id=run.id, mode=run.mode
        ):
            return self._walk(
                run,
                wf,
                dict(run.inputs or {}),
                run.mode,
                budget=budget,
                depth=run.depth or 0,
                resume=_Resume(
                    steps=steps,
                    # asked before it started: the run carries on into it
                    start_at=wf.step_index(gate.step_id) + (0 if first else 1),
                    spent_usd=run.spent_usd or 0.0,
                    note_on=note_on,
                    note_step_id=gate.step_id,
                    said=said,
                    why=(
                        f"Your answer before “{name}”. It started from here."
                        if first
                        else f"Your answer after “{name}”. The run carried on from here."
                    ),
                    approved=gate.step_id if first else None,
                ),
            )

    def _decide(self, ctx: _Ctx, sr: StepRun | None, step_id: str, **kw: Any) -> None:
        d = self.ledger.decision(ctx.run, sr, step_id, **kw)
        ctx.result.decisions.append(
            {
                "seq": d.seq,
                "step_id": step_id,
                "kind": d.kind,
                "text": d.text,
                "reason": d.reason,
                "alternatives": d.alternatives,
                "finding_id": d.finding_id,
                "field": d.field,
                "value": d.value,
            }
        )

    def _prepare_inputs(self, wf: Workflow, inputs: dict[str, Any]) -> dict[str, Any]:
        out = dict(inputs)
        for name, spec in wf.spec.inputs.items():
            if name not in out and spec.default is not None:
                out[name] = spec.default
            if spec.required and name not in out:
                raise ActivityError(f"input “{name}” is required")
            v = out.get(name)
            if spec.min_length is not None and isinstance(v, str) and len(v) < spec.min_length:
                raise ActivityError(f"input “{name}” must be at least {spec.min_length} characters")
        return out

    def _workflow_outputs(self, wf: Workflow, state: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, expr in wf.spec.outputs.items():
            m = _TEMPLATE.fullmatch(expr.strip())
            try:
                out[name] = render(expr, state) if m else expr
            except (ExprError, EvalError):
                out[name] = None
        return out


def why_it_stopped(result: RunResult) -> str:
    """Why a run that did not finish stopped, in the words its own record uses: the step
    that broke and why, or the limit it reached. A follow-up that fails is only as
    useful to its parent as this line."""
    if result.error:
        return result.error
    if result.status == "failed":
        broke = next((t.step_id for t in reversed(result.trace) if t.event == "failed"), None)
        said = next(
            (
                d
                for d in reversed(result.decisions)
                if d["step_id"] == broke
                and d["kind"] == "control"
                and d["text"].endswith("could not finish.")
            ),
            None,
        )
        if said:
            return f"{said['text'].removesuffix('.')}: {said['reason']}"
    if result.status == "paused_budget":
        said = next(
            (d for d in reversed(result.decisions) if d["text"].startswith("Paused before")), None
        )
        if said:
            return said["text"].removesuffix(".")
    if result.status == "waiting":
        return "it is waiting for someone to answer"
    return f"it ended with status {result.status}"


def _changed_steps(then: WorkflowVersion | None, wf: Workflow, ids: set[str]) -> list[str]:
    """The titles of the steps among ``ids`` whose definition differs from ``then``."""
    if then is None:
        return []
    try:
        before = Workflow.model_validate(yaml.safe_load(then.definition_yaml))
    except Exception:  # noqa: BLE001 - an old record that no longer reads says nothing
        return []
    out = []
    for s in wf.spec.steps:
        if s.id not in ids:
            continue
        old = before.step(s.id)
        if old is None or old.model_dump(by_alias=True) != s.model_dump(by_alias=True):
            out.append(s.title or s.id)
    return out


def asks_before(step: Step) -> bool:
    """A step that starts follow-up research is asked about before it starts, since what
    it would start is what the OK is about, and afterwards the money is spent."""
    return step.kind == "subworkflow"


def stops_for_ok(step: Step) -> bool:
    """Whether a step can stop a run for someone's OK at all. A wait already waits for a
    person, and a check is a mechanical test whose result is shown, not approved."""
    return step.kind not in ("wait", "check")


def fingerprint(ws: Workspace, wf: Workflow, step: Step) -> str:
    """What drives the step, as far as its trust is concerned: a change to any of it
    starts the count of OKs again. ``reset_on`` names the parts; by default all."""
    trust = wf.trust_for(step)
    parts = (trust.reset_on if trust and trust.reset_on else None) or [
        "skill",
        "model",
        "tools",
        "input_schema",
        "output_schema",
    ]
    skill = ws.load_skill(step.skill) if step.skill else None
    seen = {
        "skill": [step.skill, hashlib.sha256((skill.body if skill else "").encode()).hexdigest()],
        "model": wf.model_for(step),
        "tools": sorted(step.tools or {}),
        "input_schema": step.input,
        "output_schema": step.output.schema_ if step.output else None,
    }
    picked = {k: seen.get(k) for k in sorted(parts)}
    return hashlib.sha256(json.dumps(picked, sort_keys=True, default=str).encode()).hexdigest()[:32]


class _Resume:
    """Where a run picks up again: the results it already has, the step it starts at,
    what it had spent, and what happened, in words, for the run's record: the answer to
    a wait, or the retry of a step that broke. The note is kept against ``note_on``,
    the step record it is about, or against the step alone when there is none."""

    def __init__(
        self,
        *,
        steps: dict[str, Any],
        start_at: int,
        spent_usd: float,
        note_on: StepRun | None,
        note_step_id: str,
        said: str,
        why: str,
        approved: str | None = None,
    ):
        self.steps, self.start_at, self.spent_usd = steps, start_at, spent_usd
        self.note_on, self.note_step_id, self.said, self.why = note_on, note_step_id, said, why
        # a step someone just said OK to starting: it starts without asking again
        self.approved = approved


@dataclass
class _Ctx:
    wf: Workflow
    run: Run
    mode: str
    state: dict[str, Any]
    findings: list[Finding]
    budget: BudgetTracker
    result: RunResult
    depth: int
    own_spend: float = 0.0
    approved: str | None = None

    def spend(self, usd: float) -> None:
        self.own_spend += usd
        self.budget.add(usd)


def state_summary(state: dict[str, Any]) -> str:
    return json.dumps(
        {
            k: (v.get("status") if isinstance(v, dict) else v)
            for k, v in state.get("steps", {}).items()
        }
    )
