"""The auditor: document in, draft definition and questions out."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from wf.activities import ActivityPolicy, AnthropicModel, ModelActivity, run_with_policy
from wf.schema import Workflow, Workspace, dump_workflow, load_workflow_dict
from wf.validate import Finding, validate

from .diff import document_diff
from .draft import Draft, build_draft, materialise
from .extract import extraction_request, normalise
from .ingest import Passage, ingest
from .question import Change, apply_answer, group_questions


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


class Auditor:
    def __init__(
        self,
        ws: Workspace,
        model: ModelActivity,
        extraction_model: str = "claude-opus-5",
        policy: ActivityPolicy | None = None,
    ):
        self.ws = ws
        self.model = model
        self.extraction_model = extraction_model
        self.policy = policy or ActivityPolicy(retries=1, timeout_s=300)

    @classmethod
    def from_env(cls, ws: Workspace, model: ModelActivity | None = None) -> Auditor:
        import os

        return cls(
            ws,
            model or AnthropicModel(),
            extraction_model=os.environ.get("WF_EXTRACTION_MODEL", "claude-opus-5"),
        )

    # -- the audit path ------------------------------------------------------

    def audit(self, document: str, name: str | None = None) -> AuditResult:
        passages = ingest(document)
        req = extraction_request(passages, self.extraction_model, name_hint=name)
        resp = run_with_policy(self.policy, lambda: self.model.complete(req))
        extracted = normalise(resp, name)
        draft = build_draft(extracted, passages)
        return self.finish(draft, passages, explanations=list(resp.decisions))

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
            explanations=explanations or [],
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
        new_def, changes = apply_answer(result.definition, finding, answer, self.ws)
        result.definition = new_def
        result.changes.extend(changes)
        self.revalidate(result)
        return changes

    def revalidate(self, result: AuditResult) -> None:
        wf = result.workflow()
        self.ws.save_definition(wf)
        answered = {f.id: f for f in result.findings if f.status != "open"}
        from .question import _get

        still_relevant = [
            f
            for f in result.findings
            if f.raised_by == "auditor"
            and f.status == "open"
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

    def set_field(self, result: AuditResult, path: str, value: Any, reason: str) -> Change:
        """A direct edit (from the chat or the UI), recorded as a change."""
        from .question import _get, _set

        before = _get(result.definition, path)
        _set(result.definition, path, value)
        ch = Change(path=path, before=before, after=value, reason=reason)
        result.changes.append(ch)
        self.revalidate(result)
        return ch
