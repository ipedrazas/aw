"""An instruction file and its versions, as the skill page shows them, and the edit that
makes a new one."""

from __future__ import annotations

import difflib
from typing import Any
from urllib.parse import urlencode

from wf.audit import Change
from wf.schema import Workflow, Workspace, WorkspaceError, load_workflow_dict, parse_pin
from wf.store import repo as gitrepo


class SkillError(Exception):
    """Something the person asked for that cannot be done, said in a sentence."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def skill_rel(path: str) -> str:
    """Only instruction files are shown here: a definition or a fixture is not a skill,
    and this page is not a way to read them."""
    pin = parse_pin(path)
    rel = pin.path if pin else path
    if not rel.startswith("skills/") or not rel.endswith(".md") or "/.versions/" in rel:
        raise SkillError(404, f"{rel} is not an instruction file.")
    return rel


def skill_url(ref: str | None, **params: Any) -> str | None:
    """The skill page for a pinned reference, at the version it pins. Extra parameters
    (``workflow`` and ``step`` to edit from there, ``sha256`` from a run) go along."""
    if not ref:
        return None
    try:
        rel = skill_rel(ref)
    except SkillError:
        return None
    pin = parse_pin(ref)
    q = {"path": rel, "v": pin.version if pin else None, **params}
    return "/skill?" + urlencode({k: v for k, v in q.items() if v is not None})


def used_by(ws: Workspace, rel: str) -> list[dict[str, Any]]:
    """Every step in every saved workflow that reads this file, and the version it pins."""
    out = []
    for name in ws.list_definitions():
        try:
            wf = ws.load_definition(name)
        except WorkspaceError:
            continue
        for s in wf.spec.steps:
            if not s.skill:
                continue
            pin = parse_pin(s.skill)
            if (pin.path if pin else s.skill) == rel:
                out.append(
                    {
                        "workflow": name,
                        "step_id": s.id,
                        "step_title": s.title or s.id,
                        "version": pin.version if pin else None,
                    }
                )
    return out


def trust_resets(wf: Workflow, step_id: str) -> str | None:
    """What an edit does to the step's earned trust, in a sentence; None if nothing."""
    step = wf.step(step_id)
    t = wf.trust_for(step) if step else None
    if t is None or t.policy != "earned" or "skill" not in (t.reset_on or []):
        return None
    n = t.promote_after or 3
    return (
        "Saving starts this step's trust again: it asks you each time until you accept "
        f"it {n} times in a row with the new instructions."
    )


def text_diff(a: str, b: str) -> list[dict[str, str]]:
    """Line by line, with a few lines of context around each change."""
    rows: list[dict[str, str]] = []
    for line in difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm="", n=3):
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            rows.append({"op": "@", "text": "…"})
        else:
            rows.append({"op": line[:1] or " ", "text": line[1:]})
    return rows


def skill_view(
    ws: Workspace,
    path: str,
    *,
    version: int | None = None,
    a: int | None = None,
    b: int | None = None,
    workflow: str | None = None,
    step: str | None = None,
    sha256: str | None = None,
) -> dict[str, Any]:
    rel = skill_rel(path)
    versions = ws.skill_versions(rel)
    if not versions:
        raise SkillError(404, f"There is no instruction file {rel} in the workspace.")
    latest = versions[-1]
    users = used_by(ws, rel)

    edit = None
    if workflow and step:
        try:
            wf = ws.load_definition(workflow)
        except WorkspaceError as e:
            raise SkillError(404, str(e)) from e
        s = wf.step(step)
        pin = parse_pin(s.skill) if s and s.skill else None
        if s is None or not s.skill or (pin.path if pin else s.skill) != rel:
            raise SkillError(404, f"Step “{step}” of {workflow} does not use {rel}.")
        pinned = pin.version if pin else latest
        edit = {
            "workflow": workflow,
            "step_id": s.id,
            "step_title": s.title or s.id,
            "pinned": pinned,
            "latest": latest,
            "trust_resets": trust_resets(wf, s.id),
        }

    shown_v = version or (edit["pinned"] if edit else latest)
    shown = ws.load_skill_version(rel, shown_v)
    if shown is None:
        raise SkillError(404, f"Version {shown_v} of {rel} is not kept, so it cannot be shown.")
    if edit is not None:
        base = ws.load_skill_version(rel, edit["pinned"])
        edit["body"] = (base or shown).body

    diff = None
    if a is not None and b is not None:
        va, vb = ws.load_skill_version(rel, a), ws.load_skill_version(rel, b)
        if va is None or vb is None:
            missing = a if va is None else b
            raise SkillError(
                404, f"Version {missing} of {rel} is not kept, so it cannot be compared."
            )
        diff = {"a": a, "b": b, "rows": text_diff(va.content, vb.content)}

    return {
        "path": rel,
        "title": shown.title,
        "latest": latest,
        "versions": [
            {
                "version": v,
                "latest": v == latest,
                "pinned_by": [u for u in users if u["version"] == v],
            }
            for v in reversed(versions)
        ],
        "shown": {
            "version": shown.version,
            "body": shown.body,
            "sha256": shown.sha256,
            "commit": gitrepo.file_commit(ws.root, shown.file or rel),
            # a run records the text it read; if this is not that text, say so
            "matches_run": None if sha256 is None else sha256 == shown.sha256,
        },
        "diff": diff,
        "used_by": users,
        "edit": edit,
    }


def edit_step_skill(
    ws: Workspace, name: str, step_id: str, body: str, latest: int | None
) -> tuple[dict[str, Any], list[str], Change]:
    """Save ``body`` as the next version of the step's instruction file and pin the step
    to it. Returns the new definition, the paths written and the change to carry to
    drafts. Other steps that read the file keep the version they pin."""
    wf = ws.load_definition(name)
    step = wf.step(step_id)
    if step is None:
        raise SkillError(404, f"There is no step “{step_id}” in {name}.")
    if not step.skill:
        raise SkillError(400, f"“{step.title or step_id}” has no instructions to edit.")
    rel = skill_rel(step.skill)
    if not body.strip():
        raise SkillError(400, "The instructions are empty. Nothing was saved.")
    versions = ws.skill_versions(rel)
    if latest is not None and versions and versions[-1] != latest:
        raise SkillError(
            409,
            f"Someone saved version {versions[-1]} of these instructions while you were "
            "editing. Reload to see it; nothing was saved.",
        )
    current = ws.load_skill(step.skill)
    if current is not None and current.body.strip() == body.strip():
        raise SkillError(400, "Nothing changed, so no new version was saved.")

    version, written = ws.save_skill_version(rel, body)
    before = step.skill
    after = f"{rel}@{version}"
    data = wf.model_dump(by_alias=True, exclude_none=True)
    for s in data["spec"]["steps"]:
        if s["id"] == step_id:
            s["skill"] = after
    new_wf = load_workflow_dict(data)
    ws.save_definition(new_wf)
    written.append(str(ws.definition_path(name).relative_to(ws.root)))
    change = Change(
        path=f"steps.{step_id}.skill",
        before=before,
        after=after,
        reason=f"You edited the instructions; this is version {version}.",
    )
    return {"version": version, "skill": after, "before": before}, written, change
