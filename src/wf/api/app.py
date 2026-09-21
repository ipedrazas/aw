"""FastAPI application: JSON API under /api plus the server-rendered pages."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from wf.activities import ActivityPolicy, AnthropicModel, ModelActivity, default_activities
from wf.activities.fake import FakeModel
from wf.audit import Auditor, AuditResult, Change, group_questions
from wf.audit.chat import chat
from wf.dryrun import DryRunner
from wf.interpret import OpenFindings, RunConfig
from wf.schema import Workspace, WorkspaceError, dump_workflow
from wf.store import Artifact, Database
from wf.store import repo as gitrepo
from wf.validate import validate

from .audits import AuditStore
from .plain import plain_steps, plain_summary

WEB = Path(__file__).resolve().parents[1] / "web"


class AppState:
    def __init__(self, ws: Workspace, db: Database, model: ModelActivity, artifacts_dir: Path):
        self.ws = ws
        self.db = db
        self.model = model
        self.auditor = Auditor(
            ws, model, extraction_model=os.environ.get("WF_EXTRACTION_MODEL", "claude-opus-5")
        )
        acts = default_activities(ws, model, model_guesses=not isinstance(model, FakeModel))
        if isinstance(model, FakeModel):
            acts.policy = ActivityPolicy(retries=0)
        self.runner = DryRunner(ws, acts, db, RunConfig(artifacts_dir=artifacts_dir))
        self.audits = AuditStore(db)
        self.chat_model = os.environ.get("WF_CHAT_MODEL", "claude-sonnet-5")
        self.author = (
            os.environ.get("WF_AUTHOR_NAME", "Workflow UI"),
            os.environ.get("WF_AUTHOR_EMAIL", "ui@localhost"),
        )


def create_app(state: AppState | None = None) -> FastAPI:
    if state is None:
        ws = Workspace(os.environ.get("WF_WORKSPACE", "workspace"))
        model: ModelActivity = FakeModel() if os.environ.get("WF_FAKE_MODEL") else AnthropicModel()
        state = AppState(
            ws, Database(), model, Path(os.environ.get("WF_ARTIFACTS_DIR", "var/artifacts"))
        )

    app = FastAPI(title="Agentic workflows", version="0.1.0")
    app.state.wf = state
    templates = Jinja2Templates(directory=str(WEB / "templates"))
    if (WEB / "static").is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB / "static")), name="static")

    def st() -> AppState:
        return app.state.wf

    # -- health ------------------------------------------------------------

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "workspace": str(st().ws.root),
            "offline_model": isinstance(st().model, FakeModel),
        }

    # -- workflows ------------------------------------------------------------

    @app.get("/api/workflows")
    def list_workflows() -> list[dict[str, Any]]:
        out = []
        for name in st().ws.list_definitions():
            try:
                wf = st().ws.load_definition(name)
            except WorkspaceError as e:
                out.append({"name": name, "error": str(e)})
                continue
            result = validate(wf, st().ws)
            out.append(
                {
                    **plain_summary(wf),
                    "open_findings": len([f for f in result.findings if f.status == "open"]),
                }
            )
        return out

    @app.get("/api/workflows/{name}")
    def get_workflow(name: str) -> dict[str, Any]:
        wf = _load(st(), name)
        result = validate(wf, st().ws)
        return {
            "summary": plain_summary(wf),
            "steps": plain_steps(wf),
            "yaml": dump_workflow(wf),
            "findings": [f.model_dump(mode="json") for f in result.ordered()],
            "questions": _questions(result.findings),
            "cases": st().ws.list_cases(),
        }

    @app.post("/api/workflows/{name}/runs")
    def start_run(name: str, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        wf = _load(st(), name)
        mode = body.get("mode", "dry")
        case = body.get("case")
        inputs = dict(body.get("inputs") or {})
        expectation = None
        title = None
        if case:
            data = st().ws.load_yaml(f"cases/{case}.case.yaml")
            if not data:
                raise HTTPException(404, f"no case named {case}")
            inputs = {**dict(data.get("inputs", {})), **inputs}
            expectation = list(data.get("expectation", []))
            title = data.get("title")
        findings = validate(wf, st().ws).findings
        if mode == "live" and any(f.status == "open" for f in findings):
            raise HTTPException(
                409, "This workflow still has open questions. Answer them or run it as a dry run."
            )
        run_id = _start_in_background(st(), wf, inputs, mode, expectation, case, title, findings)
        return {"run_id": run_id, "status": "running"}

    # -- audits --------------------------------------------------------------

    @app.get("/api/audits")
    def list_audits() -> list[dict[str, Any]]:
        return st().audits.list()

    @app.post("/api/audits")
    def create_audit(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        document = str(body.get("document") or "").strip()
        if len(document) < 20:
            raise HTTPException(
                400, "Describe the process in a few sentences, or paste the document."
            )
        name = body.get("name") or None
        try:
            result = st().auditor.audit(document, name=name)
        except Exception as e:  # noqa: BLE001 - surface the reason to the UI
            raise HTTPException(502, f"The draft could not be made: {e}") from e
        audit_id = st().audits.create(result, document)
        return _audit_view(st(), audit_id)

    @app.get("/api/audits/{audit_id}")
    def get_audit(audit_id: str) -> dict[str, Any]:
        return _audit_view(st(), audit_id)

    @app.post("/api/audits/{audit_id}/answer")
    def answer(audit_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        result, _rec = _audit(st(), audit_id)
        fid = body.get("finding_id")
        if not any(f.id == fid for f in result.findings):
            raise HTTPException(404, "That question is not on this draft any more.")
        changes = st().auditor.answer(result, fid, body.get("answer"))
        st().audits.save(audit_id, result, changes, by="answer")
        return _audit_view(st(), audit_id)

    @app.post("/api/audits/{audit_id}/edit")
    def edit(audit_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        result, _rec = _audit(st(), audit_id)
        ch = st().auditor.set_field(
            result,
            str(body["path"]),
            body.get("value"),
            str(body.get("reason") or "You changed this."),
        )
        st().audits.save(audit_id, result, [ch], by="user")
        return _audit_view(st(), audit_id)

    @app.post("/api/audits/{audit_id}/undo")
    def undo(audit_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        result, _rec = _audit(st(), audit_id)
        undone = st().audits.mark_undone(audit_id, int(body["seq"]))
        if undone is None:
            raise HTTPException(404, "Nothing to undo there.")
        if "#" in undone["path"]:
            raise HTTPException(400, "Changes to an output shape cannot be undone from here yet.")
        st().auditor.set_field(
            result, undone["path"], undone["before"], f"Undid: {undone['reason']}"
        )
        result.changes = [
            c
            for c in result.changes
            if not (c.path == undone["path"] and c.after == undone["after"])
        ]
        st().audits.save(audit_id, result, [], by="user")
        return _audit_view(st(), audit_id)

    @app.post("/api/audits/{audit_id}/chat")
    def chat_turn(audit_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        result, rec = _audit(st(), audit_id)
        message = str(body.get("message") or "").strip()
        if not message:
            raise HTTPException(400, "Say something first.")
        history = st().audits.chat_history(rec)
        try:
            outcome = chat(st().model, result, history, message, model_name=st().chat_model)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"The chat could not answer: {e}") from e
        applied: list[Change] = []
        for e in outcome.edits:
            if not e.get("path"):
                continue
            try:
                applied.append(
                    st().auditor.set_field(
                        result, e["path"], e.get("value"), e.get("reason") or "From the chat."
                    )
                )
            except Exception:  # noqa: BLE001 - a bad path from the model is skipped, not fatal
                continue
        for a in outcome.answers:
            f = next(
                (x for x in result.findings if x.id == a.get("finding_id") and x.status == "open"),
                None,
            )
            if f is None:
                continue
            value: Any = None
            if (
                a.get("option_index") is not None
                and f.options
                and 0 <= int(a["option_index"]) < len(f.options)
            ):
                value = f.options[int(a["option_index"])].value
            elif a.get("text"):
                value = a["text"]
            if value is not None:
                applied.extend(st().auditor.answer(result, f.id, value))
        new_chat = [
            *(rec.chat or []),
            {"role": "user", "text": message},
            {
                "role": "assistant",
                "text": outcome.reply,
                "changes": [c.model_dump() for c in applied],
                "point_to_finding": outcome.point_to_finding,
            },
        ]
        st().audits.save(audit_id, result, applied, by="assistant", chat=new_chat)
        return _audit_view(st(), audit_id)

    @app.post("/api/audits/{audit_id}/save")
    def save(audit_id: str) -> dict[str, Any]:
        result, _rec = _audit(st(), audit_id)
        wf = result.workflow()
        st().ws.save_definition(wf)
        rels = [
            str(st().ws.definition_path(wf.metadata.name).relative_to(st().ws.root)),
            f"skills/{wf.metadata.name}",
            f"schemas/{wf.metadata.name}",
        ]
        rels = [r for r in rels if st().ws.exists(r)]
        commit = gitrepo.commit_paths(
            st().ws.root, rels, f"{result.title}: draft saved from the UI", *st().author
        )
        st().audits.save(audit_id, result, [], status="saved", commit=commit)
        return {"saved": True, "commit": commit, "workflow": wf.metadata.name}

    @app.post("/api/audits/{audit_id}/dry-run")
    def audit_dry_run(
        audit_id: str, body: dict[str, Any] = Body(default_factory=dict)
    ) -> dict[str, Any]:
        result, _rec = _audit(st(), audit_id)
        wf = result.workflow()
        st().ws.save_definition(wf)
        inputs = dict(body.get("inputs") or {})
        case = body.get("case")
        expectation = None
        title = None
        if case:
            data = st().ws.load_yaml(f"cases/{case}.case.yaml") or {}
            inputs = {**dict(data.get("inputs", {})), **inputs}
            expectation = list(data.get("expectation", []))
            title = data.get("title")
        if not inputs:
            first = next(iter(wf.spec.inputs), "topic")
            inputs = {first: body.get("topic") or "A topic to try the draft on, with enough words"}
        run_id = _start_in_background(
            st(), wf, inputs, "dry", expectation, case, title, result.findings
        )
        return {"run_id": run_id, "status": "running"}

    # -- runs ------------------------------------------------------------------

    @app.get("/api/runs")
    def list_runs(workflow: str | None = None) -> list[dict[str, Any]]:
        return st().runner.list_runs(workflow)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        try:
            report = st().runner.report(run_id)
        except KeyError as e:
            raise HTTPException(404, "No such run.") from e
        snap = st().runner.snapshot(run_id)
        return {**snap, "report": report.model_dump(mode="json")}

    @app.get("/api/runs/{run_a}/diff/{run_b}")
    def diff(run_a: str, run_b: str) -> dict[str, Any]:
        try:
            return st().runner.diff(run_a, run_b).model_dump(mode="json")
        except KeyError as e:
            raise HTTPException(404, "No such run.") from e

    @app.get("/api/runs/{run_id}/artifacts/{artifact_id}")
    def artifact(run_id: str, artifact_id: str) -> FileResponse:
        with st().db.session() as s:
            a = s.get(Artifact, artifact_id)
            if a is None or a.run_id != run_id:
                raise HTTPException(404, "No such artefact.")
            path, media, name = a.path, a.media_type, a.name
        if not Path(path).exists():
            raise HTTPException(410, "The artefact file is gone.")
        return FileResponse(path, media_type=media, filename=name)

    @app.get("/api/cases")
    def cases() -> list[dict[str, Any]]:
        out = []
        for c in st().ws.list_cases():
            data = st().ws.load_yaml(f"cases/{c}.case.yaml") or {}
            out.append(
                {
                    "name": c,
                    "title": data.get("title", c),
                    "inputs": data.get("inputs", {}),
                    "what_happened": data.get("what_happened", ""),
                }
            )
        return out

    @app.get("/api/process-docs")
    def process_docs() -> list[dict[str, Any]]:
        out = []
        for name in st().ws.list_process_docs():
            text = st().ws.path(f"process-docs/{name}").read_text()
            out.append(
                {"name": name, "text": text, "constructed": "Constructed example" in text[:200]}
            )
        return out

    # -- pages -------------------------------------------------------------------

    def page(request: Request, template: str, **ctx: Any) -> HTMLResponse:
        tpl = WEB / "templates" / f"{template}.html"
        if not tpl.exists():
            return HTMLResponse(
                f"<pre>Template {template}.html is not written yet. The JSON API is at /api.</pre>",
                status_code=200,
            )
        return templates.TemplateResponse(
            request, f"{template}.html", {"offline": isinstance(st().model, FakeModel), **ctx}
        )

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> Any:
        return RedirectResponse("/workflows")

    @app.get("/workflows", response_class=HTMLResponse)
    def workflows_page(request: Request) -> Any:
        return page(request, "workflows", workflows=list_workflows(), audits=st().audits.list())

    @app.get("/workflows/{name}", response_class=HTMLResponse)
    def workflow_page(request: Request, name: str) -> Any:
        return page(request, "workflow", data=get_workflow(name), name=name)

    @app.get("/audits/new", response_class=HTMLResponse)
    def new_audit_page(request: Request) -> Any:
        return page(request, "audit", audit=None, docs=process_docs(), cases=cases())

    @app.get("/audits/{audit_id}", response_class=HTMLResponse)
    def audit_page(request: Request, audit_id: str) -> Any:
        return page(request, "audit", audit=get_audit(audit_id), docs=process_docs(), cases=cases())

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request) -> Any:
        return page(request, "runs", runs=list_runs(None))

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: str) -> Any:
        return page(
            request,
            "run",
            run=get_run(run_id),
            others=[r for r in list_runs(None) if r["id"] != run_id],
        )

    @app.get("/runs/{run_a}/diff/{run_b}", response_class=HTMLResponse)
    def diff_page(request: Request, run_a: str, run_b: str) -> Any:
        return page(request, "diff", diff=diff(run_a, run_b), a=get_run(run_a), b=get_run(run_b))

    @app.exception_handler(OpenFindings)
    def _open_findings(_: Request, exc: OpenFindings) -> JSONResponse:
        return JSONResponse(
            {"detail": str(exc), "findings": [f.model_dump(mode="json") for f in exc.findings]},
            status_code=409,
        )

    return app


# -- helpers -----------------------------------------------------------------------


def _load(state: AppState, name: str):
    try:
        return state.ws.load_definition(name)
    except WorkspaceError as e:
        raise HTTPException(404, str(e)) from e


def _audit(state: AppState, audit_id: str) -> tuple[AuditResult, Any]:
    try:
        return state.audits.load(audit_id)
    except KeyError as e:
        raise HTTPException(404, "No such draft.") from e


def _questions(findings) -> list[dict[str, Any]]:
    return [
        {
            "type": g["type"],
            "title": g["title"],
            "findings": [f.model_dump(mode="json") for f in g["findings"]],
        }
        for g in group_questions([f for f in findings if f.status == "open"])
    ]


def _audit_view(state: AppState, audit_id: str) -> dict[str, Any]:
    result, rec = _audit(state, audit_id)
    wf = result.workflow()
    open_findings = result.open_findings()
    answered = [f for f in result.findings if f.status != "open"]
    return {
        "id": audit_id,
        "name": result.name,
        "title": result.title,
        "status": rec.status,
        "saved_commit": rec.saved_commit,
        "summary": plain_summary(wf),
        "steps": plain_steps(wf),
        "yaml": dump_workflow(wf),
        "questions": _questions(result.findings),
        "answered": [f.model_dump(mode="json") for f in answered],
        "counts": {
            "open": len(open_findings),
            "answered": len(answered),
            "total": len(result.findings),
        },
        "diff": result.diff(),
        "changes": state.audits.changes(audit_id),
        "chat": rec.chat or [],
        "explanations": result.explanations,
        "cases": state.ws.list_cases(),
        "runs": state.runner.list_runs(result.name),
    }


def _start_in_background(
    state: AppState, wf, inputs, mode, expectation, case, title, findings
) -> str:
    """Runs execute on a worker thread with their own ledger so the UI can poll."""
    from wf.store.ledger import Ledger

    ledger = Ledger(state.db)
    from wf.interpret import Interpreter

    interp = Interpreter(state.ws, state.runner.activities, ledger, state.runner.config)
    # create the run record synchronously so the id exists before we return
    inputs = interp._prepare_inputs(wf, inputs)
    commit = gitrepo.file_commit(
        state.ws.root, str(state.ws.definition_path(wf.metadata.name).relative_to(state.ws.root))
    )
    wv = ledger.workflow_version(wf, commit)
    b = wf.spec.budget
    from wf.interpret import BudgetTracker

    budget = BudgetTracker(b.max_usd if b else None, b.max_minutes if b else None)
    run = ledger.start_run(
        wv,
        inputs=inputs,
        mode=mode,
        depth=0,
        parent=None,
        parent_step_run=None,
        budget_usd=budget.max_usd,
        title=title or str(inputs.get("topic") or wf.metadata.name),
        case_name=case,
    )
    run_id = run.id

    def work() -> None:
        try:
            interp.run_existing(
                run, wf, inputs, mode, findings=findings, expectation=expectation, budget=budget
            )
            if expectation:
                state.runner._record_expectations(ledger, interp.last_result, expectation)
        except Exception as e:  # noqa: BLE001
            ledger.finish_run(
                run, status="failed", outputs=None, spent_usd=0.0, spent_minutes=0.0, error=str(e)
            )
        finally:
            ledger.close()

    threading.Thread(target=work, daemon=True, name=f"run-{run_id[:8]}").start()
    return run_id
