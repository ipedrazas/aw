"""``wf`` command line: validate, run, audit, serve, and read back the sessions."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from wf.logs import get_logger, log_level, setup_logging
from wf.schema import Workspace, WorkspaceError
from wf.startup import announce

logger = get_logger("wf.cli")


def _ws(args: argparse.Namespace) -> Workspace:
    return Workspace(args.workspace)


def _ws_or_none(args: argparse.Namespace) -> Workspace | None:
    """The workspace, for the startup lines. A missing one is the command's to report."""
    try:
        return _ws(args)
    except WorkspaceError:
        return None


def cmd_validate(args: argparse.Namespace) -> int:
    from wf.validate import validate

    ws = _ws(args)
    wf = ws.load_definition(args.name)
    result = validate(wf, ws)
    if args.json:
        print(json.dumps([f.model_dump(mode="json") for f in result.ordered()], indent=2))
    else:
        if result.ok:
            print(f"{args.name}: no findings. {len(wf.spec.steps)} steps.")
        for f in result.ordered():
            print(f"[{f.type}] {f.field}\n    {f.question}\n    {f.detail}")
    return 0 if result.ok else 1


def cmd_run(args: argparse.Namespace) -> int:
    from wf.dryrun import DryRunner

    ws = _ws(args)
    runner = DryRunner.from_env(ws, model_guesses=not args.no_model_guesses)
    if args.case:
        report = runner.run_case(args.name, args.case, mode=args.mode)
    else:
        inputs = dict(kv.split("=", 1) for kv in args.input)
        report = runner.run(args.name, inputs, mode=args.mode)
    print(report.render_text())
    return 0 if report.status == "done" else 1


def cmd_retry(args: argparse.Namespace) -> int:
    from wf.dryrun import DryRunner

    runner = DryRunner.from_env(_ws(args), model_guesses=not args.no_model_guesses)
    matches = [r["id"] for r in runner.list_runs(limit=500) if r["id"].startswith(args.run_id)]
    if len(matches) != 1:
        print(f"{len(matches) or 'No'} runs start with {args.run_id!r}; give more of the id.")
        return 1
    try:
        report = runner.retry(matches[0], skip=args.skip)
    except ValueError as e:
        print(e)
        return 1
    print(report.render_text())
    return 0 if report.status == "done" else 1


def cmd_audit(args: argparse.Namespace) -> int:
    from wf.audit import Auditor

    ws = _ws(args)
    auditor = Auditor.from_env(ws)
    doc = Path(args.document).read_text()
    result = auditor.audit(doc, name=args.name)
    print(result.render_text())
    if result.session_id:
        print(f"\nThe session that read it: wf sessions {result.session_id}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from wf.store.sessions import session_log_mode

    os.environ.setdefault("WF_WORKSPACE", str(Path(args.workspace).resolve()))
    logger.info(
        "serving on http://%s:%d — workspace %s, sessions %s, level %s",
        args.host,
        args.port,
        os.environ["WF_WORKSPACE"],
        session_log_mode(),
        (os.environ.get("WF_LOG_LEVEL") or "info").lower(),
    )
    uvicorn.run(
        "wf.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        # Logging is configured by wf.logs, which is also where the healthcheck's
        # access lines are taken out of this log.
        log_config=None,
    )
    return 0


def cmd_db_init(args: argparse.Namespace) -> int:
    from wf.store import Database, database_url

    Database()
    print(f"database ready at {database_url()}")
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    """The agentic sessions: what the models were asked, and what came back."""
    from wf.store import Database
    from wf.store.sessions import SessionLog

    log = SessionLog(Database())
    if args.id:
        try:
            # Always read the bodies: the decisions are shown either way, and only the
            # prompts wait for --prompts.
            data = log.get(args.id)
        except KeyError:
            print(f"No session {args.id}.", file=sys.stderr)
            return 1
        print(json.dumps(data, indent=2) if args.json else _render_session(data, args.prompts))
        return 0

    rows = log.list(run_id=args.run, audit_id=args.audit, kind=args.kind, limit=args.limit)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No sessions recorded. WF_SESSION_LOG=off writes none.")
        return 0
    for r in rows:
        started = (r["started_at"] or "")[:19].replace("T", " ")
        print(
            f"{r['id'][:8]}  {started}  {r['kind']:<6} {r['status']:<7} "
            f"{r['calls']:>3} calls  ${r['cost_usd']:.4f}  {r['title'][:52] or r['name']}"
        )
    print("\nOne session in full: wf sessions <id> --prompts")
    return 0


def _render_session(data: dict[str, Any], prompts: bool) -> str:
    lines = [
        f"Session {data['id']} — {data['kind']} {data['status']}",
        f"{data['title'] or data['name']}",
        f"{data['calls']} calls, {data['input_tokens']} tokens in, "
        f"{data['output_tokens']} out, ${data['cost_usd']:.4f}",
    ]
    if data.get("run_id"):
        lines.append(f"run {data['run_id']}")
    if data.get("error"):
        lines.append(f"ended: {data['error']}")
    lines.append("")
    for c in data.get("calls_detail", []):
        where = c["step_id"] or c["tag"]
        if c["fanout_index"] is not None:
            where = f"{where}[{c['fanout_index']}]"
        lines.append(
            f"## {c['seq']}. {where} — {c['model']} — {c['duration_s']:.2f}s, "
            f"{c['input_tokens']}/{c['output_tokens']} tokens, ${c['cost_usd']:.4f} [{c['status']}]"
        )
        if c.get("error"):
            lines.append(f"    failed: {c['error']}")
        for call in c.get("tool_calls", []):
            lines.append(
                f"    tool {call['name']}({_short(call.get('input'))}) → {call['summary']}"
            )
            if call.get("injection"):
                lines.append(
                    f"      instructions found in the data, read as data: {call['injection'][:120]}"
                )
        for d in c.get("decisions", []) or []:
            lines.append(f"    decision: {d.get('decision')} — {d.get('reason')}")
        if prompts:
            lines += [
                "",
                "    --- instructions ---",
                _indent(c.get("system") or "(none)"),
                "    --- input ---",
                _indent(_pretty(c.get("input"))),
                "    --- answer ---",
                _indent(_pretty(c.get("output"))),
            ]
        lines.append("")
    return "\n".join(lines)


def _short(value: Any) -> str:
    text = _pretty(value)
    return text if len(text) <= 80 else f"{text[:80]}…"


def _pretty(value: Any) -> str:
    if value is None:
        return "(none)"
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _indent(text: str) -> str:
    return "\n".join(f"    {line}" for line in text.splitlines())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="wf", description="Agentic workflows: audit, validate, dry run."
    )
    p.add_argument(
        "--workspace",
        default=os.environ.get("WF_WORKSPACE", "workspace"),
        help="workspace directory",
    )
    p.add_argument(
        "--log-level",
        default=None,
        choices=["debug", "info", "warning", "error"],
        help="this run only; WF_LOG_LEVEL is the same setting for good",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="validate a definition and print its findings as questions")
    v.add_argument("name")
    v.add_argument("--json", action="store_true")
    v.set_defaults(fn=cmd_validate)

    r = sub.add_parser("run", help="run a definition (dry by default)")
    r.add_argument("name")
    r.add_argument("--mode", choices=["dry", "live"], default="dry")
    r.add_argument("--case", help="a past case from workspace/cases to run against")
    r.add_argument("--input", "-i", action="append", default=[], help="key=value input")
    r.add_argument(
        "--no-model-guesses",
        action="store_true",
        help="use deterministic guesses instead of asking a model",
    )
    r.set_defaults(fn=cmd_run, models=True)

    t = sub.add_parser(
        "retry", help="pick up a run that broke, at the step that broke, once it is fixed"
    )
    t.add_argument("run_id", help="the run's id, or enough of its start to be the only one")
    t.add_argument(
        "--skip",
        action="store_true",
        help="carry on without the step that broke, instead of running it again",
    )
    t.add_argument("--no-model-guesses", action="store_true")
    t.set_defaults(fn=cmd_retry, models=True)

    a = sub.add_parser(
        "audit", help="turn a process document into a draft definition and questions"
    )
    a.add_argument("document")
    a.add_argument("--name", default=None)
    a.set_defaults(fn=cmd_audit, models=True)

    s = sub.add_parser("serve", help="start the web UI and API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--reload", action="store_true")
    s.set_defaults(fn=cmd_serve, models=True)

    d = sub.add_parser("db-init", help="create the database tables")
    d.set_defaults(fn=cmd_db_init)

    g = sub.add_parser("sessions", help="the agentic sessions: every exchange with a model")
    g.add_argument("id", nargs="?", help="one session in full; omit for the list")
    g.add_argument("--run", help="only the sessions of this run")
    g.add_argument("--audit", help="only the sessions of this draft")
    g.add_argument("--kind", choices=["run", "audit", "chat", "other"], help="only this kind")
    g.add_argument("--limit", type=int, default=25)
    g.add_argument(
        "--prompts", action="store_true", help="print the instructions, input and answer in full"
    )
    g.add_argument("--json", action="store_true")
    g.set_defaults(fn=cmd_sessions)

    args = p.parse_args(argv)
    if args.log_level:
        os.environ["WF_LOG_LEVEL"] = args.log_level
    setup_logging(force=bool(args.log_level))
    logger.debug("wf %s at level %s", args.cmd, log_level())
    if getattr(args, "models", False):
        # Only the commands that will ask a model say which ones; `wf sessions` and
        # `wf validate` have no use for it and should not have to read it.
        announce(_ws_or_none(args))
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
