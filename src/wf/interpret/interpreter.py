"""The interpreter walks a validated definition and executes it through activities.

It is deterministic on this side of the activity boundary: given the same
definition, inputs and recorded step outputs it makes the same sequence of
control-flow decisions. Nothing here calls a model or the network directly.

In ``dry`` mode an unanswered required field does not fail the run. The interpreter
asks the guesser, records a ``guess`` decision linked to the finding, and continues.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

from wf.activities import Activities, ActivityError, ModelRequest, ToolSpec, run_with_policy
from wf.expr import EvalError, ExprError, render
from wf.expr.template import _TEMPLATE
from wf.schema import Mode, Step, Workflow, Workspace
from wf.store import repo
from wf.store.ledger import Ledger
from wf.store.records import Run, StepRun
from wf.validate import Finding, validate

from .context import BudgetTracker, OpenFindings, TraceEvent
from .registry import ARTIFACT_TOOLS, CHECKS, TOOLS, RunnerContext

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
        the id can be handed back before the work starts)."""
        if findings is None:
            findings = validate(wf, self.ws).findings
        open_findings = [f for f in findings if f.status == "open"]
        if mode == "live" and open_findings:
            raise OpenFindings(open_findings)
        if budget is None:
            b = wf.spec.budget
            budget = BudgetTracker(b.max_usd if b else None, b.max_minutes if b else None)
        for f in open_findings:
            self.ledger.finding(run, f)
        if expectation:
            self.ledger.expectations(run, expectation)

        state: dict[str, Any] = {"inputs": inputs, "steps": {}}
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
        )

        status = "done"
        error: str | None = None
        try:
            for step in wf.spec.steps:
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
        model = step.model
        if model is None:
            f = self._finding(ctx, step, "model")
            model = str(self._guess(ctx, step, f, sr=sr).value) if f else "claude-sonnet-5"
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
            "instruction_commit": repo.file_commit(self.ws.root, skill.path) if skill else None,
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
        if step.run in ARTIFACT_TOOLS and isinstance(out, dict):
            key, media = ARTIFACT_TOOLS[step.run]
            path = out.get(key)
            if path:
                simulated = bool(out.get("simulated", ctx.mode != "live"))
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
            raise ActivityError(f"follow-up run “{title}” ended with status {child.status}")
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
            return {"waited": False, "deadline": deadline, "on_timeout": on_timeout}, {}
        self._decide(
            ctx,
            sr,
            step.id,
            kind="control",
            text=f"Waiting for “{step.title or step.id}” (up to {deadline}).",
            reason="Human waits are not automated in this phase, so the run stops here.",
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
