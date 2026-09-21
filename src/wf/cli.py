"""``wf`` command line: validate, run, audit, serve."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from wf.schema import Workspace


def _ws(args: argparse.Namespace) -> Workspace:
    return Workspace(args.workspace)


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


def cmd_audit(args: argparse.Namespace) -> int:
    from wf.audit import Auditor

    ws = _ws(args)
    auditor = Auditor.from_env(ws)
    doc = Path(args.document).read_text()
    result = auditor.audit(doc, name=args.name)
    print(result.render_text())
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    os.environ.setdefault("WF_WORKSPACE", str(Path(args.workspace).resolve()))
    uvicorn.run(
        "wf.api.app:create_app", factory=True, host=args.host, port=args.port, reload=args.reload
    )
    return 0


def cmd_db_init(args: argparse.Namespace) -> int:
    from wf.store import Database, database_url

    Database()
    print(f"database ready at {database_url()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="wf", description="Agentic workflows: audit, validate, dry run."
    )
    p.add_argument(
        "--workspace",
        default=os.environ.get("WF_WORKSPACE", "workspace"),
        help="workspace directory",
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
    r.set_defaults(fn=cmd_run)

    a = sub.add_parser(
        "audit", help="turn a process document into a draft definition and questions"
    )
    a.add_argument("document")
    a.add_argument("--name", default=None)
    a.set_defaults(fn=cmd_audit)

    s = sub.add_parser("serve", help="start the web UI and API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--reload", action="store_true")
    s.set_defaults(fn=cmd_serve)

    d = sub.add_parser("db-init", help="create the database tables")
    d.set_defaults(fn=cmd_db_init)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
