"""Where log lines go, how loud they are, and which ones are noise.

One place decides it, read from the environment like the model names next door:

| `WF_LOG_LEVEL`        | `debug`, `info`, `warning`, `error` (default `info`)      |
| `WF_LOG_FORMAT`       | `text` or `json` (default `text`)                         |
| `WF_LOG_FILE`         | a path; unset means stderr                                |
| `WF_LOG_HEALTHCHECK`  | `on` puts the healthcheck back in the main log            |
| `WF_HEALTH_LOG_FILE`  | a path; the healthcheck's lines go there instead of away  |

The container's healthcheck asks for ``/healthz`` every thirty seconds, so its access
lines are taken out of the main log by default: they say nothing that the absence of
error lines does not already say. They are dropped, or kept in a file of their own,
or put back in line, depending on the two variables above. Nothing else is filtered:
a log that hides anything but its own heartbeat cannot be trusted.

``debug`` is the level that shows the agentic sessions in full — every prompt, every
answer. See ``wf.store.sessions`` for what is written down rather than printed.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

ROOT = "wf"
SESSIONS = "wf.session"
HEALTH = "wf.health"

#: The paths whose access lines are the container's heartbeat, not traffic.
HEALTH_PATHS = ("/healthz",)

_HANDLER_NAME = "wf-log"
_TEXT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s  %(message)s"
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

_configured = False


# -- what the environment says -------------------------------------------------


def log_level() -> int:
    raw = (os.environ.get("WF_LOG_LEVEL") or "info").strip().upper()
    return logging.getLevelNamesMapping().get(raw, logging.INFO)


def log_format() -> str:
    return (
        "json" if (os.environ.get("WF_LOG_FORMAT") or "text").strip().lower() == "json" else "text"
    )


def healthcheck_in_main_log() -> bool:
    return (os.environ.get("WF_LOG_HEALTHCHECK") or "").strip().lower() in (
        "1",
        "on",
        "true",
        "yes",
    )


def health_log_file() -> str | None:
    return (os.environ.get("WF_HEALTH_LOG_FILE") or "").strip() or None


# -- formatting ----------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Structured context travels as ``extra={"fields": {...}}``."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "time": self.formatTime(record, _TIME_FORMAT),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            out.update(fields)
        if record.exc_info:
            out["exception"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str, ensure_ascii=False)


def _formatter() -> logging.Formatter:
    if log_format() == "json":
        return JsonFormatter()
    return logging.Formatter(_TEXT_FORMAT, datefmt=_TIME_FORMAT)


def _handler(path: str | None) -> logging.Handler:
    """A file handler for a path, a stream handler otherwise.

    A path that cannot be opened — a read-only filesystem, a directory that is not
    ours — falls back to the stream and says so. Logging has to survive its own
    configuration being wrong.
    """
    if path:
        try:
            target = Path(path).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            h: logging.Handler = logging.FileHandler(target, encoding="utf-8")
        except OSError as e:
            h = logging.StreamHandler()
            h.setFormatter(_formatter())
            logging.getLogger(ROOT).warning("cannot write the log to %s: %s", path, e)
            return h
    else:
        h = logging.StreamHandler()
    h.setFormatter(_formatter())
    return h


# -- the healthcheck -----------------------------------------------------------


def _requested_path(record: logging.LogRecord) -> str:
    """The path in an access record. Uvicorn passes (client, method, path, version, status)."""
    args = record.args
    if isinstance(args, tuple) and len(args) >= 3:
        return str(args[2]).split("?", 1)[0]
    return record.getMessage()


class HealthCheckFilter(logging.Filter):
    """Keeps the heartbeat out of the main log, and optionally in a log of its own."""

    def __init__(self, paths: tuple[str, ...] = HEALTH_PATHS, sink: logging.Logger | None = None):
        super().__init__()
        self.paths = paths
        self.sink = sink

    def is_healthcheck(self, record: logging.LogRecord) -> bool:
        path = _requested_path(record)
        return any(path == p or path.endswith(f" {p}") or f" {p} " in path for p in self.paths)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.is_healthcheck(record):
            return True
        if self.sink is not None:
            self.sink.handle(record)
        return False


def health_logger() -> logging.Logger | None:
    """The separate log for the healthcheck, when a file is named for it."""
    path = health_log_file()
    if not path:
        return None
    lg = logging.getLogger(HEALTH)
    lg.propagate = False
    lg.setLevel(logging.INFO)
    if not any(h.name == _HANDLER_NAME for h in lg.handlers):
        for h in list(lg.handlers):
            lg.removeHandler(h)
            h.close()
        h = _handler(path)
        if not isinstance(h, logging.FileHandler):
            return None  # nowhere to put them: the filter drops them instead
        h.set_name(_HANDLER_NAME)
        lg.addHandler(h)
    return lg


def _install_health_filter() -> None:
    access = logging.getLogger("uvicorn.access")
    for f in list(access.filters):
        if isinstance(f, HealthCheckFilter):
            access.removeFilter(f)
    if healthcheck_in_main_log():
        return
    access.addFilter(HealthCheckFilter(sink=health_logger()))


# -- setup ---------------------------------------------------------------------


def setup_logging(*, force: bool = False) -> None:
    """Configure logging from the environment. Safe to call more than once.

    Only handlers this function installed are replaced, so a test harness or an
    embedding application keeps its own.
    """
    global _configured
    if _configured and not force:
        return

    level = log_level()
    root = logging.getLogger()
    for h in list(root.handlers):
        if h.name == _HANDLER_NAME:
            root.removeHandler(h)
            h.close()
    handler = _handler(os.environ.get("WF_LOG_FILE"))
    handler.set_name(_HANDLER_NAME)
    root.addHandler(handler)
    root.setLevel(min(level, logging.WARNING))
    logging.getLogger(ROOT).setLevel(level)

    # Uvicorn installs handlers of its own when it configures logging; serve() asks it
    # not to, so its loggers reach the handler above and obey the same level.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            lg.removeHandler(h)
        lg.propagate = True
        lg.setLevel(level)

    _install_health_filter()
    _configured = True


def get_logger(name: str = ROOT) -> logging.Logger:
    return logging.getLogger(name)
