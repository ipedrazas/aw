"""FastAPI application: JSON API under /api plus the server-rendered pages."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from wf.activities import (
    ActivityPolicy,
    ModelActivity,
    default_activities,
    default_model,
    record_sessions,
)
from wf.activities.fake import FakeModel
from wf.activities.models import build_system
from wf.activities.safety import data_region
from wf.audit import AnswerRejected, Auditor, AuditResult, Change, fold_similar, group_questions
from wf.audit.chat import chat
from wf.audit.question import _set, answer_definition
from wf.audit.restore import restore_missing_files
from wf.dryrun import DryRunner
from wf.interpret import OpenFindings, RunConfig
from wf.logs import get_logger, setup_logging
from wf.schema import Workspace, WorkspaceError, dump_workflow, load_workflow_dict
from wf.settings import chat_model, provider, search_mode
from wf.startup import announce
from wf.store import Artifact, Database, Run
from wf.store import repo as gitrepo
from wf.store.sessions import SessionLog, session_log_mode
from wf.validate import validate

from .audits import AuditStore
from .plain import plain_steps, plain_summary
from .skills import SkillError, edit_step_skill, skill_url, skill_view
from .plain import model_choices, plain_steps, plain_summary

WEB = Path(__file__).resolve().parents[1] / "web"

logger = get_logger("wf.api")


class AppState:
    def __init__(self, ws: Workspace, db: Database, model: ModelActivity, artifacts_dir: Path):
        self.ws = ws
        self.db = db
        self.offline = isinstance(model, FakeModel)
        # One wrap, before anything else is handed the model: the auditor, the chat, the
        # steps and the guesser then all record into whichever session is open.
        self.model = record_sessions(model, db)
        self.sessions = SessionLog(db)
        self.auditor = Auditor(ws, self.model)
        acts = default_activities(ws, self.model, model_guesses=not self.offline)
        if self.offline:
            acts.policy = ActivityPolicy(retries=0)
        self.runner = DryRunner(ws, acts, db, RunConfig(artifacts_dir=artifacts_dir))
        self.audits = AuditStore(db)
        self.chat_model = chat_model()
        self.author = (
            os.environ.get("WF_AUTHOR_NAME", "Workflow UI"),
            os.environ.get("WF_AUTHOR_EMAIL", "ui@localhost"),
        )


def create_app(state: AppState | None = None) -> FastAPI:
    setup_logging()
    if state is None:
        ws = Workspace(os.environ.get("WF_WORKSPACE", "workspace"))
        model: ModelActivity = default_model()
        state = AppState(
            ws, Database(), model, Path(os.environ.get("WF_ARTIFACTS_DIR", "var/artifacts"))
        )
    # Before a single request: which models this process will ask for, and through whom.
    announce(state.ws, offline=state.offline)

    app = FastAPI(title="Agentic workflows", version="0.1.0")
    app.state.wf = state
    templates = Jinja2Templates(directory=str(WEB / "templates"))

    def static_url(name: str) -> str:
        """A static file's URL that changes when the file does, so a browser never keeps an old one."""
        f = WEB / "static" / name
        return f"/static/{name}?v={int(f.stat().st_mtime)}" if f.exists() else f"/static/{name}"

    templates.env.globals["static_url"] = static_url
    templates.env.globals["skill_url"] = skill_url
    if (WEB / "static").is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB / "static")), name="static")

    def st() -> AppState:
        return app.state.wf

    # -- health ------------------------------------------------------------

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        """The container asks for this every thirty seconds; see wf.logs for where it goes."""
        return {
            "ok": True,
            "workspace": str(st().ws.root),
            "offline_model": st().offline,
            # Which gateway this container is asking; a wrong key shows up here first.
            "provider": "offline" if st().offline else provider(),
            "sessions": session_log_mode(),
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
            _restore_files(st(), wf)
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
        _restore_files(st(), wf)
        result = validate(wf, st().ws)
        return {
            "summary": plain_summary(wf),
            "steps": plain_steps(wf),
            "models": model_choices(wf),
            "yaml": dump_workflow(wf),
            "findings": [f.model_dump(mode="json") for f in result.ordered()],
            "questions": _questions(result.findings, wf),
            "cases": st().ws.list_cases(),
        }

    @app.post("/api/workflows/{name}/answer")
    def answer_workflow(name: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Answer an open question on a saved workflow. The answer goes into the definition,
        is committed, and is carried to the draft it was saved from, so the two agree."""
        wf = _load(st(), name)
        _restore_files(st(), wf)
        fid = body.get("finding_id")
        still_open = {f.id: f for f in validate(wf, st().ws).findings if f.status == "open"}
        finding = still_open.get(fid)
        if finding is None:
            raise HTTPException(404, "That question is not open on this workflow any more.")
        try:
            new_def, changes = answer_definition(
                wf.model_dump(by_alias=True, exclude_none=True),
                finding,
                body.get("answer"),
                st().ws,
            )
        except AnswerRejected as e:
            raise HTTPException(400, str(e)) from e
        # the same answer to the same question asked of other steps
        not_taken = []
        for other in [
            still_open[x] for x in body.get("also") or [] if x in still_open and x != fid
        ]:
            try:
                new_def, more = answer_definition(new_def, other, body.get("answer"), st().ws)
                changes += more
            except AnswerRejected as e:
                not_taken.append(str(e))
        new_wf = load_workflow_dict(new_def)
        st().ws.save_definition(new_wf)
        rel = str(st().ws.definition_path(name).relative_to(st().ws.root))
        # an answer can rewrite a step's output shape as well as the definition
        rels = [rel] + [r for r in (f"schemas/{name}", f"skills/{name}") if st().ws.exists(r)]
        commit = gitrepo.commit_paths(
            st().ws.root, rels, f"{name}: answered “{finding.question}”", *st().author
        )
        drafts = _carry_to_drafts(st(), name, changes)
        logger.info(
            "workflow %s: answered %s",
            name,
            finding.field,
            extra={
                "fields": {
                    "event": "workflow.answered",
                    "workflow": name,
                    "field": finding.field,
                    "commit": commit,
                    "drafts": drafts,
                }
            },
        )
        return {**get_workflow(name), "commit": commit, "drafts": drafts, "not_taken": not_taken}

    @app.post("/api/workflows/{name}/model")
    def set_model(name: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Choose the model one agent step runs on, or the workflow's default.

        ``{"step": id, "model": m}`` sets a step's own model; ``"model": null`` puts it
        back on the default. ``{"step": null, "model": m}`` sets the default. Only the
        models on the page's list are taken: the ones this deployment configured, and
        any this workflow already names."""
        wf = _load(st(), name)
        sid, model = body.get("step"), body.get("model") or None
        allowed = {c["value"] for c in model_choices(wf)}
        if model is not None and model not in allowed:
            raise HTTPException(
                400, f"“{model}” is not one of the models this deployment runs, so nothing changed."
            )
        if sid:
            step = wf.step(sid)
            if step is None:
                raise HTTPException(404, f"There is no step “{sid}” in this workflow.")
            if step.kind != "agent":
                raise HTTPException(400, f"“{step.title or sid}” does not use a model.")
            path, before, what = f"steps.{sid}.model", step.model, f"“{step.title or sid}”"
            if model is None and wf.spec.defaults.model is None:
                raise HTTPException(
                    400, "This workflow has no default model, so the step has to name one."
                )
        else:
            path, before, what = "spec.defaults.model", wf.spec.defaults.model, "the default"
        if model == before:
            return {**get_workflow(name), "commit": None, "drafts": 0}
        defn = wf.model_dump(by_alias=True, exclude_none=True)
        _set(defn, path, model)
        st().ws.save_definition(load_workflow_dict(defn))
        rel = str(st().ws.definition_path(name).relative_to(st().ws.root))
        said = f"{what} runs on {model}" if model else f"{what} runs on the default"
        commit = gitrepo.commit_paths(st().ws.root, [rel], f"{name}: {said}", *st().author)
        change = Change(
            path=path, before=before, after=model, reason="Chosen on the workflow page."
        )
        drafts = _carry_to_drafts(st(), name, [change])
        logger.info(
            "workflow %s: %s set to %s",
            name,
            path,
            model,
            extra={
                "fields": {
                    "event": "workflow.model_set",
                    "workflow": name,
                    "field": path,
                    "model": model,
                    "commit": commit,
                    "drafts": drafts,
                }
            },
        )
        return {**get_workflow(name), "commit": commit, "drafts": drafts}

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
        _restore_files(st(), wf)
        findings = validate(wf, st().ws).findings
        if mode == "live":
            _refuse_while_open(
                findings,
                "Answer them on the workflow page, or run it as a dry run, which guesses instead.",
            )
        run_id = _start_in_background(st(), wf, inputs, mode, expectation, case, title, findings)
        return {"run_id": run_id, "status": "running"}

    @app.delete("/api/workflows/{name}")
    def delete_workflow(name: str) -> dict[str, Any]:
        """Remove the definition. Its runs stay: they are what happened, not what is live."""
        try:
            removed = st().ws.delete_definition(name)
        except WorkspaceError as e:
            raise HTTPException(404, str(e)) from e
        commit = gitrepo.commit_paths(
            st().ws.root, removed, f"{name}: deleted from the UI", *st().author
        )
        with st().db.session() as s:
            runs_kept = s.query(Run).filter_by(workflow_name=name).count()
        logger.info(
            "workflow %s deleted, %d run(s) kept",
            name,
            runs_kept,
            extra={
                "fields": {
                    "event": "workflow.deleted",
                    "workflow": name,
                    "files": len(removed),
                    "runs_kept": runs_kept,
                }
            },
        )
        return {"deleted": name, "removed": removed, "commit": commit, "runs_kept": runs_kept}

    @app.post("/api/workflows/{name}/rename")
    def rename_workflow(name: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Rename the definition. Past runs keep saying the old name: they are what happened."""
        new_name = str(body.get("name") or "").strip()
        if not new_name:
            raise HTTPException(400, "Give it a new name.")
        try:
            changed = st().ws.rename_definition(name, new_name)
        except WorkspaceError as e:
            raise HTTPException(404 if "no definition named" in str(e) else 400, str(e)) from e
        commit = gitrepo.commit_paths(
            st().ws.root, changed, f"{name}: renamed to {new_name} from the UI", *st().author
        )
        drafts = st().audits.rename_workflow(name, new_name)
        logger.info(
            "workflow %s renamed to %s",
            name,
            new_name,
            extra={
                "fields": {
                    "event": "workflow.renamed",
                    "workflow": name,
                    "new_name": new_name,
                    "files": len(changed),
                    "drafts": drafts,
                }
            },
        )
        return {
            "renamed": name,
            "name": new_name,
            "changed": changed,
            "commit": commit,
            "drafts": drafts,
        }

    # -- instruction files ------------------------------------------------------

    @app.get("/api/skill")
    def get_skill(
        path: str,
        v: int | None = None,
        a: int | None = None,
        b: int | None = None,
        workflow: str | None = None,
        step: str | None = None,
        sha256: str | None = None,
    ) -> dict[str, Any]:
        """An instruction file: one version of it, every version it has had, and which
        steps pin which. With ``workflow`` and ``step``, what editing it from there does."""
        try:
            return skill_view(
                st().ws, path, version=v, a=a, b=b, workflow=workflow, step=step, sha256=sha256
            )
        except SkillError as e:
            raise HTTPException(e.status, str(e)) from e

    @app.post("/api/workflows/{name}/steps/{step_id}/skill")
    def edit_skill(name: str, step_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Save edited instructions as a new version and pin the step to it. The version
        it replaces is kept, so a past run, and any other step that pins it, still reads
        exactly what it read. The change is committed and carried to the drafts."""
        _load(st(), name)
        latest = body.get("latest")
        try:
            result, written, change = edit_step_skill(
                st().ws,
                name,
                step_id,
                str(body.get("body") or ""),
                int(latest) if latest is not None else None,
            )
        except SkillError as e:
            raise HTTPException(e.status, str(e)) from e
        commit = gitrepo.commit_paths(
            st().ws.root,
            written,
            f"{name}: edited the instructions for “{step_id}”, now version {result['version']}",
            *st().author,
        )
        drafts = _carry_to_drafts(st(), name, [change])
        logger.info(
            "workflow %s: instructions for %s are now %s",
            name,
            step_id,
            result["skill"],
            extra={
                "fields": {
                    "event": "workflow.skill_edited",
                    "workflow": name,
                    "step": step_id,
                    "skill": result["skill"],
                    "before": result["before"],
                    "commit": commit,
                    "drafts": drafts,
                }
            },
        )
        return {**result, "commit": commit, "drafts": drafts}

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
        st().sessions.link_audit(result.session_id, audit_id)
        logger.info(
            "drafted %s from a document of %d characters",
            result.name,
            len(document),
            extra={"fields": {"event": "audit.created", "audit": audit_id, "name": result.name}},
        )
        return _audit_view(st(), audit_id)

    @app.get("/api/audits/{audit_id}")
    def get_audit(audit_id: str) -> dict[str, Any]:
        result, _rec = _audit(st(), audit_id)
        if st().auditor.restore_files(result):
            # files the draft's steps name had gone; written again, the questions
            # about them no longer apply
            st().auditor.revalidate(result)
            st().audits.save(audit_id, result)
        return _audit_view(st(), audit_id)

    @app.delete("/api/audits/{audit_id}")
    def delete_audit(audit_id: str) -> dict[str, Any]:
        """Remove a draft. A workflow it was already saved as is left where it is."""
        if not st().audits.delete(audit_id):
            raise HTTPException(404, "No such draft.")
        logger.info(
            "draft %s deleted",
            audit_id[:8],
            extra={"fields": {"event": "audit.deleted", "audit": audit_id}},
        )
        return {"deleted": audit_id}

    @app.post("/api/audits/{audit_id}/answer")
    def answer(audit_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        result, _rec = _audit(st(), audit_id)
        fid = body.get("finding_id")
        if not any(f.id == fid for f in result.findings):
            raise HTTPException(404, "That question is not on this draft any more.")
        try:
            changes = st().auditor.answer(result, fid, body.get("answer"))
        except AnswerRejected as e:
            raise HTTPException(400, str(e)) from e
        # the same answer to the same question asked of other steps, one change each so
        # any of them can be undone on its own; one that does not take it stays open
        not_taken = []
        for other in [x for x in body.get("also") or [] if x != fid]:
            f = next((x for x in result.findings if x.id == other and x.status == "open"), None)
            if f is None:
                continue
            try:
                changes += st().auditor.answer(result, other, body.get("answer"))
            except AnswerRejected as e:
                not_taken.append(str(e))
        st().audits.save(audit_id, result, changes, by="answer")
        return {**_audit_view(st(), audit_id), "not_taken": not_taken}

    @app.post("/api/audits/{audit_id}/edit")
    def edit(audit_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        result, _rec = _audit(st(), audit_id)
        try:
            ch = st().auditor.set_field(
                result,
                str(body["path"]),
                body.get("value"),
                str(body.get("reason") or "You changed this."),
            )
        except AnswerRejected as e:
            raise HTTPException(400, str(e)) from e
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
            outcome = chat(
                st().model,
                result,
                history,
                message,
                model_name=st().chat_model,
                audit_id=audit_id,
                about=body.get("about") or None,
            )
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"The chat could not answer: {e}") from e
        # The model takes a while; the person may have answered a question meanwhile.
        # Its turn goes onto the draft as it is now, not as it was when it was asked.
        result, rec = _audit(st(), audit_id)
        applied: list[Change] = []
        refused: list[str] = []
        for edit in outcome.edits:
            if not edit.get("path"):
                continue
            try:
                applied.append(
                    st().auditor.set_field(
                        result,
                        edit["path"],
                        edit.get("value"),
                        edit.get("reason") or "From the chat.",
                    )
                )
            except AnswerRejected as e:
                refused.append(str(e))
            except Exception:  # noqa: BLE001 - a bad path from the model is not fatal, but it is said
                refused.append(
                    f"I could not make the change “{edit.get('reason') or edit['path']}”, "
                    "so that part of the draft is unchanged."
                )
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
                try:
                    applied.extend(st().auditor.answer(result, f.id, value))
                except AnswerRejected as e:
                    # the question stays open and the person is told why
                    refused.append(str(e))
        closed = [
            {"finding_id": f.id, "question": f.question, "reason": f.answer}
            for d in outcome.dismiss
            if (
                f := st().auditor.dismiss(
                    result, str(d.get("finding_id")), str(d.get("reason") or "")
                )
            )
        ]
        reply = "\n\n".join([outcome.reply, *refused]) if refused else outcome.reply
        new_chat = [
            *(rec.chat or []),
            {"role": "user", "text": message},
            {
                "role": "assistant",
                "text": reply,
                "changes": [c.model_dump() for c in applied],
                "closed": closed,
                "point_to_finding": outcome.point_to_finding,
            },
        ]
        st().audits.save(audit_id, result, applied, by="assistant", chat=new_chat)
        return _audit_view(st(), audit_id)

    @app.post("/api/audits/{audit_id}/save")
    def save(audit_id: str) -> dict[str, Any]:
        result, _rec = _audit(st(), audit_id)
        st().auditor.revalidate(result)  # puts right what a routine gives back, among others
        wf = result.workflow()
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

    @app.post("/api/runs/{run_id}/answer")
    def answer_wait(run_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Answer the step a real run is waiting at, and carry the run on from there."""
        with st().db.session() as s:
            run = s.get(Run, run_id)
            if run is None:
                raise HTTPException(404, "No such run.")
            if run.status != "waiting":
                raise HTTPException(409, "This run is not waiting for anyone.")
            name = run.workflow_name
        wf = _load(st(), name)
        _restore_files(st(), wf)
        _refuse_while_open(
            validate(wf, st().ws).findings,
            "Answer them on the workflow page, then answer this again.",
        )
        topics = body.get("topics") or []
        if isinstance(topics, str):
            topics = [t.strip(" -*\t") for t in topics.splitlines()]
        answer = {
            "go_deeper": bool(body.get("go_deeper")),
            "topics": [str(t).strip() for t in topics if str(t).strip()],
            "note": str(body.get("note") or "").strip(),
        }
        if answer["go_deeper"] and not answer["topics"]:
            raise HTTPException(400, "Say what to go deeper into: one topic per line.")
        _resume_in_background(st(), run_id, wf, answer)
        return {"run_id": run_id, "status": "running"}

    @app.get("/api/runs")
    def list_runs(workflow: str | None = None) -> list[dict[str, Any]]:
        return st().runner.list_runs(workflow)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        try:
            snap = st().runner.snapshot(run_id)
        except KeyError as e:
            raise HTTPException(404, "No such run.") from e
        # One read of the run, used for both: a poll that sees "done" sees every
        # step's guesses with it, even if the last one landed a moment ago.
        report = st().runner.report(run_id, snap=snap)
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

    # -- sessions ----------------------------------------------------------------

    @app.get("/api/sessions")
    def list_sessions(
        run: str | None = None,
        audit: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """The agentic sessions, newest first: what the models were asked, and by whom."""
        return st().sessions.list(
            run_id=run, audit_id=audit, kind=kind, limit=max(1, min(limit, 200))
        )

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str, bodies: bool = True) -> dict[str, Any]:
        """One session with every exchange under it. `bodies=false` leaves the prompts out."""
        try:
            return st().sessions.get(session_id, bodies=bodies)
        except KeyError as e:
            raise HTTPException(404, "No such session.") from e

    @app.get("/api/runs/{run_id}/sessions")
    def run_sessions(run_id: str) -> list[dict[str, Any]]:
        return st().sessions.list(run_id=run_id)

    # -- pages -------------------------------------------------------------------

    def page(request: Request, template: str, **ctx: Any) -> HTMLResponse:
        tpl = WEB / "templates" / f"{template}.html"
        if not tpl.exists():
            return HTMLResponse(
                f"<pre>Template {template}.html is not written yet. The JSON API is at /api.</pre>",
                status_code=200,
            )
        return templates.TemplateResponse(
            request,
            f"{template}.html",
            {"offline": st().offline, "search_live": search_mode() == "exa", **ctx},
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

    @app.get("/skill", response_class=HTMLResponse)
    def skill_page(
        request: Request,
        path: str,
        v: int | None = None,
        a: int | None = None,
        b: int | None = None,
        workflow: str | None = None,
        step: str | None = None,
        sha256: str | None = None,
    ) -> Any:
        here = {"path": path, "workflow": workflow, "step": step}

        def link(extra: dict[str, Any]) -> str:
            """This page, for the same file and step, at another version or comparison."""
            q = {k: v for k, v in {**here, **extra}.items() if v is not None}
            return "/skill?" + urlencode(q)

        return page(
            request,
            "skill",
            skill=get_skill(path, v, a, b, workflow, step, sha256),
            here={k: v for k, v in here.items() if v is not None},
            link=link,
        )

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
        run = get_run(run_id)
        calls = _calls_by_step(st(), run_id, _tools_by_step(st(), run["workflow"]))
        for s in run["steps"]:
            s["calls"] = calls.get(s["step_id"], [])
        return page(
            request,
            "run",
            run=run,
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


def _carry_to_drafts(state: AppState, name: str, changes: list[Change]) -> int:
    """Make the same edits to the drafts saved as this workflow, so the draft does not
    say one thing and the workflow another. A draft the edit no longer fits is left as it
    is and logged; the workflow is what runs."""
    carried = 0
    for d in state.audits.list():
        if d.get("name") != name:
            continue
        try:
            result, _rec = state.audits.load(d["id"])
            applied = [state.auditor.set_field(result, c.path, c.after, c.reason) for c in changes]
            state.audits.save(d["id"], result, applied, by="answer")
            carried += 1
        except Exception as e:  # noqa: BLE001 - the workflow already took it; the draft is secondary
            logger.warning(
                "draft %s did not take the answer given on %s: %s",
                d["id"],
                name,
                e,
                extra={"fields": {"event": "workflow.answer_not_carried", "audit": d["id"]}},
            )
    return carried


def _restore_files(state: AppState, wf: Any) -> None:
    """Write back any file a step names that is missing, and commit it, so the missing
    file never becomes a question somebody has to answer."""
    written = restore_missing_files(wf, state.ws)
    if not written:
        return
    commit = gitrepo.commit_paths(
        state.ws.root, written, f"{wf.metadata.name}: restored files its steps name", *state.author
    )
    logger.info(
        "restored %d files %s names",
        len(written),
        wf.metadata.name,
        extra={
            "fields": {
                "event": "workflow.files_restored",
                "workflow": wf.metadata.name,
                "files": written,
                "commit": commit,
            }
        },
    )


def _tools_by_step(state: AppState, name: str) -> dict[str, list[str]]:
    """The tools each step was offered, under the names the model saw them by."""
    try:
        wf = state.ws.load_definition(name)
    except WorkspaceError:
        return {}
    seen = {"search": "search", "get_contents": "get_contents", "fetch": "get_contents"}
    return {
        s.id: [seen[t.split(".")[-1]] for t in (s.tools or {}) if t.split(".")[-1] in seen]
        for s in wf.spec.steps
    }


def _calls_by_step(
    state: AppState, run_id: str, tools: dict[str, list[str]] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """What each step asked the model, as it was sent, and every tool call it made.

    Rebuilt from the session: the system prompt is the instructions file wrapped the
    way every model activity wraps it, and the message is the step's input in its data
    region. A guess is its own exchange and is left out here; the step shows it already.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for c in state.sessions.calls_for_run(run_id):
        if not c.get("step_id") or str(c.get("tag", "")).startswith("guess:"):
            continue
        prompt = None
        if c.get("system") is not None or c.get("input") is not None:
            prompt = {
                "system": build_system(
                    c.get("system") or "", (tools or {}).get(c["step_id"]) or None
                ),
                "message": data_region("step input", c.get("input") or {}),
            }
        out.setdefault(c["step_id"], []).append({**c, "prompt": prompt})
    return out


def _audit(state: AppState, audit_id: str) -> tuple[AuditResult, Any]:
    try:
        return state.audits.load(audit_id)
    except KeyError as e:
        raise HTTPException(404, "No such draft.") from e


def _questions(findings, wf: Any = None) -> list[dict[str, Any]]:
    """The open questions by group. A question asked the same way of several steps is
    shown once, with the steps it can also answer for under ``similar``; ``count`` is
    how many questions the group holds, folded ones included."""
    titles = {s.id: s.title or s.id for s in wf.spec.steps} if wf is not None else {}

    def one(f: Any, similar: list[Any]) -> dict[str, Any]:
        return {
            **f.model_dump(mode="json"),
            "step_title": titles.get(f.step_id, f.step_id),
            "similar": [
                {"id": o.id, "step_id": o.step_id, "step_title": titles.get(o.step_id, o.step_id)}
                for o in similar
            ],
        }

    out = []
    for g in group_questions([f for f in findings if f.status == "open"]):
        folded = fold_similar(g["findings"])
        out.append(
            {
                "type": g["type"],
                "title": g["title"],
                "count": len(g["findings"]),
                "findings": [one(f, similar) for f, similar in folded],
            }
        )
    return out


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
        "questions": _questions(result.findings, wf),
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


def _refuse_while_open(findings: list[Any], then: str) -> None:
    """A real run does not guess: every question has to be answered before it goes on."""
    still_open = [f for f in findings if f.status == "open"]
    if not still_open:
        return
    named = "; ".join(f"{f.question} ({f.step_id or 'the whole workflow'})" for f in still_open[:3])
    more = f", and {len(still_open) - 3} more" if len(still_open) > 3 else ""
    raise HTTPException(
        409, f"A real run needs every question answered first. Still open: {named}{more}. {then}"
    )


def _resume_in_background(state: AppState, run_id: str, wf: Any, answer: dict[str, Any]) -> None:
    """Carry a waiting run on, on a worker thread with its own ledger, like a new one."""
    from wf.interpret import Interpreter
    from wf.store.ledger import Ledger

    ledger = Ledger(state.db)
    interp = Interpreter(state.ws, state.runner.activities, ledger, state.runner.config)
    run = ledger.session.get(Run, run_id)
    # marked running before the request returns, so the page that reloads sees it
    run.status = "running"
    ledger._commit()

    def work() -> None:
        try:
            interp.resume(run, wf, answer)
        except Exception as e:  # noqa: BLE001
            ledger.finish_run(
                run,
                status="failed",
                outputs=run.outputs,
                spent_usd=run.spent_usd or 0.0,
                spent_minutes=run.spent_minutes or 0.0,
                error=str(e),
            )
        finally:
            ledger.close()

    logger.info(
        "run %s of %s carries on after its wait",
        run_id[:8],
        wf.metadata.name,
        extra={"fields": {"event": "run.resumed", "run": run_id, "go_deeper": answer["go_deeper"]}},
    )
    threading.Thread(target=work, daemon=True, name=f"resume-{run_id[:8]}").start()


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

    logger.info(
        "run %s of %s started in %s mode",
        run_id[:8],
        wf.metadata.name,
        mode,
        extra={
            "fields": {
                "event": "run.started",
                "run": run_id,
                "workflow": wf.metadata.name,
                "mode": mode,
                "case": case,
            }
        },
    )
    threading.Thread(target=work, daemon=True, name=f"run-{run_id[:8]}").start()
    return run_id
