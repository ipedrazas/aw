"""Model names are configuration. The code names them once, in wf.settings, and asks
for a level of judgement everywhere else."""

from __future__ import annotations

import re
from pathlib import Path

from wf import settings
from wf.activities import ModelResponse, ScriptedModel, cost_of
from wf.activities.guess import ModelGuesser
from wf.audit import Auditor
from wf.audit.chat import chat
from wf.audit.service import AuditResult

SRC = Path(__file__).resolve().parents[1] / "src" / "wf"

MODEL_VARS = (
    "WF_QUICK_MODEL",
    "WF_CAREFUL_MODEL",
    "WF_EXTRACTION_MODEL",
    "WF_CHAT_MODEL",
    "WF_GUESS_MODEL",
)


def clear(monkeypatch) -> None:
    for var in MODEL_VARS:
        monkeypatch.delenv(var, raising=False)


def test_each_use_falls_back_to_a_level_of_judgement(monkeypatch):
    clear(monkeypatch)
    monkeypatch.setenv("WF_QUICK_MODEL", "quick-one")
    monkeypatch.setenv("WF_CAREFUL_MODEL", "careful-one")

    assert settings.extraction_model() == "careful-one", "reading a document is careful work"
    assert settings.chat_model() == "quick-one"
    assert settings.guess_model() == "quick-one"
    assert settings.model_for("careful") == "careful-one"
    assert settings.model_for(None) == "quick-one"

    monkeypatch.setenv("WF_GUESS_MODEL", "guess-one")
    assert settings.guess_model() == "guess-one", "its own variable wins over the fallback"


def test_the_configured_names_reach_the_things_that_use_them(ws, monkeypatch):
    clear(monkeypatch)
    monkeypatch.setenv("WF_EXTRACTION_MODEL", "extract-one")
    monkeypatch.setenv("WF_CHAT_MODEL", "chat-one")
    monkeypatch.setenv("WF_GUESS_MODEL", "guess-one")
    model = ScriptedModel(
        lambda req: ModelResponse(
            output={"reply": "ok", "edits": [], "answers": [], "point_to_finding": None}
        )
    )

    assert Auditor(ws, model).extraction_model == "extract-one"
    assert ModelGuesser(model).model_name == "guess-one"

    chat(model, _empty_result(), [], "hello")
    assert [r.model for r in model.requests] == ["chat-one"]


def test_an_unpriced_model_is_costed_as_the_careful_one(monkeypatch):
    monkeypatch.delenv("WF_MODEL_PRICING", raising=False)
    assert cost_of("a-model-with-no-price", 1_000_000, 0) == cost_of(
        settings.DEFAULT_CAREFUL, 1_000_000, 0
    )


def test_no_model_name_is_written_anywhere_but_settings():
    stray = {
        path.relative_to(SRC).as_posix(): sorted(
            set(re.findall(r"claude-[\w.-]+", path.read_text()))
        )
        for path in SRC.rglob("*.py")
        if path.name != "settings.py"
    }
    assert {k: v for k, v in stray.items() if v} == {}, "model names belong in wf/settings.py"


def _empty_result() -> AuditResult:
    return AuditResult(
        name="empty", title="Empty", passages=[], definition={}, provenance={}, findings=[]
    )
