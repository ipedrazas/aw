"""A scripted model that plays the deep research workflow plausibly, including tool
calls through the activity layer, so the interpreter can be exercised end to end
without a network."""

from __future__ import annotations

from typing import Any

from wf.activities import ModelRequest, ModelResponse, ToolCallRecord, Usage
from wf.activities.safety import find_instructions


def deep_research_script(verdict: str = "accept", followups: int = 2):
    def script(req: ModelRequest) -> ModelResponse:
        usage = Usage(input_tokens=1200, output_tokens=400, cost_usd=0.01)
        if req.tag.startswith("plan"):
            return ModelResponse(
                output={
                    "questions": [
                        "Which platforms offer durable execution for agents?",
                        "How does each recover after a failed step?",
                        "What adoption evidence exists beyond vendor claims?",
                    ],
                    "in_scope": "Vendor documentation and engineering write-ups from 2025 and 2026.",
                    "excluded": "No-code automation tools.",
                    "source_preferences": "Primary sources first.",
                },
                decisions=[
                    {
                        "decision": "Split who leads into three questions.",
                        "reason": "The topic mixes market position with capability.",
                        "alternatives": ["Keep one question"],
                    }
                ],
                usage=usage,
            )
        if req.tag.startswith("research"):
            tools = {t.name: t for t in req.tools}
            calls: list[ToolCallRecord] = []
            findings = []
            for q in [
                "durable execution AI agents platforms",
                "temporal recover failed step long human wait",
                "adoption evidence durable execution agents",
            ]:
                results = tools["search"].executor({"query": q, "max_results": 5})
                calls.append(
                    ToolCallRecord(
                        "search", {"query": q, "max_results": 5}, f"{len(results)} results"
                    )
                )
                for r in results[:3]:
                    page = tools["get_contents"].executor({"url": r["url"]})
                    inj = find_instructions(str(page))
                    calls.append(
                        ToolCallRecord(
                            "get_contents", {"url": r["url"]}, str(page.get("title", ""))[:80], inj
                        )
                    )
                    if "error" in page:
                        continue
                    findings.append(
                        {
                            "question_index": 0,
                            "claim": f"{page['title']} states something citable.",
                            "url": r["url"],
                            "title": page["title"],
                            "quality": "vendor" if "docs." in r["url"] else "primary",
                        }
                    )
            # the researcher also reads the page that tries to instruct it
            inj_url = "https://engineering-blog.example.net/posts/durable-execution-vendor-notes"
            page = tools["get_contents"].executor({"url": inj_url})
            calls.append(
                ToolCallRecord(
                    "get_contents",
                    {"url": inj_url},
                    str(page.get("title", ""))[:80],
                    find_instructions(str(page)),
                )
            )
            return ModelResponse(
                output={
                    "searches_run": 3,
                    "sources_summary": f"{len(findings)} sources kept.",
                    "findings": findings,
                    "dropped": [
                        {"url": "https://rankings.example.com/top", "reason": "Vendor ranking."}
                    ],
                },
                decisions=[
                    {
                        "decision": "Stopped at 3 of 25 searches.",
                        "reason": "Each question had at least two sources.",
                        "alternatives": ["Keep searching"],
                    }
                ],
                usage=usage,
                tool_calls=calls,
            )
        if req.tag.startswith("write") or req.tag.startswith("revise"):
            findings = (
                (req.input.get("findings") or {}).get("findings")
                or (req.input.get("report") or {}).get("sources")
                or []
            )
            urls = [f.get("url") for f in findings][:6] or [
                "https://research.example.com/reports/agent-pilots-to-production-2026"
            ]
            body = "# Durable execution platforms\n\n" + "\n\n".join(
                f"Claim {i + 1} [{i + 1}]." for i in range(len(urls))
            )
            return ModelResponse(
                output={
                    "title": "Durable execution platforms for AI agents",
                    "body_md": body,
                    "sources": [
                        {"line": 3 + 2 * i, "claim": f"Claim {i + 1}", "url": u}
                        for i, u in enumerate(urls)
                    ],
                },
                decisions=[
                    {
                        "decision": "Wrote one section per question.",
                        "reason": "The brief had three questions.",
                        "alternatives": [],
                    }
                ],
                usage=usage,
            )
        if req.tag.startswith("review"):
            fu = [
                {"topic": f"Follow-up {i + 1}", "fills_gap": 0, "estimated_usd": 1.5}
                for i in range(followups)
            ]
            return ModelResponse(
                output={
                    "verdict": verdict,
                    "reason": "Scripted verdict.",
                    "gaps": [{"description": "Adoption evidence rests on one source.", "line": 21}],
                    "followup_topics": fu if verdict == "go_deeper" else [],
                    "edits": [{"line": 9, "change": "Cite the primary source."}]
                    if verdict == "revise"
                    else [],
                },
                decisions=[
                    {
                        "decision": f"Verdict: {verdict}.",
                        "reason": "Scripted.",
                        "alternatives": ["accept", "revise", "go_deeper"],
                    }
                ],
                usage=usage,
            )
        if req.tag.startswith("guess:"):
            return ModelResponse(output={"choice": 0, "reason": "Scripted guess."}, usage=usage)
        raise AssertionError(f"unexpected model request {req.tag}")

    return script


def make_response(output: dict[str, Any], **kw: Any) -> ModelResponse:
    return ModelResponse(output=output, **kw)
