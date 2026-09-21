"""A model that never calls the network. For local UI work and smoke tests without a key.

It fills any output schema with a minimal instance, and for the auditor's extraction
it proposes one step per passage that reads like an action. It is deliberately
plain so nobody mistakes it for judgement.
"""

from __future__ import annotations

import re
from typing import Any

from .base import ModelRequest, ModelResponse, Usage

_ACTION = re.compile(
    r"\b(write|writes|search|searches|check|checks|review|reviews|send|sends|export|exports|decide|decides|draft|drafts|research|researches|approve|approves|read|reads)\b",
    re.I,
)


def fill(schema: dict[str, Any], hints: dict[str, Any] | None = None) -> Any:
    hints = hints or {}
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "null")
    if "enum" in schema:
        return schema["enum"][0]
    if t == "object":
        return {
            k: (hints[k] if k in hints else fill(sub))
            for k, sub in schema.get("properties", {}).items()
        }
    if t == "array":
        return []
    if t == "integer":
        return 0
    if t == "number":
        return 0.0
    if t == "boolean":
        return False
    if t == "null":
        return None
    return "example"


class FakeModel:
    def complete(self, request: ModelRequest) -> ModelResponse:
        if request.tag == "audit:extract":
            return self._extract(request)
        if request.tag == "audit:chat":
            out = fill(
                request.output_schema,
                {
                    "reply": "This is the offline model. It can answer questions on the right but cannot reason about your draft. Set ANTHROPIC_API_KEY for a real conversation."
                },
            )
            return ModelResponse(output=out, decisions=[], usage=Usage())
        out = fill(request.output_schema)
        if isinstance(out, dict) and "output" in request.output_schema.get("properties", {}):
            pass
        return ModelResponse(
            output=out,
            decisions=[
                {
                    "decision": f"Produced a placeholder for {request.tag}.",
                    "reason": "The offline model fills the declared shape and nothing more.",
                    "alternatives": [],
                }
            ],
            usage=Usage(0, 0, 0.0),
            model="fake",
        )

    def _extract(self, request: ModelRequest) -> ModelResponse:
        passages = request.input.get("passages", [])
        steps = []
        for p in passages:
            text = p.get("text", "")
            if not _ACTION.search(text) or len(steps) >= 8:
                continue
            words = re.findall(r"[a-z]+", (p.get("heading") or text).lower())[:3]
            sid = "_".join(words) or f"step_{len(steps) + 1}"
            kind = "agent"
            low = text.lower()
            if "send" in low or "email" in low:
                kind = "tool"
            elif "link" in low and "check" in low:
                kind = "check"
            step: dict[str, Any] = {
                "id": sid,
                "kind": kind,
                "title": (p.get("heading") or " ".join(words)).strip()[:60] or sid,
                "description": text.split(".")[0][:140] + ".",
                "passage": p["id"],
                "origin": "stated",
                "reads_from": [steps[-1]["id"]] if steps else [],
                "tools": ["search", "get_contents"] if "search" in low else [],
                "produces": [
                    {
                        "name": "summary",
                        "type": "string",
                        "enum": [],
                        "description": "What this step produced.",
                        "passage": p["id"],
                    }
                ],
                "instructions": {"summary": text[:300], "passage": p["id"]},
                "when": None,
                "run": "tools.send_email"
                if kind == "tool"
                else ("checks.http_resolves" if kind == "check" else None),
                "checks": {"items": ["The link opens"], "passage": p["id"]}
                if kind == "check"
                else None,
                "does_not_check": None,
                "requires_approval": None,
                "side_effects": {"items": ["sends the report"], "passage": p["id"]}
                if kind == "tool"
                else None,
                "deadline": None,
                "limits": None,
                "judgement": None,
            }
            steps.append(step)
        if not steps:
            steps.append(
                {
                    **self._blank("do_the_work", "Do the work"),
                    "passage": passages[0]["id"] if passages else None,
                }
            )
        out = {
            "name": "process",
            "title": "Your process",
            "description": "A draft made by the offline model: one step per passage that reads like an action.",
            "inputs": [
                {
                    "name": "topic",
                    "type": "string",
                    "required": True,
                    "description": "What the process starts from.",
                    "passage": passages[0]["id"] if passages else None,
                }
            ],
            "steps": steps,
        }
        return ModelResponse(
            output=out,
            decisions=[
                {
                    "decision": "Made one step per passage that reads like an action.",
                    "reason": "This is the offline model; it does not read for meaning.",
                    "alternatives": [],
                }
            ],
            usage=Usage(),
            model="fake",
        )

    @staticmethod
    def _blank(sid: str, title: str) -> dict[str, Any]:
        return {
            "id": sid,
            "kind": "agent",
            "title": title,
            "description": "",
            "passage": None,
            "origin": "suggested",
            "reads_from": [],
            "tools": [],
            "produces": [
                {
                    "name": "summary",
                    "type": "string",
                    "enum": [],
                    "description": "",
                    "passage": None,
                }
            ],
            "instructions": None,
            "when": None,
            "run": None,
            "checks": None,
            "does_not_check": None,
            "requires_approval": None,
            "side_effects": None,
            "deadline": None,
            "limits": None,
            "judgement": None,
        }
