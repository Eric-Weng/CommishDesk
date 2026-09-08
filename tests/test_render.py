"""Story 2.7 — the bare local HTML dump: escaping, determinism, heading round-trip, file write.

Story 3.3 adds ``narrated_text_to_html`` — the same bare treatment for the LLM
narrator's plain-text recap.
"""

from __future__ import annotations

from html import escape
from pathlib import Path

import pytest

from commishdesk.narrate import Recap, Section
from commishdesk.render import (
    narrated_text_to_html,
    recap_to_html,
    write_draft_recap,
    write_html_file,
)


def _sample() -> Recap:
    return Recap(
        title="A & B <Draft>",
        dateline='2025 · "quoted" · <tag>',
        sections=[
            Section(heading="Lead <1>", blocks=["first & block", "second <b>bold</b>"]),
            Section(heading="Grades", blocks=["Manager: A+"]),
        ],
    )


def test_recap_to_html_escapes_every_interpolated_value() -> None:
    html = recap_to_html(_sample())
    assert "<title>A &amp; B &lt;Draft&gt;</title>" in html
    assert "&lt;tag&gt;" in html
    assert "first &amp; block" in html
    # no raw markup from a value survives into the document
    assert "<Draft>" not in html
    assert "<b>bold</b>" not in html


def test_recap_to_html_is_deterministic_and_round_trips_headings() -> None:
    recap = _sample()
    first = recap_to_html(recap)
    assert first == recap_to_html(recap)
    assert first.startswith("<!doctype html>\n")
    assert "\r\n" not in first
    for section in recap.sections:
        assert f"<h2>{escape(section.heading)}</h2>" in first
        for block in section.blocks:
            assert f"<p>{escape(block)}</p>" in first


def test_narrated_text_to_html_escapes_and_maps_structure() -> None:
    text = (
        "My <Title>\n\n## Sec & 1\n\nA <b> line.\nWrapped.\n\n## Sec 2\n\nDone."
    )
    html = narrated_text_to_html(text, generated_at="2025-09-08T00:00:00.000000Z")
    assert "<title>My &lt;Title&gt;</title>" in html  # first line is title + h1
    assert "<h1>My &lt;Title&gt;</h1>" in html
    assert "<p>generated 2025-09-08T00:00:00.000000Z</p>" in html
    assert "<h2>Sec &amp; 1</h2>" in html
    assert "<p>A &lt;b&gt; line. Wrapped.</p>" in html  # wrapped lines joined
    assert "<h2>Sec 2</h2>" in html
    assert "<p>Done.</p>" in html
    # no raw markup from a value survives, and the dump stays bare
    assert "<Title>" not in html
    assert "<style>" not in html and "<script" not in html


def test_narrated_text_to_html_is_deterministic_and_lf_only() -> None:
    text = "T\n\n## H\n\nbody one\n\nbody two"
    first = narrated_text_to_html(text, generated_at="2025-09-08T00:00:00.000000Z")
    assert first == narrated_text_to_html(text, generated_at="2025-09-08T00:00:00.000000Z")
    assert first.startswith("<!doctype html>\n")
    assert "\r\n" not in first


_TS = "2025-09-08T12:34:56.000000Z"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="empty"),
        pytest.param("   \n\t  \n ", id="whitespace-only"),
        pytest.param("Just a title, nothing else", id="title-only"),
        pytest.param("# A Markdown Title\n\nOne paragraph.", id="hash-title"),
        pytest.param("Title\n\n## \n\nA paragraph under a bare marker.", id="bare-heading"),
    ],
)
def test_narrated_text_to_html_survives_degenerate_input(text: str) -> None:
    html = narrated_text_to_html(text, generated_at=_TS)
    assert html.startswith("<!doctype html>\n")
    assert "\r\n" not in html
    assert f"<p>generated {_TS}</p>" in html  # provenance stamp is always present
    assert "<h1>" in html and "</h1>" in html
    assert "<h2></h2>" not in html  # a bare marker never becomes an empty heading


def test_narrated_text_to_html_title_fallback_and_markers() -> None:
    assert "<h1>Draft Recap</h1>" in narrated_text_to_html("", generated_at=_TS)
    assert "<h1>Draft Recap</h1>" in narrated_text_to_html("   \n \n", generated_at=_TS)
    # a leading ATX marker on the first line is stripped for both title and h1
    md = narrated_text_to_html("### My Recap\n\nbody", generated_at=_TS)
    assert "<title>My Recap</title>" in md and "<h1>My Recap</h1>" in md
    # a bare "## " heading line in the body is a literal paragraph, not <h2>
    bare = narrated_text_to_html("T\n\n## \n\nbody", generated_at=_TS)
    assert "<h2></h2>" not in bare
    assert "<p>##</p>" in bare


def test_narrated_text_to_html_normalizes_crlf_to_lf() -> None:
    html = narrated_text_to_html(
        "Title\r\n\r\n## Section\r\n\r\nA line.\r\nAnother.", generated_at=_TS
    )
    assert "\r" not in html
    assert "<h2>Section</h2>" in html
    assert "<p>A line. Another.</p>" in html


def test_write_draft_recap_writes_lf_utf8_and_returns_the_path(tmp_path: Path) -> None:
    dest = tmp_path / "nested" / "recap.html"
    written = write_draft_recap(_sample(), dest)
    assert written == dest
    raw = dest.read_bytes()
    assert b"\r\n" not in raw
    assert raw.decode("utf-8") == recap_to_html(_sample())


def test_write_html_file_is_the_shared_writer(tmp_path: Path) -> None:
    dest = tmp_path / "deep" / "nest" / "page.html"
    doc = narrated_text_to_html("Voiced Recap\n\nbody — café", generated_at=_TS)
    written = write_html_file(doc, dest)
    assert written == dest
    raw = dest.read_bytes()
    assert b"\r\n" not in raw
    assert raw.decode("utf-8") == doc
