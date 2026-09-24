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


def _parts(raw: dict[str, Any], issue: WeeklyIssue | None = None) -> EmailParts:
    facts = WeeklyFacts.model_validate(raw)
    return render_weekly_email(facts, issue or render_weekly_issue(facts.narration), generated_at=_STAMP)


def _label_html(label: str) -> str:
    return f'text-transform:uppercase;color:{we.EMPH_TEXT};font-weight:bold;">{label}</td>'


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


def test_the_head_style_block_only_carries_progressive_enhancement() -> None:
    """The base style block carries layout only (``<style>`` is a progressive
    enhancement, never the sole copy of a style that matters); the dark-mode
    block is a value swap on inline literals."""
    html = _parts(_raw()).html
    head_style = html.rsplit("<style>", 1)[1].split("</style>", 1)[0]
    # the base styling is inline; the head block adds no font rules
    assert "font-family:" not in head_style
    # progressive enhancement: a width tweak…
    assert "@media only screen and (max-width:660px)" in head_style
    assert ".container{width:100%!important;}" in head_style
    assert ".px{padding-left:20px!important;padding-right:20px!important;}" in head_style
    # …and a dark-mode colour swap targeting the literal inline values
    dark = head_style.split("@media (prefers-color-scheme: dark)", 1)[1]
    # text, fill and border are mapped separately; the unanchored ``[style*="color:X"]``
    # form would also match ``background-color:X`` (the original dark-mode bug)
    assert '[style*=";color:' in dark and '[style*="background-color:' in dark
    assert '[style*="solid ' in dark
    assert '[style*="color:' not in dark
    for decl in ("font-family:", "line-height:", "margin:", "padding:"):
        assert decl not in dark, decl


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
    for section in issue.sections:  # the narrated prose reaches the text part
        for block in section.blocks:
            assert block in parts.text, block[:40]
    for data in (
        "started 119.97 · left on bench 108.86 · opponent 126.03",
        "--- playoff line ---",
        "Coach of the week: Two Minute Drill, 99.2%",
        "Yardage Yaks 126.03 over Kneel-Down Koalas 119.97",
        "Flea Flicker Union · Waiver · Add Jalen Nailor WR; Drop Tyrod Taylor QB · FAAB $25",
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
    rows = section.split('<td class="lname"')[1:]
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


def test_standings_bye_and_bubble_chips_match_the_facts() -> None:
    section = _section(_parts(_raw()).html, "Standings")
    assert len(re.findall(r">Bye<", section)) == 2 and len(re.findall(r">Bubble<", section)) == 4


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
    assert len(rows) >= 20
    for padding in rows:
        assert _lr(padding) == ("28px", "28px"), padding
    # each section heading (eyebrow dash + title) sits inside a gutter row
    for label in _LABELS:
        before = html[: html.index(_label_html(label))]
        assert _TOP_ROW.findall(before)[-1] == "44px 28px 18px 28px", label


def test_prose_paragraphs_are_padded_ink2_body_text_inside_the_gutter() -> None:
    html = _parts(_raw()).html
    para = "Screen Pass Syndicate beat Bubble-Screen Bobcats, 183.14 to 154.38."
    at = html.index(para)
    row = _TOP_ROW.match(html, html.rfind('<tr><td class="px"', 0, at))
    assert row is not None and _lr(row.group(1)) == ("28px", "28px")
    assert f"color:{we.INK2};" in html[html.rfind("<p ", 0, at) : at]


_CARD_OPEN = (
    f'<tr><td bgcolor="{we.CARD}" style="background-color:{we.CARD};border:1px solid {we.LINE};'
)
_SPACER = '<tr><td height="12" style="height:12px;font-size:0;line-height:12px;">&nbsp;</td></tr>'


def test_game_cards_are_bordered_rounded_cards_with_the_chip_inside() -> None:
    results = _section(_parts(_raw()).html, "Around the league")
    cards = results.split(_CARD_OPEN)[1:]
    assert len(cards) == 6
    tagged = 0
    for card in cards:
        body = card.split(_SPACER, 1)[0]  # everything up to the 12px gap is the card
        assert body.startswith("border-radius:12px;padding:0;")
        assert body.endswith("</td></tr></table></td></tr>")  # the card's own cell closes last
        assert "W</td>" in body and "L</td>" in body
        for word in ("Blowout", "Week high", "Nail-biter"):
            if f">{word}</td>" in body:
                tagged += 1
                # under the loser row, padded left to clear the monogram
                assert body.index("L</td>") < body.index(f">{word}</td>")
                assert "margin-top:10px;padding-left:38px;" in body
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
    assert html.count('class="stack"') == 4 + 4  # four tiles, four award cards
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


def test_power_is_one_row_per_team_with_fixed_rank_score_and_delta_columns() -> None:
    raw = _raw(published=True)
    section = _section(_parts(raw).html, "Power rankings")
    n = len(raw["narration"]["power"])
    assert len(re.findall(r'width="34" style="background-color:[^"]*width:34px;', section)) == n
    score = re.findall(r'<td bgcolor="#[0-9A-F]{6}" (width="50" align="right" style="[^"]*")', section)
    assert len(score) == n and all("width:50px;" in c for c in score)
    delta = re.findall(r'<td bgcolor="#[0-9A-F]{6}" (width="44" align="right" style="[^"]*")', section)
    assert len(delta) == n and all("width:44px;" in c for c in delta)
    assert section.count(">MODEL</div>") == n
    # nudge notes span the four columns instead of adding a fifth
    assert section.count('colspan="4"') == 2


def test_luck_is_one_row_per_team_with_fixed_name_and_value_columns_and_a_split_bar() -> None:
    raw = _raw()
    section = _section(_parts(raw).html, "The luck index")
    n = len(raw["teams"])
    assert len(re.findall(r'<td class="lname" width="150" style="width:150px;', section)) == n
    assert len(re.findall(r'<td width="44" align="right" style="width:44px;[^"]*">[−+]\d+\.\d</td>', section)) == n
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
                assert opener.group(0).startswith(('<td class="lname" width="150" style="width:150px;',
                                                   '<td class="stack" width="250" ')), opener.group(0)
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
    assert len(re.findall(r'width="50" align="right" style="[^"]*width:50px;', power)) == 16
    assert len(re.findall(r'width="44" align="right" style="[^"]*width:44px;', power)) == 16
    luck = _section(html, "The luck index")
    assert len(re.findall(r'<td class="lname" width="150" style="width:150px;', luck)) == 16
    assert len(re.findall(r'<td width="44" align="right" style="width:44px;[^"]*">[−+]\d+\.\d</td>', luck)) == 16
    assert _section(html, "Next week").count('<td width="46%" valign="top"') == 16
