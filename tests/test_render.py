from pathlib import Path

from pypdf import PdfReader

from wf.activities import PdfRenderer
from wf.activities.render import _blocks, _inline

LONG = "https://www.theregister.com/devops/2026/08/12/nodejs-creator-liberates-durable-objects-from-cloudflare-with-celld/5286954"

# A real report put its link table straight after a line of text, and the PDF showed
# it as one paragraph of pipes.
BODY = f"""# celld

Links were checked; a link that opens means the address resolved.
| Ref | Line | Section | Link | Validated |
|---|---|---|---|---|
| [1] | 6 | 1. What celld is | https://github.com/denoland/celld | true |
| [2] | 6 | 1. What celld is | {LONG} | true |
| [4] | 12 | 2. Vocabulary and architecture | https://sourcefeed.dev/a/denos_celld | false |

1. First, see https://celld.dev/docs/some_path.
2. Then **the rest**, with `code`.
"""


def test_a_table_is_read_as_a_table_even_straight_after_text():
    blocks = _blocks(BODY)
    assert [k for k, _ in blocks] == ["h1", "p", "table", "li", "li"]
    head, rows = blocks[2][1]
    assert head == ["Ref", "Line", "Section", "Link", "Validated"]
    assert len(rows) == 3 and rows[1][3] == LONG and rows[2][4] == "false"


def test_emphasis_goes_but_underscores_in_addresses_stay():
    assert _inline("see https://celld.dev/docs/some_path_here and _this_") == (
        "see https://celld.dev/docs/some_path_here and this"
    )
    assert _inline("**bold**, `code`, snake_case") == "bold, code, snake_case"
    assert _inline("[the docs](https://celld.dev/a_b)") == "the docs (https://celld.dev/a_b)"


def test_the_pdf_has_a_table_and_the_markdown_is_kept_beside_it(tmp_path):
    out = tmp_path / "SIMULATED-report.pdf"
    result = PdfRenderer().render_pdf(
        title="celld",
        report={"body_md": BODY, "sources": []},
        followups=[{"title": "More", "body_md": "| a | b |\n|---|---|\n| 1 | 2 |"}],
        link_check=None,
        simulated=True,
        out_path=str(out),
    )
    text = "\n".join(p.extract_text() for p in PdfReader(result["artifact"]).pages)
    # each row is its own line, not the table's pipes run together
    assert "|" not in text
    assert (
        "[4] 12 2. Vocabulary and architecture https://sourcefeed.dev/a/denos_celld false" in text
    )
    assert "some_path" in text and "1. First" in text

    md = Path(result["markdown"])
    assert md == tmp_path / "SIMULATED-report.md"
    written = md.read_text()
    assert written.startswith("> **SIMULATED")
    assert BODY.strip() in written and "# Follow-up 1: More" in written


def test_real_work_carries_no_marking(tmp_path):
    result = PdfRenderer().render_pdf(
        title="celld",
        report={"body_md": "Plain text.", "sources": [{"line": 1, "claim": "c", "url": "u"}]},
        followups=[],
        link_check=None,
        simulated=False,
        out_path=str(tmp_path / "report.pdf"),
    )
    written = Path(result["markdown"]).read_text()
    assert "SIMULATED" not in written
    assert written.startswith("# celld\n\nPlain text.") and "- [1] c — u" in written
