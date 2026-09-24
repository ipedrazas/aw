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
                        "search",
                        {"query": q, "max_results": 5},
                        f"{len(results)} results",
                        None,
                        results,
                    )
                )
                for r in results[:3]:
                    page = tools["get_contents"].executor({"url": r["url"]})
                    inj = find_instructions(str(page))
                    calls.append(
                        ToolCallRecord(
                            "get_contents",
                            {"url": r["url"]},
                            str(page.get("title", ""))[:80],
                            inj,
                            page,
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
                    page,
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
        if req.tag.startswith("check_support"):
            return ModelResponse(output=claim_support_answer(), usage=Usage(900, 60, 0.002))
        if req.tag.startswith("guess:"):
            return ModelResponse(output={"choice": 0, "reason": "Scripted guess."}, usage=usage)
        raise AssertionError(f"unexpected model request {req.tag}")

    return script


def claim_support_answer(verdict: str = "supports", p: float = 0.9) -> dict[str, Any]:
    """One citation's answer in the shape of schemas/claim_support.json."""
    verdicts = ["supports", "partly", "not_supported", "contradicts", "unreadable"]
    rest = round((1 - p) / (len(verdicts) - 1), 4)
    return {
        "verdict": verdict,
        "supports": verdict == "supports",
        "probabilities": {
            "verdict": {v: (p if v == verdict else rest) for v in verdicts},
            "supports": p if verdict == "supports" else 1 - p,
        },
    }


def skill_answer(req: ModelRequest) -> ModelResponse:
    """The auditor asking for one step's instructions: written prose that names what the
    step produces, and leaves the decisions and data rule for the contract to add."""
    step = req.input["step"]
    fields = ", ".join(f"`{f['name']}`" for f in step["produces"])
    return ModelResponse(
        output={
            "body": f"# {step['title']}\n\n"
            f"Written for {step['id']}: {step['description']} "
            f"The next step reads what you hand on, so fill {fields} with care."
        },
        usage=Usage(200, 120, 0.002),
    )


def make_response(output: dict[str, Any], **kw: Any) -> ModelResponse:
    return ModelResponse(output=output, **kw)


def fill_from_schema(schema: dict[str, Any], overrides: dict[str, Any] | None = None) -> Any:
    """A minimal instance of a JSON schema, for exercising drafts whose shape is generated."""
    overrides = overrides or {}
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "null")
    if "enum" in schema:
        return schema["enum"][0]
    if t == "object":
        out = {}
        for k, sub in schema.get("properties", {}).items():
            out[k] = overrides[k] if k in overrides else fill_from_schema(sub)
        return out
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


def schema_filling_script(overrides: dict[str, dict[str, Any]] | None = None):
    overrides = overrides or {}

    def script(req: ModelRequest) -> ModelResponse:
        out = fill_from_schema(req.output_schema, overrides.get(req.tag.split("[")[0]))
        return ModelResponse(
            output=out,
            decisions=[{"decision": f"Did {req.tag}.", "reason": "Scripted.", "alternatives": []}],
            usage=Usage(100, 50, 0.001),
        )

    return script


def _sourced(passage: str | None, **kw: Any) -> dict[str, Any]:
    return {**kw, "passage": passage}


def _step(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "",
        "kind": "agent",
        "title": "",
        "description": "",
        "passage": None,
        "origin": "stated",
        "reads_from": [],
        "tools": [],
        "produces": [],
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
    base.update(kw)
    return base


def extraction_for_process_doc(p: dict[str, str]) -> dict[str, Any]:
    """What a faithful extractor returns for workspace/process-docs/deep-research-process.md.
    ``p`` maps a distinctive phrase to the passage id that contains it."""
    return {
        "name": "client-research",
        "title": "Deep research reports",
        "description": "Turn a client's topic into a sourced research report and send it.",
        "inputs": [
            _sourced(
                p["A client sends us a topic"],
                name="topic",
                type="string",
                required=True,
                description="The client's topic, one line.",
            )
        ],
        "steps": [
            _step(
                id="brief",
                title="Write the brief",
                description="Turns the topic into three to five questions, the sources wanted and what is left out.",
                passage=p["turns the topic into a set of questions"],
                produces=[
                    _sourced(
                        p["turns the topic into a set of questions"],
                        name="questions",
                        type="list",
                        enum=[],
                        description="Questions the report should answer.",
                    ),
                    _sourced(
                        p["turns the topic into a set of questions"],
                        name="sources_wanted",
                        type="string",
                        enum=[],
                        description="",
                    ),
                    _sourced(
                        p["turns the topic into a set of questions"],
                        name="left_out",
                        type="string",
                        enum=[],
                        description="",
                    ),
                ],
                instructions=_sourced(
                    p["turns the topic into a set of questions"],
                    summary="Turn the topic into three to five questions. Say what kind of sources are wanted and what is left out.",
                ),
            ),
            _step(
                id="research",
                title="Do the research",
                description="Searches the web for each question and keeps a log of what was found.",
                passage=p["The researcher searches the web"],
                reads_from=["brief"],
                tools=["search", "get_contents"],
                produces=[
                    _sourced(
                        p["keeps going until each question"],
                        name="findings",
                        type="list",
                        enum=[],
                        description="Sources per question, marked primary or secondary.",
                    ),
                    _sourced(
                        p["keeps going until each question"],
                        name="log",
                        type="string",
                        enum=[],
                        description="What was searched and found.",
                    ),
                ],
                instructions=_sourced(
                    p["The researcher searches the web"],
                    summary="Search for each question. Prefer primary sources; mark news and analyst pages as secondary. Vendor rankings are not evidence. Stop when each question has a couple of solid sources or after about an hour.",
                ),
            ),
            _step(
                id="write",
                title="Write the report",
                description="One section per question, every claim cited, with a sources table.",
                passage=p["The writer takes the brief"],
                reads_from=["brief", "research", "check_links"],
                produces=[
                    _sourced(
                        p["Every factual claim gets a citation"],
                        name="report",
                        type="object",
                        enum=[],
                        description="The report body.",
                    ),
                    _sourced(
                        p["Every factual claim gets a citation"],
                        name="sources",
                        type="list",
                        enum=[],
                        description="One row per citation: line, claim, url, link status.",
                    ),
                ],
                instructions=_sourced(
                    p["The writer takes the brief"],
                    summary="One section per question in brief order, an introduction and a closing section on what is uncertain. Every claim gets a citation. Fill the whole sources table including the link status column.",
                ),
                judgement="careful",
            ),
            _step(
                id="check_links",
                kind="check",
                title="Check the links",
                description="Makes sure the links in the sources table work.",
                passage=p["makes sure the links work"],
                reads_from=["write"],
                run="checks.http_resolves",
                produces=[
                    _sourced(
                        p["makes sure the links work"],
                        name="sources",
                        type="list",
                        enum=[],
                        description="Each source with whether its link opens.",
                    )
                ],
                checks=_sourced(p["makes sure the links work"], items=["The link opens"]),
            ),
            _step(
                id="review",
                title="Review the report",
                description="A reviewer reads the report against the brief and decides what happens next.",
                passage=p["A reviewer who wasn't involved"],
                reads_from=["brief", "write", "check_links"],
                produces=[
                    _sourced(
                        p["The reviewer decides whether more research"],
                        name="verdict",
                        type="string",
                        enum=["more_research", "edits", "reject", "approved"],
                        description="What the reviewer decided.",
                    ),
                    _sourced(
                        p["The reviewer decides whether more research"],
                        name="missing",
                        type="list",
                        enum=[],
                        description="What is missing, if more research is needed.",
                    ),
                    _sourced(
                        p["The reviewer decides whether more research"],
                        name="edits",
                        type="list",
                        enum=[],
                        description="Edits wanted, if any.",
                    ),
                ],
                instructions=_sourced(
                    p["A reviewer who wasn't involved"],
                    summary="Check each section answers its question, claims are backed by the cited sources, and nothing important is missing.",
                ),
                judgement="careful",
            ),
            _step(
                id="more_research",
                kind="subworkflow",
                title="Another research pass",
                description="The researcher goes back over the missing points.",
                passage=p["The reviewer decides whether more research"],
                reads_from=["review"],
                when=_sourced(
                    p["The reviewer decides whether more research"],
                    condition="the reviewer thinks more research is needed",
                    reads_step=None,
                    reads_field=None,
                    equals=None,
                ),
            ),
            _step(
                id="make_edits",
                title="Make the edits",
                description="The writer makes the edits the reviewer listed.",
                passage=p["The reviewer decides whether more research"],
                reads_from=["write", "review"],
                produces=[
                    _sourced(
                        None,
                        name="report",
                        type="object",
                        enum=[],
                        description="The edited report.",
                    )
                ],
                when=_sourced(
                    p["The reviewer decides whether more research"],
                    condition="the report just needs edits",
                    reads_step="review",
                    reads_field="verdict",
                    equals="edits",
                ),
            ),
            _step(
                id="export_pdf",
                kind="tool",
                title="Export to PDF",
                description="Exports the approved report with our template, follow-up research appended.",
                passage=p["exported to PDF"],
                reads_from=["write", "more_research"],
                run="tools.render_pdf",
                side_effects=_sourced(p["exported to PDF"], items=[]),
            ),
            _step(
                id="send",
                kind="tool",
                title="Send the report to the client",
                description="Sends the final PDF to the client.",
                passage=p["exported to PDF"],
                reads_from=["export_pdf"],
                run="tools.send_email",
                side_effects=_sourced(
                    p["exported to PDF"], items=["sends the report to the client"]
                ),
            ),
        ],
    }
