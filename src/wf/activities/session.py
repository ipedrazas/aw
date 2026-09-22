"""Recording the exchanges a session is made of.

Every model call in the system — a step, the extraction that reads a document, the
chat that edits a draft, a guess that fills a gap — goes through
``ModelActivity.complete``. Wrapping that one method is therefore the only place that
sees all of them, and the only place that has to know how they are written down.

Two sinks, two purposes:

* the log, at ``info``: one line per call, saying what was asked and what it cost.
  At ``debug`` the same line carries the instructions, the input and the answer, for
  when you are watching a session happen (``WF_LOG_LEVEL=debug``);
* the store: a row under the open session, which is what you read afterwards
  (``wf sessions``, ``/api/sessions``).

The wrapper is transparent: it returns what the model returned and re-raises what the
model raised, having recorded the failure first.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from wf.logs import SESSIONS, get_logger
from wf.store.db import Database
from wf.store.records import now
from wf.store.sessions import SessionLog, clip_json, max_chars, span_or_loose

from .base import ModelActivity, ModelRequest, ModelResponse

logger = get_logger(SESSIONS)

#: How much of a body reaches a debug log line. The store keeps more; see WF_SESSION_MAX_CHARS.
LOG_BODY_CHARS = 4000


class SessionRecorder:
    """A model activity that records what passes through it."""

    def __init__(
        self,
        inner: ModelActivity,
        db: Database | None = None,
        *,
        log: SessionLog | None = None,
    ):
        self.inner = inner
        self.log = log or SessionLog(db)

    # Callers reach for attributes of the real model here and there (max_tool_rounds,
    # a scripted model's recorded requests); a wrapper should not hide them.
    def __getattr__(self, name: str) -> Any:
        if name in ("inner", "log"):  # asked for before __init__ set them
            raise AttributeError(name)
        return getattr(self.inner, name)

    def complete(self, request: ModelRequest) -> ModelResponse:
        span = span_or_loose()
        self.log.ensure(span)
        started = now()
        t0 = time.perf_counter()
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "asking %s for %s",
                request.model,
                request.tag,
                extra={
                    "fields": {
                        **_request_fields(span, request),
                        "event": "model.request",
                        "system": _brief(request.system),
                        "input": _brief(request.input),
                    }
                },
            )
        try:
            resp = self.inner.complete(request)
        except Exception as e:
            elapsed = time.perf_counter() - t0
            self._record(span, request, None, elapsed, started, error=f"{type(e).__name__}: {e}")
            logger.warning(
                "%s failed after %.2fs: %s",
                request.tag,
                elapsed,
                e,
                extra={
                    "fields": {
                        **_request_fields(span, request),
                        "event": "model.failed",
                        "duration_s": round(elapsed, 3),
                        "error": str(e),
                    }
                },
            )
            raise
        elapsed = time.perf_counter() - t0
        self._record(span, request, resp, elapsed, started)
        logger.info(
            "%s answered by %s in %.2fs (%d in, %d out, $%.4f)",
            request.tag,
            resp.model or request.model,
            elapsed,
            resp.usage.input_tokens,
            resp.usage.output_tokens,
            resp.usage.cost_usd,
            extra={
                "fields": {
                    **_request_fields(span, request),
                    "event": "model.answered",
                    "model": resp.model or request.model,
                    "duration_s": round(elapsed, 3),
                    "input_tokens": resp.usage.input_tokens,
                    "output_tokens": resp.usage.output_tokens,
                    "cost_usd": resp.usage.cost_usd,
                    "tool_calls": len(resp.tool_calls),
                    "decisions": len(resp.decisions),
                }
            },
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "%s answered:\n%s",
                request.tag,
                _brief(resp.output),
                extra={
                    "fields": {
                        "event": "model.answer",
                        "tag": request.tag,
                        "session": span.id,
                        "output": _brief(resp.output),
                        "decisions": resp.decisions,
                        "tools": [c.name for c in resp.tool_calls],
                    }
                },
            )
        return resp

    def _record(
        self,
        span: Any,
        request: ModelRequest,
        resp: ModelResponse | None,
        elapsed: float,
        started: Any,
        *,
        error: str | None = None,
    ) -> None:
        try:
            self.log.call(
                span,
                tag=request.tag,
                model=(resp.model if resp and resp.model else request.model),
                system=request.system,
                input=request.input,
                output_schema=request.output_schema,
                output=resp.output if resp else None,
                decisions=list(resp.decisions) if resp else [],
                tool_calls=[_tool_view(c) for c in (resp.tool_calls if resp else [])],
                input_tokens=resp.usage.input_tokens if resp else 0,
                output_tokens=resp.usage.output_tokens if resp else 0,
                cost_usd=resp.usage.cost_usd if resp else 0.0,
                duration_s=elapsed,
                started_at=started,
                status="ok" if resp else "failed",
                error=error,
            )
        except Exception as e:  # noqa: BLE001 - recording must never break the work
            logger.warning(
                "this exchange could not be recorded: %s",
                e,
                extra={"fields": {"event": "session.write_failed", "tag": request.tag}},
            )


def record_sessions(model: ModelActivity, db: Database | None = None) -> ModelActivity:
    """Wrap a model so its exchanges are logged and stored. Wrapping twice is a no-op."""
    if isinstance(model, SessionRecorder):
        return model
    return SessionRecorder(model, db)


def _request_fields(span: Any, request: ModelRequest) -> dict[str, Any]:
    """Who is asking, and about what. The bodies are added by the line that wants them."""
    fields: dict[str, Any] = {
        "tag": request.tag,
        "model": request.model,
        "session": span.id,
        "session_kind": span.kind,
    }
    if span.run_id:
        fields["run"] = span.run_id
    if span.step_id:
        fields["step"] = span.step_id
    if span.fanout_index is not None:
        fields["item"] = span.fanout_index
    if request.tools:
        fields["tools_offered"] = [t.name for t in request.tools]
    return fields


def _tool_view(call: Any) -> dict[str, Any]:
    """A tool call as the session keeps it: what was asked, and what came back.

    The step record keeps only the summary; a session is where you look when the
    summary was wrong about the page it summarised.
    """
    view: dict[str, Any] = {"name": call.name, "input": call.input, "summary": call.summary}
    if call.injection:
        view["injection"] = call.injection
    if getattr(call, "result", None) is not None:
        view["result"] = clip_json(call.result, max_chars())
    return view


def _brief(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    if len(text) > LOG_BODY_CHARS:
        return f"{text[:LOG_BODY_CHARS]}… (+{len(text) - LOG_BODY_CHARS} characters)"
    return text
