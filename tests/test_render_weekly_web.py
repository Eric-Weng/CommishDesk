"""Story 5.14a — ``render_weekly_web``, the designed weekly page.

Every row of the spec's I/O matrix (week 10, the published-rank overlay, a
nudge with no reason, stood-down sections, a hostile team name), plus the
page's self-containment, the embedded fonts, the ``<title>`` on every chart,
WCAG AA contrast for the Tuesday Morning text roles in both themes, the shared
next-week stakes line, and luck bars that plot ``season.luck`` as-is.

Inputs are the committed weekly Facts fixtures; the narrated Issue is the
template narrator's.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import zipfile
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from commishdesk.facts.schema import WeeklyFacts
from commishdesk.narrate.weekly_template import SECTION_HEADINGS, WeeklyIssue, WeeklySection, render_weekly_issue
from commishdesk.render import render_weekly_web
from commishdesk.render import style as style_mod
from tests.conftest import REPO_ROOT

FACTS_DIR = REPO_ROOT / "tests" / "fixtures" / "facts"
_STAMP = "2026-09-07T00:00:00Z"

#: The section markers, in the required page order.
_SECTION_ORDER = (
    'class="masthead"',
    'aria-label="The lead"',
    'class="awards"',
    'aria-label="Around the league"',
    'aria-label="Standings"',
    'aria-label="Power rankings"',
    'aria-label="The luck index"',
    'aria-label="Next week"',
    'aria-label="The transaction desk"',
)


def _raw(published: bool = False) -> dict[str, Any]:
    name = "expected-weekly-facts-week10-published.json" if published else "expected-weekly-facts-week10.json"
    return json.loads((FACTS_DIR / name).read_text(encoding="utf-8"))


def _page(raw: dict[str, Any], issue: WeeklyIssue | None = None) -> str:
    facts = WeeklyFacts.model_validate(raw)
    return render_weekly_web(
        facts, issue or render_weekly_issue(facts.narration), output_id="x", generated_at=_STAMP
    )


def _body(page: str) -> str:
    """The page after its stylesheet (the embedded font data is not markup)."""
    return page.split("</style>", 1)[1]


def _power_rows(page: str) -> list[tuple[str, str, str]]:
    """``(rank, team, following nudge html or "")`` per power row, in page order."""
    power = _body(page).split('aria-label="Power rankings"', 1)[1].split("</section>", 1)[0]
    items = re.findall(r'<div class="pw-item">(.*?)</div>(<p class="nudge">.*?</p>)?</div>', power)
    out = []
    for row, nudge in items:
        rank = re.search(r'<span class="rk">(.*?)</span>', row).group(1)  # type: ignore[union-attr]
        team = re.search(r'<span class="tn">(.*?)</span>', row).group(1)  # type: ignore[union-attr]
        out.append((rank, team, nudge))
    return out


# --------------------------------------------------------------------------- #
# I/O matrix
# --------------------------------------------------------------------------- #


def test_week10_renders_every_section_in_order_with_no_nudge() -> None:
    page = _page(_raw())
    body = _body(page)
    positions = [body.index(marker) for marker in _SECTION_ORDER]
    assert positions == sorted(positions)
    assert 'class="paper weekly_issue"' in body
    assert "Nudged" not in body
    ranks = [rank for rank, _team, _nudge in _power_rows(page)]
    assert ranks == [str(n) for n in range(1, 13)]


def test_published_overlay_swaps_7_and_8_with_both_chips_and_reasons() -> None:
    raw = _raw(published=True)
    rows = _power_rows(_page(raw))
    by_rank = {rank: (team, nudge) for rank, team, nudge in rows}
    assert by_rank["7"][0] == "Nickel Newts"
    assert by_rank["8"][0] == "Chip-Block Chinchillas"
    assert "Nudged up · model #8" in by_rank["7"][1]
    assert "Nudged down · model #7" in by_rank["8"][1]
    reasons = {row["team"]: row["nudge_justification"] for row in raw["narration"]["power"]}
    assert "the record settles the tie the model left open" in by_rank["7"][1]
    assert _esc_text(reasons["Nickel Newts"]) in by_rank["7"][1]
    assert _esc_text(reasons["Chip-Block Chinchillas"]) in by_rank["8"][1]
    assert sum(1 for _r, _t, nudge in rows if nudge) == 2


def _esc_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("'", "&#x27;").replace('"', "&quot;")


def test_a_nudge_with_no_reason_shows_the_chip_alone() -> None:
    raw = _raw(published=True)
    for row in raw["narration"]["power"]:
        row["nudge_justification"] = None
    by_rank = {rank: nudge for rank, _team, nudge in _power_rows(_page(raw))}
    for rank in ("7", "8"):
        assert "Nudged" in by_rank[rank]
        assert 'class="why"' not in by_rank[rank]


def test_rank_falls_back_to_the_facts_published_rank_when_narration_has_none() -> None:
    raw = _raw(published=True)
    for row in raw["narration"]["power"]:
        row["published_rank"] = None
        row["nudge_justification"] = None
    by_rank = {rank: (team, nudge) for rank, team, nudge in _power_rows(_page(raw))}
    assert by_rank["7"][0] == "Nickel Newts" and "Nudged up" in by_rank["7"][1]
    assert 'class="why"' not in by_rank["7"][1]


def test_stood_down_sections_render_no_heading_and_no_frame() -> None:
    raw = _raw()
    for team in raw["teams"]:
        team["season"]["luck"] = None
    raw["transactions"]["this_week"] = []
    raw["transactions"]["recent_trades"] = []
    raw["matchups"]["next_week"] = []
    raw["standings"]["playoff_picture"] = None
    body = _body(_page(raw))
    for gone in ("The luck index", "Who the schedule favoured", "The transaction desk", "What moved on the wire",
                 "Next week", "stakes-shared", "Playoff line", "chip-emph\">Bye"):
        assert gone not in body, gone
    # the rest still renders
    for kept in ('aria-label="Standings"', 'aria-label="Power rankings"', 'aria-label="Around the league"'):
        assert kept in body


def test_a_lead_hero_that_cannot_draw_falls_back_to_type_only() -> None:
    raw = _raw()
    lead = raw["lead_candidates"][0]
    assert lead["kind"] == "lineup_loss"
    team = next(t for t in raw["teams"] if t["roster_id"] == lead["roster_ids"][0])
    team["this_week"]["points_left_on_bench"] = None
    body = _body(_page(raw))
    lead_html = body.split('aria-label="The lead"', 1)[1].split('class="awards"', 1)[0]
    assert "<svg" not in lead_html
    assert 'class="lead-type"' in lead_html
    assert _esc_text(lead["hook"]) in lead_html


@pytest.mark.parametrize("kind", ["lineup_loss", "biggest_blowout", "closest_game", "week_high_score"])
def test_every_lead_kind_draws_its_hero(kind: str) -> None:
    raw = _raw()
    raw["lead_candidates"] = [dict(c, rank=1) for c in raw["lead_candidates"] if c["kind"] == kind]
    lead_html = _body(_page(raw)).split('aria-label="The lead"', 1)[1].split('class="awards"', 1)[0]
    assert "<svg" in lead_html and 'class="lead-type"' not in lead_html


def test_a_hostile_team_name_appears_only_escaped() -> None:
    hostile = '<script>x</script>"><svg onload=1>'
    raw = _raw(published=True)
    for team in raw["teams"]:
        if team["roster_id"] in ("7", "5"):  # the lead team and a nudged team
            team["team_name"] = hostile
    for row in raw["narration"]["power"]:
        if row["roster_id"] == "5":
            row["nudge_justification"] = hostile
    page = _page(raw)
    assert hostile not in page
    assert "<script" not in page
    assert "onload=1>" not in page
    escaped = "&lt;script&gt;x&lt;/script&gt;&quot;&gt;&lt;svg onload=1&gt;"
    body = _body(page)
    assert escaped in body
    # in SVG text / attribute values too (the lead hero's aria-label and <title>)
    assert f'aria-label="{escaped} started' in body
    assert f"<title>{escaped} started" in body


def test_correction_and_unverified_stamps_reach_the_page() -> None:
    raw = _raw()
    facts = WeeklyFacts.model_validate(raw)
    issue = render_weekly_issue(facts.narration)
    issue = issue.model_copy(
        update={
            "dateline": f"UNVERIFIED — {issue.dateline}",
            "sections": [WeeklySection(heading="Correction", blocks=["Correction — fixed a score"]), *issue.sections],
        }
    )
    body = _body(_page(raw, issue))
    assert "UNVERIFIED — " in body
    assert "Correction — fixed a score" in body
    assert body.index("Correction") < body.index('aria-label="The lead"')


def test_narrated_prose_comes_from_the_issue_sections() -> None:
    raw = _raw()
    facts = WeeklyFacts.model_validate(raw)
    issue = render_weekly_issue(facts.narration)
    marker = {heading: f"Narrated line for {heading}." for heading in SECTION_HEADINGS}
    issue = issue.model_copy(
        update={"sections": [WeeklySection(heading=h, blocks=[marker[h]]) for h in SECTION_HEADINGS]}
    )
    body = _body(_page(raw, issue))
    for heading, line in marker.items():
        assert line in body, heading


# --------------------------------------------------------------------------- #
# Self-containment, fonts, charts
# --------------------------------------------------------------------------- #


def test_page_is_self_contained() -> None:
    page = _page(_raw(published=True))
    assert page.startswith("<!doctype html>") and page.endswith("\n") and "\r" not in page
    assert page.count("<style>") == 1
    lowered = page.lower()
    for banned in ("<script", "<link", "@import", "src=", "http"):
        assert banned not in lowered, banned
    urls = re.findall(r"url\(([^)]*)\)", page)
    assert urls and all(url.startswith("data:font/woff2;base64,") for url in urls)
    assert len(urls) == len(style_mod.WEEKLY_FONT_FILES)


def test_fonts_resolve_from_the_installed_package() -> None:
    fonts = resources.files("commishdesk.render").joinpath("fonts")
    for _family, _weight, filename in style_mod.WEEKLY_FONT_FILES:
        data = fonts.joinpath(filename).read_bytes()
        assert data[:4] == b"wOF2", filename
    for licence in ("OFL-BricolageGrotesque.txt", "OFL-DMSans.txt", "OFL-IBMPlexMono.txt"):
        assert "SIL Open Font License" in fonts.joinpath(licence).read_text(encoding="utf-8")


def test_fonts_and_licences_ship_in_the_wheel(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not available")
    result = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(tmp_path)], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    (wheel,) = tmp_path.glob("*.whl")
    names = set(zipfile.ZipFile(wheel).namelist())
    for _family, _weight, filename in style_mod.WEEKLY_FONT_FILES:
        assert f"commishdesk/render/fonts/{filename}" in names
    assert {f"commishdesk/render/fonts/OFL-{n}.txt" for n in ("BricolageGrotesque", "DMSans", "IBMPlexMono")} <= names


def test_every_svg_has_a_title() -> None:
    svgs = re.findall(r"<svg\b.*?</svg>", _body(_page(_raw())), flags=re.DOTALL)
    assert len(svgs) >= 2  # the lead hero and the luck index
    for svg in svgs:
        assert re.match(r'<svg\b[^>]*role="img"[^>]*><title>[^<]+</title>', svg), svg[:120]


def test_render_is_deterministic() -> None:
    assert _page(_raw(published=True)) == _page(_raw(published=True))


#: SHA-256 of the Story 5.14a page (commit 779de47) for the two committed
#: fixtures — Story 5.14b moved its selectors into ``render/_weekly_model.py``
#: and the page must stay byte-identical. Change these only with a deliberate
#: design change to the page.
_GOLDEN_SHA256 = {
    False: "dd0b450de09c78d95648de40ca5974752b40d2cd61ef9b2578b4e59aa51f8cde",
    True: "ddc400a8254d214c00c6341eab3c14a2642d3ba1db18466aab4c4cfcf593ba82",
}


@pytest.mark.parametrize("published", [False, True])
def test_page_is_byte_identical_to_the_5_14a_golden(published: bool) -> None:
    page = _page(_raw(published=published))
    assert hashlib.sha256(page.encode("utf-8")).hexdigest() == _GOLDEN_SHA256[published]


# --------------------------------------------------------------------------- #
# Theme: WCAG AA text contrast in light and dark
# --------------------------------------------------------------------------- #


def _luminance(hex_colour: str) -> float:
    value = hex_colour.lstrip("#")
    channels = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_the_contrast_function_matches_known_values() -> None:
    assert _contrast("#000000", "#FFFFFF") == pytest.approx(21.0)
    assert _contrast("#1E9E6A", "#FFFFFF") == pytest.approx(3.41, abs=0.01)  # why light good needed a text variant
    assert _contrast("#E5484D", "#FFFFFF") == pytest.approx(3.91, abs=0.01)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("fg", ["--emph", "--good-text", "--bad-text", "--ink"])
@pytest.mark.parametrize("bg", ["--card", "--paper"])
def test_text_roles_meet_wcag_aa(theme: str, fg: str, bg: str) -> None:
    tokens = style_mod.WEEKLY_LIGHT_TOKENS if theme == "light" else style_mod.WEEKLY_DARK_TOKENS
    assert _contrast(tokens[fg], tokens[bg]) >= 4.5


def _over(wash: str, base: str) -> str:
    """An ``#rrggbbaa`` wash composited over an opaque ``#rrggbb`` base."""
    alpha = int(wash[7:9], 16) / 255
    mixed = (
        round(alpha * int(wash[i : i + 2], 16) + (1 - alpha) * int(base[i : i + 2], 16)) for i in (1, 3, 5)
    )
    return "#" + "".join(f"{channel:02X}" for channel in mixed)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize(
    ("fg", "wash"), [("--good-text", "--good-wash"), ("--bad-text", "--bad-wash"), ("--emph", "--emph-wash")]
)
def test_status_text_on_its_wash_meets_wcag_aa(theme: str, fg: str, wash: str) -> None:
    tokens = style_mod.WEEKLY_LIGHT_TOKENS if theme == "light" else style_mod.WEEKLY_DARK_TOKENS
    assert _contrast(tokens[fg], _over(tokens[wash], tokens["--card"])) >= 4.5


def test_light_text_variants_are_the_recorded_values() -> None:
    assert style_mod.WEEKLY_LIGHT_TOKENS["--good-text"] == "#157F55"
    assert style_mod.WEEKLY_LIGHT_TOKENS["--bad-text"] == "#C93338"


def test_the_stylesheet_ships_light_and_a_dark_variant() -> None:
    css = style_mod.build_weekly_style()
    assert "@media (prefers-color-scheme: dark)" in css
    assert f"--paper: {style_mod.WEEKLY_DARK_TOKENS['--paper']};" in css
    assert f"--paper: {style_mod.WEEKLY_LIGHT_TOKENS['--paper']};" in css


# --------------------------------------------------------------------------- #
# Next week and luck
# --------------------------------------------------------------------------- #


def test_shared_stakes_render_once_and_cards_carry_no_empty_chip_row() -> None:
    raw = _raw()
    assert all(
        card["stakes"] == ["bye_seed", "wildcard_race", "division_race"] for card in raw["matchups"]["next_week"]
    )
    body = _body(_page(raw))
    assert body.count('class="stakes-shared"') == 1
    next_week = body.split('aria-label="Next week"', 1)[1].split("</section>", 1)[0]
    cards = re.findall(r'<article class="card nw[^"]*">(.*?)</article>', next_week)
    assert len(cards) == len(raw["matchups"]["next_week"])
    for card, data in zip(cards, raw["matchups"]["next_week"], strict=True):
        if data["game_of_week"]:
            assert "Game of the week" in card and "Bye seed" not in card
        else:
            assert 'class="nw-chips"' not in card


def test_a_differing_stake_shows_as_a_chip_on_its_card_only() -> None:
    raw = _raw()
    raw["matchups"]["next_week"][0]["stakes"] = ["bye_seed", "wildcard_race", "division_race", "elimination"]
    body = _body(_page(raw))
    next_week = body.split('aria-label="Next week"', 1)[1].split("</section>", 1)[0]
    cards = re.findall(r'<article class="card nw[^"]*">(.*?)</article>', next_week)
    assert "Elimination" in cards[0]
    assert all("Elimination" not in card for card in cards[1:])
    assert body.count('class="stakes-shared"') == 1


def test_a_single_next_week_card_keeps_its_stakes_as_chips() -> None:
    raw = _raw()
    raw["matchups"]["next_week"] = [card for card in raw["matchups"]["next_week"] if not card["game_of_week"]][:1]
    body = _body(_page(raw))
    assert 'class="stakes-shared"' not in body
    next_week = body.split('aria-label="Next week"', 1)[1].split("</section>", 1)[0]
    assert "Bye seed" in next_week and 'class="nw-chips"' in next_week


# --------------------------------------------------------------------------- #
# Standings, results and the transaction desk
# --------------------------------------------------------------------------- #


def _team_names(raw: dict[str, Any]) -> dict[str, str]:
    from commishdesk.render.weekly_web import _team_label

    return {team.roster_id: _team_label(team) for team in WeeklyFacts.model_validate(raw).teams}


def test_standings_draw_the_playoff_line_and_bye_and_bubble_chips() -> None:
    raw = _raw()
    picture = raw["standings"]["playoff_picture"]
    names = _team_names(raw)
    body = _body(_page(raw))
    standings = body.split('aria-label="Standings"', 1)[1].split("</section>", 1)[0]
    assert standings.count("Playoff line") == 1
    before, after = standings.split("Playoff line", 1)
    assert before.count('<div class="st-row') == picture["cut_line_after_rank"]
    rows = re.findall(r'<div class="(st-row[^"]*)">(.*?)</div>', standings)
    assert len(rows) == len(raw["standings"]["overall"])
    for (cls, row), roster_id in zip(rows, raw["standings"]["overall"], strict=True):
        assert _esc_text(names[roster_id]) in row
        below = raw["standings"]["overall"].index(roster_id) >= picture["cut_line_after_rank"]
        assert ("below" in cls) == below, roster_id
        if roster_id in picture["byes"]:
            assert 'chip-emph">Bye' in row
        elif roster_id in picture["bubble"]:
            assert 'chip-notable">Bubble' in row
        else:
            assert 'class="chip' not in row
    assert after.count('<div class="st-row') == len(rows) - picture["cut_line_after_rank"]


def test_a_tied_game_shows_no_winner() -> None:
    raw = _raw()
    game = raw["matchups"]["this_week"][0]
    game.update(away_points=game["home_points"], winner_roster_id=None, margin=0.0)
    body = _body(_page(raw))
    around = body.split('aria-label="Around the league"', 1)[1].split("</section>", 1)[0]
    card = re.findall(r'<article class="card game">(.*?)</article>', around)[0]
    assert '<span class="by">Tied</span>' in card
    assert "Won by" not in card and 'class="side win"' not in card


def test_every_trade_from_the_latest_trade_week_gets_a_card() -> None:
    raw = _raw()
    latest = raw["transactions"]["recent_trades"][0]
    second = json.loads(json.dumps(latest))
    second["transaction_id"] = "id_second"
    older = json.loads(json.dumps(latest))
    older.update(transaction_id="id_older", week=latest["week"] - 1)
    raw["transactions"]["recent_trades"] = [older, latest, second]
    body = _body(_page(raw))
    assert body.count(f"Trade · week {latest['week']}") == 2
    assert f"Trade · week {latest['week'] - 1}" not in body


def test_luck_bars_plot_season_luck_most_lucky_first() -> None:
    raw = _raw()
    body = _body(_page(raw))
    plotted = [float(value) for value in re.findall(r'class="luck-bar" data-luck="([^"]+)"', body)]
    expected = sorted((t["season"]["luck"] for t in raw["teams"]), reverse=True)
    assert plotted == expected
    # the labels carry the same values, with a real minus
    assert "−1.8" in body and "+2.5" in body
