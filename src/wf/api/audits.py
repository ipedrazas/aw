"""Persist audits and their draft changes, and reconstruct them for the auditor."""

from __future__ import annotations

from typing import Any

from wf.audit import AuditResult, Change, Passage
from wf.audit.chat import ChatTurn
from wf.store import Audit, Database, DraftChange
from wf.validate import Finding


class AuditStore:
    def __init__(self, db: Database):
        self.db = db

    def create(self, result: AuditResult, document: str) -> str:
        with self.db.session() as s:
            rec = Audit(
                name=result.name,
                title=result.title,
                document=document,
                passages=[p.model_dump() for p in result.passages],
                draft=result.definition,
                provenance=result.provenance,
                findings=[f.model_dump(mode="json") for f in result.findings],
                chat=[
                    {
                        "role": "assistant",
                        "text": _opening_line(result),
                        "changes": [],
                        "point_to_finding": None,
                    }
                ],
            )
            rec.chat[0]["explanations"] = result.explanations
            s.add(rec)
            s.flush()
            return rec.id

    def load(self, audit_id: str) -> tuple[AuditResult, Audit]:
        with self.db.session() as s:
            rec = s.get(Audit, audit_id)
            if rec is None:
                raise KeyError(audit_id)
            changes = (
                s.query(DraftChange).filter_by(audit_id=audit_id).order_by(DraftChange.seq).all()
            )
            result = AuditResult(
                name=rec.name,
                title=rec.title,
                passages=[Passage.model_validate(p) for p in rec.passages],
                definition=rec.draft,
                provenance=rec.provenance,
                findings=[Finding.model_validate(f) for f in rec.findings],
                explanations=(rec.chat[0].get("explanations", []) if rec.chat else []),
                changes=[
                    Change(
                        path=c.path,
                        before=(c.before or {}).get("v"),
                        after=(c.after or {}).get("v"),
                        reason=c.reason,
                    )
                    for c in changes
                    if not c.undone
                ],
            )
            return result, rec

    def save(
        self,
        audit_id: str,
        result: AuditResult,
        new_changes: list[Change] | None = None,
        by: str = "user",
        chat: list[dict[str, Any]] | None = None,
        status: str | None = None,
        commit: str | None = None,
    ) -> None:
        with self.db.session() as s:
            rec = s.get(Audit, audit_id)
            assert rec is not None
            rec.draft = result.definition
            rec.provenance = result.provenance
            rec.findings = [f.model_dump(mode="json") for f in result.findings]
            rec.title = result.title
            rec.name = result.name
            if chat is not None:
                rec.chat = chat
            if status:
                rec.status = status
            if commit:
                rec.saved_commit = commit
            if new_changes:
                seq = s.query(DraftChange).filter_by(audit_id=audit_id).count()
                for c in new_changes:
                    seq += 1
                    s.add(
                        DraftChange(
                            audit_id=audit_id,
                            seq=seq,
                            by=by,
                            path=c.path,
                            before={"v": c.before},
                            after={"v": c.after},
                            reason=c.reason,
                        )
                    )

    def changes(self, audit_id: str) -> list[dict[str, Any]]:
        with self.db.session() as s:
            rows = s.query(DraftChange).filter_by(audit_id=audit_id).order_by(DraftChange.seq).all()
            return [
                {
                    "seq": c.seq,
                    "by": c.by,
                    "path": c.path,
                    "before": (c.before or {}).get("v"),
                    "after": (c.after or {}).get("v"),
                    "reason": c.reason,
                    "undone": c.undone,
                    "at": c.created_at.isoformat() if c.created_at else None,
                }
                for c in rows
            ]

    def mark_undone(self, audit_id: str, seq: int) -> dict[str, Any] | None:
        with self.db.session() as s:
            row = s.query(DraftChange).filter_by(audit_id=audit_id, seq=seq).first()
            if row is None or row.undone:
                return None
            row.undone = True
            return {
                "path": row.path,
                "before": (row.before or {}).get("v"),
                "after": (row.after or {}).get("v"),
                "reason": row.reason,
            }

    def chat_history(self, rec: Audit) -> list[ChatTurn]:
        return [
            ChatTurn(
                role=t.get("role", "user"),
                text=t.get("text", ""),
                changes=t.get("changes", []),
                point_to_finding=t.get("point_to_finding"),
            )
            for t in (rec.chat or [])
        ]

    def list(self) -> list[dict[str, Any]]:
        with self.db.session() as s:
            rows = s.query(Audit).order_by(Audit.updated_at.desc()).all()
            return [
                {
                    "id": a.id,
                    "name": a.name,
                    "title": a.title,
                    "status": a.status,
                    "open": sum(1 for f in a.findings if f.get("status") == "open"),
                    "steps": len(a.draft.get("spec", {}).get("steps", [])),
                    "updated_at": a.updated_at.isoformat() if a.updated_at else None,
                }
                for a in rows
            ]


def _opening_line(result: AuditResult) -> str:
    n = len(result.definition["spec"]["steps"])
    added = [e for e in result.explanations]
    open_n = len(result.open_findings())
    parts = [f"That’s {n} steps, on the right."]
    if added:
        parts.append(" ".join(e.get("decision", "") for e in added[:2]))
    parts.append(
        f"{open_n} question{'s' if open_n != 1 else ''} left for you."
        if open_n
        else "No questions left; you can try it on a topic."
    )
    return " ".join(p for p in parts if p)
