"""What the process says about itself when it loads.

Five model names, a provider, a key and a price table decide what every call in this
system costs and who answers it, and all of them come from the environment. Read
afterwards, in a bill or a session record, a mistake there is archaeology; read in the
first two lines of the log it is obvious. So the app announces its plan before it does
any work:

* ``info``: two lines — the provider, whether its key is set, and the model each task
  will be asked of;
* ``debug``: a line per task saying where the name came from and what it costs, and a
  line per agent step of every definition in the workspace saying which model will run
  it, or that the step does not say and the run will have to guess.

Announcing is once per process (``wf serve`` loads the app in the same process that
parsed the command line, and should not say it twice). ``wf.logs`` decides where these
lines go; ``wf.settings`` decides what is in them.
"""

from __future__ import annotations

import logging
from typing import Any

from wf.logs import ROOT, get_logger
from wf.schema import Workspace
from wf.settings import model_plan

logger = get_logger(f"{ROOT}.startup")

_announced = False


def announce(
    ws: Workspace | None = None, *, offline: bool | None = None, force: bool = False
) -> None:
    """Log the model plan, and at ``debug`` the model behind each step in ``ws``.

    ``offline`` overrides what the environment says, for a caller that was handed the
    offline model directly rather than asking for it with ``WF_FAKE_MODEL``.
    """
    global _announced
    if _announced and not force:
        return
    _announced = True

    plan = model_plan()
    if offline is not None:
        plan["offline"] = offline
    _announce_plan(plan)
    if ws is not None and logger.isEnabledFor(logging.DEBUG):
        _announce_steps(ws, plan)


def reset() -> None:
    """Forget that the plan was announced. For tests, and for a reload."""
    global _announced
    _announced = False


# -- the plan --------------------------------------------------------------------


def _announce_plan(plan: dict[str, Any]) -> None:
    if plan["offline"]:
        # The names below are still the ones a real run would use, so they are worth
        # printing; nothing is sent anywhere while this is on.
        logger.info(
            "the offline model answers everything; no call leaves this process "
            "(%s would have been the provider)",
            plan["provider"],
        )
    else:
        gateway = f", gateway {plan['base_url']}" if plan.get("base_url") else ""
        strict = (
            f", schemas {'enforced' if plan.get('strict_schemas') else 'suggested'}"
            if plan.get("base_url")
            else ""
        )
        logger.info(
            "provider %s (named by %s), %s is %s%s%s",
            plan["provider"],
            plan["provider_named_by"],
            plan["api_key_variable"],
            "set" if plan["api_key_set"] else "NOT set",
            gateway,
            strict,
        )
    logger.info(
        "models by task: %s; an answer may run to %s tokens",
        ", ".join(f"{t['task']}={t['model']}" for t in plan["tasks"]),
        plan["max_output_tokens"],
        extra={
            "fields": {
                "event": "models.plan",
                "max_output_tokens": plan["max_output_tokens"],
                "provider": plan["provider"],
                "provider_named_by": plan["provider_named_by"],
                "api_key_set": plan["api_key_set"],
                "offline": plan["offline"],
                "pricing_from": plan["pricing_from"],
                "base_url": plan.get("base_url"),
                "strict_schemas": plan.get("strict_schemas"),
                "models": {t["task"]: t["model"] for t in plan["tasks"]},
            }
        },
    )
    if not plan["api_key_set"] and not plan["offline"]:
        logger.warning(
            "%s is not set: the first model call will fail. WF_FAKE_MODEL=1 runs offline.",
            plan["api_key_variable"],
            extra={"fields": {"event": "models.no_key", "variable": plan["api_key_variable"]}},
        )
    for t in plan["tasks"]:
        inp, out = t["price_per_mtok"]
        logger.debug(
            "  %-11s %-44s %s (%s), $%s/$%s per Mtok%s",
            t["task"],
            t["what"],
            t["model"],
            "named by " + t["named_by"] if t["named_by"] != "default" else "the default",
            inp,
            out,
            "" if t["priced"] else " — no price of its own, costed as the careful model",
            extra={"fields": {"event": "models.task", **t}},
        )


# -- what the workspace asks for --------------------------------------------------


def _announce_steps(ws: Workspace, plan: dict[str, Any]) -> None:
    """One line per agent step: which model it will run on, and who decided.

    A step that names no model is a gap the run has to fill — by guess in a dry run,
    by the quick default otherwise — so it is worth seeing here rather than in the
    middle of a run.
    """
    quick = next(t["model"] for t in plan["tasks"] if t["task"] == "quick")
    for name in ws.list_definitions():
        try:
            wf = ws.load_definition(name)
        except Exception as e:  # noqa: BLE001 - a broken definition is a log line, not a crash
            logger.debug(
                "%s: cannot be read, so its models are unknown: %s",
                name,
                e,
                extra={"fields": {"event": "models.step_unknown", "workflow": name}},
            )
            continue
        for step in wf.spec.steps:
            if step.kind != "agent":
                continue
            model = wf.model_for(step) or quick
            declared = wf.model_for(step) is not None
            logger.debug(
                "  %s.%s runs on %s%s",
                name,
                step.id,
                model,
                "" if declared else " (the step names none; a run guesses, or falls back to quick)",
                extra={
                    "fields": {
                        "event": "models.step",
                        "workflow": name,
                        "step": step.id,
                        "model": model,
                        "declared": declared,
                    }
                },
            )
