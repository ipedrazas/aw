from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from wf.schema import Workspace

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "workspace"


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    """A throwaway copy of the sample workspace that tests may mutate."""
    dst = tmp_path / "workspace"
    shutil.copytree(WORKSPACE, dst, ignore=shutil.ignore_patterns(".git"))
    return Workspace(dst)


@pytest.fixture
def sample_ws() -> Workspace:
    return Workspace(WORKSPACE)


def read_yaml(ws: Workspace, rel: str) -> dict[str, Any]:
    return yaml.safe_load(ws.path(rel).read_text())


def write_yaml(ws: Workspace, rel: str, data: dict[str, Any]) -> None:
    ws.path(rel).write_text(yaml.safe_dump(data, sort_keys=False))


def read_json(ws: Workspace, rel: str) -> dict[str, Any]:
    return json.loads(ws.path(rel).read_text())


def write_json(ws: Workspace, rel: str, data: dict[str, Any]) -> None:
    ws.path(rel).write_text(json.dumps(data, indent=2))


def step(data: dict[str, Any], step_id: str) -> dict[str, Any]:
    for s in data["spec"]["steps"]:
        if s["id"] == step_id:
            return s
    raise KeyError(step_id)


_made: list[tuple[Any, Path | None]] = []


def make_db():
    """A fresh database for one test: SQLite in a file of its own by default;
    WF_TEST_DATABASE_URL (CI: Postgres) when set, with fresh tables.

    Not SQLite in memory: that is one connection shared by every thread, and a run's
    worker writing while the test reads on it mixes up their results."""
    import os
    import tempfile

    from sqlalchemy.orm import close_all_sessions

    from wf.store import Base, Database

    url = os.environ.get("WF_TEST_DATABASE_URL")
    folder = None
    if not url:
        folder = Path(tempfile.mkdtemp(prefix="wf-test-db-"))
        url = f"sqlite:///{folder / 'wf.db'}"
    db = Database(url)
    if folder is None:
        # a session an earlier test left in a transaction holds a lock that would
        # keep the drop waiting for ever
        close_all_sessions()
        Base.metadata.drop_all(db.engine)
        Base.metadata.create_all(db.engine)
    _made.append((db, folder))
    return db


#: The threads the app starts for work in the background (wf.api.app).
BACKGROUND = ("run-", "carry-on-", "retry-", "resume-", "ok-", "draft-")


def wait_for_background(timeout: float = 20.0) -> None:
    """Let the app's background work finish. A run says it is done a moment before its
    thread has stopped writing; closing every session under it then breaks a commit
    in flight (IllegalStateChangeError at teardown)."""
    import threading

    for t in threading.enumerate():
        if t is not threading.current_thread() and t.name.startswith(BACKGROUND):
            t.join(timeout)


def close_dbs() -> None:
    """Close what a test left open and let go of its databases' connections: an engine
    keeps its pool open until it is collected, and Postgres takes 100 clients."""
    if not _made:
        return
    from sqlalchemy.orm import close_all_sessions

    wait_for_background()
    close_all_sessions()
    while _made:
        db, folder = _made.pop()
        db.engine.dispose()
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
