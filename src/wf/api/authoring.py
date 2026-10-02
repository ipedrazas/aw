"""Keep the conversations that write a workflow, and the draft each one wrote.

The draft is an audit like any other, made the first time the conversation writes
one and updated on every draft after: so it is saved, tried and opened from the
draft page as every draft is. What is new is only the conversation."""

from __future__ import annotations

from typing import Any

from wf.audit import AuditResult, ingest
from wf.author import AuthorOutcome
from wf.store import AuthorSession, Database

from .audits import AuditStore


class AuthorStore:
    def __init__(self, db: Database, audits: AuditStore):
        self.db = db
        self.audits = audits

    def create(self) -> str:
        with self.db.session() as s:
            rec = AuthorSession(messages=[])
            s.add(rec)
            s.flush()
            return rec.id

    def load(self, session_id: str) -> dict[str, Any]:
        with self.db.session() as s:
            rec = s.get(AuthorSession, session_id)
            if rec is None:
                raise KeyError(session_id)
            return {
                "id": rec.id,
                "title": rec.title,
                "messages": list(rec.messages or []),
                "audit_id": rec.audit_id,
            }

    def definition(self, audit_id: str | None) -> dict[str, Any] | None:
        if audit_id is None:
            return None
        result, _rec = self.audits.load(audit_id)
        return result.definition

    def record_turn(self, session_id: str, message: str, outcome: AuthorOutcome) -> None:
        """What was said, and the draft as the turn left it."""
        with self.db.session() as s:
            rec = s.get(AuthorSession, session_id)
            assert rec is not None
            messages = [
                *(rec.messages or []),
                {"role": "user", "text": message},
                {
                    "role": "assistant",
                    "text": outcome.reply,
                    "drafts": outcome.drafts,
                    "wrote": outcome.wrote,
                    "check_these": outcome.check_these,
                    "ready_to_try": outcome.ready_to_try,
                },
            ]
            said = "\n\n".join(m["text"] for m in messages if m["role"] == "user")
            audit_id = rec.audit_id
            if outcome.drafts and outcome.definition is not None:
                audit_id = self._keep_draft(audit_id, said, outcome)
            rec.messages = messages
            rec.audit_id = audit_id
            if outcome.definition is not None:
                rec.title = (outcome.definition.get("metadata") or {}).get(
                    "description"
                ) or rec.title
            elif not rec.title:
                rec.title = message[:120]

    def _keep_draft(self, audit_id: str | None, said: str, outcome: AuthorOutcome) -> str:
        d = outcome.definition
        assert d is not None
        meta = d.get("metadata") or {}
        result = AuditResult(
            name=meta["name"],
            title=meta.get("description") or meta["name"],
            passages=ingest(said),
            definition=d,
            provenance={},
            findings=outcome.findings,
        )
        if audit_id is None:
            return self.audits.create(result, said)
        self.audits.save(audit_id, result)
        return audit_id

    def list(self) -> list[dict[str, Any]]:
        with self.db.session() as s:
            rows = s.query(AuthorSession).order_by(AuthorSession.updated_at.desc()).all()
            return [
                {
                    "id": r.id,
                    "title": r.title or "A new workflow",
                    "audit_id": r.audit_id,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
                for r in rows
            ]
