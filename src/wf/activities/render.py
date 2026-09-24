"""PDF rendering. Real in every mode: it has no side effects outside the workspace.

``simulated`` is a property of the artefact, so a dry-run PDF carries its marking in
the filename, on every page and in the document metadata. Nobody can forward it as
real work by accident.

The report is written in markdown and the markdown is kept beside the PDF, with the
same marking: the filename and a banner at the top.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fpdf import FPDF
from fpdf.fonts import FontFace

SIMULATED_BANNER = (
    "SIMULATED — produced by a dry run. Not real work. Do not forward as a deliverable."
)
LINK_CAVEAT = "A link that opens says nothing about whether the page supports the claim."


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


# -- reading markdown ----------------------------------------------------------

_URL = re.compile(r"https?://[^\s<>\"'`)\]]+")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
_CELL_SPLIT = re.compile(r"(?<!\\)\|")


def _row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.strip().replace("\\|", "|") for c in _CELL_SPLIT.split(line)]


def _is_table_start(lines: list[str], i: int) -> bool:
    return (
        "|" in lines[i]
        and i + 1 < len(lines)
        and "-" in lines[i + 1]
        and bool(_TABLE_RULE.match(lines[i + 1]))
    )


def _blocks(md: str) -> list[tuple[str, Any]]:
    """A small markdown reader: headings, lists, tables, code, quotes, paragraphs.
    Enough for a report body; anything it does not know reads as a paragraph."""
    lines = md.splitlines()
    blocks: list[tuple[str, Any]] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            blocks.append(("p", " ".join(para)))
            para.clear()

    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            flush()
            i += 1
            continue
        if line.lstrip().startswith("```"):
            flush()
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith("```"):
                code.append(lines[i].rstrip())
                i += 1
            blocks.append(("code", "\n".join(code)))
            i += 1
            continue
        # a table can follow a line of text with no blank line between them
        if _is_table_start(lines, i):
            flush()
            head = _row(line)
            rows = []
            i += 2
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                cells = _row(lines[i])
                rows.append((cells + [""] * len(head))[: len(head)])
                i += 1
            blocks.append(("table", (head, rows)))
            continue
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m:
            flush()
            blocks.append((f"h{min(len(m.group(1)), 3)}", m.group(2)))
            i += 1
            continue
        if re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", line):
            flush()
            i += 1
            continue
        m = re.match(r"^(\s*)[-*+]\s+(.*)", line)
        if m:
            flush()
            blocks.append(("li", (len(m.group(1)) // 2, "- " + m.group(2))))
            i += 1
            continue
        m = re.match(r"^(\s*)(\d+)[.)]\s+(.*)", line)
        if m:
            flush()
            blocks.append(("li", (len(m.group(1)) // 2, f"{m.group(2)}. {m.group(3)}")))
            i += 1
            continue
        m = re.match(r"^\s*>\s?(.*)", line)
        if m:
            flush()
            if blocks and blocks[-1][0] == "quote":
                blocks[-1] = ("quote", blocks[-1][1] + " " + m.group(1))
            else:
                blocks.append(("quote", m.group(1)))
            i += 1
            continue
        para.append(line.strip())
        i += 1
    flush()
    return blocks


def _inline(text: str) -> str:
    """Plain text from inline markdown. Emphasis markers go, but only outside addresses:
    an underscore in a URL is part of the URL."""
    text = re.sub(r"!?\[([^\]]*)\]\(([^)\s]+)[^)]*\)", _link, text)
    text = re.sub(r"<(https?://[^>\s]+)>", r"\1", text)
    out = []
    pos = 0
    for m in _URL.finditer(text):
        out.append(_plain_emphasis(text[pos : m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_plain_emphasis(text[pos:]))
    return "".join(out)


def _link(m: re.Match[str]) -> str:
    label, url = m.group(1).strip(), m.group(2)
    return url if not label or label == url else f"{label} ({url})"


def _plain_emphasis(s: str) -> str:
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"(\*\*|__)(\S(?:.*?\S)?)\1", r"\2", s)
    s = re.sub(r"(?<![\w*])\*(\S(?:.*?\S)?)\*(?![\w*])", r"\1", s)
    return re.sub(r"(?<![\w_])_(\S(?:.*?\S)?)_(?![\w_])", r"\1", s)


# -- writing the markdown file -------------------------------------------------


def report_markdown(
    *,
    title: str,
    report: dict[str, Any],
    followups: list[dict[str, Any]],
    link_check: dict[str, Any] | None,
    simulated: bool,
) -> str:
    """The report as one markdown document: the same content as the PDF."""
    parts: list[str] = []
    if simulated:
        parts.append(f"> **{SIMULATED_BANNER}**")
    parts.append(
        _with_title(("SIMULATED: " if simulated else "") + title, report.get("body_md", ""))
    )
    parts.append(_md_sources(report.get("sources", []), link_check, level=2))
    for i, fu in enumerate(followups, 1):
        if not isinstance(fu, dict):
            continue
        parts.append(f"# Follow-up {i}: {fu.get('title', '')}")
        parts.append(str(fu.get("body_md", "")).strip())
        parts.append(_md_sources(fu.get("sources", []), None, level=3))
    return "\n\n".join(p for p in parts if p) + "\n"


def _with_title(title: str, body: str) -> str:
    body = str(body or "").strip()
    first = next((ln for ln in body.splitlines() if ln.strip()), "")
    return body if first.startswith("# ") else f"# {title}\n\n{body}".strip()


def _md_sources(
    sources: list[dict[str, Any]], link_check: dict[str, Any] | None, level: int
) -> str:
    if not sources:
        return ""
    status = _status(link_check)
    lines = ["#" * level + " Sources", ""]
    for s in sources:
        lines.append(f"- [{s.get('line')}] {s.get('claim')} — {s.get('url')}{_opens(status, s)}")
    if link_check:
        lines += [
            "",
            f"_Links checked at {link_check.get('checked_at')} from {link_check.get('vantage')}. {LINK_CAVEAT}_",
        ]
    return "\n".join(lines)


def _status(link_check: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return {s.get("url"): s for s in (link_check or {}).get("sources", [])}


def _opens(status: dict[str, dict[str, Any]], source: dict[str, Any]) -> str:
    st = status.get(source.get("url"))
    if st is None:
        return ""
    if st.get("link_opens"):
        return " (link opens)"
    return f" (link does not open, {st.get('status')})"


# -- writing the PDF -------------------------------------------------------------


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
        md = out.with_suffix(".md")
        md.write_text(
            report_markdown(
                title=title,
                report=report,
                followups=followups,
                link_check=link_check,
                simulated=simulated,
            )
        )
        return {
            "artifact": str(out),
            "markdown": str(md),
            "pages": pdf.page_no(),
            "simulated": simulated,
        }

    def _body(self, pdf: FPDF, md: str) -> None:
        for kind, value in _blocks(md):
            if kind == "table":
                self._table(pdf, *value)
            elif kind == "code":
                pdf.set_font("Courier", "", 9)
                pdf.multi_cell(0, 4.5, new_x="LMARGIN", new_y="NEXT", text=_latin(value))
            elif kind == "li":
                level, text = value
                indent = 4 + 5 * level
                pdf.set_font("Helvetica", "", 10.5)
                pdf.set_x(pdf.l_margin + indent)
                pdf.multi_cell(
                    pdf.epw - indent, 6, new_x="LMARGIN", new_y="NEXT", text=_latin(_inline(text))
                )
            elif kind == "quote":
                pdf.set_font("Helvetica", "I", 10.5)
                pdf.set_x(pdf.l_margin + 6)
                pdf.multi_cell(
                    pdf.epw - 6, 6, new_x="LMARGIN", new_y="NEXT", text=_latin(_inline(value))
                )
            else:
                size, height = {"h1": (15, 8), "h2": (13, 7), "h3": (11, 6)}.get(kind, (10.5, 6))
                pdf.set_font("Helvetica", "B" if kind != "p" else "", size)
                pdf.multi_cell(
                    0, height, new_x="LMARGIN", new_y="NEXT", text=_latin(_inline(value))
                )
            pdf.ln(1.5)

    def _table(self, pdf: FPDF, head: list[str], rows: list[list[str]]) -> None:
        head = [_latin(_inline(c)) for c in head]
        rows = [[_latin(_inline(c)) for c in r] for r in rows]
        # a column is as wide as its longest cell wants, within reason: an address
        # wraps inside its cell rather than squeezing every other column to nothing.
        widths = [
            min(max([len(head[c])] + [len(r[c]) for r in rows]), 45) + 3 for c in range(len(head))
        ]
        pdf.set_font("Helvetica", "", 9)
        with pdf.table(
            col_widths=widths,
            text_align="LEFT",
            line_height=4.8,
            borders_layout="SINGLE_TOP_LINE",
            headings_style=FontFace(emphasis="BOLD"),
            padding=1.2,
        ) as table:
            for cells in [head, *rows]:
                row = table.row()
                for text in cells:
                    row.cell(text)
        pdf.ln(1)

    def _sources(
        self, pdf: FPDF, sources: list[dict[str, Any]], link_check: dict[str, Any] | None
    ) -> None:
        if not sources:
            return
        status = _status(link_check)
        pdf.ln(3)
        pdf.set_font("Helvetica", "B", 12)
        pdf.multi_cell(0, 7, new_x="LMARGIN", new_y="NEXT", text="Sources")
        pdf.set_font("Helvetica", "", 9.5)
        for s in sources:
            pdf.multi_cell(
                0,
                5.5,
                new_x="LMARGIN",
                new_y="NEXT",
                text=_latin(
                    f"[{s.get('line')}] {s.get('claim')} — {s.get('url')}{_opens(status, s)}"
                ),
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
                    f"Links checked at {link_check.get('checked_at')} from {link_check.get('vantage')}. {LINK_CAVEAT}"
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
