"""Search and fetch from recorded fixtures.

Fixtures are keyed by request. The same fixtures answer every run, so what varies
between two runs is model judgement alone.
"""

from __future__ import annotations

import re
from typing import Any

from wf.schema import Workspace

from .base import SearchResult

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(s: str) -> set[str]:
    return set(_WORD.findall(s.lower())) - {
        "the",
        "a",
        "an",
        "of",
        "for",
        "and",
        "in",
        "to",
        "on",
        "vs",
    }


class FixtureSearch:
    def __init__(self, ws: Workspace):
        self.ws = ws
        self.entries: list[dict[str, Any]] = ws.load_json("fixtures/search.json", default=[]) or []
        self.contents: dict[str, Any] = ws.load_json("fixtures/contents.json", default={}) or {}
        self.calls: list[dict[str, Any]] = []

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        self.calls.append({"op": "search", "query": query})
        q = _tokens(query)
        best: tuple[float, int] | None = None
        for i, entry in enumerate(self.entries):
            if entry["query"].strip().lower() == query.strip().lower():
                best = (2.0, i)
                break
            e = _tokens(entry["query"])
            if not q or not e:
                continue
            score = len(q & e) / len(q | e)
            if score > 0 and (best is None or score > best[0]):
                best = (score, i)
        if best is None:
            return []
        results = self.entries[best[1]].get("results", [])[:max_results]
        return [
            SearchResult(r["url"], r.get("title", ""), r.get("snippet", ""), r.get("published"))
            for r in results
        ]

    def get_contents(self, url: str) -> dict[str, Any]:
        self.calls.append({"op": "get_contents", "url": url})
        page = self.contents.get(url) or self.contents.get(url.rstrip("/"))
        if page is None:
            return {"url": url, "error": "This page is not in the recorded fixtures."}
        return {"url": url, **page}


class NoSearch:
    """Live search is not wired in this phase. Says so instead of pretending."""

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        raise RuntimeError("live web search is not configured; runs use recorded fixtures")

    def get_contents(self, url: str) -> dict[str, Any]:
        raise RuntimeError("live page fetch is not configured; runs use recorded fixtures")
