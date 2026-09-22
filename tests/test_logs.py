"""Logging is configured in one place, and the healthcheck is not allowed to fill it."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from wf import logs

LOG_VARS = (
    "WF_LOG_LEVEL",
    "WF_LOG_FORMAT",
    "WF_LOG_FILE",
    "WF_LOG_HEALTHCHECK",
    "WF_HEALTH_LOG_FILE",
)


def clear(monkeypatch) -> None:
    for var in LOG_VARS:
        monkeypatch.delenv(var, raising=False)


def access_record(path: str, status: int = 200) -> logging.LogRecord:
    """An access record shaped the way uvicorn shapes one."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("172.17.0.1:53000", "GET", path, "1.1", status),
        exc_info=None,
    )


def test_the_level_comes_from_the_environment(monkeypatch):
    clear(monkeypatch)
    assert logs.log_level() == logging.INFO, "info unless asked otherwise"
    monkeypatch.setenv("WF_LOG_LEVEL", "debug")
    assert logs.log_level() == logging.DEBUG
    monkeypatch.setenv("WF_LOG_LEVEL", "WARNING")
    assert logs.log_level() == logging.WARNING
    monkeypatch.setenv("WF_LOG_LEVEL", "shouty")
    assert logs.log_level() == logging.INFO, "a name nobody recognises falls back, it does not fail"


def test_the_healthcheck_is_dropped_by_default(monkeypatch):
    clear(monkeypatch)
    f = logs.HealthCheckFilter()
    assert f.filter(access_record("/healthz")) is False
    assert f.filter(access_record("/healthz?probe=1")) is False
    assert f.filter(access_record("/api/runs")) is True, "only the heartbeat is filtered"
    assert f.filter(access_record("/workflows/healthzoo")) is True


def test_the_healthcheck_can_have_a_log_of_its_own(tmp_path: Path, monkeypatch):
    clear(monkeypatch)
    path = tmp_path / "health" / "health.log"
    monkeypatch.setenv("WF_HEALTH_LOG_FILE", str(path))
    sink = logs.health_logger()
    assert sink is not None and sink.propagate is False, "it must not leak back into the main log"

    f = logs.HealthCheckFilter(sink=sink)
    assert f.filter(access_record("/healthz")) is False, "still out of the main log"
    for h in sink.handlers:
        h.flush()
    assert "/healthz" in path.read_text(), "and in its own file instead"


def test_the_healthcheck_can_be_put_back_in_line(monkeypatch):
    clear(monkeypatch)
    monkeypatch.setenv("WF_LOG_HEALTHCHECK", "on")
    logs.setup_logging(force=True)
    access = logging.getLogger("uvicorn.access")
    assert not [f for f in access.filters if isinstance(f, logs.HealthCheckFilter)]

    monkeypatch.delenv("WF_LOG_HEALTHCHECK")
    logs.setup_logging(force=True)
    installed = [f for f in access.filters if isinstance(f, logs.HealthCheckFilter)]
    assert len(installed) == 1, "and setting up twice does not stack filters"


def test_json_format_carries_the_structured_fields(monkeypatch, capsys):
    clear(monkeypatch)
    monkeypatch.setenv("WF_LOG_FORMAT", "json")
    formatter = logs.JsonFormatter()
    record = logging.LogRecord(
        "wf.session", logging.INFO, __file__, 1, "asked %s", ("a model",), None
    )
    record.fields = {"event": "model.answered", "cost_usd": 0.01}
    line = json.loads(formatter.format(record))
    assert line["message"] == "asked a model"
    assert line["level"] == "info" and line["logger"] == "wf.session"
    assert line["event"] == "model.answered" and line["cost_usd"] == 0.01


def test_setup_writes_to_the_file_it_is_given_and_leaves_other_handlers_alone(
    tmp_path: Path, monkeypatch
):
    clear(monkeypatch)
    root = logging.getLogger()
    foreign = logging.StreamHandler()
    root.addHandler(foreign)
    try:
        path = tmp_path / "logs" / "wf.log"
        monkeypatch.setenv("WF_LOG_FILE", str(path))
        monkeypatch.setenv("WF_LOG_LEVEL", "debug")
        logs.setup_logging(force=True)
        logs.get_logger("wf.test").debug("a line about a session")
        for h in root.handlers:
            h.flush()
        assert "a line about a session" in path.read_text()
        assert foreign in root.handlers, "a handler we did not install is not ours to remove"
        assert len([h for h in root.handlers if h.name == "wf-log"]) == 1
    finally:
        root.removeHandler(foreign)
        monkeypatch.delenv("WF_LOG_FILE")
        logs.setup_logging(force=True)
