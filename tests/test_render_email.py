"""Story 4.2 — ``render_email`` and the ``EmailParts`` (html, text) pair.

Email-safe HTML (nested ``<table role="presentation">``, every visual style
inlined, no ``<img>`` / ``<svg>`` / ``<script>`` / ``url(`` / ``var(--`` / flex /
grid), byte-determinism of *both* parts, a dark-mode-safe masthead
(``color-scheme`` declared + explicit ``bgcolor``, not media-query-dependent),
adversarial-string escaping in HTML *and* text, empty-data edges, ``llm_text``
whose first line is a ``## `` heading (title still league + season), and the
neither / both ``ValueError``.

Committed tests never read ``../brief/`` — the render input is the committed demo
oracle fixture.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from commishdesk.facts.schema import DraftRecapFacts
from commishdesk.narrate import Recap, Section, render_draft_recap
from commishdesk.render import EmailParts, render_email
from commishdesk.render.style import REACH_HEX, VALUE_HEX, fmt_signed, position_label

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_FACTS_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "facts" / "expected-draft-recap-facts.json"
)

_STAMP = "2026-09-03T00:00:00.000000Z"

# Constructs that must never appear in an email-safe body.
_FORBIDDEN = (
    "<img",
    "<svg",
    "<script",
    "url(",
    "var(--",
    "display:flex",
    "display: flex",
    "display:grid",
    "display: grid",
    "@import",
)


def _facts() -> DraftRecapFacts:
    return DraftRecapFacts.model_validate(
        json.loads(EXPECTED_FACTS_PATH.read_text(encoding="utf-8"))
    )


def _recap(facts: DraftRecapFacts) -> Recap:
    return render_draft_recap(facts.narration)


def _parts(**over: object) -> EmailParts:
    facts = over.pop("facts", None) or _facts()
    kwargs: dict[str, object] = {"generated_at": _STAMP}
    if "llm_text" not in over and "recap" not in over:
        kwargs["recap"] = _recap(facts)
    kwargs.update(over)
    return render_email(facts, **kwargs)  # type: ignore[arg-type]


def _balanced(html: str, tag: str) -> None:
    assert html.count(f"<{tag}") == html.count(f"</{tag}>"), tag


# --------------------------------------------------------------------------- #
# Email-safe HTML
# --------------------------------------------------------------------------- #


def test_html_is_a_table_layout_with_no_unsafe_construct() -> None:
    html = _parts().html
    assert html.startswith("<!DOCTYPE html>")
    assert html.endswith("\n") and "\r" not in html
    assert '<table role="presentation"' in html
    for token in _FORBIDDEN:
        assert token not in html, token
    for tag in ("table", "tr", "td", "span"):
        _balanced(html, tag)


def test_the_only_style_block_is_progressive_enhancement() -> None:
    """A ``<style>`` head block may hold only an ``@media`` width tweak / ``mso``
    conditional — never the sole copy of a style that matters. Every colour, font
    and spacing decision is inline on an element."""
    html = _parts().html
    # the last <style>...</style> is the real head block (the first is the mso
    # conditional). It carries only progressive enhancement.
    head_style = html.rsplit("<style>", 1)[1].split("</style>", 1)[0]
    assert "color:" not in head_style
    assert "font-family:" not in head_style
    assert "background-color:" not in head_style
    assert "@media only screen" in head_style  # the width tweak is here
    # everything inside the @media block is layout-only (width / padding)
    media = head_style.split("@media", 1)[1]
    assert "color" not in media and "font" not in media
    # and the real styling is inline: dozens of style="" attributes
    assert html.count('style="') > 40


def test_charts_are_bgcolor_table_cells_not_images() -> None:
    html = _parts().html
    assert "<svg" not in html and "<img" not in html
    # the pick-count bars are bgcolor cells whose width is a percentage
    assert 'bgcolor="#46524E"' in html
    assert "%;background-color:#46524E;height:12px" in html


def test_masthead_survives_dark_mode_without_a_media_query() -> None:
    html = _parts().html
    assert '<meta name="color-scheme" content="light">' in html
    assert '<meta name="supported-color-schemes" content="light">' in html
    # the masthead row paints an explicit bgcolor + inline colour, so an inverting
    # client has a concrete value to keep
    masthead = html.split("Draft Recap</div>", 1)[0]
    assert 'bgcolor="#ECEEE9"' in masthead
    assert "background-color:#ECEEE9;" in masthead
    # the masthead's base appearance is inline, not carried by a media query:
    # the @media block touches only .container width and .px padding
    media = html.rsplit("<style>", 1)[1].split("@media", 1)[1].split("</style>", 1)[0]
    assert "ECEEE9" not in media and "color" not in media


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def test_body_input_contract_requires_exactly_one() -> None:
    facts = _facts()
    with pytest.raises(ValueError, match="neither"):
        render_email(facts, generated_at=_STAMP)
    with pytest.raises(ValueError, match="both"):
        render_email(
            facts, recap=_recap(facts), llm_text="## The Lead\n\nx", generated_at=_STAMP
        )


def test_both_parts_are_byte_identical_across_two_renders() -> None:
    a, b = _parts(), _parts()
    assert a.html == b.html
    assert a.text == b.text

    facts = _facts()
    llm = "## The Lead\n\nBody one.\n\n## Team Grades\n\nBody two."
    x = render_email(facts, llm_text=llm, generated_at=_STAMP)
    y = render_email(facts, llm_text=llm, generated_at=_STAMP)
    assert x == y


def test_generated_at_is_the_only_time_value() -> None:
    a = _parts(generated_at="2020-01-01T00:00:00.000000Z")
    b = _parts(generated_at="2099-12-31T23:59:59.999999Z")
    assert a.html.replace("2020-01-01T00:00:00.000000Z", "X") == b.html.replace(
        "2099-12-31T23:59:59.999999Z", "X"
    )
    assert a.text.replace("2020-01-01T00:00:00.000000Z", "X") == b.text.replace(
        "2099-12-31T23:59:59.999999Z", "X"
    )


# --------------------------------------------------------------------------- #
# Content — masthead, sections, board data, in both parts
# --------------------------------------------------------------------------- #


def test_html_conveys_the_masthead_sections_and_board() -> None:
    facts = _facts()
    html = _parts(facts=facts).html
    assert facts.league.name in html
    assert f"{facts.league.season} Season" in html
    # a narrated section heading is present
    assert "The Lead" in html
    # round-1 board rows: the 1.01 pick's board label + player
    first = min((p for p in facts.picks if p.round == 1), key=lambda p: p.pick_no)
    assert first.board_label in html
    assert first.player.name in html
    # pick-count bars: every ranked manager appears
    for row in facts.draft_summary.pick_count_rank:
        assert row.manager in html


def test_text_part_is_recap_text_plus_the_board_and_counts() -> None:
    facts = _facts()
    parts = _parts(facts=facts)
    assert parts.text.strip()
    # provenance stamp is the very first line
    assert parts.text.startswith(f"generated {_STAMP}\n\n")
    # the template narrator's flattened prose (title + dateline + sections) follows
    recap = _recap(facts)
    assert recap.title in parts.text
    assert "The Lead" in parts.text
    # then the two data blocks
    assert "THE BOARD" in parts.text
    assert "PICKS PER TEAM" in parts.text

    r1 = sorted((p for p in facts.picks if p.round == 1), key=lambda p: p.pick_no)
    # a non-None-delta round-1 pick renders the full spec'd line, signed tail and all
    scored = next(p for p in r1 if p.delta is not None)
    assert (
        f"{scored.board_label}  {scored.manager} — {scored.player.name} "
        f"({position_label(scored.player.position)})  {fmt_signed(scored.delta)}"
    ) in parts.text
    # a delta-None round-1 pick has no numeric tail
    unscored = next(p for p in r1 if p.delta is None)
    line = next(
        ln for ln in parts.text.splitlines() if ln.startswith(f"{unscored.board_label}  ")
    )
    assert line == (
        f"{unscored.board_label}  {unscored.manager} — {unscored.player.name} "
        f"({position_label(unscored.player.position)})"
    )

    leader = facts.draft_summary.pick_count_rank[0]
    assert f"{leader.manager}  {leader.pick_count}" in parts.text


def test_llm_body_prose_with_data_blocks_in_text() -> None:
    facts = _facts()
    llm = "## The Lead\n\nOpening.\n\n## Team Grades\n\nGrades."
    parts = render_email(facts, llm_text=llm, generated_at=_STAMP)
    assert "## The Lead" not in parts.text  # markers stripped to plain lines
    assert "The Lead" in parts.text and "Opening." in parts.text
    assert "THE BOARD" in parts.text and "PICKS PER TEAM" in parts.text


def _verdict_cell(html: str, board_label: str) -> str:
    """The right-aligned verdict ``<td>`` for the board row whose Pk cell is
    ``board_label``."""
    row = html.split(f'">{board_label}</td>', 1)[1].split("</tr>", 1)[0]
    return '<td align="right"' + row.rsplit('<td align="right"', 1)[1]


def test_delta_none_row_has_no_verdict_chip() -> None:
    facts = _facts()
    no_consensus = next(
        p for p in facts.picks if p.round == 1 and p.flags == ["no_consensus"]
    )
    assert no_consensus.delta is None
    html = _parts(facts=facts).html
    assert no_consensus.board_label in html
    cell = _verdict_cell(html, no_consensus.board_label)
    assert "<span" not in cell  # the verdict cell is just &nbsp;
    for label in ("value ", "reach ", "fair "):
        assert label not in cell


def test_verdict_chip_sign_to_colour_mapping() -> None:
    facts = _facts()
    html = _parts(facts=facts).html
    r1 = [p for p in facts.picks if p.round == 1]

    value_pick = next(p for p in r1 if p.delta is not None and p.delta > 2)
    cell = _verdict_cell(html, value_pick.board_label)
    assert f">value {fmt_signed(value_pick.delta)}</span>" in cell
    assert f"color:{VALUE_HEX};" in cell

    reach_pick = next(p for p in r1 if p.delta is not None and p.delta < -2)
    cell = _verdict_cell(html, reach_pick.board_label)
    assert f">reach {fmt_signed(reach_pick.delta)}</span>" in cell
    assert f"color:{REACH_HEX};" in cell

    fair_pick = next(p for p in r1 if p.delta is not None and abs(p.delta) <= 2)
    cell = _verdict_cell(html, fair_pick.board_label)
    assert f">fair {fmt_signed(fair_pick.delta)}</span>" in cell
    # the fair chip carries neither the value nor the reach hue
    assert f"color:{VALUE_HEX};" not in cell and f"color:{REACH_HEX};" not in cell


# --------------------------------------------------------------------------- #
# Title is never parsed out of llm_text
# --------------------------------------------------------------------------- #


def test_llm_first_line_heading_is_not_eaten_as_the_title() -> None:
    facts = _facts()
    llm = "## The Lead\n\nOpening paragraph.\n\n## Team Grades\n\nGrades paragraph."
    html = render_email(facts, llm_text=llm, generated_at=_STAMP).html
    assert f"<title>{facts.league.name} — {facts.league.season} Draft Recap</title>" in html
    # both headings survive as body section headers, nothing dropped
    assert html.count(">The Lead</div>") == 1
    assert html.count(">Team Grades</div>") == 1
    assert "Opening paragraph." in html
    assert "Grades paragraph." in html


def test_masthead_title_comes_from_facts_not_the_recap() -> None:
    facts = _facts()
    recap = Recap(
        title="TOTALLY DIFFERENT TITLE",
        dateline="nope",
        sections=[Section(heading="The Lead", blocks=["hi"])],
    )
    html = render_email(facts, recap=recap, generated_at=_STAMP).html
    assert "TOTALLY DIFFERENT TITLE" not in html
    assert f"<title>{facts.league.name} — {facts.league.season} Draft Recap</title>" in html


# --------------------------------------------------------------------------- #
# Adversarial strings
# --------------------------------------------------------------------------- #

_ADVERSARIAL = 'A<b>& "Team" 😀 ‏א'


def test_adversarial_strings_are_escaped_in_html_and_text() -> None:
    base = _facts()
    picks = list(base.picks)
    picks[0] = picks[0].model_copy(
        update={
            "manager": _ADVERSARIAL,
            "player": picks[0].player.model_copy(update={"name": _ADVERSARIAL}),
        }
    )
    facts = base.model_copy(
        update={
            "league": base.league.model_copy(update={"name": _ADVERSARIAL}),
            "picks": picks,
        }
    )
    recap = Recap(
        title="ignored",
        dateline="ignored",
        sections=[Section(heading=_ADVERSARIAL, blocks=[_ADVERSARIAL])],
    )
    parts = render_email(facts, recap=recap, generated_at=_STAMP)

    assert "<b>&" not in parts.html
    assert "&lt;b&gt;&amp;" in parts.html
    # bidi controls stripped, not passed through, in BOTH parts
    for cp in (0x200E, 0x200F, 0x202A, 0x202E, 0x2066, 0x2069):
        assert chr(cp) not in parts.html
        assert chr(cp) not in parts.text
    # the table stays balanced after the injection
    for tag in ("table", "tr", "td", "span"):
        _balanced(parts.html, tag)
    # the payload is visible as text somewhere in the plain part
    assert 'A<b>& "Team"' in parts.text


# --------------------------------------------------------------------------- #
# Empty-data edges — never raise, empty-state the block
# --------------------------------------------------------------------------- #


def test_empty_pick_list_still_renders_both_parts() -> None:
    facts = _facts().model_copy(update={"picks": []})
    parts = render_email(facts, recap=_recap(_facts()), generated_at=_STAMP)
    assert parts.html.startswith("<!DOCTYPE html>")
    assert "No picks landed on the board." in parts.html
    assert "No picks landed on the board." in parts.text
    assert parts.text.strip()
    for tag in ("table", "tr", "td"):
        _balanced(parts.html, tag)


def test_empty_pick_count_rank_empty_states_that_block() -> None:
    facts = _facts()
    facts = facts.model_copy(
        update={
            "draft_summary": facts.draft_summary.model_copy(
                update={"pick_count_rank": []}
            )
        }
    )
    parts = render_email(facts, recap=_recap(_facts()), generated_at=_STAMP)
    assert "No pick-count data on the board." in parts.html
    assert "No pick-count data on the board." in parts.text


# --------------------------------------------------------------------------- #
# AD-1 import fence
# --------------------------------------------------------------------------- #


def test_email_module_imports_only_the_sanctioned_surface() -> None:
    tree = ast.parse(
        (REPO_ROOT / "commishdesk" / "render" / "email.py").read_text(encoding="utf-8")
    )
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    roots.discard("__future__")
    external = roots - sys.stdlib_module_names
    assert external <= {"commishdesk"}, external


def test_email_import_pulls_in_no_sdk_and_nothing_upstream_of_facts() -> None:
    prog = (
        "import sys;"
        "import commishdesk.render.email as e;"
        "from commishdesk.render import render_email, EmailParts, write_text_file;"
        "assert e.render_email is render_email;"
        "bad = {'anthropic', 'google.genai', 'httpx'} & sys.modules.keys();"
        "assert not bad, sorted(bad);"
        "fence = {'commishdesk.ingest', 'commishdesk.stats', 'commishdesk.adapters'} "
        "& sys.modules.keys();"
        "assert not fence, sorted(fence);"
        "print('ok')"
    )
    out = subprocess.run(
        [sys.executable, "-c", prog],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"
