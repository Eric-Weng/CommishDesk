"""Stage 5 — one content model, three surfaces: a self-contained web page, a dark-mode-safe email, and a Discord post with a rendered image.

At MVP this is a bare, unstyled local HTML dump of the template narrator's
:class:`~commishdesk.narrate.Recap` — a literal ``<h1>``/``<h2>``/``<p>``
transcription with every value ``html.escape``-d, no CSS and no script. The
designed inline-SVG render is Story 4.1. This module imports only the narrator's
output type + the standard library (AD-1).
"""

from __future__ import annotations

import html
from pathlib import Path

from commishdesk.narrate import Recap

__all__ = ["recap_to_html", "write_draft_recap"]


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


def write_draft_recap(recap: Recap, dest: Path) -> Path:
    """Write :func:`recap_to_html` to ``dest`` as UTF-8 with ``\\n`` newlines and
    return the path written."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(recap_to_html(recap), encoding="utf-8", newline="\n")
    return dest
