"""What a decisions model can be asked, read from a step's output schema.

A decisions model (Jev, ``typesafe/jev-*``) writes no text: it answers typed questions
with a value and a probability. Which questions a step asks is in its output schema,
read by these rules:

- a string property with an ``enum`` is a choice among its values;
- a boolean property is a yes or no;
- the property's ``description`` is the question, and ``x-criteria`` (value → what it
  means) says what each answer means, the same words a text model reads;
- a property named ``probabilities``, if the schema has one, is not asked: it is
  filled with the model's probabilities, one entry per question.

Anything else is a question it cannot answer. The same reading serves the call
(``wf.activities.jev``) and the check that a step can run on it before any run does
(``wf.validate.models``), so the two cannot disagree.
"""

from __future__ import annotations

from typing import Any

#: Model names OpenRouter serves through the Decisions endpoint.
DECISIONS_VENDORS = ("typesafe/", "~typesafe/")

#: The property a decisions model fills with its probabilities instead of answering.
PROBABILITIES = "probabilities"


def is_decisions_model(model: str | None) -> bool:
    return bool(model) and str(model).startswith(DECISIONS_VENDORS)


class NotAQuestion(ValueError):
    """A schema a decisions model cannot answer. ``field`` is the property it cannot
    answer, or None when the schema asks nothing at all."""

    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


def questions_from_schema(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The typed questions an output schema asks, by the rules in the module docstring."""
    questions: dict[str, dict[str, Any]] = {}
    for name, prop in (schema.get("properties") or {}).items():
        if name == PROBABILITIES:
            continue
        instructions = str(prop.get("description") or name)
        criteria = prop.get("x-criteria")
        if prop.get("type") == "string" and prop.get("enum"):
            options = [str(v) for v in prop["enum"]]
            meaning = criteria if isinstance(criteria, dict) else {}
            questions[name] = {
                "type": "choice",
                "instructions": instructions,
                "criteria": {v: str(meaning.get(v) or v) for v in options},
            }
        elif prop.get("type") == "boolean":
            meaning = criteria if isinstance(criteria, dict) else {}
            questions[name] = {
                "type": "noul",
                "instructions": instructions,
                "criteria": {
                    "true": str(meaning.get("true") or "Yes."),
                    "false": str(meaning.get("false") or "No."),
                },
            }
        else:
            raise NotAQuestion(
                f"a decisions model answers only choices and yes/no questions, and "
                f"“{name}” in this step's output is neither",
                field=name,
            )
    if not questions:
        raise NotAQuestion("this step's output asks no question a decisions model can answer")
    return questions
