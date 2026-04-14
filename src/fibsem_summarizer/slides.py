"""Convert a markdown document to a PowerPoint deck.

Conventions (kept deliberately simple):
- `# H1` starts a title-only slide.
- Each `## H2` starts a new content slide with that heading as the slide title.
- Bullets (`-`, `*`) under an H2 become bulleted lines on the slide body.
- Paragraphs become plain lines on the slide body.
- Code fences become a monospace paragraph.

The point is a deck you can open in PowerPoint/Keynote and tweak — not pixel-perfect output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from markdown_it import MarkdownIt
from markdown_it.token import Token
from pptx import Presentation
from pptx.util import Pt


# Layout indices that ship with the default python-pptx template:
#  0 = Title Slide, 1 = Title and Content, 5 = Title Only, 6 = Blank.
_TITLE_LAYOUT = 0
_CONTENT_LAYOUT = 1


def _tokens_to_slides(tokens: list[Token]) -> list[dict]:
    """Walk markdown-it tokens and produce a list of slide dicts.

    Each slide dict is either {"kind": "title", "title": str} or
    {"kind": "content", "title": str, "body": [(indent, text, is_bullet, is_code), ...]}.
    """
    slides: list[dict] = []
    current: dict | None = None

    def flush():
        nonlocal current
        if current is not None:
            slides.append(current)
            current = None

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == "heading_open":
            level = int(tok.tag[1])  # h1, h2 → 1, 2
            inline = tokens[i + 1]
            text = inline.content
            # skip heading_close
            i += 3
            if level == 1:
                flush()
                slides.append({"kind": "title", "title": text})
                current = None
            else:  # treat h2 and deeper as new slide starts
                flush()
                current = {"kind": "content", "title": text, "body": []}
            continue

        if current is None:
            # Any content before the first H2 is dropped (no slide to attach it to).
            i += 1
            continue

        if tok.type == "bullet_list_open":
            i = _consume_bullet_list(tokens, i, current, indent=0)
            continue
        if tok.type == "paragraph_open":
            inline = tokens[i + 1]
            if inline.content.strip():
                current["body"].append((0, inline.content.strip(), False, False))
            i += 3  # paragraph_open, inline, paragraph_close
            continue
        if tok.type in ("fence", "code_block"):
            for line in tok.content.splitlines():
                current["body"].append((0, line, False, True))
            i += 1
            continue
        i += 1

    flush()
    return slides


def _consume_bullet_list(
    tokens: list[Token], start: int, current: dict, indent: int
) -> int:
    """Walk a bullet_list_open ... bullet_list_close region, handling nested lists.

    Returns the index just past the matching bullet_list_close.
    """
    i = start + 1
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == "bullet_list_close":
            return i + 1
        if tok.type == "list_item_open":
            # Find the first inline inside this item; capture its text as the bullet.
            j = i + 1
            bullet_text = ""
            while j < len(tokens) and tokens[j].type != "list_item_close":
                inner = tokens[j]
                if inner.type == "inline" and not bullet_text:
                    bullet_text = inner.content.strip()
                if inner.type == "bullet_list_open":
                    # Nested list: emit current bullet first, then recurse deeper.
                    if bullet_text:
                        current["body"].append((indent, bullet_text, True, False))
                        bullet_text = ""
                    j = _consume_bullet_list(tokens, j, current, indent + 1)
                    continue
                j += 1
            if bullet_text:
                current["body"].append((indent, bullet_text, True, False))
            i = j + 1  # skip past list_item_close
            continue
        i += 1
    return i


def _render(slides: Iterable[dict], out_path: Path) -> None:
    prs = Presentation()
    for slide in slides:
        if slide["kind"] == "title":
            layout = prs.slide_layouts[_TITLE_LAYOUT]
            s = prs.slides.add_slide(layout)
            s.shapes.title.text = slide["title"]
            continue

        layout = prs.slide_layouts[_CONTENT_LAYOUT]
        s = prs.slides.add_slide(layout)
        s.shapes.title.text = slide["title"]

        # The "content" placeholder is index 1 on this layout.
        body_shape = s.placeholders[1]
        tf = body_shape.text_frame
        tf.word_wrap = True
        first = True
        for indent, text, is_bullet, is_code in slide["body"]:
            p = tf.paragraphs[0] if first else tf.add_paragraph()
            first = False
            p.text = text
            p.level = indent if is_bullet else 0
            if is_code:
                for run in p.runs:
                    run.font.name = "Menlo"
                    run.font.size = Pt(14)
    prs.save(out_path)


def markdown_to_pptx(md_path: Path, pptx_path: Path) -> Path:
    md_text = md_path.read_text(encoding="utf-8")
    tokens = MarkdownIt().parse(md_text)
    slides = _tokens_to_slides(tokens)
    if not slides:
        raise ValueError(
            f"No slides found in {md_path}. Markdown needs at least one `## H2`."
        )
    pptx_path.parent.mkdir(parents=True, exist_ok=True)
    _render(slides, pptx_path)
    return pptx_path
