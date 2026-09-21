"""Postgres records (SQLAlchemy 2.0). SQLite is used for tests and zero-config local runs.

Every artefact traces to a step run, which traces to an instruction commit and a
model. That chain is the audit trail, and it is enforced by the foreign keys here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class WorkflowVersion(Base):
    __tablename__ = "workflow_version"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), index=True)
    version: Mapped[int] = mapped_column(Integer)
    commit: Mapped[str | None] = mapped_column(
        String(80)
    )  # a git sha, or sha256:<hex> when there is no git
    schema_version: Mapped[str] = mapped_column(String(64))
    definition_yaml: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Run(Base):
    __tablename__ = "run"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workflow_version_id: Mapped[str] = mapped_column(ForeignKey("workflow_version.id"))
    workflow_name: Mapped[str] = mapped_column(String(200), index=True)
    title: Mapped[str] = mapped_column(String(500), default="")
    mode: Mapped[str] = mapped_column(String(8))  # live | dry | eval
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    outputs: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="running")
    depth: Mapped[int] = mapped_column(Integer, default=0)
    parent_run_id: Mapped[str | None] = mapped_column(ForeignKey("run.id"), nullable=True)
    parent_step_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    spent_usd: Mapped[float] = mapped_column(Float, default=0.0)
    spent_minutes: Mapped[float] = mapped_column(Float, default=0.0)
    case_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    step_runs: Mapped[list[StepRun]] = relationship(back_populates="run", order_by="StepRun.seq")
    decisions: Mapped[list[Decision]] = relationship(order_by="Decision.seq")
    artifacts: Mapped[list[Artifact]] = relationship()
    expectations: Mapped[list[Expectation]] = relationship()


class StepRun(Base):
    __tablename__ = "step_run"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("run.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    step_id: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(300), default="")
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), default="running")
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    fanout_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    output: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    instruction_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    instruction_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    instruction_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tool_calls: Mapped[list[Any]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[Run] = relationship(back_populates="step_runs")
    decisions: Mapped[list[Decision]] = relationship(order_by="Decision.seq")


class Decision(Base):
    """User-facing language, not a trace. ``kind`` says who is speaking:

    decision      the agent, about its own work
    guess         the interpreter, filling an unanswered required field
    control       the interpreter, about control flow (skipped, capped, would start)
    ignored       the runtime, about instructions found in data and ignored
    """

    __tablename__ = "decision"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("run.id"), index=True)
    step_run_id: Mapped[str | None] = mapped_column(ForeignKey("step_run.id"), nullable=True)
    step_id: Mapped[str] = mapped_column(String(100))
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16), default="decision")
    text: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text, default="")
    alternatives: Mapped[list[Any]] = mapped_column(JSON, default=list)
    finding_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    field: Mapped[str | None] = mapped_column(String(300), nullable=True)
    value: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class FindingRecord(Base):
    __tablename__ = "finding"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    finding_id: Mapped[str] = mapped_column(String(32), index=True)  # the Finding model's stable id
    source: Mapped[str] = mapped_column(String(8))  # audit | run
    audit_id: Mapped[str | None] = mapped_column(ForeignKey("audit.id"), nullable=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("run.id"), nullable=True)
    type: Mapped[str] = mapped_column(String(16))
    step_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    field: Mapped[str] = mapped_column(String(300))
    question: Mapped[str] = mapped_column(Text)
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Artifact(Base):
    __tablename__ = "artifact"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("run.id"), index=True)
    step_run_id: Mapped[str] = mapped_column(ForeignKey("step_run.id"))
    name: Mapped[str] = mapped_column(String(300))
    path: Mapped[str] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(String(100), default="application/octet-stream")
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    sha256: Mapped[str] = mapped_column(String(64))
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Expectation(Base):
    __tablename__ = "expectation"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("run.id"), index=True)
    step_id: Mapped[str] = mapped_column(String(100))
    field: Mapped[str] = mapped_column(String(300))
    equals: Mapped[dict[str, Any]] = mapped_column(JSON)  # {"value": ...}
    why: Mapped[str] = mapped_column(Text, default="")
    matched: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    actual: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(300), default="")
    document: Mapped[str] = mapped_column(Text)
    passages: Mapped[list[Any]] = mapped_column(JSON, default=list)
    draft: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # the workflow definition
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # field -> passage id
    findings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    chat: Mapped[list[Any]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    saved_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

    changes: Mapped[list[DraftChange]] = relationship(order_by="DraftChange.seq")


class DraftChange(Base):
    """One edit to a draft, with its reason, so it can be shown and undone."""

    __tablename__ = "draft_change"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    audit_id: Mapped[str] = mapped_column(ForeignKey("audit.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    by: Mapped[str] = mapped_column(String(16))  # user | assistant | answer
    path: Mapped[str] = mapped_column(String(300))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    undone: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
