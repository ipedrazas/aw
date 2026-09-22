"""Guessing the most plausible value for an unanswered required field, mid-run.

The interpreter never fails on an open finding in a dry run: it asks the guesser,
records a ``guess`` decision that names the finding, and continues. Each guess is a
place the process is silent, discovered by execution rather than inspection.

``HeuristicGuesser`` is deterministic. ``ModelGuesser`` asks a model to choose among
the finding's options with a reason, and falls back to the heuristic.
"""

from __future__ import annotations

from typing import Any

from wf.schema import Mode
from wf.settings import guess_model, quick_model

from .base import Guess, ModelActivity, ModelRequest


class HeuristicGuesser:
    def guess(self, *, finding: Any, step: Any, state: dict[str, Any], mode: Mode) -> Guess:
        field = finding.field
        key = field.rsplit(".", 1)[-1]
        title = getattr(step, "title", None) or getattr(step, "id", "this step")
        alts = [o.label for o in finding.options] if finding.options else []

        if key == "when":
            return Guess(
                True,
                f"Ran “{title}”.",
                "Nothing says when this step should run, so it ran every time.",
                ["Skip it", "Ask you"],
            )
        if ".continue_on." in field:
            return Guess(
                {"op": "continue"},
                "Carried on to the next step.",
                f"Nothing says what happens on this outcome, so the run continued as if “{title}” had nothing to add.",
                ["Stop and ask", "Run a later step only in this case"],
            )
        if key == "requires_approval":
            return Guess(
                True,
                "Treated it as needing approval.",
                "Nobody is named to sign this off, so the safe reading is that someone must.",
                ["Let it go without approval"],
            )
        if key == "side_effects":
            return Guess(
                ["unknown_external"],
                "Treated it as sending something outside the system.",
                "The step is an action and nothing says what it changes, so it was treated as leaving the system and stubbed.",
                ["Nothing leaves the system"],
            )
        if key == "max_fanout":
            return Guess(
                3,
                "Capped it at 3 at a time.",
                "No ceiling was given for how many of these run at once.",
                ["1", "5"],
            )
        if key == "limits":
            return Guess(
                {"max_depth": 1, "max_fanout": 3, "budget": "inherit"},
                "Allowed one level deeper, 3 at a time, from the same budget.",
                "No limits were given for how far this can go or what it can spend.",
                alts,
            )
        if key == "max_depth":
            return Guess(1, "Allowed one level deeper.", "No depth was given.", ["2"])
        if key == "budget":
            return Guess(
                {"max_usd": 12, "max_minutes": 45},
                "Set a limit of $12 and 45 minutes.",
                "No spending limit was given for the whole run.",
                alts,
            )
        if key == "trust":
            return Guess(
                {"policy": "always_ask"},
                "Treated it as a step that waits for you.",
                "Nothing runs unattended by default.",
                ["Run on its own"],
            )
        if key == "model":
            return Guess(
                quick_model(),
                "Used quick judgement for this step.",
                "No level of judgement was given.",
                ["Careful judgement"],
            )
        if key == "skill":
            return Guess(
                None,
                f"Ran “{title}” from its title and description only.",
                "There is no instruction file for this step, so the model was told only what the step is called and what it is for.",
                ["Write instructions first"],
            )
        if key == "deadline":
            return Guess(
                "1d", "Waited one day at most.", "No deadline was given.", ["4 hours", "A week"]
            )
        if key == "on_timeout":
            return Guess(
                "continue",
                "Carried on when the wait ran out.",
                "Nothing says what happens if nobody answers.",
                alts,
            )
        if key == "run":
            return Guess(
                None,
                f"Skipped “{title}”.",
                "It names a routine the system does not have, so nothing could run.",
                alts,
            )
        if key in ("does_not_check", "checks"):
            return Guess(
                [],
                f"Ran “{title}” without a statement of what it does not verify.",
                "The check ran, but the result should not be read as more than it is.",
                [],
            )
        if key.startswith("enum") or ".output.enum." in field:
            return Guess(
                [],
                "Treated the value as free text.",
                "The possible outcomes were not listed, so nothing branched on it.",
                [],
            )
        if finding.options:
            first = finding.options[0]
            return Guess(first.value, first.label, "Took the first option.", alts[1:])
        return Guess(
            None,
            f"Left “{key}” empty.",
            "No value was given and nothing plausible could be guessed.",
            [],
        )


class ModelGuesser:
    def __init__(
        self,
        model: ModelActivity,
        model_name: str | None = None,
        fallback: HeuristicGuesser | None = None,
    ):
        self.model = model
        self.model_name = model_name or guess_model()
        self.fallback = fallback or HeuristicGuesser()

    def guess(self, *, finding: Any, step: Any, state: dict[str, Any], mode: Mode) -> Guess:
        if not finding.options:
            return self.fallback.guess(finding=finding, step=step, state=state, mode=mode)
        labels = [o.label for o in finding.options]
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["choice", "reason"],
            "properties": {
                "choice": {"type": "integer", "minimum": 0, "maximum": len(labels) - 1},
                "reason": {"type": "string"},
            },
        }
        req = ModelRequest(
            tag=f"guess:{finding.id}",
            model=self.model_name,
            system=(
                "A workflow is running against a process document that is silent on one point. "
                "Choose the most plausible answer a careful operator would assume, and say why in one plain sentence. "
                "You are guessing on their behalf; do not pretend the document says it."
            ),
            input={
                "step": getattr(step, "title", None) or getattr(step, "id", None),
                "question": finding.question,
                "why_it_matters": finding.detail,
                "document_says": finding.source_text,
                "options": labels,
                "state_summary": {
                    k: list(v.keys()) if isinstance(v, dict) else v
                    for k, v in state.get("steps", {}).items()
                },
            },
            output_schema=schema,
        )
        try:
            resp = self.model.complete(req)
            i = int(resp.output["choice"])
            opt = finding.options[i]
            return Guess(
                opt.value,
                opt.label,
                str(resp.output.get("reason", "")),
                [lbl for j, lbl in enumerate(labels) if j != i],
            )
        except Exception:  # noqa: BLE001 - a failed guess falls back to the deterministic rule
            return self.fallback.guess(finding=finding, step=step, state=state, mode=mode)
