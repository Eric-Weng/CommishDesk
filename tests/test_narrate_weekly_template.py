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
import hashlib
import json
import re

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
    COLD_START_SECTION_HEADINGS,
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


def _family_lines(game: WeeklyNarrationGame, family: str) -> set[str]:
    """Every sentence the family's frames can render for *game*."""
    fields = {
        "w": game.winner,
        "l": game.loser,
        "wp": game.winner_pts,
        "lp": game.loser_pts,
        "m": game.margin,
    }
    return {frame.format(**fields) for frame in weekly_template._GAME_FRAMES[family]}


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
    for block, game, family in zip(blocks, (blowout, close, ordinary), ("blowout", "close", "ordinary"), strict=True):
        assert block in _family_lines(game, family)
        # the three families are mutually exclusive wordings
        for other in {"blowout", "close", "ordinary"} - {family}:
            assert block not in _family_lines(game, other)


def test_the_blowout_week_renders_a_blowout_game(week10: WeeklyNarration) -> None:
    blocks = _section(render_weekly_issue(week10), "Around the League").blocks
    biggest = max(week10.games, key=lambda game: game.margin)
    assert weekly_template._game_family(biggest) == "blowout"
    assert any(block.startswith(line) for block in blocks for line in _family_lines(biggest, "blowout"))


def test_the_nailbiter_week_renders_a_close_game(nailbiter: WeeklyNarration) -> None:
    assert nailbiter.games
    closest = min(nailbiter.games, key=lambda game: game.margin)
    assert closest.margin <= weekly_template._CLOSE_GAME_MARGIN  # a nailbiter sits inside the close band
    blocks = _section(render_weekly_issue(nailbiter), "Around the League").blocks
    assert weekly_template._game_family(closest) == "close"
    assert any(block.startswith(line) for block in blocks for line in _family_lines(closest, "close"))


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


def test_around_the_league_narrates_the_notable_games_in_full(week10: WeeklyNarration) -> None:
    section = _section(render_weekly_issue(week10), "Around the League")
    assert len(week10.games) == 6
    for index in weekly_template._notable_games(week10.games):
        game = week10.games[index]
        matching = [block for block in section.blocks if game.winner and game.winner in block]
        assert matching, game
        assert any(game.loser and game.loser in block for block in matching)


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
        sentences = [block for block in section.blocks if row.team in block]
        assert any(row.rec in block for block in sentences), row


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
    """Story 5.16: the cold-start Issue uses the four-section set, so the
    Standings heading (not "Standings and the Playoff Picture") is what the
    renderer emits — and every standings row must drop the model-rank clause
    because no roster has one yet."""
    issue = render_weekly_issue(_cold_start_narration())
    section = _section(issue, "Standings")
    assert not any("model rank" in block for block in section.blocks)


def test_power_rankings_stand_down_at_cold_start() -> None:
    issue = render_weekly_issue(_cold_start_narration())
    assert "Power Rankings" not in [section.heading for section in issue.sections]
    assert not any("unranked" in block for section in issue.sections for block in section.blocks)


def test_cold_start_issue_has_exactly_four_sections() -> None:
    issue = render_weekly_issue(_cold_start_narration())
    assert [section.heading for section in issue.sections] == list(COLD_START_SECTION_HEADINGS)


def _cold_start_completion(
    *, drop: str | None = None, extra: str | None = None, empty: str | None = None
) -> str:
    headings = [h for h in COLD_START_SECTION_HEADINGS if h != drop]
    if extra:
        headings.append(extra)
    parts = [
        f"## {h}" + ("" if h == empty else "\n\nSomething happened this week.")
        for h in headings
    ]
    return "\n".join(parts)


def test_weekly_issue_from_text_parses_a_valid_cold_start_completion() -> None:
    narration = _cold_start_narration()
    issue = weekly_template.weekly_issue_from_text(_cold_start_completion(), narration)
    assert issue is not None
    assert [s.heading for s in issue.sections] == list(COLD_START_SECTION_HEADINGS)
    assert all(s.blocks for s in issue.sections)


def test_a_title_line_before_the_first_section_is_not_a_section_at_week_one() -> None:
    narration = _cold_start_narration()
    text = "# The Jet-Sweep Jackals Week 1 Ledger" + "\n\n" + _cold_start_completion()
    issue = weekly_template.weekly_issue_from_text(text, narration)
    assert issue is not None
    assert [s.heading for s in issue.sections] == list(COLD_START_SECTION_HEADINGS)
    # only a title line before the first ``##`` is exempt: a ``#`` heading later is still a section
    later = _cold_start_completion() + "\n\n# Extra\n\nSomething."
    assert weekly_template.weekly_issue_from_text(later, narration) is None


def test_weekly_issue_from_text_rejects_a_warm_completion_at_week_one() -> None:
    warm = "\n\n".join(f"## {h}\n\nSomething happened." for h in SECTION_HEADINGS)
    assert weekly_template.weekly_issue_from_text(warm, _cold_start_narration()) is None


def test_weekly_issue_from_text_rejects_a_missing_or_empty_cold_start_heading() -> None:
    narration = _cold_start_narration()
    assert weekly_template.weekly_issue_from_text(
        _cold_start_completion(drop="Next Week"), narration
    ) is None
    assert weekly_template.weekly_issue_from_text(
        _cold_start_completion(empty="Standings"), narration
    ) is None


def test_weekly_issue_from_text_rejects_an_extra_heading_at_week_one() -> None:
    text = _cold_start_completion(extra="Power Rankings")
    assert weekly_template.weekly_issue_from_text(text, _cold_start_narration()) is None


def test_weekly_issue_to_text_on_a_cold_start_issue_prints_no_stood_down_heading() -> None:
    text = weekly_issue_to_text(render_weekly_issue(_cold_start_narration()))
    headings = [line[3:] for line in text.splitlines() if line.startswith("## ")]
    assert headings == list(COLD_START_SECTION_HEADINGS)
    for stood_down in (
        "Power Rankings",
        "The Luck Index",
        "The Transaction Desk",
        "Playoff Picture",
    ):
        assert stood_down not in text


# --------------------------------------------------------------------------- #
# Matrix: empty luck list
# --------------------------------------------------------------------------- #


def test_luck_section_stands_down_on_an_empty_list() -> None:
    assert _section(render_weekly_issue(_narration()), "The Luck Index").blocks == [weekly_template._NO_LUCK]


def test_luck_section_stands_down_below_the_games_played_minimum() -> None:
    """The real cold-start shape: one luck row per roster, every value ``None``.
    Story 5.16: at Week 1 the Luck section stands down entirely — no heading,
    no block — so nothing is rendered over an empty measurement."""
    from tests.test_facts_weekly import _synthetic

    week_model, league, players, names = _synthetic(12, week=1, playoff_week_start=10)
    doc = build_weekly_facts(week_model, league, players, names, generated_at=GENERATED_AT)
    assert doc.narration.luck  # a row per roster...
    assert all(row.luck is None for row in doc.narration.luck)  # ...none of them measurable
    issue = render_weekly_issue(doc.narration)
    assert "The Luck Index" not in [section.heading for section in issue.sections]


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
    # numbered rows come first and in model-rank order; no rank-delta arrows (Story 5.12)
    numbered = [block for block in section.blocks if re.match(r"^\d{1,2}\. ", block)]
    assert section.blocks[: len(numbered)] == numbered
    assert [int(block.split(".", 1)[0]) for block in numbered] == [r.model_rank for r in ranked[: len(numbered)]]
    assert not any("—" in block for block in section.blocks)


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
    assert roots <= {"__future__", "pydantic", "commishdesk", "re", "collections", "hashlib"}, roots
    # Story 6.0b: the one section id/heading table (stdlib only) is the other allowed import.
    assert {name for name in dotted if name.startswith("commishdesk")} == {
        "commishdesk.facts.schema",
        "commishdesk.sections",
    }


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


def test_the_unconfirmed_seeding_note_follows_the_narration_flag_alone(
    week10: WeeklyNarration,
) -> None:
    note = weekly_template._SEEDING_NOTE
    assert note.startswith("Seeding unconfirmed") and "—" not in note
    picture = week10.playoff_picture
    assert picture is not None and picture.seeding_unconfirmed is False

    heading = "Standings and the Playoff Picture"
    off = " ".join(_section(render_weekly_issue(week10), heading).blocks)
    assert note not in off

    flagged = week10.model_copy(
        update={"playoff_picture": picture.model_copy(update={"seeding_unconfirmed": True})}
    )
    on = " ".join(_section(render_weekly_issue(flagged), heading).blocks)
    assert note in on
    assert on.replace(" " + note, "") == off

    stood_down = _section(render_weekly_issue(_narration()), heading).blocks
    assert not any(note in block for block in stood_down)


def test_the_shared_switch_follows_has_prior_week_for_narrator_and_parser() -> None:
    """Narrators and renderers select the heading set through one helper keyed
    on ``period.has_prior_week``; an explicit flag wins over ``league.week``."""
    narration = _cold_start_narration()  # league.week == 1
    warm_issue = render_weekly_issue(narration, has_prior_week=True)
    assert [s.heading for s in warm_issue.sections] == list(SECTION_HEADINGS)
    cold_issue = render_weekly_issue(narration, has_prior_week=False)
    assert [s.heading for s in cold_issue.sections] == list(COLD_START_SECTION_HEADINGS)
    warm_text = "\n\n".join(f"## {h}\n\nSomething happened." for h in SECTION_HEADINGS)
    assert weekly_template.weekly_issue_from_text(warm_text, narration, has_prior_week=True) is not None
    assert weekly_template.weekly_issue_from_text(warm_text, narration, has_prior_week=False) is None


def test_week_ten_narration_keeps_the_seven_warm_sections(week10: WeeklyNarration) -> None:
    issue = render_weekly_issue(week10)
    assert [s.heading for s in issue.sections] == list(SECTION_HEADINGS)


def test_cold_start_standings_prose_is_by_points_with_positional_ranks() -> None:
    narration = _cold_start_narration()
    section = _section(render_weekly_issue(narration), "Standings")
    ordered = sorted(narration.standings, key=lambda r: (-r.pf, r.roster_id.zfill(8)))
    assert len(section.blocks) == len(ordered)
    for block, row in zip(section.blocks, ordered, strict=True):
        assert block.startswith(row.team)
        assert row.rec in block and str(row.pf) in block
    assert "model rank" not in " ".join(section.blocks)
    # no record clause, no storyline at the cold start
    around = _section(render_weekly_issue(narration), "Around the League").blocks
    assert not any(re.search(r"moves to|puts .* at|lifts .* to", block) for block in around)


# --------------------------------------------------------------------------- #
# Story 9.10: closer to the LLM voice
# --------------------------------------------------------------------------- #

_EM_DASH = "—"


@pytest.mark.parametrize("fixture", ["week10-blowout.json", "week02-nailbiter.json"])
def test_render_is_byte_identical_on_equal_input(fixture: str) -> None:
    narration = _facts(fixture, nfl_byes_next_week=BYES_NEXT_WEEK).narration
    first = weekly_issue_to_text(render_weekly_issue(narration))
    second = weekly_issue_to_text(render_weekly_issue(narration.model_copy(deep=True)))
    assert first == second


def test_frame_choice_uses_a_stable_digest_not_the_salted_builtin_hash() -> None:
    narration = _narration()
    frames = tuple(f"frame {n}" for n in range(7))
    key = "|".join(["Test League", "2025", "5", "x", "1"])
    expected = frames[int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16) % 7]
    assert weekly_template._pick(frames, narration, "x", 1) == expected


@pytest.mark.parametrize("fixture", ["week10-blowout.json", "week02-nailbiter.json"])
def test_every_section_holds_at_most_one_em_dash_outside_team_labels(fixture: str) -> None:
    narration = _facts(fixture, nfl_byes_next_week=BYES_NEXT_WEEK).narration
    labels = sorted({row.team for row in narration.standings}, key=len, reverse=True)
    for section in render_weekly_issue(narration).sections:
        text = " ".join(section.blocks)
        for label in labels:
            text = text.replace(label, "")
        assert text.count(_EM_DASH) <= 1, section.heading


def test_the_game_frames_give_each_family_several_verbs() -> None:
    frames = weekly_template._GAME_FRAMES
    assert all(len(family) >= 3 for family in frames.values())
    assert sum(len(family) for family in frames.values()) >= 12
    assert len({frame for family in frames.values() for frame in family}) == sum(len(f) for f in frames.values())


def test_a_week_of_games_uses_more_than_one_frame(week10: WeeklyNarration) -> None:
    chosen = weekly_template._notable_games(week10.games)
    blocks = _section(render_weekly_issue(week10), "Around the League").blocks[: len(chosen)]
    shapes = set()
    for index, block in zip(chosen, blocks, strict=True):
        game = week10.games[index]
        family = weekly_template._game_family(game)
        for line in sorted(_family_lines(game, family)):
            if block.startswith(line):
                shapes.add((family, line.replace(game.winner or "", "{w}").replace(game.loser or "", "{l}")))
    assert len({shape for _family, shape in shapes}) >= 3


def test_a_game_line_carries_the_winners_record_and_the_top_scorers_team(week10: WeeklyNarration) -> None:
    records = {row.team: row.rec for row in week10.standings}
    chosen = weekly_template._notable_games(week10.games)
    blocks = _section(render_weekly_issue(week10), "Around the League").blocks[: len(chosen)]
    for index, block in zip(chosen, blocks, strict=True):
        game = week10.games[index]
        assert game.winner and records[game.winner] in block
        assert game.top and game.top in block
        if game.top_team:
            assert game.top_team in block


def test_missing_facts_omit_their_clauses_and_invent_no_digit() -> None:
    game = WeeklyNarrationGame(
        matchup_id=4, winner="Alpha", winner_pts=120.0, loser="Beta", loser_pts=100.0, margin=20.0, top="Ace"
    )
    line = weekly_template._game_line(game, _narration(games=[game]), records={})
    assert "Ace" in line and "Alpha" in line and "Beta" in line
    assert not any(phrase in line for phrase in ("moves to", "That puts", "lifts"))
    assert set(re.findall(r"\d+(?:\.\d+)?", line)) <= {"120.0", "100.0", "20.0"}
    bare = game.model_copy(update={"top": None})
    assert re.findall(r"\d+(?:\.\d+)?", weekly_template._game_line(bare, _narration(games=[bare]))) != []
    row = WeeklyNarrationPower(model_rank=1, team="Alpha", roster_id="1", rec="3-1", avg_pf=101.5, all_play=None)
    assert "all-play" not in weekly_template._power_line(row, _narration()).lower()
    with_ap = row.model_copy(update={"all_play": "9-3"})
    assert "9-3" in weekly_template._power_line(with_ap, _narration())


def test_a_shared_team_label_skips_the_record_clause() -> None:
    standings = [
        WeeklyNarrationStanding(rank=1, team="Twin", roster_id="1", rec="3-1", pf=1.0),
        WeeklyNarrationStanding(rank=2, team="Twin", roster_id="2", rec="1-3", pf=1.0),
    ]
    game = _game("Twin", "Other", 120.0, 100.0)
    narration = _narration(games=[game], standings=standings)
    block = _section(render_weekly_issue(narration), "Around the League").blocks[0]
    assert "3-1" not in block and "1-3" not in block


def test_luck_is_read_in_words_with_the_printed_sign() -> None:
    def row(roster_id: str, luck: float) -> WeeklyNarrationLuck:
        return WeeklyNarrationLuck(team=f"T{roster_id}", roster_id=roster_id, rec="2-2", luck=luck, earned_wins=2.0)

    lucky = weekly_template._luck_line(row("1", 1.5), _narration())
    unlucky = weekly_template._luck_line(row("2", -1.5), _narration())
    even = weekly_template._luck_line(row("3", 0.0), _narration())
    assert "+1.5" in lucky and re.search(r"lucky|favored|breaks|ahead of the scores", lucky) and "unlucky" not in lucky
    assert "-1.5" in unlucky and re.search(r"unlucky|deserved|dodged|skipped|behind the scores", unlucky)
    assert "+0.0" in even and re.search(r"no luck|exactly as lucky", even)
    assert "Earned wins" in lucky and "2.0" in lucky


def test_the_standings_read_as_grouped_sentences_without_the_table(week10: WeeklyNarration) -> None:
    blocks = _section(render_weekly_issue(week10), "Standings and the Playoff Picture").blocks
    text = " ".join(blocks)
    assert not re.search(r"^\d{1,2}\. ", text, re.MULTILINE)
    assert "points for" not in text and "model rank" not in text
    picture = week10.playoff_picture
    assert picture is not None
    for name in [*picture.byes, *picture.bubble, picture.first_out or ""]:
        assert name in blocks[-1]
    assert picture.format in blocks[-1]


@pytest.mark.parametrize("fixture", ["week10-blowout.json", "week02-nailbiter.json"])
def test_every_digit_token_is_in_the_payload(fixture: str) -> None:
    narration = _facts(fixture, nfl_byes_next_week=BYES_NEXT_WEEK).narration
    text = weekly_issue_to_text(render_weekly_issue(narration))
    assert check_narration(text, narration).ok
    payload = narration.model_dump_json()
    body = text.split("\n", 2)[2]  # past the masthead (title and dateline)
    for token in re.findall(r"\d+(?:\.\d+)?", body):
        assert token in payload, token


def test_frozen_week_snapshot_of_the_new_render(week10: WeeklyNarration) -> None:
    text = weekly_issue_to_text(render_weekly_issue(week10))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert digest == _WEEK10_TEMPLATE_SHA256, (
        f"week-10 template render changed; if deliberate, set _WEEK10_TEMPLATE_SHA256 = {digest!r}"
    )


_WEEK10_TEMPLATE_SHA256 = "02803c6b357eeae81d4ea71289bf4083969e528539b571a0b2c19642c55e2184"


def _picture(**kw: object) -> WeeklyNarrationPlayoff:
    return WeeklyNarrationPlayoff(format="6 teams", **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("picture", "present", "absent_counts"),
    [
        (_picture(byes=["A", "B"], in_bracket=["A", "B", "C", "D"]), ["have a first-round bye"], {"A": 1, "B": 1}),
        (_picture(byes=["A"], in_bracket=["A", "C"]), ["A has a first-round bye", "C is "], {"A": 1}),
        (_picture(in_bracket=["C", "D"]), ["C and D are in the bracket", "The bracket holds"], {}),
        (_picture(bubble=["E"]), ["E is on the bubble", "The bubble holds E"], {}),
        (_picture(bubble=["E", "F"]), ["E and F are on the bubble", "The bubble holds E and F"], {}),
    ],
)
def test_playoff_block_groups_each_team_once_with_agreeing_verbs(
    picture: WeeklyNarrationPlayoff, present: list[str], absent_counts: dict[str, int]
) -> None:
    seen: set[str] = set()
    for league_name in ("L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8"):
        narration = _narration()
        narration = narration.model_copy(
            update={"league": narration.league.model_copy(update={"name": league_name})}
        )
        block = weekly_template._playoff_block(picture, narration)
        for team, count in absent_counts.items():
            assert len(re.findall(rf"\b{team}\b", block)) == count, block
        seen.update(p for p in present if p in block)
        assert "is have" not in block and "are is" not in block and "has are" not in block
    assert seen, "no expected phrase rendered under any of eight hash seeds"


def test_tie_line_reads_right_for_a_joined_label() -> None:
    tie = WeeklyNarrationGame(matchup_id=3, winner=None, winner_pts=99.5, loser="A and B", loser_pts=99.5, margin=0.0)
    for league_name in ("L1", "L2", "L3", "L4", "L5", "L6"):
        base = _narration(games=[tie])
        narration = base.model_copy(update={"league": base.league.model_copy(update={"name": league_name})})
        line = weekly_template._game_line(tie, narration)
        assert "A and B" in line and "on one side" not in line and "99.5" in line


def test_negative_zero_luck_prints_unsigned_in_the_even_frame() -> None:
    row = WeeklyNarrationLuck(team="T", roster_id="1", rec="2-2", luck=-0.0)
    line = weekly_template._luck_line(row, _narration())
    assert "+0.0" in line and "-0.0" not in line


def test_earned_wins_wording_is_number_neutral() -> None:
    row = WeeklyNarrationLuck(team="T", roster_id="1", rec="2-2", luck=1.0, earned_wins=1.0)
    for seed in range(1, 9):
        n = _narration().model_copy(update={"league": _narration().league.model_copy(update={"name": f"L{seed}"})})
        assert "1.0 wins" not in weekly_template._luck_line(row, n)


def test_an_empty_record_renders_no_dangling_is() -> None:
    rows = [
        WeeklyNarrationStanding(rank=1, team="A", roster_id="1", rec="", pf=10.0),
        WeeklyNarrationStanding(rank=2, team="B", roster_id="2", rec="", pf=5.0),
    ]
    narration = _narration(standings=rows)
    warm = " ".join(weekly_template._standings_blocks(rows, narration, cold_start=False))
    cold = " ".join(weekly_template._standings_blocks(rows, narration, cold_start=True))
    for text in (warm, cold):
        assert " is ." not in text and " at ." not in text and "  " not in text
    assert "A and B" in warm


def test_an_unranked_power_row_has_no_number_prefix() -> None:
    row = WeeklyNarrationPower(model_rank=None, team="A", roster_id="1", rec="1-0", avg_pf=1.5)
    line = weekly_template._power_line(row, _narration())
    assert line.endswith(" Unranked.") and "for now" not in line and not re.match(r"^\d", line)


def test_a_game_line_never_repeats_a_team_label_in_its_clauses() -> None:
    game = WeeklyNarrationGame(
        matchup_id=7, winner="Alpha", winner_pts=120.0, loser="Beta", loser_pts=100.0,
        margin=20.0, top="Ace Back", top_team="Alpha",
    )
    narration = _narration(
        games=[game],
        standings=[
            WeeklyNarrationStanding(rank=1, team="Alpha", roster_id="1", rec="6-6", pf=1.0),
            WeeklyNarrationStanding(rank=2, team="Beta", roster_id="2", rec="5-7", pf=1.0),
        ],
    )
    line = _section(render_weekly_issue(narration, has_prior_week=True), "Around the League").blocks[0]
    assert line.count("Alpha") == 1 and "6-6" in line and "Ace Back" in line
    loser_side = game.model_copy(update={"top_team": "Beta"})
    line = weekly_template._game_line(loser_side, narration, records={"Alpha": "6-6"})
    assert line.count("Alpha") == 1 and line.count("Beta") == 1


def test_a_trade_this_week_is_not_zero_weeks_ago() -> None:
    desk = WeeklyNarrationTransactions(
        last_trade_week=5, weeks_since_last_trade=0, complete=True, this_week_count=1, recent_trade_count=1
    )
    text = weekly_issue_to_text(render_weekly_issue(_narration(transactions=desk), has_prior_week=True))
    assert "zero weeks" not in text and "The last trade landed this week." in text


# --------------------------------------------------------------------------- #
# Story 9.10 (structure): highlights, not every row
# --------------------------------------------------------------------------- #


def _opening(block: str, labels: list[str]) -> str:
    """A block's opener with team labels masked: its first two tokens."""
    for label in sorted(labels, key=len, reverse=True):
        block = block.replace(label, "T")
    return " ".join(re.sub(r"^\d{1,2}\. ", "", block).split()[:2])


@pytest.mark.parametrize("fixture", ["week10-blowout.json", "week02-nailbiter.json"])
def test_no_two_adjacent_blocks_open_the_same_way(fixture: str) -> None:
    narration = _facts(fixture, nfl_byes_next_week=BYES_NEXT_WEEK).narration
    labels = [row.team for row in narration.standings]
    issue = render_weekly_issue(narration)
    for heading in ("Around the League", "Power Rankings", "The Luck Index"):
        blocks = _section(issue, heading).blocks
        if heading == "Around the League":
            blocks = blocks[: len(weekly_template._notable_games(narration.games)) + 1]
        openers = [_opening(block, labels) for block in blocks]
        assert all(a != b for a, b in zip(openers, openers[1:], strict=False)), (heading, openers)


def test_the_notable_games_are_the_closest_the_biggest_and_a_top_scorer_loss(week10: WeeklyNarration) -> None:
    games = week10.games
    chosen = weekly_template._notable_games(games)
    assert len(chosen) == 4 and chosen == sorted(chosen)
    assert min(range(len(games)), key=lambda i: games[i].margin) in chosen
    assert max(range(len(games)), key=lambda i: games[i].margin) in chosen
    assert any(games[i].top_team == games[i].loser for i in chosen)


def test_around_the_league_collapses_the_rest_into_one_spelled_sentence(week10: WeeklyNarration) -> None:
    blocks = _section(render_weekly_issue(week10), "Around the League").blocks
    chosen = weekly_template._notable_games(week10.games)
    assert len(chosen) == 4
    for index in chosen:
        game = week10.games[index]
        assert any(game.winner in block and game.loser in block for block in blocks[:4])
    rest = blocks[4]
    assert rest in {frame.format(n="two") for frame in (*weekly_template._REST_CLEAR, *weekly_template._REST_MIXED)}
    assert not re.search(r"\d", rest)  # a spelled count, no numeral
    skipped = [g for i, g in enumerate(week10.games) if i not in chosen]
    assert all(weekly_template._game_family(g) != "close" for g in skipped)
    assert rest in {frame.format(n="two") for frame in weekly_template._REST_CLEAR}


def test_a_mixed_rest_is_not_called_never_close() -> None:
    games = [_game(f"W{i}", f"L{i}", 120.0 + i, 100.0) for i in range(8)]
    games[6] = _game("W6", "L6", 101.0, 100.0)
    games[7] = _game("W7", "L7", 102.0, 100.0)  # two close games: only one is "the closest"
    narration = _narration(games=games)
    blocks = _section(render_weekly_issue(narration), "Around the League").blocks
    chosen = weekly_template._notable_games(games)
    rest = [g for i, g in enumerate(games) if i not in chosen]
    assert any(weekly_template._game_family(g) == "close" for g in rest)
    assert blocks[len(chosen)] in {
        frame.format(n=weekly_template._spell(len(rest))) for frame in weekly_template._REST_MIXED
    }


def test_a_small_week_or_a_tie_narrates_every_game() -> None:
    four = [_game(f"W{i}", f"L{i}", 130.0 + i, 100.0) for i in range(4)]
    blocks = _section(render_weekly_issue(_narration(games=four)), "Around the League").blocks
    assert len(blocks) == 4 and all(f"W{i}" in block for i, block in enumerate(blocks))
    tie = WeeklyNarrationGame(matchup_id=99, winner=None, winner_pts=99.5, loser="A and B", loser_pts=99.5, margin=0.0)
    eight = [*[_game(f"W{i}", f"L{i}", 130.0 + i, 100.0) for i in range(7)], tie]
    blocks = _section(render_weekly_issue(_narration(games=eight)), "Around the League").blocks
    assert len(blocks) == 8 and not any("The other" in block for block in blocks)


def test_power_narrates_the_top_rows_then_highlights(week10: WeeklyNarration) -> None:
    blocks = _section(render_weekly_issue(week10), "Power Rankings").blocks
    ranked = sorted(week10.power, key=lambda row: row.model_rank if row.model_rank is not None else 10**6)
    numbered = [block for block in blocks if re.match(r"^\d{1,2}\. ", block)]
    assert len(numbered) == 3
    for row, block in zip(ranked[:3], numbered, strict=True):
        assert block.startswith(f"{row.model_rank}. {row.team}") and row.rec in block
    assert 4 <= len(blocks) <= 6
    narrated = " ".join(blocks)
    deltas = [r.week_delta for r in week10.power if r.week_delta]
    up = [r.team for r in week10.power if r.week_delta == max(deltas)]
    down = [r.team for r in week10.power if r.week_delta == min(deltas)]
    assert any(f"{team} " in narrated for team in up) and any(f"{team} " in narrated for team in down)
    assert any(w in narrated for w in ("jump", "climbed")) and any(w in narrated for w in ("tumble", "slid"))
    assert sum(row.team in narrated for row in week10.power) < len(week10.power)  # not every row


def test_power_highlights_are_left_out_when_the_payload_has_none() -> None:
    power = [
        WeeklyNarrationPower(model_rank=n, team=f"T{n}", roster_id=str(n), rec="2-2", avg_pf=100.0 + n)
        for n in range(1, 7)
    ]
    blocks = _section(render_weekly_issue(_narration(power=power), has_prior_week=True), "Power Rankings").blocks
    assert len(blocks) == 3 and all(re.match(r"^\d\. ", block) for block in blocks)


def test_power_names_the_widest_standings_versus_model_gap() -> None:
    power = [
        WeeklyNarrationPower(model_rank=n, team=f"T{n}", roster_id=str(n), rec="2-2", avg_pf=100.0 + n)
        for n in range(1, 7)
    ]
    # T6 sits 1st in the standings but 6th in the model: a gap of five, the widest
    standings = [
        WeeklyNarrationStanding(rank=r, team=f"T{n}", roster_id=str(n), rec="2-2", pf=1.0)
        for r, n in enumerate([6, 1, 2, 3, 4, 5], start=1)
    ]
    blocks = _section(
        render_weekly_issue(_narration(power=power, standings=standings), has_prior_week=True), "Power Rankings"
    ).blocks
    gap = blocks[-1]
    assert "T6" in gap and "standings rank 1" in gap and "model rank 6" in gap


def test_luck_names_the_luckiest_and_unluckiest_then_counts_the_rest(week10: WeeklyNarration) -> None:
    section = _section(render_weekly_issue(week10), "The Luck Index")
    measurable = sorted((r for r in week10.luck if r.luck is not None), key=lambda r: -(r.luck or 0.0))
    assert len(section.blocks) == 3
    assert measurable[0].team in section.blocks[0] and f"{measurable[0].luck:+}" in section.blocks[0]
    assert measurable[-1].team in section.blocks[1] and f"{measurable[-1].luck:+}" in section.blocks[1]
    assert section.blocks[2].startswith("The other ten ") and not re.search(r"\d", section.blocks[2])
    assert "within a win" not in section.blocks[2]  # a +2.2 sits among the rest: only "in between" is true


def test_luck_rest_says_within_a_win_only_when_every_remaining_row_is() -> None:
    def luck_rows(values: list[float]) -> list[WeeklyNarrationLuck]:
        return [
            WeeklyNarrationLuck(team=f"T{i}", roster_id=str(i), rec="2-2", luck=v) for i, v in enumerate(values)
        ]

    near = _section(render_weekly_issue(_narration(luck=luck_rows([2.0, 0.5, -0.5, 1.0, -2.0]))), "The Luck Index")
    assert "within a win" in near.blocks[-1] and "The other three" in near.blocks[-1]
    far = _section(render_weekly_issue(_narration(luck=luck_rows([2.0, 1.5, 0.5, -2.0]))), "The Luck Index")
    assert "within a win" not in far.blocks[-1] and "The other two" in far.blocks[-1]
    one = _section(render_weekly_issue(_narration(luck=luck_rows([2.0, 0.5, -2.0]))), "The Luck Index")
    assert one.blocks[-1].startswith("The other team sits ")


def test_one_or_two_luck_rows_are_narrated_as_they_are() -> None:
    rows = [
        WeeklyNarrationLuck(team="T1", roster_id="1", rec="2-2", luck=1.2),
        WeeklyNarrationLuck(team="T2", roster_id="2", rec="2-2", luck=-0.4),
    ]
    two = _section(render_weekly_issue(_narration(luck=rows)), "The Luck Index").blocks
    assert len(two) == 2 and "T1" in two[0] and "T2" in two[1]
    one = _section(render_weekly_issue(_narration(luck=rows[:1])), "The Luck Index").blocks
    assert len(one) == 1 and "T1" in one[0]


def test_the_rollup_sentence_is_recognised_for_the_email() -> None:
    for frame in (*weekly_template._REST_CLEAR, *weekly_template._REST_MIXED):
        assert weekly_template.is_game_rollup(frame.format(n="four"))
    assert not weekly_template.is_game_rollup("Bubble-Screen Bobcats has lost six straight.")
