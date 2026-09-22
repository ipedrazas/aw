"""Agentic sessions, kept.

A session is one piece of agentic work: a workflow run, an audit of a process
document, a turn of the chat that edits a draft. Under it sits every exchange with a
model that the work caused — the instructions it was given, the input, the answer,
the tools it called, what it cost, how long it took. A run record already says what
the workflow decided; a session says what the model was actually asked, which is the
thing you need when the answer is wrong and the decision looks reasonable.

Where sessions come from:

* the span is opened around the work (``session_span``) and carries no database of
  its own, so the interpreter and the auditor do not learn about storage;
* the row is written by whoever records the first call in that span, which is the
  ``SessionRecorder`` wrapped around the model — so a piece of work that never asks a
  model leaves no empty session behind;
* totals are updated as each call lands, so a session that is still running reads
  correctly, and one whose process died is honest about where it stopped.

Configuration:

| `WF_SESSION_LOG`       | `full` (default), `meta`, `off`                       |
| `WF_SESSION_MAX_CHARS` | how much of one body is kept; `0` keeps all (40000)   |
| `SESSION_ROOT`         | where each session is also written as its own file    |
|                        | (`/app/var/sessions` by default)                      |

``meta`` writes the row without the prompt bodies, for when the prompts are too large
or too sensitive to keep. ``off`` writes nothing; the log lines still happen, since
what is printed and what is stored are two different questions.

Sessions live in the database, but each one is also mirrored to
``<SESSION_ROOT>/<session id>.log`` as it is written to. That file is what
``list_from_disk`` and ``get_from_disk`` read, so a session survives a restart even
for a checkout pointed at an in-memory or throwaway database.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select

from .db import Database
from .records import AgentSession, ModelCall, now

FULL, META, OFF = "full", "meta", "off"
DEFAULT_MAX_CHARS = 40_000
DEFAULT_SESSION_ROOT = "/app/var/sessions"


def session_log_mode() -> str:
    """``full``, ``meta`` or ``off``. Read on each call, like the model names."""
    raw = (os.environ.get("WF_SESSION_LOG") or FULL).strip().lower()
    if raw in ("0", "no", "none", OFF):
        return OFF
    if raw in ("meta", "metadata", "counts"):
        return META
    return FULL


def max_chars() -> int:
    raw = (os.environ.get("WF_SESSION_MAX_CHARS") or "").strip()
    if not raw:
        return DEFAULT_MAX_CHARS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_MAX_CHARS


def session_root() -> Path:
    """Where each session is mirrored to its own file. ``SESSION_ROOT`` overrides the
    default, which matches the container's ``var/`` directory. Read on each call, like
    the other configuration here, and created if it does not exist yet."""
    raw = (os.environ.get("SESSION_ROOT") or DEFAULT_SESSION_ROOT).strip()
    root = Path(raw).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


def session_file(session_id: str) -> Path:
    return session_root() / f"{session_id}.log"


# -- the span ------------------------------------------------------------------


@dataclass
class SessionSpan:
    """What is being done, and what has been asked so far while doing it."""

    kind: str
    name: str = ""
    title: str = ""
    run_id: str | None = None
    audit_id: str | None = None
    mode: str | None = None

    id: str | None = None  # set when the first call is written
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    # the step this span is inside, when it is inside one
    step_run_id: str | None = None
    step_id: str | None = None
    fanout_index: int | None = None

    log: Any = None  # the SessionLog that adopted this span, if any
    _tail: list[tuple[str | None, str | None, int | None]] = field(default_factory=list)

    def add(self, *, input_tokens: int, output_tokens: int, cost_usd: float) -> int:
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cost_usd = round(self.cost_usd + cost_usd, 6)
        return self.calls

    def describe(self) -> str:
        where = self.step_id or self.name or self.kind
        return f"{self.kind}:{where}"


_CURRENT: ContextVar[SessionSpan | None] = ContextVar("wf_session_span", default=None)


def current_span() -> SessionSpan | None:
    return _CURRENT.get()


def span_or_loose() -> SessionSpan:
    """The open span, or one for a call nobody claimed, so no exchange is lost."""
    span = _CURRENT.get()
    if span is None:
        span = SessionSpan(kind="other", name="unattached")
        _CURRENT.set(span)
    return span


@contextmanager
def session_span(
    kind: str,
    *,
    name: str = "",
    title: str = "",
    run_id: str | None = None,
    audit_id: str | None = None,
    mode: str | None = None,
) -> Iterator[SessionSpan]:
    """Open a session around a piece of work. Nested work gets its own session."""
    span = SessionSpan(
        kind=kind, name=name, title=title, run_id=run_id, audit_id=audit_id, mode=mode
    )
    token = _CURRENT.set(span)
    try:
        yield span
    except BaseException as e:
        _close(span, "failed", f"{type(e).__name__}: {e}")
        raise
    else:
        _close(span, "done", None)
    finally:
        _CURRENT.reset(token)


@contextmanager
def step_span(
    *, step_run_id: str, step_id: str, fanout_index: int | None = None
) -> Iterator[SessionSpan | None]:
    """Name the step a call belongs to, for the span that is open."""
    span = _CURRENT.get()
    if span is None:
        yield None
        return
    span._tail.append((span.step_run_id, span.step_id, span.fanout_index))
    span.step_run_id, span.step_id, span.fanout_index = step_run_id, step_id, fanout_index
    try:
        yield span
    finally:
        span.step_run_id, span.step_id, span.fanout_index = span._tail.pop()


def _close(span: SessionSpan, status: str, error: str | None) -> None:
    if span.id and span.log is not None:
        span.log.close(span, status=status, error=error)


# -- storage -------------------------------------------------------------------


def clip_text(value: str, limit: int) -> str:
    if limit and len(value) > limit:
        return f"{value[:limit]}\n… {len(value) - limit} more characters, not kept"
    return value


def clip_json(value: Any, limit: int) -> Any:
    """Keep a body as JSON while it fits, as clipped text when it does not."""
    if value is None:
        return None
    try:
        text = json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"not_json": clip_text(str(value), limit)}
    if not limit or len(text) <= limit:
        return value
    return {"clipped": clip_text(text, limit)}


def _tool_names(tool_calls: list[Any]) -> list[Any]:
    """Which tools were called, without what passed through them: the `meta` shape."""
    return [
        {"name": c.get("name"), "summary": c.get("summary")}
        for c in tool_calls
        if isinstance(c, dict)
    ]


class SessionLog:
    """Writes sessions and reads them back. One short database session per write, so
    it is safe to use from a run's worker thread beside that run's ledger."""

    def __init__(self, db: Database | None = None):
        self.db = db or Database()

    # -- writing ---------------------------------------------------------------

    def open(self, span: SessionSpan) -> str:
        with self.db.session() as s:
            row = AgentSession(
                kind=span.kind,
                name=span.name,
                title=span.title[:500],
                run_id=span.run_id,
                audit_id=span.audit_id,
                mode=span.mode,
            )
            s.add(row)
            s.flush()
            span.id = row.id
        span.log = self
        return span.id or ""

    def ensure(self, span: SessionSpan) -> None:
        """Give the span a row before the call it is about to make, so a session that is
        still in flight can already be found. Nothing to do when nothing is stored."""
        if span.id is None and session_log_mode() != OFF:
            self.open(span)

    def call(
        self,
        span: SessionSpan,
        *,
        tag: str,
        model: str,
        system: str | None,
        input: Any,
        output_schema: Any,
        output: Any,
        decisions: list[Any],
        tool_calls: list[Any],
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        duration_s: float,
        started_at: Any,
        status: str = "ok",
        error: str | None = None,
    ) -> str | None:
        """Record one exchange, and bring the session's totals up to date."""
        mode = session_log_mode()
        if mode == OFF:
            span.add(input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost_usd)
            return None
        if span.id is None:
            self.open(span)
        seq = span.add(input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost_usd)
        limit = max_chars()
        bodies = mode == FULL
        with self.db.session() as s:
            call = ModelCall(
                session_id=span.id,
                seq=seq,
                tag=tag[:200],
                model=model[:100],
                run_id=span.run_id,
                step_run_id=span.step_run_id,
                step_id=span.step_id,
                fanout_index=span.fanout_index,
                system=clip_text(system, limit) if (bodies and system) else None,
                input=clip_json(input, limit) if bodies else None,
                output_schema=output_schema if bodies else None,
                output=clip_json(output, limit) if bodies else None,
                decisions=decisions if bodies else [],
                tool_calls=tool_calls if bodies else _tool_names(tool_calls),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=round(cost_usd, 6),
                duration_s=round(duration_s, 3),
                status=status,
                error=error,
                started_at=started_at,
                finished_at=now(),
            )
            s.add(call)
            sess = s.get(AgentSession, span.id)
            if sess is not None:
                sess.calls = span.calls
                sess.input_tokens = span.input_tokens
                sess.output_tokens = span.output_tokens
                sess.cost_usd = span.cost_usd
            s.flush()
            call_id = call.id
        self._write_to_disk(span.id)
        return call_id

    def close(self, span: SessionSpan, *, status: str, error: str | None) -> None:
        if span.id is None:
            return
        with self.db.session() as s:
            sess = s.get(AgentSession, span.id)
            if sess is None:
                return
            sess.status = status
            sess.error = error
            sess.finished_at = now()
        self._write_to_disk(span.id)

    def _write_to_disk(self, session_id: str | None) -> None:
        """Mirror the session, as it stands, to its own file — so it can be read back
        without the database, and survives the process that wrote it exiting."""
        if session_id is None:
            return
        try:
            record = self.get(session_id)
            session_file(session_id).write_text(
                json.dumps(record, default=str, ensure_ascii=False), encoding="utf-8"
            )
        except (KeyError, OSError):
            pass

    def link_audit(self, session_id: str | None, audit_id: str) -> None:
        """An audit row is written after the session that drafted it; join them up."""
        if not session_id:
            return
        with self.db.session() as s:
            sess = s.get(AgentSession, session_id)
            if sess is not None:
                sess.audit_id = audit_id

    # -- reading ---------------------------------------------------------------

    def list(
        self,
        *,
        run_id: str | None = None,
        audit_id: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self.db.session() as s:
            q = select(AgentSession).order_by(AgentSession.started_at.desc())
            if run_id:
                q = q.where(AgentSession.run_id == run_id)
            if audit_id:
                q = q.where(AgentSession.audit_id == audit_id)
            if kind:
                q = q.where(AgentSession.kind == kind)
            rows = s.scalars(q.limit(limit)).all()
            return [_session_view(r) for r in rows]

    def get(self, session_id: str, *, bodies: bool = True) -> dict[str, Any]:
        with self.db.session() as s:
            sess = s.get(AgentSession, session_id)
            if sess is None:
                raise KeyError(session_id)
            calls = s.scalars(
                select(ModelCall).where(ModelCall.session_id == session_id).order_by(ModelCall.seq)
            ).all()
            return {
                **_session_view(sess),
                "calls_detail": [_call_view(c, bodies=bodies) for c in calls],
            }

    # -- reading, from the mirrored files ---------------------------------------

    def list_from_disk(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Sessions as they were last written to ``SESSION_ROOT``, newest first. Reads
        only the files, so it works without a database — after a restart, or from a
        process that never had one."""
        files = sorted(session_root().glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for f in files[:limit]:
            try:
                record = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            out.append({k: v for k, v in record.items() if k != "calls_detail"})
        return out

    def get_from_disk(self, session_id: str) -> dict[str, Any]:
        """One session, read back from its file rather than the database."""
        f = session_file(session_id)
        if not f.exists():
            raise KeyError(session_id)
        return json.loads(f.read_text(encoding="utf-8"))


def _session_view(r: AgentSession) -> dict[str, Any]:
    return {
        "id": r.id,
        "kind": r.kind,
        "name": r.name,
        "title": r.title,
        "run_id": r.run_id,
        "audit_id": r.audit_id,
        "mode": r.mode,
        "status": r.status,
        "error": r.error,
        "calls": r.calls,
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
        "cost_usd": r.cost_usd,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
    }


def _call_view(c: ModelCall, *, bodies: bool = True) -> dict[str, Any]:
    view: dict[str, Any] = {
        "id": c.id,
        "seq": c.seq,
        "tag": c.tag,
        "model": c.model,
        "step_id": c.step_id,
        "fanout_index": c.fanout_index,
        "input_tokens": c.input_tokens,
        "output_tokens": c.output_tokens,
        "cost_usd": c.cost_usd,
        "duration_s": c.duration_s,
        "status": c.status,
        "error": c.error,
        "tool_calls": c.tool_calls,
        "started_at": c.started_at.isoformat() if c.started_at else None,
    }
    if bodies:
        view.update(
            {
                "system": c.system,
                "input": c.input,
                "output_schema": c.output_schema,
                "output": c.output,
                "decisions": c.decisions,
            }
        )
    return view
