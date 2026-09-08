"""Stage 5 — one content model, three surfaces: a self-contained web page, a
dark-mode-safe email, and a Discord post with a rendered image.

At MVP this is a bare, unstyled local HTML dump of the template narrator's
:class:`~commishdesk.narrate.Recap` — a literal ``<h1>``/``<h2>``/``<p>``
transcription with every value ``html.escape``-d, no CSS and no script. The
designed inline-SVG render is Story 4.1. This module imports only the narrator's
output type + the standard library (AD-1).
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from commishdesk.narrate import Recap

__all__ = [
    "narrated_text_to_html",
    "recap_to_html",
    "write_draft_recap",
    "write_html_file",
]

#: A Markdown ATX heading line with real text after the marker (``## Superlatives``).
#: A bare marker with nothing after it (``##`` / ``## ``) deliberately does not match —
#: it falls through to a normal ``<p>`` of the literal line, never ``<h2></h2>``.
_HEADING_LINE = re.compile(r"^#{1,6}[ \t]+(\S.*?)\s*$")
#: A leading ATX marker to strip off the title line (``# My Recap`` -> ``My Recap``).
_TITLE_MARKER = re.compile(r"^#{1,6}[ \t]+")


def recap_to_html(recap: Recap) -> str:
    """Render ``recap`` as a bare, deterministic HTML document (``\\n`` newlines).

    ``<h1>`` title, ``<p>`` dateline, then ``<h2>`` + ``<p>`` per section. Every
    interpolated value is escaped; there is no CSS, no ``<style>``, and no script.
    """
    escape = html.escape
    out: list[str] = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{escape(recap.title)}</title>",
        "</head>",
        "<body>",
        f"<h1>{escape(recap.title)}</h1>",
        f"<p>{escape(recap.dateline)}</p>",
    ]
    for section in recap.sections:
        out.append(f"<h2>{escape(section.heading)}</h2>")
        for block in section.blocks:
            out.append(f"<p>{escape(block)}</p>")
    out.append("</body>")
    out.append("</html>")
    return "\n".join(out) + "\n"


def narrated_text_to_html(text: str, *, generated_at: str) -> str:
    """Render the LLM narrator's plain-text recap as a bare, deterministic HTML
    document (``\\n`` newlines).

    A literal, defensive transcription — it never trusts the model's output shape:

    * newlines are normalized to ``\\n`` first;
    * the first non-empty line, with any leading ``#``-``######`` marker stripped,
      becomes both the ``<title>`` and the ``<h1>`` (falling back to the literal
      ``"Draft Recap"`` when there is no non-empty line);
    * ``<p>generated {generated_at}</p>`` immediately follows the ``<h1>`` — the
      only provenance stamp on this surface;
    * any ``#``-``######`` heading line with text after the marker becomes a flat
      ``<h2>``; a bare marker with nothing after it is emitted as an ordinary
      ``<p>`` of the literal line;
    * every other blank-line-separated run of prose becomes a ``<p>`` (wrapped
      lines joined with a single space).

    Every interpolated value is ``html.escape``-d; there is no CSS, no ``<style>``,
    and no script. The designed render is Story 4.1 — this is deliberately unstyled.
    """
    escape = html.escape
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")

    title = "Draft Recap"
    body_start = len(lines)
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped:
            continue
        candidate = _TITLE_MARKER.sub("", stripped).strip()
        if candidate and set(candidate) != {"#"}:
            title = candidate
        body_start = index + 1
        break

    out: list[str] = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{escape(title)}</title>",
        "</head>",
        "<body>",
        f"<h1>{escape(title)}</h1>",
        f"<p>generated {escape(generated_at)}</p>",
    ]

    paragraph: list[str] = []

    def _flush() -> None:
        if paragraph:
            out.append(f"<p>{escape(' '.join(paragraph))}</p>")
            paragraph.clear()

    for raw in lines[body_start:]:
        line = raw.strip()
        if not line:
            _flush()
            continue
        heading = _HEADING_LINE.match(line)
        if heading:
            _flush()
            out.append(f"<h2>{escape(heading.group(1))}</h2>")
        else:
            paragraph.append(line)
    _flush()

    out.append("</body>")
    out.append("</html>")
    return "\n".join(out) + "\n"


def write_html_file(document: str, dest: Path) -> Path:
    """Write an HTML *document* string to *dest* as UTF-8 with ``\\n`` newlines,
    creating parent directories, and return the path written. The one file-write
    contract shared by every HTML surface in this stage."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(document, encoding="utf-8", newline="\n")
    return dest


def write_draft_recap(recap: Recap, dest: Path) -> Path:
    """Write :func:`recap_to_html` to ``dest`` (via :func:`write_html_file`) and
    return the path written."""
    return write_html_file(recap_to_html(recap), dest)
