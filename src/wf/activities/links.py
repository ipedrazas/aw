"""Link checks. Deterministic, no model. The result records its vantage point, because
"reachable from here, then" is not "reachable by you, now"."""

from __future__ import annotations

import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from wf.schema import Workspace

from .base import LinkStatus

FIXTURES = "recorded fixtures (workspace/fixtures/http.json)"
LIVE = "live requests from this server"


class FixtureLinkCheck:
    vantage = FIXTURES

    def __init__(self, ws: Workspace):
        self.statuses: dict[str, Any] = ws.load_json("fixtures/http.json", default={}) or {}

    def recorded(self, url: str) -> LinkStatus | None:
        entry = self.statuses.get(url) or self.statuses.get(url.rstrip("/"))
        if not entry:
            return None
        status = int(entry["status"])
        return LinkStatus(url=url, status=status, opens=200 <= status < 400, reason="recorded")

    def check(self, urls: list[str]) -> list[LinkStatus]:
        return [
            self.recorded(url) or LinkStatus(url=url, status=0, opens=False, reason="not recorded")
            for url in urls
        ]


def _refuse(url: str) -> str | None:
    """Why this URL is not ours to request, or None. The check fetches what a model wrote,
    so it must not become a way to reach the network the server sits on."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "not a web address"
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or None, proto=socket.IPPROTO_TCP)
    except OSError:
        return "the name does not resolve"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return "points inside a private network; not requested"
    return None


class LiveLinkCheck:
    """Asks each server, the way the step says: a 2xx or 3xx within ``timeout_s``.

    A URL the fixtures recorded is answered from them, so a past case replays the way it
    happened; everything else is requested from here. The vantage says which, per run.
    Redirects are followed by hand so every hop passes the same address check.
    """

    MAX_HOPS = 5
    HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; wf-link-check/0.1)", "Accept": "*/*"}

    def __init__(
        self,
        ws: Workspace | None = None,
        *,
        timeout_s: float = 10.0,
        workers: int = 8,
        transport: httpx.BaseTransport | None = None,  # tests hand in a mock
    ):
        self.fixtures = FixtureLinkCheck(ws) if ws is not None else None
        self.transport = transport
        self.timeout_s = timeout_s
        self.workers = workers
        self.vantage = LIVE

    def check(self, urls: list[str]) -> list[LinkStatus]:
        recorded = {u: self.fixtures.recorded(u) for u in urls} if self.fixtures else {}
        todo = [u for u in dict.fromkeys(urls) if not recorded.get(u)]
        live: dict[str, LinkStatus] = {}
        if todo:
            with ThreadPoolExecutor(max_workers=min(self.workers, len(todo))) as pool:
                live = dict(zip(todo, pool.map(self._one, todo), strict=True))
        n_recorded = sum(1 for u in urls if recorded.get(u))
        n_live = len(urls) - n_recorded
        parts = []
        if n_live:
            parts.append(f"{LIVE} ({n_live})")
        if n_recorded:
            parts.append(f"{FIXTURES} ({n_recorded})")
        self.vantage = "; ".join(parts) or LIVE
        return [recorded.get(u) or live[u] for u in urls]

    def _one(self, url: str) -> LinkStatus:
        try:
            with httpx.Client(
                timeout=self.timeout_s,
                headers=self.HEADERS,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                current = url
                for _ in range(self.MAX_HOPS + 1):
                    why = _refuse(current)
                    if why:
                        return LinkStatus(url=url, status=0, opens=False, reason=why)
                    r = client.head(current)
                    if not (200 <= r.status_code < 400):
                        # Plenty of servers refuse HEAD but serve the page; ask properly.
                        with client.stream("GET", current) as g:
                            r = g
                    if r.is_redirect and r.headers.get("location"):
                        current = urljoin(current, r.headers["location"])
                        continue
                    status = r.status_code
                    return LinkStatus(
                        url=url,
                        status=status,
                        opens=200 <= status < 400,
                        reason="" if current == url else f"after redirect to {current}",
                    )
                return LinkStatus(url=url, status=0, opens=False, reason="too many redirects")
        except httpx.TimeoutException:
            return LinkStatus(
                url=url, status=0, opens=False, reason=f"no answer in {self.timeout_s:g}s"
            )
        except httpx.HTTPError as e:
            return LinkStatus(url=url, status=0, opens=False, reason=type(e).__name__)
