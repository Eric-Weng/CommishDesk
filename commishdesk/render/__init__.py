"""Stage 5 — one content model, three surfaces: a self-contained web page, a
dark-mode-safe email, and a Discord post with a rendered image.

The bare, unstyled local HTML dump of the template narrator's
:class:`~commishdesk.narrate.Recap` (:func:`recap_to_html`) and of the LLM
narrator's plain text (:func:`narrated_text_to_html`) — a literal
``<h1>``/``<h2>``/``<p>`` transcription with every value bidi-stripped then
escaped (``_esc``), no CSS and no script — is kept as-is (``test_I4`` still folds
in ``recap_to_html``). :func:`recap_to_html` reads its argument structurally (a
local :class:`_RecapLike` protocol), so the same generic dump serves either the
draft-recap :class:`~commishdesk.narrate.Recap` or the weekly
:class:`~commishdesk.narrate.weekly_template.WeeklyIssue` (Story 5.11a) with no
runtime branch. The
designed inline-SVG render is :func:`~commishdesk.render.web.render_web` (Story
4.1); the email-deliverable render — client-safe ``<table>`` HTML plus a
``text/plain`` alternative — is :func:`~commishdesk.render.email.render_email`
(Story 4.2), returning an :class:`~commishdesk.render.email.EmailParts` pair. The
CLI writes all three files. The weekly Issue's designed page is
:func:`~commishdesk.render.weekly_web.render_weekly_web` (Story 5.14a); the weekly
run writes it in place of the generic dump. This module imports the narrator output type,
``commishdesk.facts`` schema types (via ``render/web.py`` / ``render/email.py``),
and the standard library only (AD-1).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from commishdesk.narrate import Recap
from commishdesk.render._body import _esc
from commishdesk.render.discord import render_discord_summary, render_weekly_discord_summary
from commishdesk.render.email import EmailParts, render_email
from commishdesk.render.web import render_web
from commishdesk.render.weekly_web import render_weekly_web

__all__ = [
    "EmailParts",
    "narrated_text_to_html",
    "recap_to_html",
    "render_discord_summary",
    "render_email",
    "render_web",
    "render_weekly_discord_summary",
    "render_weekly_web",
    "write_draft_recap",
    "write_html_file",
    "write_text_file",
]


class _SectionLike(Protocol):
    """The structural shape :func:`recap_to_html` reads from one section: a
    ``heading`` and its ordered ``blocks`` of prose. Both
    :class:`~commishdesk.narrate.Section` (draft recap) and
    :class:`~commishdesk.narrate.weekly_template.WeeklySection` (weekly Issue)
    satisfy it field-for-field. Declared as read-only properties so a model with
    a mutable ``list[str]`` attribute still matches."""

    @property
    def heading(self) -> str: ...

    @property
    def blocks(self) -> Sequence[str]: ...


class _RecapLike(Protocol):
    """The structural shape :func:`recap_to_html` reads from a rendered Issue: a
    ``title``, a one-line ``dateline``, and its ordered ``sections``. Both the
    draft-recap :class:`~commishdesk.narrate.Recap` and the weekly
    :class:`~commishdesk.narrate.weekly_template.WeeklyIssue` satisfy it, so the
    one Story 2.7 generic HTML dump serves either without a runtime branch."""

    @property
    def title(self) -> str: ...

    @property
    def dateline(self) -> str: ...

    @property
    def sections(self) -> Sequence[_SectionLike]: ...


#: A Markdown ATX heading line with real text after the marker (``## Superlatives``).
#: A bare marker with nothing after it (``##`` / ``## ``) deliberately does not match —
#: it falls through to a normal ``<p>`` of the literal line, never ``<h2></h2>``.
_HEADING_LINE = re.compile(r"^#{1,6}[ \t]+(\S.*?)\s*$")
#: A leading ATX marker to strip off the title line (``# My Recap`` -> ``My Recap``).
_TITLE_MARKER = re.compile(r"^#{1,6}[ \t]+")


def recap_to_html(recap: _RecapLike) -> str:
    """Render ``recap`` as a bare, deterministic HTML document (``\\n`` newlines).

    ``<h1>`` title, ``<p>`` dateline, then ``<h2>`` + ``<p>`` per section. Every
    interpolated value is bidi-stripped then escaped (``_esc``); there is no CSS,
    no ``<style>``, and no script.

    ``recap`` is read structurally (:class:`_RecapLike`), so the same dump serves
    a draft-recap :class:`~commishdesk.narrate.Recap` or a weekly
    :class:`~commishdesk.narrate.weekly_template.WeeklyIssue` — its bytes for any
    given title / dateline / sections are identical either way.
    """
    escape = _esc
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

    Every interpolated value is bidi-stripped then escaped (``_esc``); there is no
    CSS, no ``<style>``, and no script. The designed render is Story 4.1 — this is
    deliberately unstyled.
    """
    escape = _esc
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


def write_text_file(document: str, dest: Path) -> Path:
    """Write a plain-text *document* string to *dest* as UTF-8 with ``\\n``
    newlines, creating parent directories, and return the path written — the
    ``text/plain`` sibling of :func:`write_html_file` (Story 4.2's
    ``render_email`` returns an ``(html, text)`` pair; the CLI writes both)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(document, encoding="utf-8", newline="\n")
    return dest


def write_draft_recap(recap: Recap, dest: Path) -> Path:
    """Write :func:`recap_to_html` to ``dest`` (via :func:`write_html_file`) and
    return the path written."""
    return write_html_file(recap_to_html(recap), dest)
