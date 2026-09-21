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


def make_db():
    """SQLite in memory by default; WF_TEST_DATABASE_URL (CI: Postgres) when set, with fresh tables."""
    import os

    from wf.store import Base, Database

    url = os.environ.get("WF_TEST_DATABASE_URL", "sqlite://")
    db = Database(url)
    if not url.startswith("sqlite"):
        Base.metadata.drop_all(db.engine)
        Base.metadata.create_all(db.engine)
    return db
