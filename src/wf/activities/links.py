"""Link checks. Deterministic, no model. The result records its vantage point, because
"reachable from here, then" is not "reachable by you, now"."""

from __future__ import annotations

from typing import Any

from wf.schema import Workspace

from .base import LinkStatus


class FixtureLinkCheck:
    vantage = "recorded fixtures (workspace/fixtures/http.json)"

    def __init__(self, ws: Workspace):
        self.statuses: dict[str, Any] = ws.load_json("fixtures/http.json", default={}) or {}

    def check(self, urls: list[str]) -> list[LinkStatus]:
        out = []
        for url in urls:
            entry = self.statuses.get(url) or self.statuses.get(url.rstrip("/"))
            status = int(entry["status"]) if entry else 0
            out.append(LinkStatus(url=url, status=status, opens=200 <= status < 400))
        return out
