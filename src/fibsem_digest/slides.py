"""Render a markdown document to a styled PDF using WeasyPrint."""
from pathlib import Path

from markdown_it import MarkdownIt
from weasyprint import CSS, HTML


_CSS = """
@page { size: A4; margin: 2cm 2.2cm; }
body {
    font-family: -apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif;
    font-size: 11pt;
    line-height: 1.45;
    color: #1a1a1a;
}
h1 { font-size: 22pt; margin: 0 0 0.6em; color: #111; }
h2 {
    font-size: 15pt;
    margin: 1.4em 0 0.4em;
    padding-bottom: 0.15em;
    border-bottom: 1px solid #ddd;
    page-break-after: avoid;
}
h3 { font-size: 12pt; margin: 1em 0 0.3em; page-break-after: avoid; }
p { margin: 0.4em 0; }
ul, ol { margin: 0.3em 0 0.6em 1.2em; padding: 0; }
li { margin: 0.15em 0; }
code {
    font-family: Menlo, Consolas, monospace;
    font-size: 0.92em;
    background: #f3f3f3;
    padding: 0.05em 0.3em;
    border-radius: 3px;
}
pre {
    background: #f6f6f6;
    padding: 0.6em 0.8em;
    border-radius: 4px;
    font-size: 0.9em;
    overflow-x: auto;
    page-break-inside: avoid;
}
pre code { background: none; padding: 0; }
a { color: #0a5fb4; text-decoration: none; }
blockquote {
    border-left: 3px solid #ccc;
    margin: 0.6em 0;
    padding: 0.1em 0.9em;
    color: #555;
}
"""


def markdown_to_pdf(md_path: Path, pdf_path: Path) -> Path:
    md_text = md_path.read_text(encoding="utf-8")
    html_body = MarkdownIt("commonmark", {"html": False}).render(md_text)
    html_doc = f"<!doctype html><meta charset='utf-8'><title>{md_path.stem}</title><body>{html_body}</body>"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html_doc).write_pdf(pdf_path, stylesheets=[CSS(string=_CSS)])
    return pdf_path
