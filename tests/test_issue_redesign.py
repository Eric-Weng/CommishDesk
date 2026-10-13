"""Story 8.16b -- the Issue page redesign pass (engine).

Team identity (the 16-color palette with a contrast test, unique monograms shared by the
web page and the email, colors assigned once and kept), the lineup-loss wording, the
tie-only-when-nonzero record, "Out of the race", the stable section ids, the 32-team page,
the ``power_history`` current-week fix and the narration replay seam.

The existing 5B tests (bump chart, luck, expandable cards) live in
``test_render_weekly_web.py`` and are updated there, not here.
"""

from __future__ import annotations

import colorsys
import copy
import json
import logging
import re
from pathlib import Path
from typing import Any

import pytest

from commishdesk.errors import CommishDeskError, StoreError
from commishdesk.facts import WeeklyFacts, build_weekly_facts
from commishdesk.ingest import build_league_model, build_player_names, build_player_snapshot, build_week_model
from commishdesk.narrate.weekly_template import render_weekly_issue
from commishdesk.render import _weekly_model as wm
from commishdesk.render import render_weekly_email, render_weekly_web
from commishdesk.render.style import (
    TEAM_PALETTE_DARK,
    TEAM_PALETTE_LIGHT,
    WEEKLY_DARK_TOKENS,
    WEEKLY_LIGHT_TOKENS,
)
from commishdesk.store import FileStore
from commishdesk.weekly import (
    TEAM_COLORS_NAMESPACE,
    assign_team_colors,
    build_weekly_issue,
    narration_record,
    parse_narration_record,
    record_published_weekly,
)
from tests.conftest import REPO_ROOT
from tests.test_render_weekly_web import _body, _page, _plain, _raw
from tests.test_weekly_entry import _Adapter

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
_STAMP = "2026-09-07T00:00:00Z"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _luminance(hex_color: str) -> float:
    channels = [int(hex_color.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


_LONG = (
    "The Unreasonably Long Named Fighting Mongooses of Wellington",
    "Supercalifragilisticexpialidocious Squad",
    "Antidisestablishmentarian Armadillos Athletic Club",
)


def scaled_raw(raw: dict[str, Any], n: int) -> dict[str, Any]:
    """The week-10 Facts scaled to *n* teams with very long names (every id and rank unique)."""
    raw = copy.deepcopy(raw)
    teams = raw["teams"]
    base = len(teams)
    for k in range(base + 1, n + 1):
        team = copy.deepcopy(teams[(k - 1) % base])
        team["roster_id"] = str(k)
        team["team_name"] = f"{_LONG[k % 3]} {k}"
        team["manager"] = f"manager{k}"
        team["season"]["rank"] = k
        team["season"]["power"]["model_rank"] = k
        if team["season"]["power"].get("published_rank") is not None:
            team["season"]["power"]["published_rank"] = k
        for row in team["history"]["weekly"]:
            if row.get("power_rank") is not None:
                row["power_rank"] = k
            if row.get("published_rank") is not None:
                row["published_rank"] = k
        teams.append(team)
    for i, team in enumerate(teams[:base]):
        if i % 4 == 0:
            team["team_name"] = f"{_LONG[i % 3]} {i + 1}"
    raw["standings"]["overall"] = [team["roster_id"] for team in teams]
    this, nxt = raw["matchups"]["this_week"], raw["matchups"]["next_week"]
    for j in range(len(this) * 2 + 1, n, 2):
        game = copy.deepcopy(this[j % len(this)])
        game["matchup_id"] = 100 + j
        game["home_roster_id"], game["away_roster_id"] = str(j), str(j + 1)
        for headline in game.get("headline_players", []):
            headline["roster_id"] = str(j)
        this.append(game)
        card = copy.deepcopy(nxt[j % len(nxt)])
        card["matchup_id"] = 200 + j
        card["a_roster_id"], card["b_roster_id"] = str(j), str(j + 1)
        nxt.append(card)
    power = raw["narration"]["power"]
    for team in teams[base:]:
        row = copy.deepcopy(power[0])
        row["roster_id"], row["team"], row["model_rank"] = team["roster_id"], team["team_name"], int(team["roster_id"])
        power.append(row)
    by_id = {team["roster_id"]: team for team in teams}
    for row in power[:base]:
        row["team"] = by_id[row["roster_id"]]["team_name"]
    luck = raw["narration"].get("luck", [])
    for team in teams[base:]:
        if luck:
            row = copy.deepcopy(luck[0])
            row["roster_id"], row["team"] = team["roster_id"], team["team_name"]
            luck.append(row)
    for row in luck[:base]:
        row["team"] = by_id[row["roster_id"]]["team_name"]
    raw["league"]["format"]["team_count"] = n
    return raw


def _render(raw: dict[str, Any], **kwargs: Any) -> str:
    facts = WeeklyFacts.model_validate(raw)
    return render_weekly_web(
        facts, render_weekly_issue(facts.narration), output_id="x", generated_at=_STAMP, **kwargs
    )


def _avatar_codes(page: str) -> list[str]:
    return re.findall(r'<span class="mg[^"]*" aria-hidden="true">([^<]+)</span>', page)


# --------------------------------------------------------------------------- #
# A-1 -- the palette
# --------------------------------------------------------------------------- #


def test_the_palette_has_sixteen_colors_and_the_twelve_approved_ones_first() -> None:
    assert len(TEAM_PALETTE_LIGHT) == len(TEAM_PALETTE_DARK) == wm.PALETTE_SIZE == 16
    assert len(set(TEAM_PALETTE_LIGHT)) == 16 and len(set(TEAM_PALETTE_DARK)) == 16
    assert TEAM_PALETTE_LIGHT[:12] == (
        "#C2410C", "#1D4ED8", "#0E7490", "#15803D", "#7C3AED", "#B91C1C",
        "#52525B", "#4D7C0F", "#A16207", "#A21CAF", "#1E3A8A", "#BE185D",
    )  # fmt: skip


def test_the_four_extra_colors_are_none_of_them_gold() -> None:
    """Gold is reserved for the Game of the week border."""
    for color in TEAM_PALETTE_LIGHT[12:]:
        hue = colorsys.rgb_to_hls(*(int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)))[0] * 360
        assert not 35 <= hue <= 55, (color, hue)
    gold = WEEKLY_LIGHT_TOKENS["--gold"]
    assert gold not in TEAM_PALETTE_LIGHT and WEEKLY_DARK_TOKENS["--gold"] not in TEAM_PALETTE_DARK


@pytest.mark.parametrize("index", range(16))
def test_every_palette_color_meets_contrast_in_light_and_dark(index: int) -> None:
    """Text and avatar rings: 4.5:1 on the card in both themes; as a graphic (a chart line, an
    underline) 3:1 on every page surface."""
    light, dark = TEAM_PALETTE_LIGHT[index], TEAM_PALETTE_DARK[index]
    assert _contrast(light, WEEKLY_LIGHT_TOKENS["--card"]) >= 4.5, light
    assert _contrast(dark, WEEKLY_DARK_TOKENS["--card"]) >= 4.5, dark
    for surface in ("--paper", "--paper-2"):
        assert _contrast(light, WEEKLY_LIGHT_TOKENS[surface]) >= 3.0, (light, surface)
        assert _contrast(dark, WEEKLY_DARK_TOKENS[surface]) >= 3.0, (dark, surface)


def test_both_themes_carry_the_palette_tokens_and_the_page_uses_them() -> None:
    page = _page(_raw())
    css = page.split("<style>", 1)[1].split("</style>", 1)[0]
    for n, (light, dark) in enumerate(zip(TEAM_PALETTE_LIGHT, TEAM_PALETTE_DARK, strict=True)):
        assert f"--tm{n}: {light};" in css and f"--tm{n}: {dark};" in css
        assert f".tc{n} {{ --c: var(--tm{n}); }}" in css


# --------------------------------------------------------------------------- #
# A-1 -- monograms
# --------------------------------------------------------------------------- #


def test_hash_mark_herons_and_hail_mary_club_get_hh_and_hm() -> None:
    codes = wm.monogram_codes(["Hash-Mark Herons", "Hail Mary Club"])
    assert codes == {"Hash-Mark Herons": "HH", "Hail Mary Club": "HM"}
    # the order the teams arrive in does not change who keeps the contested code
    assert wm.monogram_codes(["Hail Mary Club", "Hash-Mark Herons"])["Hail Mary Club"] == "HM"


def test_midfield_magpies_and_motion_mallards_get_mi_and_mo() -> None:
    codes = wm.monogram_codes(["Midfield Magpies", "Motion Mallards"])
    assert codes == {"Midfield Magpies": "MI", "Motion Mallards": "MO"}


def test_the_approved_twelve_team_league_keeps_its_codes() -> None:
    names = [
        "Stiff-Arm Stags", "Route-Tree Roadrunners", "Trick-Play Turtles", "Hash-Mark Herons",
        "Overtime Otters", "Screen-Pass Seagulls", "Bubble-Screen Bobcats", "Neutral Zone",
        "Tight-End Terriers", "Motion Mallards", "Hail Mary Club", "Midfield Magpies",
    ]  # fmt: skip
    codes = wm.monogram_codes(names)
    assert len(set(codes.values())) == 12
    assert codes["Hash-Mark Herons"] == "HH" and codes["Hail Mary Club"] == "HM"
    assert codes["Motion Mallards"] == "MO" and codes["Midfield Magpies"] == "MI"
    assert codes["Stiff-Arm Stags"] == "SA" and codes["Neutral Zone"] == "NZ"


def test_codes_stay_unique_for_a_big_league_of_clashing_names() -> None:
    names = [f"{a} {b}" for a in ("Hail", "Hash", "Hot", "Heavy") for b in ("Mary", "Marks", "Moves", "Mice", "Mules")]
    names += [f"Solo{i}" for i in range(12)]
    codes = wm.monogram_codes(names)
    assert len(codes) == 32 and len(set(codes.values())) == 32
    assert all(len(code) == 2 for code in codes.values())
    assert codes == wm.monogram_codes(names)  # deterministic


def test_the_web_page_and_the_email_print_the_same_unique_codes() -> None:
    raw = _raw()
    facts = WeeklyFacts.model_validate(raw)
    labels = [wm.team_label(team) for team in facts.teams]
    codes = wm.monogram_codes(labels)
    assert len(set(codes.values())) == len(labels)
    web = _avatar_codes(_body(_page(raw)))
    assert web and set(web) <= set(codes.values())
    email = render_weekly_email(facts, render_weekly_issue(facts.narration), generated_at=_STAMP).html
    email_codes = re.findall(r'<td width="26" height="26"[^>]*>([A-Z0-9]{2})</td>', email)
    assert email_codes and set(email_codes) <= set(codes.values())


def test_two_teams_that_used_to_share_a_code_print_different_ones() -> None:
    raw = _raw()
    for team in raw["teams"]:
        if team["roster_id"] == "1":
            team["team_name"] = "Hash-Mark Herons"
        if team["roster_id"] == "2":
            team["team_name"] = "Hail Mary Club"
    for row in raw["narration"]["power"]:
        row["team"] = next(t["team_name"] for t in raw["teams"] if t["roster_id"] == row["roster_id"])
    page = _page(raw)
    codes = set(_avatar_codes(_body(page)))
    assert {"HH", "HM"} <= codes


# --------------------------------------------------------------------------- #
# A-1 -- colors assigned once and stored
# --------------------------------------------------------------------------- #


def test_a_team_color_is_assigned_once_and_never_changes(tmp_path: Path) -> None:
    store = FileStore(tmp_path)
    first = assign_team_colors(store, "L1", ["3", "1", "2"])
    assert first == {"3": 0, "1": 1, "2": 2}
    # a later week lists the teams in another order and adds a roster: old slots hold, the new one takes the next free
    later = assign_team_colors(store, "L1", ["1", "2", "9", "3"])
    assert later == {"1": 1, "2": 2, "9": 3, "3": 0}
    assert assign_team_colors(store, "L1", ["3", "1", "2"]) == first
    stored = store.read_cache(TEAM_COLORS_NAMESPACE, "L1")
    assert stored == {"slots": {"3": 0, "1": 1, "2": 2, "9": 3}}


def test_colors_are_keyed_on_the_roster_id_not_the_team_name() -> None:
    raw = _raw()
    facts = WeeklyFacts.model_validate(raw)
    slots = {team.roster_id: n for n, team in enumerate(facts.teams)}
    before = _render(raw, team_colors=slots)
    renamed = copy.deepcopy(raw)
    renamed["teams"][0]["team_name"] = "A Brand New Name"
    after = _render(renamed, team_colors=slots)
    first = renamed["teams"][0]["roster_id"]
    assert slots[first] == 0
    assert 'class="mg mg-l tc0"' in before and 'class="mg mg-l tc0"' in after


def test_an_unreadable_store_falls_back_by_roster_id_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class _Broken(FileStore):
        def read_cache(self, namespace: str, key: str) -> dict[str, Any] | None:
            raise StoreError("unreadable")

    with caplog.at_level(logging.WARNING):
        slots = assign_team_colors(_Broken(tmp_path), "L1", ["10", "2", "1"])
    assert slots == {"1": 0, "2": 1, "10": 2}  # numeric roster-id order
    assert "team colors are unreadable" in caplog.text


def test_nothing_stored_still_renders_the_same_colors_every_time() -> None:
    raw = _raw()
    assert _render(raw) == _render(raw)
    assert _render(raw) == _render(raw, team_colors=wm.default_team_colors(t["roster_id"] for t in raw["teams"]))


def test_slots_17_to_32_reuse_the_colors_with_the_dotted_style() -> None:
    assert wm.color_classes(0) == "tc0" and wm.color_classes(15) == "tc15"
    assert wm.color_classes(16) == "tc0 td" and wm.color_classes(31) == "tc15 td"
    assert wm.color_classes(None) == ""


# --------------------------------------------------------------------------- #
# A-1 / A-7 -- 14 and 32 teams
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("teams", [14, 32])
def test_big_leagues_render_with_unique_codes_and_overflow_safe_markup(teams: int) -> None:
    page = _render(scaled_raw(_raw(published=True), teams))
    body = _body(page)
    codes = _avatar_codes(body)
    assert len(set(codes)) == teams  # every team a distinct monogram
    css = page.split("<style>", 1)[1].split("</style>", 1)[0]
    # long names wrap instead of widening the page
    assert re.search(r"\.side \.tn \{[^}]*overflow-wrap: anywhere;", css)
    assert re.search(r"\.nw-side \.tn \{[^}]*overflow-wrap: anywhere;", css)
    assert "overflow-x: hidden" in css
    assert "grid-template-columns: minmax(0, 1fr);" in css  # the phone single column
    # fixed-width svgs scale down with the viewport
    assert "svg.chart { display: block; max-width: 100%; height: auto; }" in css
    chart_height = int(re.search(r'data-bump-chart.*?viewBox="0 0 900 (\d+)"', body, flags=re.DOTALL).group(1))  # type: ignore[union-attr]
    assert chart_height <= 800  # the row pitch shrinks for a big league
    if teams > wm.PALETTE_SIZE:
        assert body.count('class="bump-team tc') == teams
        assert body.count(" td\" data-team") == teams - wm.PALETTE_SIZE
    else:
        assert " td\" data-team" not in body


# --------------------------------------------------------------------------- #
# A-1 -- team names carry the underline in running text
# --------------------------------------------------------------------------- #


def test_team_names_in_prose_are_bold_and_underlined_in_their_color() -> None:
    raw = _raw()
    body = _body(_page(raw))
    lead = body.split('aria-label="The lead"', 1)[1].split("</section>", 1)[0]
    assert re.search(r'<b class="tm tc\d+">Kneel-Down Koalas</b>', lead)
    # the longest name wins where one name contains another
    # the longest name wins where one name holds another ("Koalas" inside "Kneel-Down Koalas")
    from commishdesk.render.weekly_web import _Ctx

    facts = WeeklyFacts.model_validate(raw)
    ctx = _Ctx(facts, render_weekly_issue(facts.narration))
    nested = ctx.text("Kneel-Down Koalas beat Yardage Yaks")
    assert nested.count('<b class="tm') == 2 and "Kneel-Down Koalas</b>" in nested


def test_a_team_name_never_breaks_the_markup_when_it_holds_html_or_an_apostrophe() -> None:
    raw = _raw()
    raw["teams"][0]["team_name"] = "O'Brien & <Sons>"
    for row in raw["narration"]["power"]:
        if row["roster_id"] == raw["teams"][0]["roster_id"]:
            row["team"] = "O'Brien & <Sons>"
    page = _render(raw)
    assert "<Sons>" not in page
    assert "O&#x27;Brien &amp; &lt;Sons&gt;" in page


# --------------------------------------------------------------------------- #
# A-2 -- lineup wording
# --------------------------------------------------------------------------- #


def test_the_lineup_loss_wording_never_says_the_bench_total() -> None:
    raw = _raw()
    body = _body(_page(raw))
    lead = body.split('aria-label="The lead"', 1)[1].split('class="awards"', 1)[0]
    assert "A perfect lineup adds" in lead
    assert "More than the 6.06-point loss." in lead
    assert "TreVeyon Henderson scored 51.66 on the bench." in lead
    assert "on the bench alone" not in lead and "Left on the bench" not in lead
    assert "A PERFECT LINEUP ADDS 108.86" in lead  # the chart label says the same
    hook = raw["lead_candidates"][0]["hook"]
    assert hook == "Kneel-Down Koalas lost by 6.06, and a perfect lineup adds 108.86, including TreVeyon Henderson."
    assert raw["narration"]["printed_headline"] == hook


def test_the_hook_without_a_bench_scorer_still_reads_cleanly() -> None:
    from commishdesk.facts import leads

    raw = _raw()
    facts = WeeklyFacts.model_validate(raw)
    teams = [
        team.model_copy(update={"this_week": team.this_week.model_copy(update={"bench_regret": None})})
        if team.this_week is not None
        else team
        for team in facts.teams
    ]
    candidate = leads.build_weekly_lead_candidates(teams, facts.period)[0]
    assert candidate.kind == "lineup_loss"
    assert candidate.hook == "Kneel-Down Koalas lost by 6.06, and a perfect lineup adds 108.86."


def test_the_perfect_lineup_sub_line_only_claims_the_loss_it_outweighs() -> None:
    assert wm.lineup_loss_sub(10.68, 7.39, "Tucker Norden", 13.87) == (
        "More than the 7.39-point loss. Tucker Norden scored 13.87 on the bench."
    )
    assert wm.lineup_loss_sub(5.0, 7.39, None, None) == ""
    assert wm.lineup_loss_sub(10.68, 7.39, None, None) == "More than the 7.39-point loss."


def test_the_email_says_a_perfect_lineup_adds() -> None:
    facts = WeeklyFacts.model_validate(_raw())
    parts = render_weekly_email(facts, render_weekly_issue(facts.narration), generated_at=_STAMP)
    assert "a perfect lineup adds 108.86" in parts.text
    assert "left on bench" not in parts.text and "On the bench" not in parts.html


# --------------------------------------------------------------------------- #
# A-2 / A-3 -- layout
# --------------------------------------------------------------------------- #


def test_the_lead_card_is_left_and_the_key_number_and_awards_share_one_right_column() -> None:
    body = _body(_page(_raw()))
    lead = body.split('aria-label="The lead"', 1)[1].split("</section>", 1)[0]
    assert lead.index('class="card lead-main"') < lead.index('class="lead-side"')
    side = lead.split('class="lead-side"', 1)[1]
    assert side.index('class="keynum') < side.index('class="awards"')
    assert "teamcard" not in body  # the separate team card is gone
    for kind in ("aw-good", "aw-emph", "aw-bad"):
        assert f'class="award {kind}"' in side
    # each award: the title, the team's avatar, a name and one line, the stat on the right
    assert re.search(
        r'class="award aw-good"><div class="k">Coach of the week</div><div class="aw-body"><span class="mg', side
    )
    assert '<div class="v">99.2%</div>' in side


def test_every_lead_kind_keeps_a_neutral_key_number_card() -> None:
    for kind in ("lineup_loss", "biggest_blowout", "closest_game", "week_high_score"):
        raw = _raw()
        raw["lead_candidates"] = [dict(c, rank=1) for c in raw["lead_candidates"] if c["kind"] == kind]
        body = _body(_page(raw))
        assert 'class="keynum' in body and "teamcard" not in body, kind


def test_results_have_one_toggle_and_every_detail_visible_without_it() -> None:
    page = _page(_raw())
    body = _body(page)
    around = body.split('aria-label="Around the league"', 1)[1].split("</section>", 1)[0]
    assert around.count("data-detail-all") == 1
    assert 'class="card-expand"' not in around  # no per-card buttons
    games = re.findall(r'<article class="card game">', around)
    assert len(games) == around.count('class="game-detail"') == len(games) > 0
    css = page.split("<style>", 1)[1].split("</style>", 1)[0]
    assert re.search(r"\.game-detail,\s*\.nw-detail \{[^}]*display: block;", css)  # in the flow, visible
    assert "position: absolute" not in re.search(r"\.game-detail,\s*\.nw-detail \{[^}]*\}", css).group(0)  # type: ignore[union-attr]
    assert re.search(r"\.detail-all \{[^}]*display: none;", css)  # the toggle waits for the interaction layer


def test_results_rows_share_one_grid_and_cards_are_as_tall_as_their_row() -> None:
    css = _page(_raw()).split("<style>", 1)[1].split("</style>", 1)[0]
    assert "grid-template-columns: 36px minmax(0, 1fr) 96px;" in css
    assert "-webkit-line-clamp: 2;" in css
    assert re.search(r"\.grid3 \{[^}]*grid-auto-rows: 1fr;[^}]*align-items: stretch;", css)


def test_a_tied_game_row_and_record_print_without_a_winner_highlight() -> None:
    raw = _raw()
    game = raw["matchups"]["this_week"][0]
    game["winner_roster_id"] = None
    game["margin"] = 0
    for team in raw["teams"]:
        if team["roster_id"] in (game["home_roster_id"], game["away_roster_id"]):
            team["season"]["record"]["t"] = 1
    page = _page(raw)
    body = _body(page)
    around = body.split('aria-label="Around the league"', 1)[1].split("</section>", 1)[0]
    assert "Tied" in around
    standings = body.split('aria-label="Standings"', 1)[1].split("</section>", 1)[0]
    # a tie shows as W-L-T (and only a tie does)
    assert re.search(r'<span class="rec[^"]*">\d+-\d+-1</span>', standings) or re.search(
        r'<span class="rec-act"[^>]*>\d+-\d+-1</span>', standings
    )
    assert re.search(r'class="rec[^"]*">\d+-\d+</span>|class="rec-act"[^>]*>\d+-\d+</span>', standings)


# --------------------------------------------------------------------------- #
# A-4 -- next week
# --------------------------------------------------------------------------- #


def _next_week_cards(page: str) -> list[str]:
    section = _body(page).split('aria-label="Next week"', 1)[1].split("</section>", 1)[0]
    return re.findall(r'<article class="card nw[^"]*">(.*?)</article>', section)


def test_the_game_of_the_week_has_a_gold_border_and_nothing_else() -> None:
    css = _page(_raw()).split("<style>", 1)[1].split("</style>", 1)[0]
    block = re.search(r"\.nw\.gotw \{([^}]*)\}", css).group(1)  # type: ignore[union-attr]
    assert block.strip() == "border: 2px solid var(--gold);"
    assert "--emph" not in block and "box-shadow" not in block and "background" not in block


def test_an_eliminated_team_wears_out_of_the_race_under_its_record() -> None:
    raw = _raw()
    card = raw["matchups"]["next_week"][1]
    card["b_eliminated"] = True
    card["stakes"] = [*card["stakes"], "draft_position"]
    card["bye_impact"] = [
        {"roster_id": card["b_roster_id"], "player_id": "1", "name": "Some Player", "pos": "WR", "nfl_team": "NO"}
    ]
    page = _page(raw)
    cards = _next_week_cards(page)
    mine = cards[1]
    assert mine.count("Out of the race") == 1
    # under that team's record, in its own side
    b_side = mine.split('class="vs"', 1)[1]
    assert "Out of the race" in b_side and "Out of the race" not in mine.split('class="vs"', 1)[0]
    assert "Draft position" not in mine
    assert 'class="card-expand"' not in mine and "nw-detail" not in mine
    assert sum("Out of the race" in c for c in cards) == 1
    assert "Draft position" not in _body(page)


def test_both_teams_out_get_the_tag_on_their_own_side() -> None:
    raw = _raw()
    card = raw["matchups"]["next_week"][2]
    card["a_eliminated"] = card["b_eliminated"] = True
    cards = _next_week_cards(_page(raw))
    assert cards[2].count("Out of the race") == 2


def test_next_week_cards_reserve_their_header_row_and_records_drop_a_zero_tie() -> None:
    raw = _raw()
    cards = _next_week_cards(_page(raw))
    assert cards and all('<div class="nw-chips">' in card for card in cards)
    assert "7-3-0" not in "".join(cards) and "7-3" in "".join(cards)
    assert wm.display_record("7-4-0") == "7-4" and wm.display_record("7-4-1") == "7-4-1"
    assert wm.display_record("not a record") == "not a record"


# --------------------------------------------------------------------------- #
# A-5 -- power rankings
# --------------------------------------------------------------------------- #


def test_the_power_table_has_one_header_and_the_narrators_notes_sit_under_their_rows() -> None:
    raw = _raw()
    page = _page(raw)
    section = _body(page).split('aria-label="Power rankings"', 1)[1].split("</section>", 1)[0]
    assert section.count('class="pw-head"') == 1
    assert "Power score" in section and "Model score" not in section
    rows = re.findall(r'<div class="pw-item">.*?</div>(?:<p class="pw-why">.*?</p>)?', section)
    assert len(rows) == 12 and all('class="pw-why"' in row for row in rows)
    # no second numbered list after the table
    tail = section.split('class="power"', 1)[1]
    assert not re.search(r'<div class="prose"><p>1\. ', tail)
    # the score is a bar plus its number, 0 to 100
    score = (
        r'<span class="ps"><span class="bar thin" aria-hidden="true">'
        r'<i style="width:[\d.]+%"></i></span><b>85\.3</b>'
    )
    assert re.search(score, section)


def test_the_power_chart_colors_every_line_and_labels_its_end() -> None:
    raw = _raw(published=True)
    body = _body(_page(raw))
    chart = body.split("data-bump-chart", 1)[1].split("</svg>", 1)[0]
    assert len(re.findall(r'<g class="bump-team tc\d+"', chart)) == 12
    assert len(re.findall(r'<text class="bump-endlabel"', chart)) == 12
    assert 'class="bump-key"' in body and "Solid line" in body and "Dashed line" in body
    css = _page(raw).split("<style>", 1)[1].split("</style>", 1)[0]
    assert "stroke: var(--c, var(--ink-3));" in css  # the line takes the team's color
    assert re.search(r"\.bump-detail \{[^}]*height: 140px;", css)  # fixed height, so the buttons never move
    assert re.search(r"\.bump-team\.is-dim \{[^}]*opacity: \.14;", css)


def test_the_chart_is_stretched_for_a_twelve_team_league() -> None:
    body = _body(_page(_raw(published=True)))
    height = int(re.search(r'data-bump-chart.*?viewBox="0 0 900 (\d+)"', body, flags=re.DOTALL).group(1))  # type: ignore[union-attr]
    assert 650 <= height <= 760


def test_power_history_labels_the_current_week_correctly_in_a_playoff_week() -> None:
    """epic-5b-retro-item-1: the last history row carries a model rank only in the regular
    season, so in a later week the current week is its own point, never spliced onto a stale week."""
    facts = WeeklyFacts.model_validate(_raw(published=True))
    team = next(t for t in facts.teams if t.roster_id == "4")
    row = next(r for r in wm.power_rows(facts) if r.team.roster_id == "4")
    # the week being rendered is 11, but history stops at week 10
    points = wm.power_history(team, row, 11)
    assert [p.week for p in points][-2:] == [10, 11]
    assert points[-1].published == row.published and points[-1].reason == row.reason
    assert points[-2].reason is None  # the old week keeps its own data
    # at the rendered week the splice is the old behaviour
    same = wm.power_history(team, row, 10)
    assert same[-1].week == 10 and same[-1].published == row.published
    assert wm.power_history(team, row) == same
    # no model rank for the new week: plot what exists, never a stale relabel
    unranked = row._replace(model=None)
    assert [p.week for p in wm.power_history(team, unranked, 11)][-1] == 10


# --------------------------------------------------------------------------- #
# A-6 / A-7 -- luck, spacing, transactions
# --------------------------------------------------------------------------- #


def test_the_luck_graphic_is_left_and_the_words_right() -> None:
    body = _body(_page(_raw()))
    luck = body.split('aria-label="The luck index"', 1)[1].split("</section>", 1)[0]
    assert luck.index('class="luck-fig"') < luck.index('<div class="prose')
    css = _page(_raw()).split("<style>", 1)[1].split("</style>", 1)[0]
    assert re.search(r"\.luck-grid \{[^}]*grid-template-columns: minmax\(0, 1\.35fr\) minmax\(0, 1fr\);", css)
    stacked = r"@media \(max-width: 900px\) \{[^@]*\.luck-grid \{ grid-template-columns: minmax\(0, 1fr\); \}"
    assert re.search(stacked, css)


def test_spacing_rounding_and_full_width_blurbs() -> None:
    css = _page(_raw()).split("<style>", 1)[1].split("</style>", 1)[0]
    assert re.search(r"\.paper \{[^}]*gap: 30px;", css)
    assert ".lead-main, .table-card, .luck-card, .tx { border-radius: 10px; }" in css
    assert re.search(r"\.card \{[^}]*border-radius: 14px;", css)
    assert "section > .prose p, .luck-grid > .prose p { max-width: none; }" in css


def test_a_trade_takes_its_own_row_so_its_two_sides_do_not_wrap() -> None:
    raw = _raw()
    assert raw["transactions"]["recent_trades"], "the week-10 fixture carries a trade"
    body = _body(_page(raw))
    trade = re.search(r'<article class="card tx tx-trade">(.*?)</article>', body, flags=re.DOTALL)
    assert trade is not None and 'class="trade"' in trade.group(1)
    css = _page(raw).split("<style>", 1)[1].split("</style>", 1)[0]
    assert ".tx-trade { grid-column: 1 / -1; }" in css
    assert re.search(r"\.trade \{[^}]*align-items: start;", css)  # both sides start on one line
    waiver = re.search(r'<article class="card tx">', body)
    assert waiver is not None  # the waiver cards stay in the grid


# --------------------------------------------------------------------------- #
# A-9 -- section ids
# --------------------------------------------------------------------------- #


def test_every_section_has_its_permanent_id_once() -> None:
    from commishdesk.sections import SECTION_IDS

    body = _body(_page(_raw()))
    for section_id in SECTION_IDS:
        assert body.count(f'<section id="{section_id}"') == 1, section_id
    # the ids appear in the page's section order
    positions = [body.index(f'<section id="{section_id}"') for section_id in SECTION_IDS]
    assert positions == sorted(positions)


def test_a_cold_start_page_carries_only_the_ids_of_its_four_sections() -> None:
    from commishdesk.sections import COLD_START_SECTION_IDS, SECTION_IDS

    raw = json.loads((FIXTURE_DIR / "facts" / "expected-weekly-facts-week01.json").read_text(encoding="utf-8"))
    body = _body(_page(raw))
    for section_id in SECTION_IDS:
        assert (f'<section id="{section_id}"' in body) == (section_id in COLD_START_SECTION_IDS), section_id


# --------------------------------------------------------------------------- #
# The page stays script-free and under the same CSP
# --------------------------------------------------------------------------- #


def test_the_redesigned_page_has_no_script_no_subresource_and_no_handler() -> None:
    from commishdesk.render.enhancer import validate_page

    for raw in (_raw(), _raw(published=True), scaled_raw(_raw(published=True), 32)):
        page = _render(raw)
        validate_page(page, enhanced=False)
        assert "<script" not in page and "js-reveal" not in page
        assert not re.search(r"\burl\((?!data:font/woff2;base64,)", page)


# --------------------------------------------------------------------------- #
# Item 96 -- a tied game names both teams
# --------------------------------------------------------------------------- #


def test_a_tied_game_names_both_teams_in_the_narration_projection() -> None:
    bundle = json.loads((FIXTURE_DIR / "week10-blowout.json").read_text(encoding="utf-8"))
    rows = [m for m in bundle["matchups"]["10"]]
    pair = [m for m in rows if m["matchup_id"] == rows[0]["matchup_id"]]
    assert len(pair) == 2
    for m in pair:
        m["points"] = 100.0
    week, league = build_week_model(bundle), build_league_model(bundle)
    facts = build_weekly_facts(
        week, league, build_player_snapshot(bundle), build_player_names(bundle), generated_at=_STAMP
    )
    names = {t.roster_id: (t.team_name or t.manager) for t in facts.teams}
    tied = [g for g in facts.narration.games if g.winner is None]
    assert len(tied) == 1
    game = tied[0]
    sides = {str(m["roster_id"]) for m in pair}
    for roster_id in sides:
        assert names[roster_id] in (game.loser or "")
    assert " and " in (game.loser or "")
    text = render_weekly_issue(facts.narration)
    line = next(b for s in text.sections for b in s.blocks if "finished level" in b)
    assert all(names[roster_id] in line for roster_id in sides)


def test_the_narrated_record_drops_a_zero_tie_and_keeps_a_real_one() -> None:
    from commishdesk.facts.weekly import _rec_str

    assert _rec_str(7, 4, 0) == "7-4" and _rec_str(7, 4, 1) == "7-4-1"


# --------------------------------------------------------------------------- #
# A-9 -- the narration replay seam
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ANTHROPIC_API_KEY", "LLM_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _fresh_build(tmp_path: Path) -> tuple[FileStore, Any]:
    store = FileStore(tmp_path)
    built = build_weekly_issue(store, "L1", 17, adapter=_Adapter(), suppressions=set())
    record_published_weekly(store, "L1", 17, built.facts_json, built.published_ranks, built.nudge_justifications)
    return store, built


def test_a_replay_renders_the_same_page_and_email_as_the_fresh_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, fresh = _fresh_build(tmp_path)
    record = json.loads(json.dumps(narration_record(fresh)))  # through JSON, as it is stored
    storylines_before = store.read_storylines("L1")

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("a replay must not narrate or touch narrative memory")

    monkeypatch.setattr("commishdesk.weekly.produce_weekly_issue", _boom)
    monkeypatch.setattr("commishdesk.weekly.persist_weekly_storylines", _boom)
    monkeypatch.setattr("commishdesk.weekly.weekly_llm_selection", _boom)

    replayed = build_weekly_issue(store, "L1", 17, adapter=_Adapter(), suppressions=set(), replay=record)
    assert replayed.web_html == fresh.web_html
    assert replayed.email == fresh.email
    assert replayed.issue == fresh.issue
    assert replayed.published_ranks == fresh.published_ranks
    assert replayed.nudge_justifications == fresh.nudge_justifications
    assert store.read_storylines("L1") == storylines_before


def test_a_replay_keeps_the_stored_prose_even_when_the_facts_are_recomputed(tmp_path: Path) -> None:
    """With no Facts snapshot the Facts are rebuilt; the prose is still exactly the stored text."""
    _store, fresh = _fresh_build(tmp_path / "first")
    record = narration_record(fresh)
    other = FileStore(tmp_path / "second")  # a store that never saw the first run
    replayed = build_weekly_issue(other, "L1", 17, adapter=_Adapter(), suppressions=set(), replay=record)
    assert replayed.issue == fresh.issue
    lead = next(s for s in fresh.issue.sections if s.section_id == "lead")
    assert _plain(lead.blocks[-1]).split(".")[0][:30] in _plain(replayed.web_html)


def test_a_replay_with_no_stored_narration_is_an_error_not_a_paid_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spent = []
    monkeypatch.setattr("commishdesk.weekly.produce_weekly_issue", lambda *a, **k: spent.append(1))
    for bad in ({}, {"version": 99}, {"version": 1, "issue": {"title": "x"}}, {"version": 1, "issue": None}):
        with pytest.raises(CommishDeskError):
            build_weekly_issue(FileStore(tmp_path), "L1", 17, adapter=_Adapter(), suppressions=set(), replay=bad)
    assert spent == []
    with pytest.raises(CommishDeskError, match="stored narration"):
        parse_narration_record({})


def test_the_narration_record_round_trips_and_is_json_safe(tmp_path: Path) -> None:
    _store, fresh = _fresh_build(tmp_path)
    record = narration_record(fresh)
    assert record["version"] == 1 and record["week"] == 17 and record["league_id"] == "L1"
    issue, ranks, justifications, reverted = parse_narration_record(json.loads(json.dumps(record)))
    assert issue == fresh.issue and ranks == fresh.published_ranks
    assert justifications == fresh.nudge_justifications and reverted == fresh.l4_reverted


def test_the_hosted_build_stores_team_colors_with_the_league(tmp_path: Path) -> None:
    store, built = _fresh_build(tmp_path)
    stored = store.read_cache(TEAM_COLORS_NAMESPACE, "L1")
    assert stored is not None
    slots = stored["slots"]
    assert set(slots) == {team.roster_id for team in built.doc.teams}
    assert len(set(slots.values())) == len(slots)
    build_weekly_issue(store, "L1", 17, adapter=_Adapter(), suppressions=set())
    assert store.read_cache(TEAM_COLORS_NAMESPACE, "L1") == stored  # never re-assigned


def test_the_cli_keeps_the_narration_beside_the_page_and_replays_it_without_narrating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from commishdesk.cli import app
    from tests.test_cli_weekly import _WEEK10, _load_fixture, _stub_adapter

    runner = CliRunner()
    _stub_adapter(monkeypatch, week_bundle=_load_fixture(_WEEK10), nfl_state={"week": 13, "season_type": "regular"})
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    first, second = tmp_path / "first", tmp_path / "second"

    result = runner.invoke(app, ["--league", "111", "--week", "10", "--out-dir", str(first)])
    assert result.exit_code == 0, result.output
    narration = first / "commishdesk-111-weekly-week10.narration.json"
    record = json.loads(narration.read_text(encoding="utf-8"))
    assert record["version"] == 1 and record["issue"]["sections"]
    assert str(narration) in result.output

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("a replay must not narrate")

    monkeypatch.setattr("commishdesk.cli._produce_weekly_issue", _boom)
    monkeypatch.setattr("commishdesk.cli.persist_weekly_storylines", _boom)
    result = runner.invoke(
        app,
        ["--league", "111", "--week", "10", "--out-dir", str(second), "--replay-narration", str(narration)],
    )
    assert result.exit_code == 0, result.output
    page = "commishdesk-111-weekly-week10.html"
    stamp = re.compile(r"generated [0-9T:.\-]+Z?")
    assert stamp.sub("", (first / page).read_text(encoding="utf-8")) == stamp.sub(
        "", (second / page).read_text(encoding="utf-8")
    )
    assert not (second / "commishdesk-111-weekly-week10.narration.json").exists()  # nothing new to keep


def test_the_cli_refuses_a_replay_that_is_not_a_plain_local_weekly_run(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from commishdesk.cli import app

    runner = CliRunner()
    path = tmp_path / "n.json"
    path.write_text("{}", encoding="utf-8")
    for args in (
        ["--league", "1", "--replay-narration", str(path)],
        ["--league", "1", "--week", "3", "--post", "--replay-narration", str(path)],
        ["--league", "1", "--week", "3", "--reason", "x", "--replay-narration", str(path)],
        ["--league", "1", "--draft-recap", "--replay-narration", str(path)],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code != 0 and "replay-narration" in result.output


def test_a_replay_refuses_a_narration_stored_for_another_league_or_week() -> None:
    record = {"version": 1, "league_id": "111", "week": 10}
    with pytest.raises(CommishDeskError, match="league"):
        parse_narration_record(record, league_id="222", week=10)
    with pytest.raises(CommishDeskError, match="week"):
        parse_narration_record(record, league_id="111", week=9)
