"""The weekly template narrator — ``commishdesk/narrate/weekly_template.py``.

One test per row of the story's I/O & Edge-Case Matrix, plus the structural
guards the Boundaries section calls for: the AD-1 import fence (stdlib +
``commishdesk.facts.schema`` only), "no empty section heading", the three
distinct game sentence families, determinism, and the content-safety gate
(``check_narration``) over the rendered text.

The week-10 oracle is the real pipeline off the committed ``week10-blowout.json``
fixture; ``week02-nailbiter.json`` supplies the one-point-game case.
"""

from __future__ import annotations

import ast
import json

import pytest

from commishdesk.facts import WeeklyFacts, build_weekly_facts
from commishdesk.facts.schema import (
    WeeklyNarration,
    WeeklyNarrationGame,
    WeeklyNarrationLeague,
    WeeklyNarrationLuck,
    WeeklyNarrationNextWeek,
    WeeklyNarrationPlayoff,
    WeeklyNarrationPower,
    WeeklyNarrationStanding,
    WeeklyNarrationTransactions,
)
from commishdesk.ingest import (
    build_league_model,
    build_player_names,
    build_player_snapshot,
    build_week_model,
)
from commishdesk.narrate import weekly_template
from commishdesk.narrate.safety import check_narration
from commishdesk.narrate.weekly_template import (
    WeeklyIssue,
    WeeklySection,
    render_weekly_issue,
    weekly_issue_to_text,
)
from tests.conftest import REPO_ROOT

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
WEEKLY_TEMPLATE = REPO_ROOT / "commishdesk" / "narrate" / "weekly_template.py"

GENERATED_AT = "2026-09-07T00:00:00Z"
BYES_NEXT_WEEK = frozenset({"IND", "NO"})

#: The seven reference sections, in order — ``week10-reference-newsletter.md``'s
#: section set at the template narrator's level of prose.
SECTION_HEADINGS = [
    "The Lead",
    "Around the League",
    "Standings and the Playoff Picture",
    "Power Rankings",
    "The Luck Index",
    "Next Week",
    "The Transaction Desk",
]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _facts(fixture: str, *, nfl_byes_next_week: frozenset[str] | None = None) -> WeeklyFacts:
    """The real weekly pipeline off a committed bundle — the same chain the
    weekly CLI path (Story 5.11a) will run."""
    bundle = json.loads((FIXTURE_DIR / fixture).read_text(encoding="utf-8"))
    return build_weekly_facts(
        build_week_model(bundle),
        build_league_model(bundle),
        build_player_snapshot(bundle),
        build_player_names(bundle),
        generated_at=GENERATED_AT,
        nfl_byes_next_week=nfl_byes_next_week,
    )


@pytest.fixture(scope="module")
def week10() -> WeeklyNarration:
    return _facts("week10-blowout.json", nfl_byes_next_week=BYES_NEXT_WEEK).narration


@pytest.fixture(scope="module")
def nailbiter() -> WeeklyNarration:
    return _facts("week02-nailbiter.json").narration


def _section(issue: WeeklyIssue, heading: str) -> WeeklySection:
    return next(section for section in issue.sections if section.heading == heading)


def _narration(
    *,
    games: list[WeeklyNarrationGame] | None = None,
    standings: list[WeeklyNarrationStanding] | None = None,
    power: list[WeeklyNarrationPower] | None = None,
    luck: list[WeeklyNarrationLuck] | None = None,
    next_week: list[WeeklyNarrationNextWeek] | None = None,
    byes: list[str] | None = None,
    playoff: WeeklyNarrationPlayoff | None = None,
    transactions: WeeklyNarrationTransactions | None = None,
) -> WeeklyNarration:
    """A hand-built weekly narration — the matrix's stand-down and sentence-family
    rows share it."""
    return WeeklyNarration(
        league=WeeklyNarrationLeague(name="Test League", season="2025", week=5, team_count=4),
        week_shape="regular",
        games=games or [],
        standings=standings or [],
        playoff_picture=playoff,
        power=power or [],
        luck=luck or [],
        next_week=next_week or [],
        next_week_nfl_byes=byes,
        transactions=transactions or WeeklyNarrationTransactions(),
        lead_candidates=[],
        storyline_candidates=[],
    )


def _game(winner: str, loser: str, winner_pts: float, loser_pts: float) -> WeeklyNarrationGame:
    return WeeklyNarrationGame(
        matchup_id=1,
        winner=winner,
        winner_pts=winner_pts,
        loser=loser,
        loser_pts=loser_pts,
        margin=round(abs(winner_pts - loser_pts), 2),
        top=None,
    )


# --------------------------------------------------------------------------- #
# Matrix: every reference section renders non-empty
# --------------------------------------------------------------------------- #


def test_emits_all_seven_reference_sections(week10: WeeklyNarration) -> None:
    issue = render_weekly_issue(week10)
    assert isinstance(issue, WeeklyIssue)
    assert [section.heading for section in issue.sections] == SECTION_HEADINGS
    assert all(section.blocks for section in issue.sections)
    assert week10.league.name in issue.title
    assert week10.league.name in issue.dateline


def test_no_section_heading_is_ever_empty(week10: WeeklyNarration) -> None:
    """The stand-down AC: a section with nothing to report renders one deliberate
    line, never a heading over nothing."""
    for section in render_weekly_issue(week10).sections:
        assert section.heading.strip()
        assert section.blocks
        assert all(block.strip() for block in section.blocks)


def test_every_section_stands_down_rather_than_render_nothing() -> None:
    """The degenerate narration: no games, no playoffs, no luck, no power, no
    schedule, no moves. Every one of the seven sections still carries a block."""
    issue = render_weekly_issue(_narration())
    assert [section.heading for section in issue.sections] == SECTION_HEADINGS
    assert all(section.blocks for section in issue.sections)


def test_a_section_with_nothing_to_report_renders_its_stand_down_line() -> None:
    issue = render_weekly_issue(_narration())
    assert _section(issue, "Around the League").blocks == [weekly_template._NO_GAMES]
    assert _section(issue, "Power Rankings").blocks == [weekly_template._NO_POWER]
    assert _section(issue, "Next Week").blocks == [weekly_template._NO_SCHEDULE]
    assert _section(issue, "Standings and the Playoff Picture").blocks == [
        weekly_template._NO_STANDINGS,
        weekly_template._NO_PLAYOFFS,
    ]


# --------------------------------------------------------------------------- #
# Matrix: sentence families (blowout / one-point / ordinary)
# --------------------------------------------------------------------------- #


def test_each_scoreline_picks_a_different_sentence_family() -> None:
    blowout = _game("Alpha", "Beta", 150.0, 60.0)
    close = _game("Gamma", "Delta", 101.0, 100.0)
    ordinary = _game("Epsilon", "Zeta", 120.0, 105.0)
    assert {
        weekly_template._game_family(blowout),
        weekly_template._game_family(close),
        weekly_template._game_family(ordinary),
    } == {"blowout", "close", "ordinary"}

    section = _section(render_weekly_issue(_narration(games=[blowout, close, ordinary])), "Around the League")
    blocks = section.blocks
    assert len(blocks) == 3
    assert blocks[0].startswith("Alpha") and "blew past" in blocks[0]
    assert blocks[1].startswith("Gamma") and "edged" in blocks[1]
    assert blocks[2].startswith("Epsilon") and "beat" in blocks[2]
    # the three families are mutually exclusive wordings
    assert "blew past" not in blocks[1] and "blew past" not in blocks[2]
    assert "edged" not in blocks[0] and "edged" not in blocks[2]
    assert "beat" not in blocks[0] and "beat" not in blocks[1]


def test_the_blowout_week_renders_a_blowout_game(week10: WeeklyNarration) -> None:
    blocks = _section(render_weekly_issue(week10), "Around the League").blocks
    assert any("blew past" in block for block in blocks)


def test_the_nailbiter_week_renders_a_close_game(nailbiter: WeeklyNarration) -> None:
    assert nailbiter.games
    closest = min(nailbiter.games, key=lambda game: game.margin)
    assert closest.margin <= weekly_template._CLOSE_GAME_MARGIN  # a nailbiter sits inside the close band
    blocks = _section(render_weekly_issue(nailbiter), "Around the League").blocks
    assert any("edged" in block for block in blocks)


def test_a_tie_never_renders_the_word_none() -> None:
    tie = WeeklyNarrationGame(
        matchup_id=3,
        winner=None,
        winner_pts=99.5,
        loser="Home Team",
        loser_pts=99.5,
        margin=0.0,
        top=None,
    )
    assert weekly_template._game_family(tie) == "tie"
    text = weekly_issue_to_text(render_weekly_issue(_narration(games=[tie])))
    assert "None" not in text
    assert "Home Team" in text


# --------------------------------------------------------------------------- #
# Matrix: around the league / lead
# --------------------------------------------------------------------------- #


def test_around_the_league_renders_every_game(week10: WeeklyNarration) -> None:
    section = _section(render_weekly_issue(week10), "Around the League")
    assert len(week10.games) == 6
    for game in week10.games:
        matching = [block for block in section.blocks if game.winner and block.startswith(game.winner)]
        assert matching, game
        assert any(game.loser and game.loser in block for block in matching)


def test_lead_carries_the_ranked_hooks(week10: WeeklyNarration) -> None:
    section = _section(render_weekly_issue(week10), "The Lead")
    assert week10.lead_candidates
    for candidate in week10.lead_candidates:
        if candidate.hook:
            assert candidate.hook in section.blocks


def test_december_style_storyline_hooks_render_in_around_the_league(week10: WeeklyNarration) -> None:
    section = _section(render_weekly_issue(week10), "Around the League")
    lead_hooks = {candidate.hook for candidate in week10.lead_candidates if candidate.hook}
    shown = [c.hook for c in week10.storyline_candidates if c.hook and c.hook not in lead_hooks]
    for hook in shown:
        assert hook in section.blocks


# --------------------------------------------------------------------------- #
# Matrix: standings + playoff picture
# --------------------------------------------------------------------------- #


def test_standings_list_every_roster(week10: WeeklyNarration) -> None:
    section = _section(render_weekly_issue(week10), "Standings and the Playoff Picture")
    assert week10.standings
    for row in week10.standings:
        assert any(block.startswith(f"{row.rank}. {row.team}") for block in section.blocks)


def test_playoff_picture_renders_for_the_week10_league(week10: WeeklyNarration) -> None:
    picture = week10.playoff_picture
    assert picture is not None
    blob = " ".join(_section(render_weekly_issue(week10), "Standings and the Playoff Picture").blocks)
    assert picture.format in blob
    for label in [*picture.byes, *picture.in_bracket]:
        assert label in blob


def test_playoff_block_stands_down_without_a_picture(nailbiter: WeeklyNarration) -> None:
    issue = render_weekly_issue(_narration())
    assert issue.sections  # the narration still renders
    assert _section(issue, "Standings and the Playoff Picture").blocks[-1] == weekly_template._NO_PLAYOFFS


def _cold_start_narration() -> WeeklyNarration:
    """The real cold-start shape (week 1, before the model has rank data):
    every ``model_rank`` is ``None``, on both ``power[]`` and ``standings[]``."""
    from tests.test_facts_weekly import _synthetic

    week_model, league, players, names = _synthetic(12, week=1, playoff_week_start=10)
    doc = build_weekly_facts(week_model, league, players, names, generated_at=GENERATED_AT)
    assert doc.narration.power and all(row.model_rank is None for row in doc.narration.power)
    assert doc.narration.standings and all(row.model_rank is None for row in doc.narration.standings)
    return doc.narration


def test_standings_omit_the_model_rank_clause_at_cold_start() -> None:
    narration = _cold_start_narration()
    section = _section(render_weekly_issue(narration), "Standings and the Playoff Picture")
    for row in narration.standings:
        line = next(block for block in section.blocks if block.startswith(f"{row.rank}. {row.team}"))
        assert "model rank" not in line


def test_power_rankings_render_unranked_at_cold_start() -> None:
    narration = _cold_start_narration()
    section = _section(render_weekly_issue(narration), "Power Rankings")
    assert section.blocks
    for row in narration.power:
        assert any(block.startswith(row.team) and block.endswith("unranked.") for block in section.blocks)


# --------------------------------------------------------------------------- #
# Matrix: empty luck list
# --------------------------------------------------------------------------- #


def test_luck_section_stands_down_on_an_empty_list() -> None:
    assert _section(render_weekly_issue(_narration()), "The Luck Index").blocks == [weekly_template._NO_LUCK]


def test_luck_section_stands_down_below_the_games_played_minimum() -> None:
    """The real cold-start shape: one luck row per roster, every value ``None``."""
    from tests.test_facts_weekly import _synthetic

    week_model, league, players, names = _synthetic(12, week=1, playoff_week_start=10)
    doc = build_weekly_facts(week_model, league, players, names, generated_at=GENERATED_AT)
    assert doc.narration.luck  # a row per roster...
    assert all(row.luck is None for row in doc.narration.luck)  # ...none of them measurable
    section = _section(render_weekly_issue(doc.narration), "The Luck Index")
    assert section.blocks == [weekly_template._NO_LUCK]


def test_luck_index_names_every_measurable_roster(week10: WeeklyNarration) -> None:
    section = _section(render_weekly_issue(week10), "The Luck Index")
    measurable = [row for row in week10.luck if row.luck is not None]
    assert measurable
    assert len(section.blocks) == len(measurable)
    for row in measurable:
        assert any(block.startswith(f"{row.team} —") for block in section.blocks)


# --------------------------------------------------------------------------- #
# Matrix: byes next week
# --------------------------------------------------------------------------- #


def test_next_week_names_the_bye_teams(week10: WeeklyNarration) -> None:
    assert week10.next_week_nfl_byes == ["IND", "NO"]
    blob = " ".join(_section(render_weekly_issue(week10), "Next Week").blocks)
    for team in week10.next_week_nfl_byes:
        assert team in blob


def test_next_week_omits_the_bye_line_when_no_bye_data_was_supplied(week10: WeeklyNarration) -> None:
    no_byes = _facts("week10-blowout.json").narration
    assert no_byes.next_week_nfl_byes is None
    section = _section(render_weekly_issue(no_byes), "Next Week")
    assert all("On bye next week" not in block for block in section.blocks)


def test_next_week_names_the_bye_teams_even_with_no_schedule_yet() -> None:
    """Bye data must not be lost just because the fantasy schedule is empty
    (end of season, or a reduction-ladder cut) -- the two are independent."""
    narration = _narration(byes=["IND", "NO"])
    assert not narration.next_week
    section = _section(render_weekly_issue(narration), "Next Week")
    blob = " ".join(section.blocks)
    assert "IND" in blob and "NO" in blob
    assert weekly_template._NO_SCHEDULE not in section.blocks


def test_next_week_card_names_power_ranks_and_the_game_of_the_week(week10: WeeklyNarration) -> None:
    ranked = [card for card in week10.next_week if card.a_model_rank is not None and card.b_model_rank is not None]
    assert ranked
    featured = next(card for card in week10.next_week if card.game_of_week)
    blob = " ".join(_section(render_weekly_issue(week10), "Next Week").blocks)
    for card in ranked:
        assert f"power ranks {card.a_model_rank} and {card.b_model_rank}" in blob
    assert "The game of the week." in blob
    featured_line = next(
        block for block in _section(render_weekly_issue(week10), "Next Week").blocks if featured.a in block
    )
    assert featured_line.endswith("The game of the week.")


def test_stakes_tags_render_as_prose_not_raw_identifiers(week10: WeeklyNarration) -> None:
    """`stats/stakes.py`'s tags (``bye_seed``, ``wildcard_race``, ...) are
    internal identifiers, not prose -- the section must translate them, never
    print the snake_case tag verbatim."""
    assert any(card.stakes for card in week10.next_week)
    blob = " ".join(_section(render_weekly_issue(week10), "Next Week").blocks)
    for card in week10.next_week:
        for tag in card.stakes:
            assert tag not in blob, f"raw stakes tag {tag!r} leaked into the prose"
    assert "first-round bye" in blob or "wildcard spot" in blob or "division race" in blob


def test_stakes_tag_vocabulary_has_not_drifted_from_stats_stakes() -> None:
    from commishdesk.stats.stakes import _TAG_ORDER

    assert set(weekly_template._STAKES_PHRASES) == set(_TAG_ORDER)


def test_an_unrecognized_stakes_tag_falls_back_to_the_raw_string() -> None:
    """The fallback is deliberate (a new tag stays visible, never silently
    dropped) -- exercise it directly rather than only via the known vocabulary."""
    assert weekly_template._stakes_phrase(["a_brand_new_tag"]) == "a_brand_new_tag"


# --------------------------------------------------------------------------- #
# Matrix: quiet transactions week
# --------------------------------------------------------------------------- #


def test_a_quiet_week_renders_one_deliberate_line() -> None:
    quiet = WeeklyNarrationTransactions(
        last_trade_week=None,
        weeks_since_last_trade=None,
        complete=False,
        this_week_count=0,
        recent_trade_count=0,
    )
    section = _section(render_weekly_issue(_narration(transactions=quiet)), "The Transaction Desk")
    assert section.blocks == [weekly_template._QUIET_WIRE]


def test_the_transaction_desk_reports_a_busy_week(week10: WeeklyNarration) -> None:
    desk = week10.transactions
    assert desk.this_week_count >= 1 and desk.recent_trade_count >= 1
    assert desk.last_trade_week is not None
    blob = " ".join(_section(render_weekly_issue(week10), "The Transaction Desk").blocks)
    assert f"The last trade landed in week {desk.last_trade_week}" in blob


def test_incomplete_trade_history_with_real_activity_says_so() -> None:
    """`stats/transactions.py`'s only ``complete=False`` construction site
    always pairs it with ``last_trade_week=None`` -- the real shape a partial
    ingest history produces when moves happened this week regardless."""
    partial = WeeklyNarrationTransactions(
        last_trade_week=None,
        weeks_since_last_trade=None,
        complete=False,
        this_week_count=2,
        recent_trade_count=1,
    )
    blocks = _section(render_weekly_issue(_narration(transactions=partial)), "The Transaction Desk").blocks
    assert blocks == [
        "Two moves this week.",
        "One trade in the recent window.",
        "The trade history is incomplete, so the market is hard to read.",
    ]


# --------------------------------------------------------------------------- #
# Power rankings
# --------------------------------------------------------------------------- #


def test_power_rankings_follow_the_model_rank(week10: WeeklyNarration) -> None:
    section = _section(render_weekly_issue(week10), "Power Rankings")
    ranked = sorted(week10.power, key=lambda row: row.model_rank if row.model_rank is not None else 10**6)
    assert ranked and ranked[0].model_rank is not None
    assert section.blocks[0].startswith(f"{ranked[0].model_rank}. {ranked[0].team}")
    blob = "\n".join(section.blocks)
    for row in week10.power:
        assert row.team in blob
    # no rank-delta arrows / published ranks (Story 5.12)
    assert not any("+" in block.split(" — ")[0] for block in section.blocks)


# --------------------------------------------------------------------------- #
# Matrix: determinism + the content-safety gate
# --------------------------------------------------------------------------- #


def test_render_is_deterministic(week10: WeeklyNarration) -> None:
    first, second = render_weekly_issue(week10), render_weekly_issue(week10)
    assert first.model_dump() == second.model_dump()
    assert weekly_issue_to_text(first) == weekly_issue_to_text(second)


def test_rendered_text_carries_every_heading(week10: WeeklyNarration) -> None:
    text = weekly_issue_to_text(render_weekly_issue(week10))
    for heading in SECTION_HEADINGS:
        assert f"## {heading}" in text


@pytest.mark.parametrize("fixture", ["week10-blowout.json", "week02-nailbiter.json"])
def test_output_passes_the_content_safety_gate(fixture: str) -> None:
    narration = _facts(fixture, nfl_byes_next_week=BYES_NEXT_WEEK).narration
    report = check_narration(weekly_issue_to_text(render_weekly_issue(narration)), narration)
    assert report.ok, report.findings


def test_no_literal_none_in_the_prose(week10: WeeklyNarration, nailbiter: WeeklyNarration) -> None:
    for narration in (week10, nailbiter):
        assert "None" not in weekly_issue_to_text(render_weekly_issue(narration))


# --------------------------------------------------------------------------- #
# AD-1 copies must not silently drift from their real sources
# --------------------------------------------------------------------------- #


def test_spell_helper_has_not_drifted_from_facts_leads() -> None:
    import commishdesk.facts.leads as leads

    assert weekly_template._ONES == leads._ONES
    for n in range(0, 40):
        assert weekly_template._spell(n) == leads._spell(n), n


def test_blowout_ratio_has_not_drifted_from_stats_weekly() -> None:
    from commishdesk.stats.weekly import BLOWOUT_RATIO

    assert weekly_template._BLOWOUT_LOSER_RATIO == BLOWOUT_RATIO


# --------------------------------------------------------------------------- #
# The house model style: frozen, closed to unknown keys
# --------------------------------------------------------------------------- #


def test_weekly_issue_and_section_are_frozen_and_closed() -> None:
    from pydantic import ValidationError

    section = WeeklySection(heading="Test", blocks=["a"])
    with pytest.raises(ValidationError):
        section.heading = "Changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        WeeklySection(heading="Test", blocks=["a"], extra="not allowed")  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# Fence: AD-1 — stdlib + commishdesk.facts.schema only
# --------------------------------------------------------------------------- #


def test_module_imports_only_the_facts_schema_and_the_standard_library() -> None:
    tree = ast.parse(WEEKLY_TEMPLATE.read_text(encoding="utf-8"))
    dotted: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            dotted.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            dotted.add(node.module)
    roots = {name.split(".")[0] for name in dotted}
    assert roots <= {"__future__", "pydantic", "commishdesk"}, roots
    assert {name for name in dotted if name.startswith("commishdesk")} == {"commishdesk.facts.schema"}


def test_module_reads_no_network_store_render_or_ingest_module() -> None:
    tree = ast.parse(WEEKLY_TEMPLATE.read_text(encoding="utf-8"))
    dotted: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            dotted.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            dotted.add(node.module)
        elif isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name in {"import_module", "__import__"}:
                dotted.update(a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str))
    # "httpx" is deliberately not listed here: it would be a bare top-level import
    # (never "commishdesk.httpx"), so it belongs to the other module-import-fence
    # test above (`roots <= {...}`), not this commishdesk-submodule check.
    forbidden = {"adapters", "store", "render", "deliver", "statmods", "consensus", "ingest", "narrate"}
    offenders = [name for name in dotted if name.startswith("commishdesk.") and name.split(".")[1] in forbidden]
    assert not offenders, offenders
