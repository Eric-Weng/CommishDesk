"""Story 5.14c — ``render_weekly_email``, the weekly Issue as a reduced web-form
email.

The Email v2 board: a dark masthead over a rounded paper sheet, four tinted stat
tiles, a lead card with a cell-width bench bar plus 2x2 award cards, tinted
winner rows in results, BYE/BUBBLE chips on the standings table, a model-score
power list, a diverging luck index built from table cells, tinted next-week
cards, and short transaction cards. Also the email-safe construct ban, the
masthead's dark-inversion contrast, the ``text/plain`` part carrying every
section, the AA-safe text roles, escaped hostile names, and the reissue stamps.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from commishdesk.facts.schema import WeeklyFacts
from commishdesk.narrate.weekly_template import WeeklyIssue, WeeklySection, render_weekly_issue
from commishdesk.render import EmailParts, render_weekly_email
from commishdesk.render import weekly_email as we
from commishdesk.render._body import _esc
from tests.conftest import REPO_ROOT
from tests.test_render_email import _FORBIDDEN
from tests.test_render_weekly_web import _contrast

FACTS_DIR = REPO_ROOT / "tests" / "fixtures" / "facts"
_STAMP = "2026-09-07T00:00:00Z"

#: Section labels in the required order (the web page's order).
_LABELS = (
    "The lead",
    "Around the league",
    "Standings",
    "Power rankings",
    "The luck index",
    "Next week",
    "The transaction desk",
)

#: The opening of a section's header row, and of the footer row — the delimiters
#: of one section's HTML.
_SECTION_ROW = '<tr><td class="px" style="padding:44px'
_FOOTER_ROW = '<tr><td class="px" style="padding:24px 28px 30px 28px;">'


def _raw(published: bool = False) -> dict[str, Any]:
    name = "expected-weekly-facts-week10-published.json" if published else "expected-weekly-facts-week10.json"
    return json.loads((FACTS_DIR / name).read_text(encoding="utf-8"))


def _week01_raw() -> dict[str, Any]:
    return json.loads(
        (FACTS_DIR / "expected-weekly-facts-week01.json").read_text(encoding="utf-8")
    )


def _parts(raw: dict[str, Any], issue: WeeklyIssue | None = None) -> EmailParts:
    facts = WeeklyFacts.model_validate(raw)
    return render_weekly_email(facts, issue or render_weekly_issue(facts.narration), generated_at=_STAMP)


def _label_html(label: str) -> str:
    # Story 6-11: the eyebrow cell's font rules moved to a head class (its inline style is
    # now just the text colour, followed by a generated ``tN`` class), so the stable
    # handle on a section's label cell is its text
    return f">{label}</td>"


def _section(html: str, label: str) -> str:
    """The HTML of one section: from its label to the next section header (or the
    footer, for the final section)."""
    start = html.index(_label_html(label))
    stops = [
        idx
        for idx in (html.find(_SECTION_ROW, start + 1), html.find(_FOOTER_ROW, start + 1))
        if idx != -1
    ]
    end = min(stops) if stops else len(html)
    return html[start:end]


def _masthead_html(html: str) -> str:
    """The masthead row's HTML content (up to the opening of the tiles row)."""
    after = html.split('class="container"', 1)[1]
    _, _, block = after.partition("<tr>")
    row, _, _ = block.partition("</tr>")
    return row


def _invert(hex_colour: str) -> str:
    value = int(hex_colour.lstrip("#"), 16)
    return f"#{0xFFFFFF - value:06X}"


# --------------------------------------------------------------------------- #
# Email-safe HTML
# --------------------------------------------------------------------------- #


def test_week10_is_a_640px_table_layout_with_no_unsafe_construct() -> None:
    html = _parts(_raw()).html
    assert html.startswith("<!DOCTYPE html>") and html.endswith("\n") and "\r" not in html
    assert 'class="container" width="640"' in html and "max-width:640px" in html
    for token in (*_FORBIDDEN, "<link", "flex", "grid"):
        assert token not in html, token
    for tag in ("table", "tr", "td", "div", "span", "p", "b"):
        assert len(re.findall(rf"<{tag}[ >]", html)) == html.count(f"</{tag}>"), tag


def _head_style(html: str) -> str:
    return html.rsplit("<style>", 1)[1].split("</style>", 1)[0]


def test_the_head_style_block_carries_the_font_classes_and_the_dark_swap_only() -> None:
    """Story 6-11 (relaxes 5-14c "inline is truth"): repeated font / spacing rules live
    in generated head classes; the layout tweaks and the dark-mode value swap stay;
    the dark block is still colour-only."""
    html = _parts(_raw()).html
    head_style = _head_style(html)
    assert "@media only screen and (max-width:660px)" in head_style
    assert ".container{width:100%!important;}" in head_style
    assert ".px{padding-left:20px!important;padding-right:20px!important;}" in head_style
    # the fonts moved into head classes (webfont name, then Arial / the generic)
    base, dark = head_style.split("@media (prefers-color-scheme: dark)", 1)
    assert "font-family:IBM Plex Mono,monospace" in base
    assert "font-family:DM Sans,Arial,sans-serif" in base
    assert "font-family:Bricolage Grotesque,Arial,sans-serif" in base
    assert "Helvetica" not in html and "Courier" not in html
    # the dark block still swaps literal colours only
    assert '[style*=";color:' in dark and '[style*="background-color:' in dark
    assert '[style*="solid ' in dark
    assert '[style*="color:' not in dark
    for decl in ("font-family:", "line-height:", "margin:", "padding:"):
        assert decl not in dark, decl


def test_hoisted_classes_are_defined_used_and_leave_a_minimal_inline_fallback() -> None:
    """Every generated ``tN`` class used in the body is defined in the head and every
    defined one is used; a cell that took a class keeps its text colour, its fill, its
    borders and padding, and its widths inline for clients that drop head styles."""
    html = _parts(_raw()).html
    head = _head_style(html)
    body = html.split("</head>", 1)[1]
    defined = set(re.findall(r"\.(t\d+)\{", head))
    used = {c for attr in re.findall(r'class="([^"]*)"', body) for c in attr.split() if re.fullmatch(r"t\d+", c)}
    assert defined and defined == used
    # at most a handful of one-off cells keep a font rule inline (a group used twice is hoisted)
    inline_fonts = re.findall(r'style="[^"]*font-family:[^"]*"', body)
    assert len(inline_fonts) <= 16
    # the fallback: a title cell and a team-name cell still carry colour and padding inline
    team_cell = r'<td [^>]*style="[^"]*padding:12px 6px;[^"]*color:#1F2140;[^"]*" class="t\d+">Two Minute Drill</td>'
    assert re.search(team_cell, body)
    # and no cell lost its fill: bgcolor attrs and background-color declarations still pair up
    assert body.count("bgcolor=") >= 100 and body.count("background-color:") >= body.count("bgcolor=")


def test_sections_render_in_the_required_order() -> None:
    html = _parts(_raw()).html
    positions = [html.index(_label_html(label)) for label in _LABELS]
    assert positions == sorted(positions)
    assert html.index(">Trench Warfare</div>") < positions[0]


def test_the_masthead_has_a_nameplate_week_line_and_stat_tiles() -> None:
    html = _parts(_raw()).html
    masthead = html.split('class="container"', 1)[1].split(_label_html("The lead"), 1)[0]
    assert "Trench Warfare" in masthead and "Week 10 · Season 2025 · 12 teams" in masthead
    for tile in ("High", "Closest", "Blowouts", "Pts scored"):
        assert f">{tile}<" in masthead


def test_the_masthead_survives_dark_client_inversion() -> None:
    html = _parts(_raw()).html
    assert '<meta name="color-scheme" content="light">' in html
    assert '<meta name="supported-color-schemes" content="light">' in html
    masthead = _masthead_html(html)
    # the masthead paints an explicit bgcolor + inline colour, so an inverting
    # client always has a concrete pair to keep
    assert f'bgcolor="{we.MAST}"' in masthead
    assert f"color:{we.ON_MAST}" in masthead
    assert f"color:{we.MAST_SUB}" in masthead
    assert _contrast(we.ON_MAST, we.MAST) >= 4.5
    assert _contrast(_invert(we.ON_MAST), _invert(we.MAST)) >= 4.5
    assert _contrast(we.MAST_SUB, we.MAST) >= 4.5


def test_text_colours_are_the_aa_roles_and_ink3_carries_nothing() -> None:
    html = _parts(_raw(published=True)).html
    assert we.GOOD_TEXT == "#157F55" and we.BAD_TEXT == "#C93338"
    assert "#9A9CB2" not in html  # ink-3 is never used in the light email
    for colour in (we.INK, we.INK2, we.GOOD_TEXT, we.BAD_TEXT, we.EMPH_TEXT):
        assert _contrast(colour, we.CARD) >= 4.5, colour


def test_the_render_is_deterministic_and_generated_at_is_the_only_time() -> None:
    a = _parts(_raw(published=True))
    assert a == _parts(_raw(published=True))
    facts = WeeklyFacts.model_validate(_raw())
    other = render_weekly_email(facts, render_weekly_issue(facts.narration), generated_at="OTHER")
    assert _parts(_raw()).html.replace(_STAMP, "OTHER") == other.html


# --------------------------------------------------------------------------- #
# text/plain
# --------------------------------------------------------------------------- #


def test_text_part_carries_every_html_section_heading_prose_and_data() -> None:
    raw = _raw()
    parts = _parts(raw)
    assert parts.text.strip() and parts.text.endswith("\n") and "\r" not in parts.text
    for label in _LABELS:
        assert _label_html(label) in parts.html
        assert f"\n\n{label.upper()}\n" in parts.text, label
    facts = WeeklyFacts.model_validate(raw)
    issue = render_weekly_issue(facts.narration)
    # Story 6-11: the narrated prose reaches the text part only where the email keeps
    # it: the whole lead, the trend lines after the per-game sentences, and nothing of
    # the per-row / per-card sentences under the tables
    by_heading = {section.heading: section.blocks for section in issue.sections}
    games = len(raw["matchups"]["this_week"])
    kept = [*by_heading["The Lead"], *by_heading["Around the League"][games:]]
    dropped = [
        *by_heading["Around the League"][:games],
        *by_heading["Standings and the Playoff Picture"],
        *by_heading["Power Rankings"],
        *by_heading["The Luck Index"],
        *by_heading["Next Week"],
        *by_heading["The Transaction Desk"],
    ]
    assert len(kept) > 5 and len(dropped) > 40
    for block in kept:
        assert block in parts.text and _esc(block) in parts.html, block[:40]
    for block in dropped:
        assert block not in parts.text and _esc(block) not in parts.html, block[:40]
    for data in (
        "started 119.97 · left on bench 108.86 · opponent 126.03",
        "--- playoff line ---",
        "Coach of the week: Two Minute Drill, 99.2%",
        "Yardage Yaks 126.03 over Kneel-Down Koalas 119.97",
        "Flea Flicker Union · Waiver · Add Jalen Nailor WR; Drop Tyrod Taylor QB · FAAB $25",
        "1 move · 1 trade",
    ):
        assert data in parts.text, data


# --------------------------------------------------------------------------- #
# I/O matrix
# --------------------------------------------------------------------------- #


def test_week10_lead_is_the_bench_bar_as_a_cell_row() -> None:
    lead = _section(_parts(_raw()).html, "The lead")
    assert "Kneel-Down Koalas lost by 6.06 with 108.86 on the bench" in lead
    # the bar's cells: the started fill, a hued gap, a fixed opponent marker
    assert f'bgcolor="{we.BAR}"' in lead
    assert f'bgcolor="{we.BARLO}"' in lead
    assert f'bgcolor="{we.BAD}"' in lead
    # the board's three labelled figures replace the duplicate caption line, which
    # survives in text/plain only
    assert "started 119.97" not in lead and "left on bench" not in lead
    for label, figure in (("Started", "119.97"), ("On the bench", "108.86"), ("Opponent", "126.03")):
        assert f">{label}</div>" in lead and f">{figure}</div>" in lead
    for award in ("Coach of the week", "Player of the week", "Bust of the week", "Goose egg club"):
        assert award in lead


@pytest.mark.parametrize(
    ("kind", "numbers"),
    [
        ("biggest_blowout", "Flea Flicker Union 229.96 · Nickel Newts 91.77 · margin 138.19"),
        ("closest_game", "Yardage Yaks 126.03 · Kneel-Down Koalas 119.97 · margin 6.06"),
        ("week_high_score", "Backpedal Buffalo 247.20 · league average 159.24"),
    ],
)
def test_other_lead_kinds_are_a_text_line_with_the_key_numbers(kind: str, numbers: str) -> None:
    raw = _raw()
    raw["lead_candidates"] = [dict(c, rank=1) for c in raw["lead_candidates"] if c["kind"] == kind]
    parts = _parts(raw)
    lead = _section(parts.html, "The lead")
    assert numbers in lead and numbers in parts.text
    # no bench bar for these kinds: the opponent marker never appears before the
    # award cards
    before_awards = lead.split("Coach of the week", 1)[0]
    assert f'bgcolor="{we.BAD}"' not in before_awards


def test_published_overlay_orders_power_by_published_rank_with_the_cited_reason() -> None:
    raw = _raw(published=True)
    parts = _parts(raw)
    power = _section(parts.html, "Power rankings")
    assert power.index("Nickel Newts") < power.index("Chip-Block Chinchillas")
    assert "▲ Nudged up · model #8" in power and "▼ Nudged down · model #7" in power
    reason = next(r["nudge_justification"] for r in raw["narration"]["power"] if r["team"] == "Nickel Newts")
    assert _esc(reason) in power
    assert f"▲ Nudged up · model #8 — {reason}" in parts.text


def test_a_nudge_without_a_reason_shows_the_text_line_alone() -> None:
    raw = _raw(published=True)
    for row in raw["narration"]["power"]:
        row["nudge_justification"] = None
    parts = _parts(raw)
    power = _section(parts.html, "Power rankings")
    assert "▲ Nudged up · model #8" in power
    assert "▲ Nudged up · model #8\n" in parts.text


def test_stood_down_sections_leave_no_heading_in_either_part() -> None:
    raw = _raw()
    for team in raw["teams"]:
        team["season"]["luck"] = None
    raw["transactions"]["this_week"] = []
    raw["transactions"]["recent_trades"] = []
    raw["matchups"]["next_week"] = []
    parts = _parts(raw)
    for label in ("The luck index", "Next week", "The transaction desk"):
        assert _label_html(label) not in parts.html
        assert label.upper() not in parts.text
    assert "█" not in parts.html and "On the line in every game" not in parts.text
    for label in ("The lead", "Around the league", "Standings", "Power rankings"):
        assert _label_html(label) in parts.html


def test_week01_cold_start_omits_power_luck_transactions_and_playoff_picture() -> None:
    """Story 5.16: at Week 1 the email renders only the four cold-start sections
    (lead, results, standings by points, next week) — no power rankings, no luck
    index, no transaction desk, and no playoff-picture rows."""
    parts = _parts(_week01_raw())

    for label in (
        "Power rankings",
        "The luck index",
        "The transaction desk",
        "Standings and the Playoff Picture",
    ):
        assert _label_html(label) not in parts.html, label
        assert label.upper() not in parts.text, label

    assert _label_html("Standings") in parts.html
    assert "Standings by points" in parts.text
    assert "THE LEAD" in parts.text
    assert "NEXT WEEK" in parts.text


def test_shared_stakes_appear_once_and_a_differing_stake_stays_on_its_card() -> None:
    raw = _raw()
    raw["matchups"]["next_week"][0]["stakes"].append("elimination")
    parts = _parts(raw)
    for part in (parts.html, parts.text):
        assert part.count("Bye seed") == 1
        assert part.count("Wildcard race") == 1
        assert part.count("Division race") == 1
    assert "On the line in every game: Bye seed, Wildcard race, Division race" in parts.text
    # a stake that differs per card stays on that card, in both parts
    assert parts.html.count("Elimination") == 1
    assert "Flea Flicker Union (7-3-0) vs Bubble-Screen Bobcats (2-8-0) [Elimination]" in parts.text


def test_the_stat_tiles_carry_the_week10_figures() -> None:
    html = _parts(_raw()).html
    masthead = html.split('class="container"', 1)[1].split(_label_html("The lead"), 1)[0]
    tiles = dict(re.findall(r">(High|Closest|Blowouts|Pts scored)</div><div[^>]*>([^<]*)<", masthead))
    assert tiles == {"High": "247.20", "Closest": "6.06", "Blowouts": "3", "Pts scored": "1,911"}


def test_a_single_blowout_reads_wasnt_close() -> None:
    assert we._results_title([type("G", (), {"is_blowout": True})()]) == "One game, one that wasn’t close"


def test_luck_bars_sit_on_the_side_and_colour_of_their_sign() -> None:
    section = _section(_parts(_raw()).html, "The luck index")
    rows = section.split('<td class="lname')[1:]
    seen = set()
    for row in rows:
        sign = re.search(r">([−+])\d+\.\d</td>", row).group(1)  # type: ignore[union-attr]
        halves = row.split('<td width="50%"')
        fill = we.GOOD if sign == "+" else we.BAD
        other = we.BAD if sign == "+" else we.GOOD
        assert f'bgcolor="{fill}"' in row and f'bgcolor="{other}"' not in row
        bar_half = 2 if sign == "+" else 1
        assert f'bgcolor="{fill}"' in halves[bar_half]
        seen.add(sign)
    assert seen == {"+", "−"}


def _with_start_week(start: int | None) -> dict[str, Any]:
    raw = _raw()  # week 10
    if start is None:
        raw["league"]["format"]["playoff"] = None
    else:
        raw["league"]["format"]["playoff"]["start_week"] = start
    return raw


_BRACKET = "The bracket takes 6 teams."
_PER_ROW = re.compile(r"points for, model rank")


def test_standings_chips_subtitle_and_bracket_summary_show_only_inside_the_playoff_window() -> None:
    """Story 6-11: week 10 with the playoffs at week 15 is 5 weeks out, so the table
    stands alone (no chips, the plain subtitle, no bracket summary); from 4 weeks out,
    and through the playoff weeks, the chips, the subtitle and the summary come back.
    The playoff line stays either way."""
    outside = _parts(_raw())
    assert we.PLAYOFF_WINDOW_WEEKS == 4
    section = _section(outside.html, "Standings")
    assert ">Bye<" not in section and ">Bubble<" not in section
    assert ">The table</div>" in section and "make it" not in section
    assert _BRACKET not in outside.html and _BRACKET not in outside.text
    assert "[BYE]" not in outside.text and "[BUBBLE]" not in outside.text
    assert "Playoff line" in section and "--- playoff line ---" in outside.text
    for start in (14, 12, 10, 8):  # 4 weeks out, closer, the first playoff week, past it
        inside = _parts(_with_start_week(start))
        section = _section(inside.html, "Standings")
        assert len(re.findall(r">Bye<", section)) == 2 and len(re.findall(r">Bubble<", section)) == 4, start
        assert ">Six make it, two get byes</div>" in section, start
        assert _BRACKET in section and _BRACKET in inside.text, start
        assert inside.text.count("[BYE]") == 2 and inside.text.count("[BUBBLE]") == 4, start
        assert "Playoff line" in section, start
    # a league that declares no playoff shape has no window to be inside
    none = _parts(_with_start_week(None))
    assert ">Bye<" not in none.html and _BRACKET not in none.text
    # the per-row sentences are gone in and out of the window
    for parts in (outside, _parts(_with_start_week(14))):
        assert not _PER_ROW.search(parts.html) and not _PER_ROW.search(parts.text)


def test_luck_index_uses_block_bars_in_text_and_table_cells_in_html() -> None:
    raw = _raw()
    parts = _parts(raw)
    # text/plain carries the ``█`` bars
    bars = re.findall(r"█+", parts.text)
    assert len(bars) == len(raw["teams"])
    assert max(len(bar) for bar in bars) == 11 and min(len(bar) for bar in bars) >= 1
    # the html carries table cells instead
    assert "█" not in parts.html
    assert we.luck_bar(0.0, 2.5) == "█" and we.luck_bar(0.01, 2.5) == "█"
    assert we.luck_bar(-2.5, 2.5) == "█" * 11 and we.luck_bar(9.0, 0.0) == "█"


def test_transactions_render_two_cards() -> None:
    desk = _section(_parts(_raw()).html, "The transaction desk")
    assert "This week" in desk
    assert "Latest trade · week 9 · 1 week ago" in desk
    assert "Add" in desk and "Drop" in desk
    assert "receives" in desk


def test_a_trade_side_receiving_nothing_reads_the_same_in_both_parts() -> None:
    raw = _raw()
    side = raw["transactions"]["recent_trades"][0]["sides"][0]
    side.update(players=[], picks=[], faab=0)
    parts = _parts(raw)
    assert "receives nothing listed" in _section(parts.html, "The transaction desk")
    assert "receives nothing listed" in parts.text


def test_with_no_hooked_lead_the_headline_is_the_narrated_lead() -> None:
    raw = _raw()
    raw["lead_candidates"] = []
    facts = WeeklyFacts.model_validate(raw)
    issue = render_weekly_issue(facts.narration)
    first = next(s.blocks for s in issue.sections if s.heading == "The Lead")[0]
    parts = render_weekly_email(facts, issue, generated_at=_STAMP)
    assert _esc(first) in _section(parts.html, "The lead")
    assert first in parts.text


def test_a_hostile_name_is_escaped_in_the_html() -> None:
    hostile = '<script>x</script>"><svg onload=1>'
    raw = _raw(published=True)
    for team in raw["teams"]:
        if team["roster_id"] in ("7", "5"):
            team["team_name"] = hostile
    for row in raw["narration"]["power"]:
        if row["roster_id"] == "5":
            row["nudge_justification"] = hostile
    parts = _parts(raw)
    assert hostile not in parts.html and "<script" not in parts.html and "<svg" not in parts.html
    assert "&lt;script&gt;x&lt;/script&gt;&quot;&gt;&lt;svg onload=1&gt;" in parts.html
    assert hostile in parts.text  # text/plain is literal


def test_correction_and_unverified_render_above_the_lead() -> None:
    raw = _raw()
    facts = WeeklyFacts.model_validate(raw)
    issue = render_weekly_issue(facts.narration)
    issue = issue.model_copy(
        update={
            "dateline": f"UNVERIFIED — {issue.dateline}",
            "sections": [WeeklySection(heading="Correction", blocks=["Correction — fixed a score"]), *issue.sections],
        }
    )
    parts = _parts(raw, issue)
    assert parts.html.index("Correction — fixed a score") < parts.html.index(_label_html("The lead"))
    assert "UNVERIFIED — " in parts.html
    assert parts.text.index("CORRECTION\nCorrection — fixed a score") < parts.text.index("THE LEAD")


# --------------------------------------------------------------------------- #
# Design pass: gutters, game cards, text/plain identity, the dark palette
# --------------------------------------------------------------------------- #

_TOP_ROW = re.compile(r'<tr><td class="px" style="padding:([^;"]+);')


def _lr(padding: str) -> tuple[str, str]:
    """Left and right padding of a 1-4 value ``padding`` shorthand."""
    parts = padding.split()
    if len(parts) == 1:
        return parts[0], parts[0]
    if len(parts) == 2:
        return parts[1], parts[1]
    return parts[3] if len(parts) == 4 else parts[1], parts[1]


def test_every_top_level_row_keeps_the_28px_gutter() -> None:
    html = _parts(_raw(published=True)).html
    rows = _TOP_ROW.findall(html)
    # the render once lost the gutter after the lead: no row may
    assert len(rows) >= 17  # fewer than before 6-11: the per-row prose rows are gone
    for padding in rows:
        assert _lr(padding) == ("28px", "28px"), padding
    # each section heading (eyebrow dash + title) sits inside a gutter row
    for label in _LABELS:
        before = html[: html.index(_label_html(label))]
        assert _TOP_ROW.findall(before)[-1] == "44px 28px 18px 28px", label


def test_prose_paragraphs_are_padded_ink2_body_text_inside_the_gutter() -> None:
    html = _parts(_raw()).html
    para = "Bubble-Screen Bobcats has lost six straight."  # a kept trend line
    at = html.index(para)
    row = _TOP_ROW.match(html, html.rfind('<tr><td class="px"', 0, at))
    assert row is not None and _lr(row.group(1)) == ("28px", "28px")
    assert f"color:{we.INK2};" in html[html.rfind("<p ", 0, at) : at]


_CARD_OPEN = (
    f'<tr><td bgcolor="{we.CARD}" style="background-color:{we.CARD};border:1px solid {we.LINE};'
)
_SPACER = '<tr><td height="12" style="height:12px;font-size:0;line-height:12px;">&nbsp;</td></tr>'


def test_game_cards_are_bordered_rounded_cards_with_the_chip_inside() -> None:
    html = _parts(_raw()).html
    head = _head_style(html)
    assert "border-radius:12px;" in head
    results = _section(html, "Around the league")
    cards = results.split(_CARD_OPEN)[1:]
    assert len(cards) == 6
    tagged = 0
    for card in cards:
        body = card.split(_SPACER, 1)[0]  # everything up to the 12px gap is the card
        assert body.startswith('padding:0;" class="t')  # radius moved to a head class
        assert body.endswith("</td></tr></table></td></tr>")  # the card's own cell closes last
        assert "W</td>" in body and "L</td>" in body
        for word in ("Blowout", "Week high", "Nail-biter"):
            if f">{word}</td>" in body:
                tagged += 1
                # under the loser row, padded left to clear the monogram
                assert body.index("L</td>") < body.index(f">{word}</td>")
                assert 'padding-left:38px;"' in body and "margin-top:10px" in head
    assert tagged == 4  # two blowouts, week high, nail-biter
    assert results.count(_SPACER) == 6  # 12px between cards, nothing else between them
    # the cards sit in a table inside the section wrapper's gutter
    assert '<tr><td class="px" style="padding:0 28px 0;"><table' in results


def test_text_part_keeps_the_game_of_the_week_tag_and_the_caption() -> None:
    text = _parts(_raw()).text
    assert "Backpedal Buffalo (7-3-0) vs No-Huddle Narwhals (6-4-0) [Game of the week]\n" in text
    assert text.count("[Game of the week]") == 1
    assert "started 119.97 · left on bench 108.86 · opponent 126.03" in text
    assert "    On bye: Jonathan Taylor (RB IND), Josh Downs (WR IND)" in text


_DARK_RULE = re.compile(
    r"^(?P<sel>\[[^{]*\])\{(?P<prop>[a-z-]+):(?P<val>#[0-9A-F]{6})!important;\}$", re.M
)


def _dark_rules(html: str) -> dict[tuple[str, str], tuple[str, str]]:
    """{(property, light literal): (dark value, selector)} from the dark block."""
    head = html.rsplit("<style>", 1)[1].split("</style>", 1)[0]
    block = head.split("@media (prefers-color-scheme: dark){", 1)[1]
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for match in _DARK_RULE.finditer(block):
        light = re.search(r"#[0-9A-F]{6}", match["sel"])
        assert light is not None
        out[(match["prop"], light.group(0))] = (match["val"], match["sel"])
    return out


def test_dark_block_maps_text_fill_and_border_separately_with_the_board_palette() -> None:
    rules = _dark_rules(_parts(_raw()).html)
    # fills
    assert rules[("background-color", "#FFFFFF")][0] == "#1D1F38"  # card
    assert rules[("background-color", "#FBF7EF")][0] == "#141527"  # paper
    assert rules[("background-color", "#1F2140")][0] == "#0E0F1E"  # masthead
    assert rules[("background-color", "#F0EADB")][0] == "#24264A"
    # text: the same light hex plays a different role, so it gets a different dark value
    assert rules[("color", "#1F2140")][0] == "#F0EEF8"  # ink text (the masthead fill is #0E0F1E)
    assert rules[("color", "#FFFFFF")][0] == "#F0EEF8"  # white on the masthead
    assert rules[("color", "#5A5D7A")][0] == "#B2B4CE"
    assert rules[("color", "#3E4CC2")][0] == "#A9B2FF"
    assert rules[("color", "#157F55")][0] == rules[("color", "#0E6B45")][0] == "#4CC38D"
    assert rules[("color", "#C93338")][0] == rules[("color", "#A9282D")][0] == "#FF7A7F"
    # borders
    assert rules[("border-color", "#ECE4D4")][0] == "#2C2E4C"
    # a text rule never matches a fill declaration, a fill rule only matches fills
    for (prop, _light), (_dark, selector) in rules.items():
        if prop == "color" and 'background-color:' in selector:
            # only the game-of-the-week header override names a fill, and it names it as context
            assert selector.startswith(f'[style*="background-color:{we.NOTABLE};color:')
        elif prop == "color":
            assert all(
                part.startswith(('[style*=";color:', '[style^="color:'))
                for part in selector.replace("],[", "]|[").split("|")
            ), selector
        if prop == "background-color":
            assert selector.startswith('[style*="background-color:') and "," not in selector
        if prop == "border-color":
            assert "solid " in selector or "dashed " in selector
    # the white card never turns into a light fill, and ink text never into a dark one
    for (prop, _light), (dark, _selector) in rules.items():
        if prop == "background-color":
            assert dark != we.D_INK
    assert rules[("color", we.INK)][0] not in (we.D_CARD, we.D_PAPER, we.D_MAST)


def _lum(hex_colour: str) -> float:
    def chan(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def _ratio(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _text_on_fill_pairs(html: str) -> set[tuple[str, str]]:
    """Every (text, fill) literal pair set in one inline style."""
    pairs = set()
    for style in re.findall(r'style="([^"]*)"', html):
        fill = re.search(r"background-color:(#[0-9A-F]{6})", style)
        text = re.search(r"(?:^|;)color:(#[0-9A-F]{6})", style)
        if fill and text:
            pairs.add((text.group(1), fill.group(1)))
    return pairs


def test_every_text_on_fill_pair_is_aa_in_the_light_and_the_dark_block() -> None:
    html = _parts(_raw(published=True)).html
    rules = _dark_rules(html)
    pairs = _text_on_fill_pairs(html)
    assert len(pairs) >= 8
    for text, fill in pairs:
        assert _ratio(text, fill) >= 4.5, (text, fill)
        dark_text = rules[("color", text)][0]
        if (text, fill) == (we.INK, we.NOTABLE):  # the amber header keeps the board's on-emphasis ink
            dark_text = we.D_PAPER
        assert _ratio(dark_text, rules[("background-color", fill)][0]) >= 4.5, (text, fill)
    # roles whose text sits in a child element of a filled cell (dark)
    fills = (
        we.D_EMPH_WASH_CARD, we.D_GOOD_WASH_CARD, we.D_BAD_WASH_CARD, we.D_NOTABLE_WASH_CARD,
        we.D_EMPH_WASH_PAPER, we.D_GOOD_WASH_PAPER, we.D_BAD_WASH_PAPER, we.D_NOTABLE_WASH_PAPER,
        we.D_WIN_TINT, we.D_MONO_BG, we.D_CARD, we.D_PAPER,
    )
    for fill in fills:
        assert _ratio(we.D_INK, fill) >= 4.5 and _ratio(we.D_INK2, fill) >= 4.5, fill
    for fg, bg in (
        (we.D_EMPH_TEXT, we.D_EMPH_WASH_CARD), (we.D_GOOD_TEXT, we.D_GOOD_WASH_CARD),
        (we.D_BAD_TEXT, we.D_BAD_WASH_CARD), (we.D_GOOD_LAB, we.D_GOOD_WASH_PAPER),
        (we.D_BAD_LAB, we.D_BAD_WASH_PAPER), (we.D_EMPH_TEXT, we.D_EMPH_WASH_PAPER),
        (we.D_GOOD_TEXT, we.D_CARD), (we.D_BAD_TEXT, we.D_CARD), (we.D_EMPH_TEXT, we.D_PAPER),
        (we.D_ON_MAST, we.D_MAST), (we.D_MAST_SUB, we.D_MAST), (we.D_PAPER, we.D_NOTABLE),
    ):
        assert _ratio(fg, bg) >= 4.5, (fg, bg)
    # and the light side, on the washes
    for fg, bg in (
        (we.EMPH_TEXT, we.EMPH_WASH_CARD), (we.GOOD_TEXT, we.GOOD_WASH_CARD),
        (we.BAD_TEXT, we.BAD_WASH_CARD), (we.INK, we.NOTABLE_WASH_PAPER),
        (we.GOOD_LAB, we.GOOD_WASH_PAPER), (we.BAD_LAB, we.BAD_WASH_PAPER),
        (we.EMPH_TEXT, we.EMPH_WASH_PAPER), (we.INK2, we.WIN_TINT), (we.INK, we.NOTABLE),
    ):
        assert _ratio(fg, bg) >= 4.5, (fg, bg)


def test_narrow_client_rules_stack_columns_and_hide_the_standings_bar() -> None:
    html = _parts(_raw()).html
    head = html.rsplit("<style>", 1)[1].split("</style>", 1)[0]
    assert ".stack{display:block!important;width:100%!important;" in head
    assert ".gap,.hide{display:none!important;}" in head
    assert ".fw{width:100%!important;" in head
    assert len(re.findall(r'class="stack(?: t\d+)?"', html)) == 4 + 4  # four tiles, four award cards
    assert 'class="hide"' in html and 'class="fw"' in html


# --------------------------------------------------------------------------- #
# Column structure (results, standings, power, luck, next week) and long names
# --------------------------------------------------------------------------- #

_TD_OPEN = re.compile(r"<td\b([^>]*)>")


def _own_text_cells(section: str, text: str) -> list[str]:
    """Opening-tag attributes of every cell whose own leading text is ``text``."""
    return [
        m.group(1)
        for m in _TD_OPEN.finditer(section)
        if section[m.end() : section.find("<", m.end())] == text
    ]


def _assert_fixed_right(section: str, text: str) -> None:
    hits = _own_text_cells(section, text)
    assert hits, text
    for attrs in hits:
        width = re.search(r'\swidth="(\d+)"', attrs)
        assert width is not None, (text, attrs)
        assert f"width:{width.group(1)}px" in attrs, (text, attrs)
        assert 'align="right"' in attrs, (text, attrs)


def test_results_are_one_card_per_game_with_fixed_score_and_result_columns() -> None:
    raw = _raw()
    results = _section(_parts(raw).html, "Around the league")
    games = raw["matchups"]["this_week"]
    assert results.count(_CARD_OPEN) == len(games)
    for game in games:
        for points in (game["home_points"], game["away_points"]):
            _assert_fixed_right(results, f"{points:.2f}")
    # every side row is monogram | name | score | result: score (70) and W/L (24) fixed and right
    assert len(re.findall(r'width="70" align="right" style="width:70px;', results)) == 2 * len(games)
    assert len(re.findall(r'width="24" align="right" style="width:24px;', results)) == 2 * len(games)
    assert len(re.findall(r'width="34" valign="middle" style="width:34px;', results)) == 2 * len(games)


def test_standings_have_one_row_per_team_with_fixed_right_aligned_numeral_columns() -> None:
    raw = _raw()
    section = _section(_parts(raw).html, "Standings")
    teams = raw["teams"]
    order = [t["team_name"] for t in sorted(teams, key=lambda t: t["season"]["rank"])]
    positions = [section.index(f">{name}</td>") for name in order]
    assert positions == sorted(positions)
    for name in order:
        assert section.count(f">{name}</td>") == 1
    for team in teams:
        rec = team["season"]["record"]
        _assert_fixed_right(section, f"{rec['w']}-{rec['l']}" + (f"-{rec['t']}" if rec["t"] else ""))
        _assert_fixed_right(section, f"{team['season']['points_for']:,.0f}")
    # W-L (44), points for (46) and the rank (30) columns are fixed on every row
    assert len(re.findall(r'width="44" align="right"', section)) == len(teams)
    assert len(re.findall(r'width="46" align="right"', section)) == len(teams)
    assert len(re.findall(r'<td bgcolor="#[0-9A-F]{6}" width="30" style="[^"]*width:30px;', section)) == len(teams)


def test_power_is_one_row_per_team_with_fixed_rank_and_delta_columns_and_no_model_score() -> None:
    """Story 6-11: the ``model 0.87`` figure ("means nothing to readers") is cut from
    the table and the text; rank order and the movement arrows stay."""
    raw = _raw(published=True)
    parts = _parts(raw)
    section = _section(parts.html, "Power rankings")
    n = len(raw["narration"]["power"])
    assert len(re.findall(r'width="34" style="background-color:[^"]*width:34px;', section)) == n
    delta = re.findall(r'<td bgcolor="#[0-9A-F]{6}" (width="44" align="right" style="[^"]*")', section)
    assert len(delta) == n and all("width:44px;" in c for c in delta)
    assert 'width="50"' not in section and "MODEL" not in section
    assert not re.search(r"model \d\.\d\d|>\d\.\d\d<", section)
    assert not re.search(r"model \d\.\d\d", parts.text)
    # the movement arrows and the rank order survive
    assert "▲" in section and "▼" in section
    assert re.search(r"^ ?1\. .* — \d+-\d+(-\d+)?( [▲▼]\d+)?$", parts.text, re.M)
    # nudge notes span the three columns instead of adding a fourth
    assert section.count('colspan="3"') == 2 and 'colspan="4"' not in section


def test_luck_is_one_row_per_team_with_fixed_name_and_value_columns_and_a_split_bar() -> None:
    raw = _raw()
    section = _section(_parts(raw).html, "The luck index")
    n = len(raw["teams"])
    assert len(re.findall(r'<td class="lname t\d+" width="150" style="width:150px;', section)) == n
    value = r'<td width="44" align="right" style="width:44px;[^"]*"(?: class="t\d+")?>[−+]\d+\.\d</td>'
    assert len(re.findall(value, section)) == n
    # each bar is a two-sided track around one centre rule
    assert section.count("border-left:1px solid") == n
    assert section.count('<td width="50%"') == 2 * n
    assert "█" not in section


def test_next_week_is_one_three_column_card_per_game() -> None:
    raw = _raw()
    section = _section(_parts(raw).html, "Next week")
    cards = raw["matchups"]["next_week"]
    assert section.count('<td width="46%" valign="top" style="padding:16px 4px 16px 16px;">') == len(cards)
    right = '<td width="46%" valign="top" align="right" style="padding:16px 16px 16px 4px;">'
    assert section.count(right) == len(cards)
    assert section.count('<td width="8%" align="center" valign="middle"') == len(cards)
    assert section.count(">VS</td>") == len(cards)
    assert section.count("★ Game of the week") == sum(1 for c in cards if c["game_of_week"])


_LONG = 30


def _long_name_raw() -> tuple[dict[str, Any], list[str]]:
    """The week-10 raw dict grown to 16 teams (8 games each week), every team name
    30 characters with spaces so it can wrap."""
    raw = _raw(published=True)
    template = {t["roster_id"]: t for t in raw["teams"]}
    for i, src in enumerate(("1", "2", "3", "4"), start=13):
        clone = json.loads(json.dumps(template[src]))
        clone["roster_id"] = str(i)
        clone["season"]["rank"] = i
        raw["teams"].append(clone)
        raw["standings"]["overall"].append(str(i))
        raw["standings"]["playoff_picture"]["consolation"].append(str(i))
        raw["standings"]["divisions"][str(1 + i % 3)].append(str(i))
        raw["narration"]["power"].append(
            {**raw["narration"]["power"][0], "roster_id": str(i), "model_rank": i,
             "published_rank": None, "nudge_justification": None, "week_delta": 0}
        )
    names = []
    for team in raw["teams"]:
        name = f"Longname Roster {int(team['roster_id']):02d} ".ljust(_LONG, "X")
        team["team_name"] = name
        names.append(name)
    for row in raw["narration"]["power"]:
        row["team"] = next(t["team_name"] for t in raw["teams"] if t["roster_id"] == row["roster_id"])
    for j, (a, b) in enumerate((("13", "14"), ("15", "16")), start=7):
        game = json.loads(json.dumps(raw["matchups"]["this_week"][0]))
        game.update(matchup_id=j, home_roster_id=a, away_roster_id=b, winner_roster_id=a)
        for headline in game["headline_players"]:
            headline["roster_id"] = a
        raw["matchups"]["this_week"].append(game)
        card = json.loads(json.dumps(raw["matchups"]["next_week"][1]))
        card.update(matchup_id=j, a_roster_id=a, b_roster_id=b, game_of_week=False, bye_impact=[])
        raw["matchups"]["next_week"].append(card)
    return raw, names


def test_long_names_on_a_16_team_league_wrap_and_keep_numeral_columns_fixed() -> None:
    raw, names = _long_name_raw()
    assert len(raw["teams"]) == 16 and all(len(n) == _LONG for n in names)
    parts = _parts(raw)
    html = parts.html
    for token in (*_FORBIDDEN, "<link", "flex", "grid"):
        assert token not in html, token
    for tag in ("table", "tr", "td", "div", "span", "p", "b"):
        assert len(re.findall(rf"<{tag}[ >]", html)) == html.count(f"</{tag}>"), tag
    for name in names:
        assert name in html and name in parts.text
    # a team-name cell never forbids wrapping, and carries no fixed width beyond the
    # luck index's own 150px name column
    for name in names:
        for at in (m.start() for m in re.finditer(re.escape(name), html)):
            start = html.rfind("<td", 0, at)
            opener = _TD_OPEN.match(html, start)
            assert opener is not None
            assert "white-space:nowrap" not in html[start:at], name
            # the next-week card columns are percentages (the board's 46/8/46)
            if re.search(r'width="\d+"|width:\d+px', opener.group(1)):
                # only the board's own fixed cells: the luck index's 150px name column and
                # the 250px award cards (a name there wraps inside the card)
                assert re.match(
                    r'<td class="(?:lname|stack)(?: t\d+)?" width="(?:150|250)" ', opener.group(0)
                ), opener.group(0)
    # nowrap is for chips only, never for a cell holding a name
    for match in re.finditer(r"<td[^>]*white-space:nowrap[^>]*>([^<]*)<", html):
        assert all(name not in match.group(1) for name in names)
    # numeral columns keep their fixed width and right alignment at 16 teams
    results = _section(html, "Around the league")
    assert len(re.findall(r'width="70" align="right" style="width:70px;', results)) == 16
    assert len(re.findall(r'width="24" align="right" style="width:24px;', results)) == 16
    standings = _section(html, "Standings")
    assert len(re.findall(r'width="44" align="right"', standings)) == 16
    assert len(re.findall(r'width="46" align="right"', standings)) == 16
    power = _section(html, "Power rankings")
    assert len(re.findall(r'width="44" align="right" style="[^"]*width:44px;', power)) == 16
    luck = _section(html, "The luck index")
    assert len(re.findall(r'<td class="lname t\d+" width="150" style="width:150px;', luck)) == 16
    value = r'<td width="44" align="right" style="width:44px;[^"]*"(?: class="t\d+")?>[−+]\d+\.\d</td>'
    assert len(re.findall(value, luck)) == 16
    assert _section(html, "Next week").count('<td width="46%" valign="top"') == 16


def test_the_unconfirmed_seeding_note_follows_the_facts_flag_alone_in_both_parts() -> None:
    note = "seeding unconfirmed — derived from the standings"
    raw = _raw()
    assert raw["standings"]["playoff_picture"]["seeding_unconfirmed"] is False
    base_issue = render_weekly_issue(WeeklyFacts.model_validate(raw).narration)
    off = _parts(raw, base_issue)
    assert note not in off.html and note not in off.text

    flagged = json.loads(json.dumps(raw))
    flagged["standings"]["playoff_picture"]["seeding_unconfirmed"] = True
    on = _parts(flagged, base_issue)
    assert note in on.html
    assert note in on.text
    assert note not in off.html and note not in off.text


def test_week01_email_has_no_playoff_line_and_no_bye_or_bubble_chips() -> None:
    parts = _parts(_week01_raw())
    section = _section(parts.html, "Standings")
    assert ">Bye<" not in section and ">Bubble<" not in section
    assert "playoff line" not in parts.text
    assert "[BYE]" not in parts.text and "[BUBBLE]" not in parts.text
    assert "seeding unconfirmed" not in parts.text


def test_week01_email_standings_rows_are_ordered_by_points_for() -> None:
    raw = _week01_raw()
    expected = [
        t["team_name"] for t in sorted(raw["teams"], key=lambda t: -t["season"]["points_for"])
    ]
    record_order = [
        t["team_name"]
        for rid in raw["standings"]["overall"]
        for t in raw["teams"]
        if t["roster_id"] == rid
    ]
    assert expected != record_order
    parts = _parts(raw)

    def ordered(haystack: str) -> bool:
        positions = [haystack.index(name) for name in expected]
        return positions == sorted(positions)

    assert ordered(_section(parts.html, "Standings"))
    table = parts.text.split("Standings by points\n", 1)[1].split("\n\n", 1)[0]
    assert ordered(table)


def test_week01_prose_sections_match_the_cold_start_set() -> None:
    """The cold-start heading set is read as cards, never as a second notice section:
    a narrated block under the cold-start Standings heading does not become a
    "Standings" notice, and (Story 6-11) the standings table keeps no per-row prose,
    so the block reaches neither part."""
    raw = _week01_raw()
    facts = WeeklyFacts.model_validate(raw)
    issue = render_weekly_issue(facts.narration)
    marker = "MARKERSTANDINGSPROSE"
    sections = [
        s.model_copy(update={"blocks": [marker]}) if s.heading == "Standings" else s
        for s in issue.sections
    ]
    assert any(s.heading == "Standings" for s in sections)
    parts = _parts(raw, issue.model_copy(update={"sections": sections}))
    assert marker not in parts.html and marker not in parts.text
    assert len(re.findall(_label_html("Standings"), parts.html)) == 1
    assert parts.text.splitlines().count("STANDINGS") == 1
    assert "Standings by points" in parts.text


def test_week01_email_standings_ranks_are_positional() -> None:
    parts = _parts(_week01_raw())
    table = parts.text.split("Standings by points\n", 1)[1].split("\n\n", 1)[0]
    assert [int(line.split(".", 1)[0]) for line in table.splitlines()] == list(range(1, 13))


# --------------------------------------------------------------------------- #
# Story 6-11 carry-over: Gmail clips a message over ~102 KB (F6)
# --------------------------------------------------------------------------- #

#: The size the weekly email HTML should stay under so the app-added per-Reader
#: footer (~3 KB) still leaves it below Gmail's ~102 KB clip.
GMAIL_CLIP_BUDGET = 95_000

#: The week-10 fixture (12 teams) has to leave real headroom, not just fit: it was
#: 106,871 bytes before the 6-11 trim (PR #93 base) and is ~72 KB now.
_WEEK10_BUDGET = 85_000


def _size(html: str) -> int:
    return len(html.encode("utf-8"))


def test_cold_start_email_is_under_the_gmail_clip_budget() -> None:
    assert _size(_parts(_week01_raw()).html) < GMAIL_CLIP_BUDGET


def test_week10_email_is_well_under_the_gmail_clip_budget() -> None:
    assert _size(_parts(_raw()).html) < _WEEK10_BUDGET
    assert _size(_parts(_raw(published=True)).html) < _WEEK10_BUDGET


def test_sixteen_team_long_name_email_is_under_the_gmail_clip_budget() -> None:
    raw, _ = _long_name_raw()
    assert _size(_parts(raw).html) < GMAIL_CLIP_BUDGET


def test_inside_the_playoff_window_the_week10_email_still_leaves_headroom() -> None:
    """The chips and the bracket summary come back near the playoffs; that is the
    largest week-10 shape, and it must still fit with room for the app footer."""
    assert _size(_parts(_with_start_week(14)).html) < _WEEK10_BUDGET


def test_email_output_is_deterministic_and_keeps_the_plain_text_part() -> None:
    first, second = _parts(_raw()), _parts(_raw())
    assert first.html == second.html and first.text == second.text
    assert first.text.strip() and "THE LEAD" in first.text


def test_no_style_attribute_repeats_a_declaration() -> None:
    html = _parts(_raw()).html
    for style in re.findall(r'style="([^"]*)"', html):
        decls = [d for d in style.split(";") if d]
        assert len(decls) == len(set(decls)), style


def test_dedupe_keeps_the_last_copy_so_an_override_still_wins() -> None:
    dedupe = we._dedupe_declarations
    assert dedupe('<p style="margin:0;margin-top:4px;margin:0">') == '<p style="margin-top:4px;margin:0">'
    assert dedupe('<p style="color:red;color:red;">') == '<p style="color:red;">'
    url = '<p style="background:url(a;b);x:1;x:1">'
    assert dedupe(url) == url


# --------------------------------------------------------------------------- #
# Story 6-11 trim: prose cuts, the transaction counts, the top-links marker, style classes
# --------------------------------------------------------------------------- #


def test_the_per_row_and_per_card_prose_is_cut_from_both_parts() -> None:
    parts = _parts(_raw())
    for needle in (
        "topped the scoring",  # per-game sentence
        "points for, model rank",  # per-standings-row sentence
        "points a week.",  # per-power-row sentence
        "wins earned, luck",  # per-luck-row sentence
        "power ranks",  # per-next-week-card sentence
        "On the line:",  # per-card stake sentence (the shared row says "On the line in every game")
        "Eight moves",
        "move this week.",  # "N moves this week." trailing prose
        "in the recent window",
    ):
        assert needle not in parts.html and needle not in parts.text, needle
    # what stays: the three trend lines, the cards' tag, the single shared stake row
    for trend in (
        "No-Huddle Narwhals has run the league's worst luck",
        "Yardage Yaks has run the league's best luck",
        "Bubble-Screen Bobcats has lost six straight.",
    ):
        assert trend in parts.text and _esc(trend) in parts.html, trend
    assert parts.text.count("On the line in every game") == 1
    assert "[Game of the week]" in parts.text


def test_the_transaction_counts_fold_into_the_section_subtitle() -> None:
    raw = _raw()
    desk = _section(_parts(raw).html, "The transaction desk")
    assert ">1 move · 1 trade</div>" in desk and "One move, one trade" not in desk
    raw["narration"]["transactions"].update(this_week_count=8, recent_trade_count=0)
    parts = _parts(raw)
    assert ">8 moves · 0 trades</div>" in _section(parts.html, "The transaction desk")
    assert "THE TRANSACTION DESK\n8 moves · 0 trades\nThis week:" in parts.text


def test_the_top_links_marker_is_one_inert_comment_under_the_masthead() -> None:
    """The app (batch/footer.py) builds the signed per-Reader links after render; the
    engine only reserves the spot, once, between the masthead and the stat tiles."""
    assert we.TOP_LINKS_MARKER == "<!--commishdesk:top-links-->"
    for raw in (_raw(), _week01_raw()):
        parts = _parts(raw)
        assert parts.html.count(we.TOP_LINKS_MARKER) == 1
        at = parts.html.index(we.TOP_LINKS_MARKER)
        assert parts.html.index(">Commishdesk</div>") < at < parts.html.index(">High<")
        assert "top-links" not in parts.text
        assert parts.html.replace(we.TOP_LINKS_MARKER, "").count("<!--") == 1  # the mso block only


def test_hoisting_never_moves_a_colour_fill_border_padding_or_width_into_the_head() -> None:
    html = _parts(_raw()).html
    classes = _head_style(html).split("@media", 1)[0]
    rules = re.findall(r"\.t\d+\{([^}]*)\}", classes)
    assert rules
    for rule in rules:
        for decl in rule.split(";"):
            if decl:
                assert decl.split(":", 1)[0] in we._HOIST_PROPS, decl
    assert not re.search(r"(?:^|;)(?:color|background-color|border|padding|width|height):", ";".join(rules))


def test_every_text_and_fill_colour_left_inline_still_matches_a_dark_selector() -> None:
    """The dark swap keys on the inline literals; hoisting must leave every one of
    them where its attribute selector can see it."""
    html = _parts(_raw(published=True)).html
    text_map, fill_map = we._text_map(), we._background_map()
    seen_text = seen_fill = 0
    for style in re.findall(r'style="([^"]*)"', html):
        for colour in re.findall(r"(?:^|;)color:(#[0-9A-F]{6})", style):
            if colour in text_map:
                seen_text += 1
                assert f";color:{colour}" in style or style.startswith(f"color:{colour}"), style
        for colour in re.findall(r"background-color:(#[0-9A-F]{6})", style):
            if colour in fill_map:
                seen_fill += 1
                assert f"background-color:{colour}" in style
        if f"background-color:{we.NOTABLE};" in style and f"color:{we.INK};" in style:
            assert f"background-color:{we.NOTABLE};color:{we.INK}" in style  # the amber header rule's context
    assert seen_text > 100 and seen_fill > 100


def test_hoist_styles_groups_repeats_leaves_singletons_and_keeps_the_rest_inline() -> None:
    hoist = we._hoist_styles
    html = (
        '<td style="font-size:11px;color:#111111;padding:1px;">a</td>'
        '<td class="px" style="padding:2px;font-size:11px;color:#222222;">b</td>'
        '<td style="font-size:99px;color:#333333;">once</td>'
        '<td style="font-size:0;line-height:4px;">bar</td><td style="font-size:0;line-height:4px;">bar</td>'
        '<td style="background:url(x;y);font-size:11px;">u</td><td style="background:url(x;y);font-size:11px;">u</td>'
    )
    out, css = hoist(html)
    assert css == ".t0{font-size:11px;}"
    assert '<td style="color:#111111;padding:1px;" class="t0">a</td>' in out
    assert '<td class="px t0" style="padding:2px;color:#222222;">b</td>' in out
    assert '<td style="font-size:99px;color:#333333;">once</td>' in out  # a group used once stays inline
    assert out.count('<td style="font-size:0;line-height:4px;">bar</td>') == 2  # collapsed cells stay
    assert out.count('<td style="background:url(x;y);font-size:11px;">u</td>') == 2  # url(...) untouched
    assert hoist(html) == (out, css)  # deterministic


def test_hoisted_html_with_its_classes_is_byte_identical_between_runs() -> None:
    a = _parts(_raw(published=True)).html
    b = _parts(_raw(published=True)).html
    assert a == b and _head_style(a) == _head_style(b)


def test_every_dark_text_colour_has_its_start_anchored_selector_and_the_head_fits_gmails_limit() -> None:
    """Hoisting leaves some text styles starting with ``color:``, which only the
    ``[style^=...]`` half of the text selector matches; and Gmail ignores a head
    ``<style>`` block over ~16 KB, so the whole block (classes + dark swap) stays under."""
    for raw in (_raw(published=True), _long_name_raw()[0]):
        head = _head_style(_parts(raw).html)
        dark = head.split("@media (prefers-color-scheme: dark){", 1)[1]
        for light in we._text_map():
            assert f'[style^="color:{light}"]' in dark and f'[style*=";color:{light}"]' in dark, light
        assert len(head) < 16_000, len(head)
