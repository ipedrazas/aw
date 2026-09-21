"""Anything that leaves the system. In this phase nothing does: the recording sender
writes down what it would have sent and returns a stub."""

from __future__ import annotations

from typing import Any


class RecordingSend:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, *, to: str, subject: str, body: str, attachments: list[str]) -> dict[str, Any]:
        rec = {
            "to": to,
            "subject": subject,
            "body_chars": len(body),
            "attachments": attachments,
            "delivered": False,
        }
        self.sent.append(rec)
        return {"delivered": False, "recorded": True, "to": to, "subject": subject}
