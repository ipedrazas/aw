"""PDF rendering. Real in every mode: it has no side effects outside the workspace.

``simulated`` is a property of the artefact, so a dry-run PDF carries its marking in
the filename, on every page and in the document metadata. Nobody can forward it as
real work by accident.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fpdf import FPDF

SIMULATED_BANNER = (
    "SIMULATED — produced by a dry run. Not real work. Do not forward as a deliverable."
)


class _Pdf(FPDF):
    def __init__(self, simulated: bool):
        super().__init__()
        self.simulated = simulated
        self.set_auto_page_break(auto=True, margin=18)

    def header(self) -> None:  # noqa: D401
        if not self.simulated:
            return
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(180, 40, 40)
        self.cell(0, 6, _latin(SIMULATED_BANNER), align="C", new_x="LMARGIN", new_y="NEXT")
        # diagonal watermark
        self.set_font("Helvetica", "B", 60)
        self.set_text_color(235, 200, 200)
        with self.rotation(35, self.w / 2, self.h / 2):
            self.text(self.w / 2 - 60, self.h / 2, "SIMULATED")
        self.set_text_color(0, 0, 0)
        self.ln(2)

    def footer(self) -> None:
        self.set_y(-14)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(120, 120, 120)
        label = "Simulated. " if self.simulated else ""
        self.cell(0, 8, f"{label}Page {self.page_no()}", align="C")
        self.set_text_color(0, 0, 0)


def _plain(md: str) -> list[tuple[str, str]]:
    """Very small markdown reader: headings, bullets, paragraphs. Enough for a report body."""
    blocks: list[tuple[str, str]] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            blocks.append(("p", " ".join(para)))
            para.clear()

    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush()
            continue
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            flush()
            blocks.append((f"h{len(m.group(1))}", m.group(2)))
            continue
        if re.match(r"^\s*[-*]\s+", line):
            flush()
            blocks.append(("li", re.sub(r"^\s*[-*]\s+", "", line)))
            continue
        para.append(line.strip())
    flush()
    return [(k, re.sub(r"[*_`]", "", t)) for k, t in blocks]


class PdfRenderer:
    def render_pdf(
        self,
        *,
        title: str,
        report: dict[str, Any],
        followups: list[dict[str, Any]],
        link_check: dict[str, Any] | None,
        simulated: bool,
        out_path: str,
    ) -> dict[str, Any]:
        pdf = _Pdf(simulated)
        pdf.set_title(("SIMULATED: " if simulated else "") + title)
        pdf.set_subject(
            "Simulated dry-run output. Not real work." if simulated else "Research report"
        )
        pdf.set_keywords("simulated, dry-run" if simulated else "report")
        pdf.set_creator("wf")
        pdf.add_page()

        pdf.set_font("Helvetica", "B", 18)
        pdf.multi_cell(
            0,
            9,
            new_x="LMARGIN",
            new_y="NEXT",
            text=_latin(("SIMULATED: " if simulated else "") + title),
        )
        pdf.ln(2)
        if simulated:
            pdf.set_font("Helvetica", "I", 10)
            pdf.multi_cell(
                0,
                6,
                new_x="LMARGIN",
                new_y="NEXT",
                text=_latin(
                    "This document was produced by a dry run against recorded sources. Its contents are illustrative and must not be treated as findings."
                ),
            )
            pdf.ln(2)

        self._body(pdf, report.get("body_md", ""))
        self._sources(pdf, report.get("sources", []), link_check)

        for i, fu in enumerate(followups, 1):
            if not isinstance(fu, dict):
                continue
            pdf.add_page()
            pdf.set_font("Helvetica", "B", 15)
            pdf.multi_cell(
                0,
                8,
                new_x="LMARGIN",
                new_y="NEXT",
                text=_latin(f"Follow-up {i}: {fu.get('title', '')}"),
            )
            pdf.ln(2)
            self._body(pdf, fu.get("body_md", ""))
            self._sources(pdf, fu.get("sources", []), None)

        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        pdf.output(str(out))
        return {"artifact": str(out), "pages": pdf.page_no(), "simulated": simulated}

    def _body(self, pdf: FPDF, md: str) -> None:
        for kind, text in _plain(md):
            if kind == "h1":
                pdf.set_font("Helvetica", "B", 15)
                pdf.multi_cell(0, 8, new_x="LMARGIN", new_y="NEXT", text=_latin(text))
            elif kind == "h2":
                pdf.set_font("Helvetica", "B", 13)
                pdf.multi_cell(0, 7, new_x="LMARGIN", new_y="NEXT", text=_latin(text))
            elif kind == "h3":
                pdf.set_font("Helvetica", "B", 11)
                pdf.multi_cell(0, 6, new_x="LMARGIN", new_y="NEXT", text=_latin(text))
            elif kind == "li":
                pdf.set_font("Helvetica", "", 10.5)
                pdf.multi_cell(0, 6, new_x="LMARGIN", new_y="NEXT", text=_latin("• " + text))
            else:
                pdf.set_font("Helvetica", "", 10.5)
                pdf.multi_cell(0, 6, new_x="LMARGIN", new_y="NEXT", text=_latin(text))
            pdf.ln(1.5)

    def _sources(
        self, pdf: FPDF, sources: list[dict[str, Any]], link_check: dict[str, Any] | None
    ) -> None:
        if not sources:
            return
        status = {}
        if link_check:
            for s in link_check.get("sources", []):
                status[s.get("url")] = s
        pdf.ln(3)
        pdf.set_font("Helvetica", "B", 12)
        pdf.multi_cell(0, 7, new_x="LMARGIN", new_y="NEXT", text="Sources")
        pdf.set_font("Helvetica", "", 9.5)
        for s in sources:
            st = status.get(s.get("url"))
            opens = ""
            if st is not None:
                opens = (
                    " (link opens)"
                    if st.get("link_opens")
                    else f" (link does not open, {st.get('status')})"
                )
            pdf.multi_cell(
                0,
                5.5,
                new_x="LMARGIN",
                new_y="NEXT",
                text=_latin(f"[{s.get('line')}] {s.get('claim')} — {s.get('url')}{opens}"),
            )
        if link_check:
            pdf.ln(2)
            pdf.set_font("Helvetica", "I", 8.5)
            pdf.multi_cell(
                0,
                5,
                new_x="LMARGIN",
                new_y="NEXT",
                text=_latin(
                    f"Links checked at {link_check.get('checked_at')} from {link_check.get('vantage')}. A link that opens says nothing about whether the page supports the claim."
                ),
            )


def _latin(s: str) -> str:
    """Core PDF fonts are Latin-1. Replace what they cannot show rather than crash."""
    return (
        s.replace("—", "-")
        .replace("–", "-")
        .replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("•", "-")
        .replace("…", "...")
        .encode("latin-1", "replace")
        .decode("latin-1")
    )
