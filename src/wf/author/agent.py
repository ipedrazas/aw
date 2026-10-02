"""The authoring agent: a conversation that writes a workflow beside it.

The audit path reads a document once and turns every gap into a question the page
asks. Here the model leads instead: it talks with the person, writes the whole
workflow when it knows enough, and is told by the validator what is wrong with it,
which it fixes itself or brings into the conversation. How it should do that is a
skill (``skill/SKILL.md`` and its references), not code.

What stays in code is what the model may not decide: every draft must load and is
checked before it is kept; its files go only under the workflow's own folders; it
cannot take the name of a workflow that already exists; and a change to what a run
may spend, what leaves the system, who signs off or which model runs is said to the
person beside the reply, whoever asked for it.
"""

from __future__ import annotations

import copy
import json
import re
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from wf.activities import ModelActivity, ModelRequest, ToolSpec
from wf.audit.catalog import capabilities
from wf.audit.question import use_runner_schema
from wf.schema import Workspace, dump_workflow, load_workflow_dict
from wf.settings import author_model
from wf.store.sessions import session_span
from wf.validate import Finding, validate

SKILL_DIR = Path(__file__).with_name("skill")
NAME = re.compile(r"^[a-z][a-z0-9-]{1,40}$")

# What a run may spend or how far it goes, what leaves the system, who signs off,
# when a run stops for someone, which model runs: the person's to say (as in
# ``wf.audit.triage.PROTECTED``), so a draft that changes one says so.
PROTECTED_STEP = (
    "requires_approval",
    "side_effects",
    "limits",
    "search_further",
    "trust",
    "model",
    "deadline",
    "on_timeout",
)

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reply", "ready_to_try"],
    "properties": {
        "reply": {
            "type": "string",
            "description": "What you say to the person: plain sentences, as a colleague would.",
        },
        "ready_to_try": {
            "type": "boolean",
            "description": "True when the draft as written is worth a dry run.",
        },
    },
}


def _split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    if text.startswith("---\n"):
        head, _, body = text[4:].partition("\n---\n")
        return yaml.safe_load(head) or {}, body
    return {}, text


@cache
def skill() -> tuple[dict[str, Any], str]:
    """The skill's front matter and body. Kept beside this file so it changes with it."""
    return _split_front_matter((SKILL_DIR / "SKILL.md").read_text())


def references() -> dict[str, str]:
    return {p.stem: p.read_text() for p in sorted((SKILL_DIR / "reference").glob("*.md"))}


class AuthorOutcome(BaseModel):
    reply: str
    ready_to_try: bool = False
    definition: dict[str, Any] | None = None  # the draft after this turn
    findings: list[Finding] = Field(default_factory=list)
    wrote: list[str] = Field(default_factory=list)  # files written this turn
    drafts: int = 0  # how many drafts this turn kept
    check_these: list[str] = Field(default_factory=list)  # protected changes, said plainly


def findings_view(findings: list[Finding]) -> list[dict[str, Any]]:
    return [
        {
            "type": f.type,
            "step": f.step_id,
            "field": f.field,
            "question": f.question,
            "detail": f.detail,
        }
        for f in findings
        if f.status == "open"
    ]


def _step_values(d: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    steps = ((d or {}).get("spec") or {}).get("steps") or []
    return {s["id"]: {k: s.get(k) for k in PROTECTED_STEP} for s in steps if "id" in s}


def protected_changes(before: dict[str, Any] | None, after: dict[str, Any]) -> list[str]:
    """What changed in the parts only the person may decide, in plain words."""
    out: list[str] = []
    b_spec, a_spec = (before or {}).get("spec") or {}, after.get("spec") or {}
    if b_spec.get("budget") != a_spec.get("budget"):
        out.append("what a run may spend")
    if (b_spec.get("defaults") or {}).get("trust") != (a_spec.get("defaults") or {}).get("trust"):
        out.append("how often steps check with you")
    if (b_spec.get("defaults") or {}).get("model") != (a_spec.get("defaults") or {}).get("model"):
        out.append("the model steps run on")
    b_steps, a_steps = _step_values(before), _step_values(after)
    words = {
        "requires_approval": "who approves",
        "side_effects": "what leaves the system",
        "limits": "how far it may go",
        "search_further": "how far it searches further",
        "trust": "how often it checks with you",
        "model": "its model",
        "deadline": "how long it waits",
        "on_timeout": "what happens when the wait runs out",
    }
    titles = {s["id"]: s.get("title") or s["id"] for s in a_spec.get("steps") or [] if "id" in s}
    for sid, vals in a_steps.items():
        was = b_steps.get(sid, dict.fromkeys(PROTECTED_STEP))
        changed = [words[k] for k in PROTECTED_STEP if vals.get(k) != was.get(k)]
        if changed:
            out.append(f"“{titles[sid]}”: {', '.join(changed)}")
    return out


class Drafting:
    """The draft as this turn leaves it, and the tools that read and write it."""

    def __init__(self, ws: Workspace, definition: dict[str, Any] | None):
        self.ws = ws
        self.start = copy.deepcopy(definition)
        self.definition = copy.deepcopy(definition)
        self.findings: list[Finding] = []
        self.wrote: list[str] = []
        self.drafts = 0

    @property
    def name(self) -> str | None:
        return ((self.definition or {}).get("metadata") or {}).get("name")

    # -- reading -----------------------------------------------------------------

    def read_reference(self, args: dict[str, Any]) -> Any:
        refs = references()
        name = str(args.get("name") or "")
        if name not in refs:
            return {"error": f"No reference called {name!r}. There are: {', '.join(refs)}."}
        return refs[name]

    def what_the_system_can_do(self, _args: dict[str, Any]) -> Any:
        caps = capabilities(self.ws, exclude=self.name)
        return {
            **caps,
            "instructions": [
                {k: i.get(k) for k in ("ref", "title", "what", "takes", "result")}
                for i in caps["instructions"]
            ],
        }

    def read_workflow(self, args: dict[str, Any]) -> Any:
        name = str(args.get("name") or "")
        if not self.ws.definition_path(name).exists():
            return {"error": f"There is no workflow called {name!r}."}
        return self.ws.definition_path(name).read_text()

    def read_workspace_file(self, args: dict[str, Any]) -> Any:
        rel = str(args.get("path") or "").partition("@")[0]
        if not (rel.startswith("skills/") or rel.startswith("schemas/")) or ".." in rel:
            return {"error": "Only instructions (skills/...) and result shapes (schemas/...)."}
        if not self.ws.exists(rel):
            return {"error": f"There is no file {rel!r}."}
        return self.ws.path(rel).read_text()

    def check_draft(self, _args: dict[str, Any]) -> Any:
        if self.definition is None:
            return {"error": "There is no draft yet."}
        return {"open_points": findings_view(self.findings)}

    # -- writing -----------------------------------------------------------------

    def write_draft(self, args: dict[str, Any]) -> Any:
        """Keep the draft only when it loads; say what is wrong with it either way."""
        try:
            d = yaml.safe_load(str(args.get("definition_yaml") or ""))
        except yaml.YAMLError as e:
            return {"accepted": False, "errors": [f"The YAML does not parse: {e}"]}
        if not isinstance(d, dict):
            return {"accepted": False, "errors": ["The definition must be a YAML mapping."]}
        name = str((d.get("metadata") or {}).get("name") or "")
        errors: list[str] = []
        if not NAME.fullmatch(name):
            errors.append(f"metadata.name {name!r} must be a short lower-case slug.")
        elif name != self.name and self.ws.definition_path(name).exists():
            errors.append(f"There is already a workflow called {name!r}; choose another name.")
        files: list[tuple[str, Any]] = []
        for i in args.get("instructions") or []:
            rel = str(i.get("path") or "")
            if not (rel.startswith(f"skills/{name}/") and rel.endswith(".md")) or ".." in rel:
                errors.append(f"Instructions go under skills/{name}/ and end in .md, not {rel!r}.")
                continue
            _, body = _split_front_matter(str(i.get("body") or ""))
            files.append((rel, body))
        for r in args.get("results") or []:
            rel = str(r.get("path") or "")
            if not (rel.startswith(f"schemas/{name}/") and rel.endswith(".json")) or ".." in rel:
                errors.append(
                    f"Result shapes go under schemas/{name}/ and end in .json, not {rel!r}."
                )
                continue
            try:
                files.append((rel, json.loads(str(r.get("schema_json") or ""))))
            except json.JSONDecodeError as e:
                errors.append(f"The result shape {rel} is not JSON: {e}")
        if errors:
            return {"accepted": False, "errors": errors}
        try:
            load_workflow_dict(d)
        except ValidationError as e:
            return {
                "accepted": False,
                "errors": [
                    f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()
                ][:20],
            }

        for rel, content in files:
            if isinstance(content, str):
                self.ws.save_skill(rel, 1, content)
            else:
                self.ws.save_schema(rel, content)
            self.wrote.append(rel)
        # a step names its own instructions at the version just written
        written = {rel for rel, c in files if isinstance(c, str)}
        for s in d["spec"].get("steps") or []:
            if str(s.get("skill") or "").partition("@")[0] in written:
                s["skill"] = f"{s['skill'].partition('@')[0]}@1"
        for s in d["spec"].get("steps") or []:
            if s.get("run"):
                use_runner_schema(d, self.ws, s["id"])
        wf = load_workflow_dict(d)
        self.ws.save_definition(wf)
        self.definition = d
        self.findings = validate(wf, self.ws).findings
        self.drafts += 1
        return {
            "accepted": True,
            "open_points": findings_view(self.findings),
            "files_written": [rel for rel, _ in files],
        }

    def tools(self) -> list[ToolSpec]:
        refs = list(references())
        obj = {"type": "object", "additionalProperties": False}
        return [
            ToolSpec(
                name="read_reference",
                description="One of the skill's references: "
                + ", ".join(refs)
                + ". Read definition before your first draft.",
                input_schema={
                    **obj,
                    "required": ["name"],
                    "properties": {"name": {"type": "string", "enum": refs}},
                },
                executor=self.read_reference,
            ),
            ToolSpec(
                name="what_the_system_can_do",
                description="The tools, routines, instructions and models a workflow can use, and the person's other workflows.",
                input_schema={**obj, "properties": {}, "required": []},
                executor=self.what_the_system_can_do,
                max_calls=3,
            ),
            ToolSpec(
                name="read_workflow",
                description="One of the person's workflows, as YAML. deep-research is a complete example.",
                input_schema={
                    **obj,
                    "required": ["name"],
                    "properties": {"name": {"type": "string"}},
                },
                executor=self.read_workflow,
            ),
            ToolSpec(
                name="read_workspace_file",
                description="An instruction file (skills/...) or a result shape (schemas/...), by path.",
                input_schema={
                    **obj,
                    "required": ["path"],
                    "properties": {"path": {"type": "string"}},
                },
                executor=self.read_workspace_file,
                max_calls=20,
            ),
            ToolSpec(
                name="write_draft",
                description=(
                    "Write the whole workflow: its definition as YAML, the instructions for "
                    "its judgement steps, and its result shapes. It is checked; a draft that "
                    "does not load is refused with the reasons, and one that loads is kept "
                    "and shown to the person, with what is still open."
                ),
                input_schema={
                    **obj,
                    "required": ["definition_yaml", "instructions", "results"],
                    "properties": {
                        "definition_yaml": {"type": "string"},
                        "instructions": {
                            "type": "array",
                            "items": {
                                **obj,
                                "required": ["path", "body"],
                                "properties": {
                                    "path": {"type": "string"},
                                    "body": {"type": "string"},
                                },
                            },
                        },
                        "results": {
                            "type": "array",
                            "items": {
                                **obj,
                                "required": ["path", "schema_json"],
                                "properties": {
                                    "path": {"type": "string"},
                                    "schema_json": {"type": "string"},
                                },
                            },
                        },
                    },
                },
                executor=self.write_draft,
                max_calls=6,
            ),
            ToolSpec(
                name="check_draft",
                description="What is still open in the draft as last written.",
                input_schema={**obj, "properties": {}, "required": []},
                executor=self.check_draft,
                max_calls=5,
            ),
        ]


def author_turn(
    model: ModelActivity,
    ws: Workspace,
    conversation: list[dict[str, Any]],
    message: str,
    definition: dict[str, Any] | None,
    *,
    model_name: str | None = None,
    audit_id: str | None = None,
) -> AuthorOutcome:
    """One turn: the person's message in, the reply and the draft as it now stands out.

    ``conversation`` is what was said before, oldest first (``role``, ``text``);
    ``definition`` the draft as the last turn left it, or None before the first."""
    drafting = Drafting(ws, definition)
    if definition is not None:
        try:
            drafting.findings = validate(load_workflow_dict(definition), ws).findings
        except ValidationError:
            drafting.findings = []
    _, body = skill()
    req = ModelRequest(
        tag="author:turn",
        model=model_name or author_model(),
        system=body,
        tools=drafting.tools(),
        input={
            "conversation": [{"role": t["role"], "text": t["text"]} for t in conversation[-30:]],
            "message": message,
            "draft": (
                dump_workflow(load_workflow_dict(definition)) if definition is not None else None
            ),
            "open_points": findings_view(drafting.findings),
        },
        output_schema=OUTPUT_SCHEMA,
    )
    with session_span("author", name=drafting.name or "", title=message[:200], audit_id=audit_id):
        resp = model.complete(req)
    out = resp.output or {}
    kept = drafting.definition
    return AuthorOutcome(
        reply=str(out.get("reply") or ""),
        ready_to_try=bool(out.get("ready_to_try")),
        definition=kept,
        findings=drafting.findings,
        wrote=drafting.wrote,
        drafts=drafting.drafts,
        check_these=(protected_changes(drafting.start, kept) if drafting.drafts and kept else []),
    )
