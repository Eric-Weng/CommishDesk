"""Story 2.7 — the bare local HTML dump: escaping, determinism, heading round-trip, file write."""

from __future__ import annotations

from html import escape
from pathlib import Path

from commishdesk.narrate import Recap, Section
from commishdesk.render import recap_to_html, write_draft_recap


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


def test_write_draft_recap_writes_lf_utf8_and_returns_the_path(tmp_path: Path) -> None:
    dest = tmp_path / "nested" / "recap.html"
    written = write_draft_recap(_sample(), dest)
    assert written == dest
    raw = dest.read_bytes()
    assert b"\r\n" not in raw
    assert raw.decode("utf-8") == recap_to_html(_sample())
