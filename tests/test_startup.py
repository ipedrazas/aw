"""What the log says when the app loads: which model each task will be asked of, and
through whom. A wrong model is cheap to see here and expensive to see in the bill."""

from __future__ import annotations

import logging

import pytest

from wf import settings, startup
from wf.schema import Workspace

ENV = (
    "WF_MODEL_PROVIDER",
    "WF_QUICK_MODEL",
    "WF_CAREFUL_MODEL",
    "WF_EXTRACTION_MODEL",
    "WF_CHAT_MODEL",
    "WF_GUESS_MODEL",
    "WF_MODEL_PRICING",
    "WF_OPENROUTER_STRICT",
    "WF_FAKE_MODEL",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for var in ENV:
        monkeypatch.delenv(var, raising=False)
    startup.reset()
    yield
    startup.reset()


def announce(caplog, ws: Workspace | None = None, **kw) -> list[logging.LogRecord]:
    with caplog.at_level(logging.DEBUG, logger="wf"):
        startup.announce(ws, **kw)
    return list(caplog.records)


def fields(records: list[logging.LogRecord], event: str) -> list[dict]:
    return [r.fields for r in records if getattr(r, "fields", {}).get("event") == event]


# -- the plan -------------------------------------------------------------------


def test_the_plan_is_every_task_and_the_model_it_will_ask(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-a-real-key")
    monkeypatch.setenv("WF_CAREFUL_MODEL", "a-careful-one")

    plan = settings.model_plan()

    assert plan["provider"] == settings.ANTHROPIC
    assert plan["api_key_set"] is True
    assert {t["task"] for t in plan["tasks"]} == {task for task, _, _ in settings.TASKS}
    by_task = {t["task"]: t for t in plan["tasks"]}
    assert by_task["careful"]["model"] == "a-careful-one"
    assert by_task["careful"]["named_by"] == "WF_CAREFUL_MODEL"
    assert by_task["extraction"]["model"] == "a-careful-one", "it follows careful judgement"
    assert by_task["extraction"]["named_by"] == "default"
    assert by_task["careful"]["priced"] is False, "a model we do not price is flagged as such"


def test_the_plan_carries_no_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-a-real-key")
    assert "sk-not-a-real-key" not in repr(settings.model_plan())


def test_the_gateway_is_named_when_it_is_the_one_in_use(monkeypatch, caplog):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-not-a-real-key")

    records = announce(caplog)

    (plan,) = fields(records, "models.plan")
    assert plan["provider"] == settings.OPENROUTER
    assert plan["provider_named_by"] == "OPENROUTER_API_KEY", "the key alone decided it"
    assert plan["base_url"] == settings.OPENROUTER_BASE_URL
    assert plan["models"]["quick"] == settings.OPENROUTER_QUICK
    assert settings.OPENROUTER_BASE_URL in caplog.text


def test_a_missing_key_is_a_warning_before_the_first_call(caplog):
    records = announce(caplog)

    assert fields(records, "models.no_key"), "nothing would work; say so at load"
    assert any(r.levelno == logging.WARNING for r in records)


def test_offline_says_so_instead_of_complaining_about_the_key(monkeypatch, caplog):
    monkeypatch.setenv("WF_FAKE_MODEL", "1")

    records = announce(caplog)

    assert not fields(records, "models.no_key"), "no key is needed when nothing is asked"
    assert fields(records, "models.plan")[0]["offline"] is True
    assert "offline model answers everything" in caplog.text


def test_a_caller_holding_the_offline_model_is_believed_over_the_environment(caplog):
    records = announce(caplog, offline=True)

    assert fields(records, "models.plan")[0]["offline"] is True
    assert not fields(records, "models.no_key")


def test_the_plan_is_announced_once_a_process(caplog):
    with caplog.at_level(logging.DEBUG, logger="wf"):
        startup.announce()
        startup.announce()
    assert len(fields(list(caplog.records), "models.plan")) == 1

    with caplog.at_level(logging.DEBUG, logger="wf"):
        startup.announce(force=True)
    assert len(fields(list(caplog.records), "models.plan")) == 2


# -- the steps ------------------------------------------------------------------


def test_every_agent_step_says_which_model_will_run_it(sample_ws, caplog):
    records = announce(caplog, sample_ws)

    steps = fields(records, "models.step")
    assert steps, "the sample workspace has agent steps"
    assert all(s["model"] for s in steps)
    assert {s["workflow"] for s in steps} <= set(sample_ws.list_definitions())
    review = next(s for s in steps if s["step"] == "review")
    assert review["declared"] is True
    assert review["model"] == settings.careful_model()


def test_a_step_that_names_no_model_is_shown_as_the_gap_it_is(ws, caplog):
    path = ws.definition_path("deep-research")
    path.write_text(path.read_text().replace("      model: claude-sonnet-5\n", "", 1))

    records = announce(caplog, ws)

    unnamed = [s for s in fields(records, "models.step") if not s["declared"]]
    assert unnamed, "a step with no model of its own is worth a line"
    assert unnamed[0]["model"] == settings.quick_model(), "what it would fall back to"


def test_a_definition_that_cannot_be_read_does_not_stop_the_rest(ws, caplog):
    ws.definition_path("broken").write_text("this: is not: a workflow\n")

    records = announce(caplog, ws)

    assert fields(records, "models.step_unknown"), "say which one, and carry on"
    assert fields(records, "models.step"), "the readable ones are still announced"


def test_the_steps_are_a_debug_detail(sample_ws, caplog):
    with caplog.at_level(logging.INFO, logger="wf"):
        startup.announce(sample_ws)

    assert fields(list(caplog.records), "models.plan"), "the plan is worth an info line"
    assert not fields(list(caplog.records), "models.step"), "a line per step is not"
