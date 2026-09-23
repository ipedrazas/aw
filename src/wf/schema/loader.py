"""Load definitions, instruction files, output schemas and fixtures from a workspace.

A workspace is a directory (normally its own git repository) with this shape::

    definitions/   *.workflow.yaml
    skills/        instruction files, markdown with a ``version:`` front matter key
    schemas/       JSON Schema for step outputs
    fixtures/      recorded tool responses for dry runs
    process-docs/  the customer's own process documents
    cases/         past cases with known outcomes, for dry runs to diverge from
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .definition import Workflow
from .refs import PinnedRef, parse_pin

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
# A definition name is a file stem and nothing else: deleting is not a place to
# discover that a name from a URL can contain a path.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class WorkspaceError(Exception):
    pass


@dataclass(frozen=True)
class Skill:
    path: str
    version: int | None
    content: str
    body: str
    sha256: str

    @property
    def title(self) -> str:
        for line in self.body.splitlines():
            if line.startswith("# "):
                return line[2:].strip()
        return Path(self.path).stem


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise WorkspaceError(f"workspace directory not found: {self.root}")

    # -- paths -------------------------------------------------------------

    def path(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if self.root not in p.parents and p != self.root:
            raise WorkspaceError(f"path escapes the workspace: {rel}")
        return p

    def exists(self, rel: str) -> bool:
        try:
            return self.path(rel).exists()
        except WorkspaceError:
            return False

    # -- definitions -------------------------------------------------------

    def definition_path(self, name: str) -> Path:
        return self.root / "definitions" / f"{name}.workflow.yaml"

    def list_definitions(self) -> list[str]:
        d = self.root / "definitions"
        if not d.is_dir():
            return []
        return sorted(p.name[: -len(".workflow.yaml")] for p in d.glob("*.workflow.yaml"))

    def load_definition(self, name: str) -> Workflow:
        p = self.definition_path(name)
        if not p.exists():
            raise WorkspaceError(f"no definition named {name!r} in {self.root / 'definitions'}")
        return load_workflow_text(p.read_text())

    def save_definition(self, wf: Workflow) -> Path:
        p = self.definition_path(wf.metadata.name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(dump_workflow(wf))
        return p

    def delete_definition(self, name: str) -> list[str]:
        """Remove a definition and the files a draft wrote for it, and say what went.

        Only the ``skills/<name>/`` and ``schemas/<name>/`` directories go with it:
        those are what saving a draft writes, so deleting removes what saving added.
        Instruction files shared between definitions sit at the top of ``skills/`` and
        are left alone. Returns the workspace-relative paths removed.
        """
        if not _NAME.fullmatch(name):
            raise WorkspaceError(f"not a definition name: {name!r}")
        p = self.definition_path(name)
        if not p.exists():
            raise WorkspaceError(f"no definition named {name!r} in {self.root / 'definitions'}")
        removed = [str(p.relative_to(self.root))]
        p.unlink()
        for rel in (f"skills/{name}", f"schemas/{name}"):
            d = self.path(rel)
            if not d.is_dir():
                continue
            removed.extend(
                sorted(str(f.relative_to(self.root)) for f in d.rglob("*") if f.is_file())
            )
            shutil.rmtree(d)
        return removed

    def rename_definition(self, name: str, new_name: str) -> list[str]:
        """Rename a definition: the file, its own ``metadata.name``, and the files a draft
        wrote for it. Returns the workspace-relative paths that changed, old and new alike.
        """
        if not _NAME.fullmatch(name):
            raise WorkspaceError(f"not a definition name: {name!r}")
        if not _NAME.fullmatch(new_name):
            raise WorkspaceError(f"not a definition name: {new_name!r}")
        p = self.definition_path(name)
        if not p.exists():
            raise WorkspaceError(f"no definition named {name!r} in {self.root / 'definitions'}")
        if new_name == name:
            raise WorkspaceError("the new name is the same as the current one")
        new_p = self.definition_path(new_name)
        if new_p.exists():
            raise WorkspaceError(f"a definition named {new_name!r} already exists")
        wf = self.load_definition(name)
        wf.metadata.name = new_name
        changed = [str(p.relative_to(self.root))]
        p.unlink()
        new_p.parent.mkdir(parents=True, exist_ok=True)
        new_p.write_text(dump_workflow(wf))
        changed.append(str(new_p.relative_to(self.root)))
        for old_rel, new_rel in (
            (f"skills/{name}", f"skills/{new_name}"),
            (f"schemas/{name}", f"schemas/{new_name}"),
        ):
            d = self.path(old_rel)
            if not d.is_dir():
                continue
            changed.extend(
                sorted(str(f.relative_to(self.root)) for f in d.rglob("*") if f.is_file())
            )
            nd = self.path(new_rel)
            shutil.move(str(d), str(nd))
            changed.extend(
                sorted(str(f.relative_to(self.root)) for f in nd.rglob("*") if f.is_file())
            )
        return changed

    def resolve_workflow_ref(self, ref: str, current: Workflow | None = None) -> Workflow | None:
        """``deep-research@4`` -> the definition with that name, if its version matches."""
        pin = parse_pin(ref)
        name, version = (pin.path, pin.version) if pin else (ref, None)
        if current is not None and current.metadata.name == name:
            wf = current
        else:
            try:
                wf = self.load_definition(name)
            except WorkspaceError:
                return None
        if version is not None and wf.metadata.version != version:
            return None
        return wf

    # -- skills ------------------------------------------------------------

    def load_skill(self, ref: str) -> Skill | None:
        pin = parse_pin(ref)
        rel = pin.path if pin else ref
        try:
            p = self.path(rel)
        except WorkspaceError:
            return None
        if not p.is_file():
            return None
        text = p.read_text()
        version, body = split_front_matter(text)
        return Skill(
            path=rel,
            version=version,
            content=text,
            body=body,
            sha256=hashlib.sha256(text.encode()).hexdigest(),
        )

    def save_skill(self, rel: str, version: int, body: str, title: str | None = None) -> Path:
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        fm = f"---\nversion: {version}\n---\n"
        if title and not body.lstrip().startswith("#"):
            body = f"# {title}\n\n{body}"
        p.write_text(fm + body.rstrip() + "\n")
        return p

    # -- schemas -----------------------------------------------------------

    def load_schema(self, rel: str) -> dict[str, Any] | None:
        try:
            p = self.path(rel)
        except WorkspaceError:
            return None
        if not p.is_file():
            return None
        return json.loads(p.read_text())

    def save_schema(self, rel: str, schema: dict[str, Any]) -> Path:
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(schema, indent=2) + "\n")
        return p

    # -- fixtures, cases, docs --------------------------------------------

    def load_json(self, rel: str, default: Any = None) -> Any:
        try:
            p = self.path(rel)
        except WorkspaceError:
            return default
        if not p.is_file():
            return default
        return json.loads(p.read_text())

    def load_yaml(self, rel: str, default: Any = None) -> Any:
        try:
            p = self.path(rel)
        except WorkspaceError:
            return default
        if not p.is_file():
            return default
        return yaml.safe_load(p.read_text())

    def list_cases(self) -> list[str]:
        d = self.root / "cases"
        if not d.is_dir():
            return []
        return sorted(p.name[: -len(".case.yaml")] for p in d.glob("*.case.yaml"))

    def list_process_docs(self) -> list[str]:
        d = self.root / "process-docs"
        if not d.is_dir():
            return []
        return sorted(p.name for p in d.glob("*.md"))


def split_front_matter(text: str) -> tuple[int | None, str]:
    m = _FRONT_MATTER.match(text)
    if not m:
        return None, text
    data = yaml.safe_load(m.group(1)) or {}
    version = data.get("version")
    return (int(version) if version is not None else None), text[m.end() :]


def load_workflow_text(text: str) -> Workflow:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise WorkspaceError("definition is not a mapping")
    try:
        return Workflow.model_validate(data)
    except ValidationError as e:
        raise WorkspaceError(f"definition does not match the schema:\n{e}") from e


def load_workflow_dict(data: dict[str, Any]) -> Workflow:
    return Workflow.model_validate(data)


class _Dumper(yaml.SafeDumper):
    pass


def _str_presenter(dumper: yaml.SafeDumper, data: str) -> yaml.Node:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(str, _str_presenter)


def dump_workflow(wf: Workflow) -> str:
    data = wf.model_dump(by_alias=True, exclude_none=True, exclude_defaults=False)
    # drop empty containers so the YAML stays readable
    data = _prune(data)
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100)


def _prune(x: Any) -> Any:
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            v = _prune(v)
            if v in ({}, [], None):
                continue
            out[k] = v
        return out
    if isinstance(x, list):
        return [_prune(v) for v in x]
    return x


__all__ = [
    "PinnedRef",
    "Skill",
    "Workspace",
    "WorkspaceError",
    "dump_workflow",
    "load_workflow_dict",
    "load_workflow_text",
    "parse_pin",
    "split_front_matter",
]
