"""Search and fetch through Exa, the provider the definitions name (``exa.search``,
``exa.get_contents``).

The same two calls as the fixtures, so a step cannot tell which one answered; the run
can, because the step's tool calls record every query, every URL and what came back.
Page text is cut to a size a step can afford to read: the model pays for every
character it is handed.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from .base import SearchResult

BASE_URL = "https://api.exa.ai"


class ExaSearch:
    vantage = "Exa (live web search)"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        snippet_chars: int = 500,
        page_chars: int = 12_000,
        timeout_s: float = 30.0,
        transport: httpx.BaseTransport | None = None,  # tests hand in a mock
    ):
        key = api_key or os.environ.get("EXA_API_KEY")
        if not key:
            raise RuntimeError("EXA_API_KEY is not set; live search needs it")
        self.snippet_chars = snippet_chars
        self.page_chars = page_chars
        self.client = httpx.Client(
            base_url=os.environ.get("EXA_BASE_URL", BASE_URL),
            headers={"x-api-key": key, "Content-Type": "application/json"},
            timeout=timeout_s,
            transport=transport,
        )
        self.calls: list[dict[str, Any]] = []

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        r = self.client.post(path, json=body)
        if r.status_code >= 400:
            # The body says why (a bad key, a spent quota); the status alone does not.
            raise RuntimeError(f"Exa {path} answered {r.status_code}: {r.text[:300]}")
        return r.json()

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        self.calls.append({"op": "search", "query": query})
        data = self._post(
            "/search",
            {
                "query": query,
                "numResults": max(1, min(int(max_results), 10)),
                "type": "auto",
                "contents": {"text": {"maxCharacters": self.snippet_chars}},
            },
        )
        return [
            SearchResult(
                url=str(r.get("url", "")),
                title=str(r.get("title") or ""),
                snippet=" ".join(str(r.get("text") or "").split())[: self.snippet_chars],
                published=(r.get("publishedDate") or None),
            )
            for r in data.get("results", [])
            if r.get("url")
        ]

    def get_contents(self, url: str) -> dict[str, Any]:
        self.calls.append({"op": "get_contents", "url": url})
        data = self._post("/contents", {"urls": [url], "text": {"maxCharacters": self.page_chars}})
        results = data.get("results") or []
        if not results:
            statuses = data.get("statuses") or []
            why = (statuses[0].get("error") or statuses[0].get("status")) if statuses else None
            return {"url": url, "error": f"Exa could not fetch this page ({why or 'no content'})."}
        page = results[0]
        return {
            "url": url,
            "title": str(page.get("title") or ""),
            "text": str(page.get("text") or ""),
            "published": page.get("publishedDate") or None,
        }
