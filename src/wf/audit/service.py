"""The auditor: document in, draft definition and questions out."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from wf import settings
from wf.activities import (
    ActivityPolicy,
    ModelActivity,
    default_model,
    record_sessions,
    run_with_policy,
)
from wf.schema import Workflow, Workspace, dump_workflow, load_workflow_dict
from wf.store.db import Database
from wf.store.sessions import session_span
from wf.validate import Finding, validate

from .diff import document_diff
from .draft import Draft, build_draft, materialise
from .extract import extraction_request, normalise
from .ingest import Passage, ingest
from .question import AnswerRejected, Change, apply_answer, group_questions, why_rejected
from .restore import restore_missing_files


@dataclass
class AuditResult:
    name: str
    title: str
    passages: list[Passage]
    definition: dict[str, Any]
    provenance: dict[str, str]
    findings: list[Finding]
    explanations: list[dict[str, Any]] = field(
        default_factory=list
    )  # the extractor's decisions: "I split your step"
    changes: list[Change] = field(default_factory=list)
    session_id: str | None = None  # the session that read the document, if one was recorded

    def workflow(self) -> Workflow:
        return load_workflow_dict(self.definition)

    def yaml(self) -> str:
        return dump_workflow(self.workflow())

    def open_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.status == "open"]

    def questions(self) -> list[dict[str, Any]]:
        return group_questions(self.open_findings())

    def diff(self) -> dict[str, Any]:
        return document_diff(self.passages, self.workflow(), self.provenance, self.findings)

    def render_text(self) -> str:
        lines = [
            f"# {self.title} ({self.name})",
            "",
            f"{len(self.definition['spec']['steps'])} steps, {len(self.open_findings())} open questions.",
            "",
        ]
        for group in self.questions():
            lines.append(f"## {group['title']}")
            for f in group["findings"]:
                lines.append(f"- [{f.step_id or 'workflow'}] {f.question}")
                if f.detail:
                    lines.append(f"    {f.detail}")
                if f.source_text:
                    lines.append(f"    “{f.source_text[:160]}”")
            lines.append("")
        if self.explanations:
            lines.append("## What I changed")
            for e in self.explanations:
                lines.append(f"- {e.get('decision')} {e.get('reason', '')}")
        return "\n".join(lines)


def _first_line(document: str) -> str:
    """The document's own first line, as the session's title."""
    for line in document.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:200]
    return "a process document"


class Auditor:
    def __init__(
        self,
        ws: Workspace,
        model: ModelActivity,
        extraction_model: str | None = None,
        policy: ActivityPolicy | None = None,
    ):
        self.ws = ws
        self.model = model
        self.extraction_model = extraction_model or settings.extraction_model()
        self.policy = policy or ActivityPolicy(retries=1, timeout_s=300)

    @classmethod
    def from_env(
        cls, ws: Workspace, model: ModelActivity | None = None, db: Database | None = None
    ) -> Auditor:
        """Wired the way the product wires it: the exchanges are recorded as a session.

        The plain constructor leaves the model alone, which is what the tests want.
        """
        return cls(ws, record_sessions(model or default_model(), db))

    # -- the audit path ------------------------------------------------------

    def audit(self, document: str, name: str | None = None) -> AuditResult:
        passages = ingest(document)
        req = extraction_request(passages, self.extraction_model, name_hint=name)
        with session_span("audit", name=name or "", title=_first_line(document)) as span:
            resp = run_with_policy(self.policy, lambda: self.model.complete(req))
            session_id = span.id
        extracted = normalise(resp, name)
        draft = build_draft(extracted, passages)
        result = self.finish(draft, passages, explanations=list(resp.decisions))
        result.session_id = session_id
        return result

    def finish(
        self,
        draft: Draft,
        passages: list[Passage],
        explanations: list[dict[str, Any]] | None = None,
    ) -> AuditResult:
        wf = materialise(draft, self.ws)
        findings = self.validate(
            wf, draft.provenance, passages, extra=[*draft.assumptions, *draft.branch_gaps]
        )
        return AuditResult(
            name=draft.name,
            title=draft.title,
            passages=passages,
            definition=draft.definition,
            provenance=draft.provenance,
            findings=findings,
            explanations=[
                *(explanations or []),
                *({"decision": n, "reason": "", "alternatives": []} for n in draft.notes),
            ],
        )

    def validate(
        self,
        wf: Workflow,
        provenance: dict[str, str],
        passages: list[Passage],
        extra: list[Finding] | None = None,
    ) -> list[Finding]:
        """Run both passes and attach the document's own words to each finding."""
        by_id = {p.id: p for p in passages}
        found = validate(wf, self.ws).findings
        merged: dict[str, Finding] = {}
        for f in [*(extra or []), *found]:
            if f.field in merged and merged[f.field].type == f.type:
                continue
            key = f"{f.type}:{f.field}"
            if key in merged:
                continue
            if f.source_text is None:
                pid = provenance.get(f.field) or (
                    provenance.get(f"steps.{f.step_id}") if f.step_id else None
                )
                if pid and pid in by_id:
                    f.source_text = by_id[pid].text
            merged[key] = f
        # a branch gap raised by the auditor supersedes the validator's "when" finding for the same step
        out = list(merged.values())
        return sorted(
            out,
            key=lambda f: (
                -f.unblocks,
                ["conflict", "gap", "unreachable", "assumption"].index(f.type),
                f.field,
            ),
        )

    # -- answers --------------------------------------------------------------

    def answer(self, result: AuditResult, finding_id: str, answer: Any) -> list[Change]:
        finding = next(f for f in result.findings if f.id == finding_id)
        was = (finding.status, finding.answer)
        try:
            new_def, changes = apply_answer(result.definition, finding, answer, self.ws)
            wf = load_workflow_dict(new_def)
        except AnswerRejected:
            raise
        except (ValidationError, TypeError, ValueError, KeyError, IndexError, StopIteration) as e:
            # the answer does not fit the field: the draft and the question stay as they were
            finding.status, finding.answer = was
            raise AnswerRejected(why_rejected(finding, answer)) from e
        result.definition = new_def
        result.changes.extend(changes)
        self.revalidate(result, wf)
        return changes

    def dismiss(self, result: AuditResult, finding_id: str, reason: str) -> Finding | None:
        """Close an open question that does not apply. The draft is unchanged; answering it
        later, from the answered list, opens it again."""
        f = next((x for x in result.findings if x.id == finding_id and x.status == "open"), None)
        if f is None:
            return None
        f.status, f.answer = "dismissed", reason.strip() or "It does not apply."
        return f

    def revalidate(self, result: AuditResult, wf: Workflow | None = None) -> None:
        from .question import use_runner_schema

        # a step that runs a routine gives back the routine's shape; drafts made before
        # this was kept in step are put right on their next change
        for s in result.definition["spec"]["steps"]:
            if s.get("run"):
                use_runner_schema(result.definition, self.ws, s["id"])
        wf = result.workflow()
        self.restore_files(result, wf)
        self.ws.save_definition(wf)
        answered = {f.id: f for f in result.findings if f.status != "open"}
        from .question import _get

        # a question about a step that is no longer there goes with it
        step_ids = {s["id"] for s in result.definition["spec"]["steps"]}
        still_relevant = [
            f
            for f in result.findings
            if f.raised_by == "auditor"
            and f.status == "open"
            and (f.step_id is None or f.step_id in step_ids)
            and not (f.field.endswith(".when") and _get(result.definition, f.field) is not None)
        ]
        fresh = self.validate(wf, result.provenance, result.passages, extra=still_relevant)
        kept: list[Finding] = []
        for f in fresh:
            if f.id in answered:
                kept.append(answered[f.id])
            else:
                kept.append(f)
        # keep answered findings so the UI can show them as done
        ids = {f.id for f in kept}
        kept.extend(f for f in answered.values() if f.id not in ids)
        result.findings = kept

    def restore_files(self, result: AuditResult, wf: Workflow | None = None) -> list[str]:
        """Write again any file a step names that the workspace lacks, from the draft and
        the document's words for the step, so nobody is asked where it went."""
        by_id = {p.id: p.text for p in result.passages}
        source_for = {
            k.split(".", 1)[1]: by_id[v]
            for k, v in result.provenance.items()
            if k.count(".") == 1 and k.startswith("steps.") and v in by_id
        }
        return restore_missing_files(wf or result.workflow(), self.ws, source_for)

    @staticmethod
    def _step_path(result: AuditResult, path: str) -> str:
        """The chat sees the draft as written, so it says ``spec.steps.get_topic`` or
        ``spec.steps[0]``; both mean ``steps.get_topic``."""
        bits = path.replace("[", ".").replace("]", "").split(".")
        if bits[:2] == ["spec", "steps"] and len(bits) > 2:
            bits = bits[1:]
        if bits[0] == "steps" and len(bits) > 1 and bits[1].isdigit():
            steps = result.definition["spec"]["steps"]
            if int(bits[1]) < len(steps):
                bits[1] = steps[int(bits[1])]["id"]
        return ".".join(bits)

    def set_field(self, result: AuditResult, path: str, value: Any, reason: str) -> Change:
        """A direct edit (from the chat or the UI), recorded as a change.

        Removing a whole step (``steps.<id>`` set to null) is recorded against the step
        list, so undoing it puts the step back where it was and rewires what read it.
        """
        from .question import _get, _set, remove_step

        path = self._step_path(result, path)
        bits = path.split(".")
        if value is None and len(bits) == 2 and bits[0] == "steps":
            if not any(s["id"] == bits[1] for s in result.definition["spec"]["steps"]):
                raise AnswerRejected(f"There is no step “{bits[1]}”, so the draft is unchanged.")
            path, value = "spec.steps", remove_step(result.definition, bits[1])
        before = _get(result.definition, path)
        new_def = copy.deepcopy(result.definition)
        _set(new_def, path, value)
        try:
            wf = load_workflow_dict(new_def)
        except ValidationError as e:
            raise AnswerRejected(
                f"“{value}” cannot go into {path}, so the draft is unchanged."
            ) from e
        result.definition = new_def
        ch = Change(path=path, before=before, after=value, reason=reason)
        result.changes.append(ch)
        self.revalidate(result, wf)
        return ch
