"""Story 4.1 — ``render_web`` and the shared visual system.

Self-containment (zero external sub-resource requests), byte-determinism,
three-way theme presence, well-formed inline SVG, escaping of adversarial values,
empty-data edge cases, and ``llm_text`` whose first line is a ``## `` heading
(title still from ``facts.league``, no section lost).

Committed tests never read ``../brief/`` — the render input is the committed demo
oracle fixture.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from xml.dom import minidom

import pytest

from commishdesk.facts.schema import DraftRecapFacts, PickRow, PlayerRef
from commishdesk.narrate import Recap, Section, render_draft_recap
from commishdesk.render import render_web
from commishdesk.render import style as style_mod
from commishdesk.render import web as web_mod

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_FACTS_PATH = REPO_ROOT / "tests" / "fixtures" / "facts" / "expected-draft-recap-facts.json"

_STAMP = "2026-09-03T00:00:00.000000Z"


def _facts() -> DraftRecapFacts:
    return DraftRecapFacts.model_validate(
        json.loads(EXPECTED_FACTS_PATH.read_text(encoding="utf-8"))
    )


def _recap(facts: DraftRecapFacts) -> Recap:
    return render_draft_recap(facts.narration)


def _page(**over: object) -> str:
    facts = over.pop("facts", None) or _facts()
    kwargs: dict[str, object] = {
        "output_id": "demo",
        "generated_at": _STAMP,
    }
    if "llm_text" not in over and "recap" not in over:
        kwargs["recap"] = _recap(facts)
    kwargs.update(over)
    return render_web(facts, **kwargs)  # type: ignore[arg-type]


def _svgs(page: str) -> list[str]:
    return re.findall(r"<svg\b.*?</svg>", page, flags=re.DOTALL)


# --------------------------------------------------------------------------- #
# Self-containment
# --------------------------------------------------------------------------- #


def test_page_is_one_self_contained_document() -> None:
    page = _page()
    assert page.startswith("<!doctype html>")
    assert page.endswith("\n") and "\r" not in page
    assert page.count("<style>") == 1 and page.count("</style>") == 1
    assert "<script" not in page.lower()


def _assert_no_sub_resource(page: str) -> None:
    """No actual sub-resource construct — not a blunt 'no http anywhere' (which a
    URL in LLM prose or a league name would legitimately trip)."""
    assert "<link " not in page and "<link>" not in page
    assert "<script" not in page
    assert " src=" not in page and " srcset=" not in page
    assert "@import" not in page
    style = page.split("<style>", 1)[1].split("</style>", 1)[0]
    assert "url(" not in style
    assert "http://" not in style and "https://" not in style
    # no protocol-relative //host in any attribute value
    assert not re.search(r'=\s*"[^"]*//[^"/]', page)
    assert not re.search(r"=\s*'[^']*//[^'/]", page)
    # every href (there are none today) must be a bare fragment
    for target in re.findall(r'href="([^"]*)"', page):
        assert target.startswith("#"), target


def test_no_external_sub_resource_request() -> None:
    _assert_no_sub_resource(_page())
    # and a league name / prose containing a bare URL does not become a sub-resource
    facts = _facts()
    facts = facts.model_copy(
        update={"league": facts.league.model_copy(update={"name": "Visit http://evil.test now"})}
    )
    page = render_web(
        facts,
        llm_text="## The Lead\n\nSee https://spam.example/x for details.",
        output_id="x",
        generated_at=_STAMP,
    )
    _assert_no_sub_resource(page)
    assert "Visit http://evil.test now" in page  # kept as escaped visible text


def test_charts_are_inline_svg_in_document_order() -> None:
    page = _page()
    svgs = _svgs(page)
    assert len(svgs) == 3, "grid + pick-count bar + positional timeline"
    grid, bar, timeline = svgs
    assert "Draft board grid" in grid
    assert "Picks made per team" in bar
    assert "When each position was drafted" in timeline
    # the grid is the lead visual — first <svg> in the body
    assert page.index(grid) < page.index(bar) < page.index(timeline)


def test_every_svg_has_a_title_first_child() -> None:
    for svg in _svgs(_page()):
        assert re.match(r"<svg\b[^>]*>\s*<title>[^<]+</title>", svg), svg[:120]


def test_figwrap_scroll_container_is_keyboard_operable() -> None:
    page = _page()
    wraps = re.findall(r'<div class="figwrap"[^>]*>', page)
    assert len(wraps) == 3
    for wrap in wraps:
        assert 'tabindex="0"' in wrap
        assert 'role="group"' in wrap
        assert "aria-label=" in wrap


def test_positions_use_the_shared_four_hue_set_and_reach_value_colours() -> None:
    page = _page()
    for var in ("--pos-qb", "--pos-rb", "--pos-wr", "--pos-te"):
        assert f"var({var})" in page
    assert "var(--value)" in page and "var(--reach)" in page


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def test_body_input_contract_requires_exactly_one() -> None:
    facts = _facts()
    with pytest.raises(ValueError, match="neither"):
        render_web(facts, output_id="x", generated_at=_STAMP)
    with pytest.raises(ValueError, match="both"):
        render_web(
            facts,
            recap=_recap(facts),
            llm_text="## The Lead\n\nx",
            output_id="x",
            generated_at=_STAMP,
        )


def test_two_renders_of_the_same_inputs_are_byte_identical() -> None:
    assert _page() == _page()
    facts = _facts()
    llm = "## The Lead\n\nBody one.\n\n## Team Grades\n\nBody two."
    a = render_web(facts, llm_text=llm, output_id="42", generated_at=_STAMP)
    b = render_web(facts, llm_text=llm, output_id="42", generated_at=_STAMP)
    assert a == b


def test_generated_at_is_the_only_time_value() -> None:
    a = _page(generated_at="2020-01-01T00:00:00.000000Z")
    b = _page(generated_at="2099-12-31T23:59:59.999999Z")
    assert a.replace("2020-01-01T00:00:00.000000Z", "X") == b.replace(
        "2099-12-31T23:59:59.999999Z", "X"
    )


# --------------------------------------------------------------------------- #
# Three-way theme structure (mirrors the reference stylesheet)
# --------------------------------------------------------------------------- #


def test_all_three_theme_states_are_present() -> None:
    css = style_mod.build_style()
    assert css.strip().startswith(":root {")
    assert "@media (prefers-color-scheme: dark) {" in css
    assert ':root:not([data-theme="light"]) {' in css
    assert ':root[data-theme="dark"] {' in css
    # paper/ink defined on bare :root (never only inside a media/[data-theme] block)
    root_block = css.split("}", 1)[0]
    assert "--paper:" in root_block and "--ink:" in root_block


def test_dark_palette_differs_from_light_for_paper_and_ink() -> None:
    assert style_mod._LIGHT_TOKENS["--paper"] != style_mod._DARK_TOKENS["--paper"]
    assert style_mod._LIGHT_TOKENS["--ink"] != style_mod._DARK_TOKENS["--ink"]
    assert set(style_mod._LIGHT_TOKENS) == set(style_mod._DARK_TOKENS)


# --------------------------------------------------------------------------- #
# Well-formed SVG + escaping
# --------------------------------------------------------------------------- #


def test_every_svg_parses_as_xml() -> None:
    for svg in _svgs(_page()):
        minidom.parseString(svg)  # raises on malformed markup


_ADVERSARIAL = 'A<b>& "Team" 😀 ‏א'


def test_adversarial_strings_are_escaped_in_html_and_svg() -> None:
    facts = _facts().model_copy(
        update={
            "league": _facts().league.model_copy(
                update={"name": _ADVERSARIAL, "season": "2025"}
            )
        }
    )
    # a manager and a player name carrying the same payload, on the board
    picks = list(facts.picks)
    picks[0] = picks[0].model_copy(
        update={
            "manager": _ADVERSARIAL,
            "player": picks[0].player.model_copy(update={"name": _ADVERSARIAL}),
        }
    )
    facts = facts.model_copy(update={"picks": picks})
    recap = Recap(
        title="ignored",
        dateline="ignored",
        sections=[Section(heading=_ADVERSARIAL, blocks=[_ADVERSARIAL])],
    )
    page = render_web(facts, recap=recap, output_id="x", generated_at=_STAMP)

    assert "<b>&" not in page
    assert "&lt;b&gt;&amp;" in page
    # the bidi control (U+200F, in _ADVERSARIAL) is stripped, not passed through
    for cp in (0x200E, 0x200F, 0x202A, 0x202E, 0x2066, 0x2069):
        assert chr(cp) not in page
    # every <svg> still well-formed after the injection
    for svg in _svgs(page):
        minidom.parseString(svg)
    # tag structure stays balanced for the structural elements
    for tag in ("section", "figure", "svg", "text", "figcaption"):
        assert page.count(f"<{tag}") == page.count(f"</{tag}>"), tag
    assert page.count("<style>") == page.count("</style>") == 1
    assert page.count("<head>") == page.count("</head>") == 1
    assert page.count("<html") == page.count("</html>") == 1


def test_masthead_title_comes_from_facts_not_the_recap() -> None:
    facts = _facts()
    recap = Recap(
        title="TOTALLY DIFFERENT TITLE",
        dateline="nope",
        sections=[Section(heading="The Lead", blocks=["hi"])],
    )
    page = render_web(facts, recap=recap, output_id="x", generated_at=_STAMP)
    assert "TOTALLY DIFFERENT TITLE" not in page
    assert f"<title>{facts.league.name} — {facts.league.season} Draft Recap</title>" in page
    assert f'<h1 class="nameplate">{facts.league.name}</h1>' in page


def test_llm_first_line_heading_is_not_eaten_as_the_title() -> None:
    facts = _facts()
    llm = "## The Lead\n\nOpening paragraph.\n\n## Team Grades\n\nGrades paragraph."
    page = render_web(facts, llm_text=llm, output_id="x", generated_at=_STAMP)
    # title still league + season
    assert f"<title>{facts.league.name} — {facts.league.season} Draft Recap</title>" in page
    # no section lost, and "The Lead" is a heading not the page title
    assert "<h2>The Lead</h2>" in page
    assert "<h2>Team Grades</h2>" in page
    assert "<p>Opening paragraph.</p>" in page
    assert "<h1>The Lead</h1>" not in page


def test_llm_plain_first_line_becomes_body_prose() -> None:
    facts = _facts()
    llm = "Trench Warfare Draft Recap\n\n## The Lead\n\nBody."
    page = render_web(facts, llm_text=llm, output_id="x", generated_at=_STAMP)
    assert "<p>Trench Warfare Draft Recap</p>" in page
    assert "<h1>Trench Warfare Draft Recap</h1>" not in page


# --------------------------------------------------------------------------- #
# Empty-data edge cases — never raise, empty-state the block
# --------------------------------------------------------------------------- #


def test_empty_pick_list_still_renders_a_page() -> None:
    facts = _facts().model_copy(update={"picks": []})
    page = render_web(facts, recap=_recap(_facts()), output_id="x", generated_at=_STAMP)
    assert page.startswith("<!doctype html>")
    assert "No picks landed on the board." in page
    assert "No picks to place on a timeline." in page
    for tag in ("section", "figure"):
        assert page.count(f"<{tag}") == page.count(f"</{tag}>"), tag
    assert page.count("<html") == page.count("</html>") == 1


def test_empty_pick_count_rank_empty_states_that_block() -> None:
    facts = _facts()
    facts = facts.model_copy(
        update={
            "draft_summary": facts.draft_summary.model_copy(
                update={"pick_count_rank": []}
            )
        }
    )
    page = render_web(facts, recap=_recap(_facts()), output_id="x", generated_at=_STAMP)
    assert "No pick-count data on the board." in page


def test_pick_with_delta_none_draws_a_cell_and_no_marker() -> None:
    facts = _facts()
    # pick 5 in the fixture is flags == ["no_consensus"] -> delta is None
    no_consensus = next(p for p in facts.picks if p.flags == ["no_consensus"])
    assert no_consensus.delta is None
    page = _page(facts=facts)
    grid = _svgs(page)[0]
    minidom.parseString(grid)
    # its board label is still drawn as a cell label
    assert no_consensus.board_label in grid


def test_wide_board_scrolls_inside_its_own_container() -> None:
    page = _page()
    assert ".figwrap {" in page and "overflow-x: auto" in page
    assert "html, body { max-width: 100%; overflow-x: hidden; }" in page


# --------------------------------------------------------------------------- #
# style.py — stdlib only, no credentialed/SDK import, importable without a cycle
# --------------------------------------------------------------------------- #


def test_style_module_imports_only_the_standard_library() -> None:
    tree = ast.parse((REPO_ROOT / "commishdesk" / "render" / "style.py").read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    roots.discard("__future__")
    assert roots <= sys.stdlib_module_names, roots


def test_style_and_web_import_with_no_cycle_and_no_sdk() -> None:
    prog = (
        "import sys;"
        "import commishdesk.render.style as s;"
        "import commishdesk.render.web as w;"
        "from commishdesk.render import render_web;"
        "assert w.render_web is render_web;"
        "bad = {'anthropic', 'google.genai', 'httpx'} & sys.modules.keys();"
        "assert not bad, sorted(bad);"
        # AD-1 fence: render/ reads the Facts JSON + narrator output, nothing upstream
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


# --------------------------------------------------------------------------- #
# Snake vs linear board order
# --------------------------------------------------------------------------- #


def _pick(pick_no: int, rnd: int, slot: int) -> PickRow:
    return PickRow(
        pick_no=pick_no,
        round=rnd,
        slot=slot,
        board_label=f"{rnd}.{slot:02d}",
        roster_id=str(slot),
        manager=f"m{slot}",
        player=PlayerRef(sleeper_id=str(pick_no), name=f"Player {pick_no}", position="RB"),
        consensus_slot=pick_no,
        consensus_label=f"{rnd}.{slot:02d}",
        delta=0,
        flags=[],
    )


@pytest.mark.parametrize(
    "rnd, slot, snake, expected_seat",
    [
        (1, 1, False, 1),
        (1, 4, False, 4),
        (2, 1, False, 1),  # linear: even round keeps slot order
        (2, 4, False, 4),
        (1, 1, True, 1),  # snake: odd round unchanged
        (1, 4, True, 4),
        (2, 1, True, 4),  # snake: even round mirrors (team_count=4)
        (2, 4, True, 1),
        (3, 2, True, 2),  # snake: odd round unchanged again
    ],
)
def test_seat_linear_and_snake_odd_even(rnd: int, slot: int, snake: bool, expected_seat: int) -> None:
    assert web_mod._seat(_pick(1, rnd, slot), 4, snake) == expected_seat


def _snake_facts() -> DraftRecapFacts:
    """The demo fixture with every even-round pick's ``pick_no`` mirrored relative
    to ``slot`` — a valid snake ordering of the same pick set."""
    facts = _facts()
    tc = facts.league.format.team_count
    picks = []
    for p in facts.picks:
        if p.round % 2 == 0:
            mirrored = (p.round - 1) * tc + (tc - p.slot + 1)
            picks.append(p.model_copy(update={"pick_no": mirrored}))
        else:
            picks.append(p)
    return facts.model_copy(update={"picks": picks})


def test_snake_board_is_detected_and_even_rounds_are_mirrored() -> None:
    facts = _snake_facts()
    picks = sorted(facts.picks, key=lambda p: p.pick_no)
    tc = facts.league.format.team_count
    assert web_mod._is_snake(picks, tc) is True

    even = next(p for p in facts.picks if p.round == 2 and p.slot == 1)
    assert web_mod._seat(even, tc, True) == tc  # slot 1 in round 2 -> last column
    grid = _svgs(_page(facts=facts))[0]
    minidom.parseString(grid)
    expected_x = web_mod._G_GUT + web_mod._G_PAD + (tc - 1) * web_mod._G_CW
    assert f'<rect class="grid-cell" x="{expected_x + 2}"' in grid


def test_grid_never_truncates_when_draft_rounds_is_too_small() -> None:
    facts = _facts()
    max_round = max(p.round for p in facts.picks)
    facts = facts.model_copy(update={"draft": facts.draft.model_copy(update={"rounds": 3})})
    assert max_round > 3
    grid = _svgs(_page(facts=facts))[0]
    for pick in facts.picks:
        assert pick.board_label in grid, pick.board_label


# --------------------------------------------------------------------------- #
# Figure content (targeted assertions, not a golden)
# --------------------------------------------------------------------------- #


def test_pick_count_bar_has_one_widest_bar_per_row() -> None:
    facts = _facts()
    rank = facts.draft_summary.pick_count_rank
    bar = _svgs(_page(facts=facts))[1]
    widths = [float(w) for w in re.findall(r'<rect class="bar"[^>]*width="([\d.]+)"', bar)]
    assert len(widths) == len(rank)
    assert widths[0] == max(widths)  # rank is pre-sorted, leader's bar is widest


def test_grid_has_one_cell_per_pick_and_one_marker_per_nonzero_delta() -> None:
    facts = _facts()
    page = _page(facts=facts)
    grid = _svgs(page)[0]
    assert grid.count('class="grid-cell"') == len(facts.picks)
    nonzero = sum(1 for p in facts.picks if p.delta not in (None, 0))
    assert grid.count('class="grid-mark"') == nonzero


def test_timeline_has_one_circle_per_skill_position_pick() -> None:
    facts = _facts()
    timeline = _svgs(_page(facts=facts))[2]
    skill = sum(1 for p in facts.picks if (p.player.position or "").upper() in ("QB", "RB", "WR", "TE"))
    assert timeline.count("<circle") == skill


def test_timeline_note_does_not_overclaim_every_pick() -> None:
    page = _page()
    note = re.search(
        r"When each position went.*?<p class=\"fig-note\">([^<]+)</p>", page, re.DOTALL
    )
    assert note is not None
    assert "Every pick" not in note.group(1)
    assert "quarterback, running back, receiver and tight end" in note.group(1)


# --------------------------------------------------------------------------- #
# _surname particles + suffixes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Ashton Jeanty", "Jeanty"),
        ("Amon-Ra St. Brown", "St. Brown"),
        ("Marvin Harrison Jr.", "Harrison Jr."),
        ("Kenneth Walker III", "Walker III"),
        ("Puka Nacua", "Nacua"),
        ("De'Von Achane", "Achane"),
        ("", ""),
    ],
)
def test_surname(name: str, expected: str) -> None:
    assert web_mod._surname(name) == expected


# --------------------------------------------------------------------------- #
# Formatters
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "n, expected",
    [(36, "+36"), (-57, "−57"), (0, "0"), (1, "+1"), (-1, "−1")],
)
def test_fmt_signed(n: int, expected: str) -> None:
    assert style_mod.fmt_signed(n) == expected
    assert "-" not in style_mod.fmt_signed(n)  # hyphen-minus never leaks


@pytest.mark.parametrize(
    "n, expected",
    [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (11, "11th"), (12, "12th"), (23, "23rd")],
)
def test_ordinal(n: int, expected: str) -> None:
    assert style_mod.ordinal(n) == expected


def test_pct() -> None:
    assert style_mod.pct(3, 4) == "75%"
    assert style_mod.pct(1, 3) == "33%"
    assert style_mod.pct(5, 0) == "0%"


def test_position_helpers() -> None:
    assert style_mod.position_label("qb") == "QB"
    assert style_mod.position_label("UNK") == "—"
    assert style_mod.position_label(None) == "—"
    assert style_mod.position_plural("WR") == "wide receivers"
    assert style_mod.position_plural("XYZ") == "xyzs"
