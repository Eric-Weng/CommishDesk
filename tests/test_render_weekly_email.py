"""Story 5.14b — ``render_weekly_email``, the weekly Issue as an email pair.

Every row of the spec's I/O matrix that touches the email (week 10, the
published overlay, stood-down sections, a hostile name, a reissue), plus the
email-safe construct ban (``_FORBIDDEN``), the dark-inversion check on the
masthead, the ``text/plain`` part carrying every section, the shared stakes
shown once, and the AA text colours.
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


def _raw(published: bool = False) -> dict[str, Any]:
    name = "expected-weekly-facts-week10-published.json" if published else "expected-weekly-facts-week10.json"
    return json.loads((FACTS_DIR / name).read_text(encoding="utf-8"))


def _parts(raw: dict[str, Any], issue: WeeklyIssue | None = None) -> EmailParts:
    facts = WeeklyFacts.model_validate(raw)
    return render_weekly_email(facts, issue or render_weekly_issue(facts.narration), generated_at=_STAMP)


def _label_html(label: str) -> str:
    return f"text-transform:uppercase;color:{we.INK2};border-top:2px solid {we.RULE};padding-top:8px;\">{label}</div>"


def _section(html: str, label: str) -> str:
    """The HTML of one section: from its label to the next ``<tr>``."""
    return html.split(_label_html(label), 1)[1].split('<tr><td class="px"', 1)[0]


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
    head_style = html.rsplit("<style>", 1)[1].split("</style>", 1)[0]
    assert "color:" not in head_style and "font-family:" not in head_style


def test_sections_render_in_the_required_order() -> None:
    html = _parts(_raw()).html
    positions = [html.index(_label_html(label)) for label in _LABELS]
    assert positions == sorted(positions)
    assert html.index(">Trench Warfare</td>") < positions[0]


def test_the_masthead_is_the_nameplate_and_week_line_with_no_tiles() -> None:
    html = _parts(_raw()).html
    masthead = html.split('class="container"', 1)[1].split(_label_html("The lead"), 1)[0]
    assert "Trench Warfare" in masthead and "Week 10 · Season 2025 · 12 teams" in masthead
    for tile in ("High", "Closest", "Blowouts", "Pts scored"):
        assert f">{tile}<" not in masthead


def _masthead_cells(html: str) -> list[tuple[str, str]]:
    masthead = html.split('class="container"', 1)[1].split(_label_html("The lead"), 1)[0]
    masthead = masthead.rsplit("<tr>", 1)[0]  # up to the lead's own row
    cells = re.findall(r"<td\b([^>]*)>", masthead)
    assert len(cells) >= 4
    pairs = []
    for attrs in cells:
        bg = re.search(r'bgcolor="(#[0-9A-Fa-f]{6})"', attrs)
        fg = re.search(r"[;\"]color:(#[0-9A-Fa-f]{6})", attrs)
        assert bg and fg, attrs  # every masthead cell: explicit bgcolor + inline colour
        pairs.append((fg.group(1), bg.group(1)))
    return pairs


def _invert(hex_colour: str) -> str:
    value = int(hex_colour.lstrip("#"), 16)
    return f"#{0xFFFFFF - value:06X}"


def test_the_masthead_survives_dark_client_inversion() -> None:
    html = _parts(_raw()).html
    assert '<meta name="color-scheme" content="light">' in html
    assert '<meta name="supported-color-schemes" content="light">' in html
    for fg, bg in _masthead_cells(html):
        if fg == bg:
            continue  # the heavy rule: a solid bar, no text
        assert _contrast(fg, bg) >= 4.5, (fg, bg)
        assert _contrast(_invert(fg), _invert(bg)) >= 4.5, (fg, bg)


def test_text_colours_are_the_aa_roles_and_ink3_carries_nothing() -> None:
    html = _parts(_raw(published=True)).html
    assert we.GOOD_TEXT == "#157F55" and we.BAD_TEXT == "#C93338"
    assert "#9A9CB2" not in html  # ink-3
    for colour in (we.INK, we.INK2, we.GOOD_TEXT, we.BAD_TEXT, we.EMPH):
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
    for data in ("started 119.97 · left on bench 108.86 · opponent 126.03", "--- playoff line ---",
                 "Coach of the week: Two Minute Drill, 99.2%", "Yardage Yaks 126.03 over Kneel-Down Koalas 119.97",
                 "Flea Flicker Union · Waiver · Add Jalen Nailor WR; Drop Tyrod Taylor QB · FAAB $25"):
        assert data in parts.text, data


# --------------------------------------------------------------------------- #
# I/O matrix
# --------------------------------------------------------------------------- #


def test_week10_lead_is_the_bench_bar_as_a_two_cell_row() -> None:
    lead = _section(_parts(_raw()).html, "The lead")
    assert "Kneel-Down Koalas lost by 6.06 with 108.86 on the bench" in lead
    cells = re.findall(r'<td width="(\d+)%" bgcolor="(#[0-9A-F]{6})"', lead)
    assert cells[:2] == [("52", we.BAR), ("48", we.BAD_FILL)]
    assert "started 119.97 · left on bench 108.86 · opponent 126.03" in lead
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
    assert "bgcolor" not in lead.split("Coach of the week", 1)[0]  # no bar


def test_published_overlay_orders_power_by_published_rank_with_the_cited_reason() -> None:
    raw = _raw(published=True)
    parts = _parts(raw)
    power = _section(parts.html, "Power rankings")
    assert power.index("Nickel Newts") < power.index("Chip-Block Chinchillas")
    assert "▲ Nudged up · model #8" in power and "▼ Nudged down · model #7" in power
    reason = next(r["nudge_justification"] for r in raw["narration"]["power"] if r["team"] == "Nickel Newts")
    assert reason.replace("'", "&#x27;") in power
    assert f"▲ Nudged up · model #8 — {reason}" in parts.text


def test_a_nudge_without_a_reason_shows_the_text_line_alone() -> None:
    raw = _raw(published=True)
    for row in raw["narration"]["power"]:
        row["nudge_justification"] = None
    parts = _parts(raw)
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


def test_shared_stakes_appear_once_and_matchups_show_only_differing_stakes() -> None:
    raw = _raw()
    raw["matchups"]["next_week"][0]["stakes"].append("elimination")
    parts = _parts(raw)
    for part in (parts.html, parts.text):
        assert part.count("On the line in every game: Bye seed, Wildcard race, Division race") == 1
        assert part.count("Bye seed") == 1
        assert part.count("Elimination") == 1
    assert "Flea Flicker Union (7-3-0) vs Bubble-Screen Bobcats (2-8-0) [Elimination]" in parts.text


def test_luck_bars_are_block_characters_scaled_to_at_most_eleven() -> None:
    raw = _raw()
    parts = _parts(raw)
    bars = re.findall(r"letter-spacing:-1px;color:#[0-9A-F]{6};\">(█+)</td>", parts.html)
    assert len(bars) == len(raw["teams"])
    assert max(len(bar) for bar in bars) == 11 and min(len(bar) for bar in bars) >= 1
    assert we.luck_bar(0.0, 2.5) == "█" and we.luck_bar(0.01, 2.5) == "█"
    assert we.luck_bar(-2.5, 2.5) == "█" * 11 and we.luck_bar(9.0, 0.0) == "█"


def test_transactions_are_two_short_tables() -> None:
    desk = _section(_parts(_raw()).html, "The transaction desk")
    assert desk.count("<table") == 2
    assert "Latest trade · week 9 · 1 week ago" in desk and "Receives" in desk


def test_a_trade_side_receiving_nothing_reads_the_same_in_both_parts() -> None:
    raw = _raw()
    side = raw["transactions"]["recent_trades"][0]["sides"][0]
    side.update(players=[], picks=[], faab=0)
    parts = _parts(raw)
    assert "nothing listed</td>" in _section(parts.html, "The transaction desk")
    assert "receives nothing listed" in parts.text


def test_with_no_hooked_lead_the_headline_is_the_narrated_lead() -> None:
    raw = _raw()
    raw["lead_candidates"] = []
    facts = WeeklyFacts.model_validate(raw)
    issue = render_weekly_issue(facts.narration)
    first = next(s.blocks for s in issue.sections if s.heading == "The Lead")[0]
    parts = render_weekly_email(facts, issue, generated_at=_STAMP)
    assert we._headline(first) in _section(parts.html, "The lead")
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
