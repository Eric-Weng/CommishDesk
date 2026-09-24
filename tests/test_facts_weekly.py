"""Story 5.8: the weekly Facts document (``build_weekly_facts``).

One test per row of the spec's I/O & Edge-Case Matrix, mirroring
``tests/test_stats_stakes.py``: the week-10 build on the committed fixture,
golden/pinned reconciliation (the private ``week10-facts.json`` is skip-gated,
the always-on pins run everywhere), cold start, a bye/eliminated roster, a
bracket week, missing bye data, the 32-team narration cap, the unreducible
error, determinism, the schema-violation path, and the AST import-fence pair
plus the "no hardcoded league shape" guard.

The committed CI oracle ``tests/fixtures/facts/expected-weekly-facts-week10.json``
is a frozen ``model_dump(mode="json")`` snapshot of the whole document, built
with a fixed ``generated_at``; the byte-equality check is skipped when the file
is absent (a workspace that predates the regeneration).

``season.luck`` reconciles with the golden since Story 5.13a made the fixture's
``Roster`` totals point-in-time (``tools/point_in_time_rosters.py``); it is
asserted below, always-on as ``wins - expected_wins`` and against the golden when
present.

``season.coaching_efficiency.pct`` also does not reconcile exactly — this
story sums ``compute_weekly_lineups`` over weeks ``1..n`` under one supplied
(week-n) player snapshot, so ``docs/EDGE-CASES.md``'s roster-4 IR/taxi
divergence (Story 5.5, week 10 only) compounds across every roster's ten
weeks. Not asserted here either; see that doc's "Divergence from the phase-0
golden" section.

:data:`SCHEMA_VERSION` is asserted as ``0.8.0`` (Story 5.15) alongside the
oracle check.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest

from commishdesk.errors import CommishDeskError, SchemaValidationError
from commishdesk.facts import SCHEMA_VERSION, WeeklyFacts, build_weekly_facts
from commishdesk.ingest import (
    Draft,
    LeagueFormat,
    LeagueModel,
    Matchup,
    PlayerSnapshot,
    PlayoffFormat,
    Roster,
    Team,
    WeekModel,
    build_league_model,
    build_player_names,
    build_player_snapshot,
    build_week_model,
)
from tests.conftest import REPO_ROOT

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
FACTS_DIR = FIXTURE_DIR / "facts"
EXPECTED_WEEKLY_PATH = FACTS_DIR / "expected-weekly-facts-week10.json"
PUBLISHED_OVERLAY_PATH = FACTS_DIR / "expected-weekly-facts-week10-published.json"
STATS_WEEKLY = REPO_ROOT / "commishdesk" / "facts" / "weekly.py"

#: The private phase-0 golden — a planning artifact outside the repo.
_GOLDEN_PATH = REPO_ROOT.parent / "brief" / "phase-0" / "week10-facts.json"
GOLDEN = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8")) if _GOLDEN_PATH.is_file() else None
requires_golden = pytest.mark.skipif(
    GOLDEN is None,
    reason="phase-0 golden is a private planning artifact, not in the tree",
)
requires_committed_oracle = pytest.mark.skipif(
    not EXPECTED_WEEKLY_PATH.is_file(),
    reason="expected-weekly-facts-week10.json has not been regenerated in this workspace",
)

GENERATED_AT = "2026-09-07T00:00:00Z"
WEEK10 = "week10-blowout.json"
BYES_NEXT_WEEK = frozenset({"IND", "NO"})


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _bundle(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _week10_facts(*, nfl_byes_next_week: frozenset[str] | None = BYES_NEXT_WEEK) -> WeeklyFacts:
    bundle = _bundle(WEEK10)
    week = build_week_model(bundle)
    league = build_league_model(bundle)
    players = build_player_snapshot(bundle)
    names = build_player_names(bundle)
    return build_weekly_facts(
        week,
        league,
        players,
        names,
        generated_at=GENERATED_AT,
        nfl_byes_next_week=nfl_byes_next_week,
    )


def _synthetic(
    n_teams: int, *, week: int = 5, playoff_week_start: int | None = 10
) -> tuple[WeekModel, LeagueModel, dict[str, PlayerSnapshot], dict[str, str]]:
    """A hand-built N-team league-week — the 32-team cap row and the cold-start
    row share it."""
    ids = [str(i) for i in range(1, n_teams + 1)]
    league = LeagueModel(
        league_id="id_league",
        name="Big League",
        season=2025,
        platform="sleeper",
        format=LeagueFormat(
            team_count=n_teams,
            roster_slots=["QB", "RB", "WR", "TE", "FLEX"],
            flex_eligibility={"FLEX": ["RB", "WR", "TE"]},
            scoring_label="PPR",
            is_superflex_or_2qb=False,
            te_premium=False,
            playoff=PlayoffFormat(bracket_teams=min(8, n_teams)),
        ),
        teams=[Team(roster_id=r, manager=f"m{r}") for r in ids],
        picks=[],
        draft=Draft(id="id_draft"),
    )
    matchups: list[Matchup] = []
    for wk in range(1, week + 1):
        for index in range(0, n_teams, 2):
            a, b = ids[index], ids[index + 1]
            pair = index // 2 + 1
            matchups.append(
                Matchup(
                    week=wk,
                    roster_id=a,
                    matchup_id=pair,
                    opponent_roster_id=b,
                    points=100.0,
                    starters=["p1"],
                    starters_points=[100.0],
                    players_points={"p1": 100.0},
                )
            )
            matchups.append(
                Matchup(
                    week=wk,
                    roster_id=b,
                    matchup_id=pair,
                    opponent_roster_id=a,
                    points=90.0,
                    starters=["p2"],
                    starters_points=[90.0],
                    players_points={"p2": 90.0},
                )
            )
    week_model = WeekModel(
        week=week,
        rosters=[Roster(roster_id=r) for r in ids],
        matchups=matchups,
        transactions=[],
        playoff_week_start=playoff_week_start,
    )
    players = {
        "p1": PlayerSnapshot(player_id="p1", position="QB", nfl_team="KC"),
        "p2": PlayerSnapshot(player_id="p2", position="RB", nfl_team="BUF"),
    }
    names = {"p1": "Player One", "p2": "Player Two"}
    return week_model, league, players, names


# --------------------------------------------------------------------------- #
# Matrix: week-10 build
# --------------------------------------------------------------------------- #


def test_week10_build_validates_and_has_twelve_teams() -> None:
    doc = _week10_facts()
    assert isinstance(doc, WeeklyFacts)
    assert doc.schema_version == "0.8.0" == SCHEMA_VERSION
    assert doc.issue_type == "weekly"
    assert doc.week == 10
    assert len(doc.teams) == 12
    # every roster id is a string and the block is in numeric roster order
    assert [t.roster_id for t in doc.teams] == [str(i) for i in range(1, 13)]


def test_week10_has_six_this_week_and_six_next_week_games() -> None:
    doc = _week10_facts()
    assert len(doc.matchups.this_week) == 6
    assert len(doc.matchups.next_week) == 6
    for game in doc.matchups.this_week:
        assert int(game.home_roster_id) < int(game.away_roster_id)  # home is the lower id
        assert len(game.headline_players) == 2  # each side's top starter
    # The fixture's own name promises both values are reachable, not just False.
    by_pair = {(g.home_roster_id, g.away_roster_id): g.is_blowout for g in doc.matchups.this_week}
    assert by_pair[("1", "5")] is True  # margin 138.19, the golden's biggest blowout
    assert by_pair[("7", "12")] is False  # margin 6.06, the golden's closest game


def test_week10_marquee_is_three_versus_nine() -> None:
    """The spec's matrix pins the week-10 marquee as rosters 3 v 9 (both with a
    first-round bye still in reach)."""
    doc = _week10_facts()
    card = next(card for card in doc.matchups.next_week if card.game_of_week)
    assert {card.a_roster_id, card.b_roster_id} == {"3", "9"}


def test_week10_carries_the_week_nine_trade_and_the_week_ten_waiver() -> None:
    doc = _week10_facts()
    assert doc.transactions.market_note.last_trade_week == 9
    assert doc.transactions.recent_trades  # the week-9 trade is inside the window
    assert doc.transactions.this_week  # the week-10 waiver
    faabs = [move.faab for move in doc.transactions.this_week if move.faab is not None]
    assert 25 in faabs


def test_week10_teams_carry_this_week_and_next_week() -> None:
    doc = _week10_facts()
    for team in doc.teams:
        assert team.this_week is not None
        assert team.next_week is not None
        assert team.history.weekly  # ten weeks played
        assert team.season.record.w + team.season.record.l + team.season.record.t == 10


def test_this_week_starters_are_the_actual_lineup_not_the_optimal_one() -> None:
    """``starters`` must be what the manager actually started (``Matchup.starters``),
    never ``TeamLineup``'s optimal solve — the two differ whenever a roster has a
    ``bench_regret``, and a player cannot be both a starter and the bench_regret pick."""
    doc = _week10_facts()
    for team in doc.teams:
        tw = team.this_week
        assert tw is not None
        started_ids = {s.player_id for s in tw.starters if s.player_id is not None}
        assert sum(s.points for s in tw.starters) == pytest.approx(tw.points, abs=0.01)
        if tw.bench_regret is not None:
            assert tw.bench_regret.player_id not in started_ids


def test_week10_history_rows_cover_every_week_played() -> None:
    doc = _week10_facts()
    for team in doc.teams:
        assert [row.week for row in team.history.weekly] == list(range(1, 11))
        assert team.history.weekly[0].power_rank is None  # cold start
        assert team.history.weekly[0].all_play is None
        assert team.history.weekly[0].luck is None
        assert team.history.weekly[-1].power_rank is not None


def test_week10_bye_impact_names_the_starter_on_a_next_week_bye() -> None:
    doc = _week10_facts()
    flagged = [entry for card in doc.matchups.next_week for entry in (card.bye_impact or [])]
    assert flagged, "the week-10 fixture must start at least one player on a week-11 bye"
    assert {entry.nfl_team for entry in flagged} <= BYES_NEXT_WEEK
    for entry in flagged:
        assert entry.name  # names are resolved


def test_week10_era_started_on_bye_is_none_without_bye_data() -> None:
    doc = _week10_facts(nfl_byes_next_week=None)
    assert doc.period.nfl_byes_next_week is None
    for team in doc.teams:
        assert team.this_week is not None
        assert team.this_week.started_on_bye is None
        if team.next_week is not None:
            assert team.next_week.starters_on_bye == []
    for card in doc.matchups.next_week:
        assert card.bye_impact is None


def test_week10_playoff_picture_is_derived_for_the_league() -> None:
    doc = _week10_facts()
    picture = doc.standings.playoff_picture
    assert picture is not None
    assert picture.cut_line_after_rank == 6
    assert len(picture.in_bracket) == 6
    assert len(picture.byes) == 2
    assert doc.standings.overall  # ranked order
    assert doc.standings.through_week == 10


def test_week10_leaders_are_present_and_named() -> None:
    doc = _week10_facts()
    assert doc.leaders.week_high_player is not None
    assert doc.leaders.week_high_player.name
    assert len(doc.leaders.top_players) <= 5
    assert len(doc.leaders.worst_starters) <= 5
    assert doc.leaders.best_coaching is not None
    assert doc.leaders.worst_coaching is not None


# --------------------------------------------------------------------------- #
# Story 5.15 — playoff seeding
# --------------------------------------------------------------------------- #


def test_week10_regular_season_playoff_picture_is_not_unconfirmed() -> None:
    doc = _week10_facts()
    assert doc.standings.playoff_picture is not None
    assert doc.standings.playoff_picture.seeding_unconfirmed is False


def test_playoff_week_without_seeding_marks_derived_seeding_unconfirmed() -> None:
    doc = _weekly_facts("week17-playoffs.json")
    assert doc.standings.playoff_picture is not None
    assert doc.standings.playoff_picture.source == "derived"
    assert doc.standings.playoff_picture.seeding_unconfirmed is True
    assert doc.narration.playoff_picture is not None
    assert doc.narration.playoff_picture.seeding_unconfirmed is True


def test_playoff_seeding_flows_into_facts() -> None:
    from commishdesk.stats.standings import PlayoffSeeding, validate_playoff_seeding

    bundle = _bundle("week17-playoffs.json")
    week = build_week_model(bundle)
    league = build_league_model(bundle)
    roster_ids = [str(r.roster_id) for r in week.rosters]

    override = PlayoffSeeding(
        kind="override", seed_roster_ids=tuple(roster_ids[:6][::-1])
    )
    bracket = (
        league.format.playoff.bracket_teams
        if league.format.playoff is not None
        else None
    )
    validated = validate_playoff_seeding(
        override, roster_ids=roster_ids, bracket_teams=bracket
    )
    doc = build_weekly_facts(
        week,
        league,
        build_player_snapshot(bundle),
        build_player_names(bundle),
        generated_at=GENERATED_AT,
        playoff_seeding=validated,
    )
    assert doc.standings.playoff_picture is not None
    assert doc.standings.playoff_picture.source == "commissioner"
    assert doc.standings.playoff_picture.seeding_unconfirmed is False
    assert doc.narration.playoff_picture is not None
    assert doc.narration.playoff_picture.seeding_unconfirmed is False


# --------------------------------------------------------------------------- #
# Matrix: weekly lead candidates and cross-week storylines (Story 5.9)
# --------------------------------------------------------------------------- #


def _weekly_facts(
    fixture_name: str,
    *,
    previous_storylines: Any = (),
    playoff_seeding: Any = None,
) -> WeeklyFacts:
    bundle = _bundle(fixture_name)
    week = build_week_model(bundle)
    league = build_league_model(bundle)
    players = build_player_snapshot(bundle)
    names = build_player_names(bundle)
    return build_weekly_facts(
        week,
        league,
        players,
        names,
        generated_at=GENERATED_AT,
        previous_storylines=previous_storylines,
        playoff_seeding=playoff_seeding,
    )


def test_week10_lead_candidates_open_on_the_lineup_loss_angle() -> None:
    """FR-14 / AC1: the roster that lost a game it should have won leads, not
    the week's highest score."""
    doc = _week10_facts()
    assert [c.kind for c in doc.lead_candidates] == [
        "lineup_loss",
        "biggest_blowout",
        "closest_game",
        "week_high_score",
    ]
    assert doc.lead_candidates[0].roster_ids == ["7"]
    assert [c.rank for c in doc.lead_candidates] == [1, 2, 3, 4]
    assert all(c.hook for c in doc.lead_candidates)
    assert doc.lead_candidates == _week10_facts().lead_candidates  # deterministic


def test_lead_candidates_skip_lineup_loss_when_no_team_qualifies() -> None:
    """No roster left more on the bench than it lost by this week -> the next
    fired kind leads; the list is never empty (the ``week_high_score`` floor)."""
    week_model, league, players, names = _synthetic(12, week=5, playoff_week_start=10)
    doc = build_weekly_facts(week_model, league, players, names, generated_at=GENERATED_AT)
    kinds = [c.kind for c in doc.lead_candidates]
    assert kinds  # never empty
    assert "lineup_loss" not in kinds
    assert doc.lead_candidates[0].kind != "week_high_score"  # a higher-priority kind still fired


def test_weekly_storylines_carry_across_weeks_with_growing_weeks_running() -> None:
    """AD-14 / FR-20: a storyline active in one week's payload is active in a
    later week's, ``weeks_running`` growing with it -- traced on the real
    week02/week05 fixtures (same league), which happen to open and sustain
    ``luck_extreme:2``."""
    week2 = _weekly_facts("week02-nailbiter.json")
    opened = [c for c in week2.storyline_candidates if c.id == "luck_extreme:2"]
    assert opened and opened[0].weeks_running == 1

    from commishdesk.facts.storylines import advance_storylines

    after_week2 = advance_storylines((), kind="weekly", week=2, teams=week2.teams, period=week2.period)
    week5 = _weekly_facts("week05-trade.json", previous_storylines=after_week2)
    carried = [c for c in week5.storyline_candidates if c.id == "luck_extreme:2"]
    assert carried and carried[0].weeks_running == 4  # weeks 2,3,4,5

    # week05-trade.json also opens power_climb:8 fresh -- verified in review to
    # be otherwise unasserted anywhere in the suite (only luck_extreme:2 was
    # checked here). Pin its kind/roster/wording directly.
    climbed = [c for c in week5.storyline_candidates if c.id == "power_climb:8"]
    assert climbed and climbed[0].roster_ids == ["8"]
    assert climbed[0].hook == "Screen Pass Syndicate climbed five spots in the power ranks."


def test_weekly_storylines_grow_by_exactly_one_across_truly_consecutive_weeks() -> None:
    """AD-14 / FR-20, the matrix's literal claim: two genuinely back-to-back
    periods (N then N+1, no gap) grow ``weeks_running`` by exactly one. Hand
    -built ``WeeklyTeam`` rows (not a real fixture pair) because none of the
    committed weekly fixtures happen to open a storyline at week 1 that a
    real week 2 build could carry -- see the review note on this test."""
    from commishdesk.facts.schema import (
        WeeklyCoachingEfficiency,
        WeeklyHistory,
        WeeklyPeriod,
        WeeklyPower,
        WeeklyRecord,
        WeeklySeason,
        WeeklyTeam,
        WeekSummaryRef,
    )
    from commishdesk.facts.storylines import advance_storylines, project_storyline_candidates

    def _team(luck: float) -> WeeklyTeam:
        return WeeklyTeam(
            roster_id="1",
            manager="m1",
            team_name="Team One",
            season=WeeklySeason(
                record=WeeklyRecord(w=1, l=0, t=0),
                rank=1,
                points_for=0.0,
                points_against=0.0,
                avg_for=0.0,
                luck=luck,
                power=WeeklyPower(),
                coaching_efficiency=WeeklyCoachingEfficiency(actual=0.0, optimal=0.0),
            ),
            history=WeeklyHistory(),
        )

    summary = WeekSummaryRef(games=1, total_points=0.0, avg_team_score=0.0, blowout_count=0, blowout_threshold=0.65)
    period = WeeklyPeriod(week=1, type="regular", has_prior_week=False, summary=summary)
    week1_teams = [_team(2.0)]
    at_week1 = advance_storylines((), kind="weekly", week=1, teams=week1_teams, period=period)
    opened = [c for c in project_storyline_candidates(at_week1, kind="weekly") if c.id == "luck_extreme:1"]
    assert opened and opened[0].weeks_running == 1

    at_week2 = advance_storylines(at_week1, kind="weekly", week=2, teams=week1_teams, period=period)
    carried = [c for c in project_storyline_candidates(at_week2, kind="weekly") if c.id == "luck_extreme:1"]
    assert carried and carried[0].weeks_running == 2  # one more, not four


def test_luck_extreme_fires_the_unlucky_branch_independently_of_the_lucky_one() -> None:
    """Story 5.9 review: the ``season.luck <= -1.5`` branch was unexercised by
    every committed fixture. Hand-built ``WeeklyTeam`` rows drive both sides of
    the threshold directly, without authoring a new fixture file."""
    from commishdesk.facts.schema import (
        WeeklyCoachingEfficiency,
        WeeklyHistory,
        WeeklyPeriod,
        WeeklyPower,
        WeeklyRecord,
        WeeklySeason,
        WeeklyTeam,
        WeekSummaryRef,
    )
    from commishdesk.facts.storylines import advance_storylines

    def _team(roster_id: str, luck: float) -> WeeklyTeam:
        return WeeklyTeam(
            roster_id=roster_id,
            manager=f"m{roster_id}",
            team_name=f"Team {roster_id}",
            season=WeeklySeason(
                record=WeeklyRecord(w=1, l=0, t=0),
                rank=1,
                points_for=0.0,
                points_against=0.0,
                avg_for=0.0,
                luck=luck,
                power=WeeklyPower(),
                coaching_efficiency=WeeklyCoachingEfficiency(actual=0.0, optimal=0.0),
            ),
            history=WeeklyHistory(),
        )

    teams = [_team("1", 2.0), _team("2", -2.0)]
    summary = WeekSummaryRef(games=1, total_points=0.0, avg_team_score=0.0, blowout_count=0, blowout_threshold=0.65)
    period = WeeklyPeriod(week=5, type="regular", has_prior_week=True, summary=summary)
    storylines = advance_storylines((), kind="weekly", week=5, teams=teams, period=period)
    by_id = {s.id: s for s in storylines}
    assert by_id["luck_extreme:1"].headline.startswith("Team 1 has run the league's best luck")
    assert by_id["luck_extreme:2"].headline.startswith("Team 2 has run the league's worst luck")


def test_storyline_persists_through_a_mid_season_rename() -> None:
    """Storylines are keyed by roster id, never by name -- a team renamed
    between two builds keeps its storyline."""
    from commishdesk.facts.storylines import advance_storylines

    week2 = _weekly_facts("week02-nailbiter.json")
    after_week2 = advance_storylines((), kind="weekly", week=2, teams=week2.teams, period=week2.period)

    bundle5 = _bundle("week05-trade.json")
    week_model5 = build_week_model(bundle5)
    league5 = build_league_model(bundle5)
    renamed_teams = [
        t.model_copy(update={"team_name": "Brand New Name"}) if t.roster_id == "2" else t
        for t in league5.teams
    ]
    league5 = league5.model_copy(update={"teams": renamed_teams})
    week5 = build_weekly_facts(
        week_model5,
        league5,
        build_player_snapshot(bundle5),
        build_player_names(bundle5),
        generated_at=GENERATED_AT,
        previous_storylines=after_week2,
    )
    carried = [c for c in week5.storyline_candidates if c.id == "luck_extreme:2"]
    assert carried and carried[0].roster_ids == ["2"]
    assert "Brand New Name" in carried[0].hook  # the label follows the rename; the key does not


def test_weekly_storylines_stand_down_on_a_cold_start() -> None:
    """``luck_extreme`` / ``power_climb`` need a prior week (``None`` inputs);
    ``streak`` cannot reach its length-3 floor in one week either -- no weekly
    storyline can fire at week 1. Leads still compute."""
    week_model, league, players, names = _synthetic(12, week=1, playoff_week_start=10)
    doc = build_weekly_facts(week_model, league, players, names, generated_at=GENERATED_AT)
    assert doc.storyline_candidates == []
    assert doc.lead_candidates  # leads don't need history


def test_advance_storylines_prunes_resolved_threads_after_the_configured_window() -> None:
    """Story 5.9 / AC5 (deferred-work.md spec-3-1): a resolved thread more than
    ``STORYLINE_PRUNE_AFTER_WEEKS`` behind the current week is dropped; one
    still inside the window survives, and a passthrough row of the other kind
    is never pruned by this call regardless of its own age."""
    from commishdesk.facts.schema import Storyline
    from commishdesk.facts.storylines import STORYLINE_PRUNE_AFTER_WEEKS, advance_storylines

    aged_weekly = Storyline(
        id="luck_extreme:1", league_id="1", headline="old", status="resolved",
        first_week=1, last_week=2, kind="weekly",
    )
    aged_draft = Storyline(
        id="grade_extreme:3", league_id="1", headline="draft", status="resolved",
        first_week=1, last_week=1, kind="draft_recap",
    )
    previous = [aged_weekly, aged_draft]

    boundary_week = 2 + STORYLINE_PRUNE_AFTER_WEEKS  # exactly at the window -> kept
    at_boundary = advance_storylines(previous, kind="weekly", week=boundary_week, teams=[])
    assert {s.id for s in at_boundary} == {"luck_extreme:1", "grade_extreme:3"}

    past_window = boundary_week + 1  # one week further -> the weekly row is pruned
    pruned = advance_storylines(previous, kind="weekly", week=past_window, teams=[])
    assert {s.id for s in pruned} == {"grade_extreme:3"}  # draft passthrough survives untouched


def test_project_storyline_candidates_computes_weeks_running() -> None:
    """Story 5.9 / AC4 (deferred-work.md spec-3-1): ``weeks_running`` is
    ``last_week - first_week + 1``, not an incidental default."""
    from commishdesk.facts.schema import Storyline
    from commishdesk.facts.storylines import project_storyline_candidates

    fresh = Storyline(
        id="luck_extreme:1", league_id="1", headline="h", status="active", first_week=5, last_week=5, kind="weekly"
    )
    aged = Storyline(
        id="streak:2", league_id="1", headline="h2", status="active", first_week=3, last_week=7, kind="weekly"
    )
    candidates = {c.id: c for c in project_storyline_candidates([fresh, aged], kind="weekly")}
    assert candidates["luck_extreme:1"].weeks_running == 1
    assert candidates["streak:2"].weeks_running == 5


# --------------------------------------------------------------------------- #
# Matrix: committed oracle (byte-equal) — skipped when absent
# --------------------------------------------------------------------------- #


@requires_committed_oracle
def test_week10_matches_the_committed_oracle() -> None:
    """The whole built document against the committed snapshot, with the fixed
    ``generated_at``."""
    built = _week10_facts().model_dump(mode="json")
    expected = json.loads(EXPECTED_WEEKLY_PATH.read_text(encoding="utf-8"))
    expected = {k: v for k, v in expected.items() if not k.startswith("_")}
    assert built == expected


# --------------------------------------------------------------------------- #
# Matrix: golden reconcile (private, skip-gated) + always-on pins
# --------------------------------------------------------------------------- #

#: The raw week-10 picture the spec's golden pins (record, rank, points-for,
#: model rank) — copied literals so the always-on suite pins the matrix without
#: reading the private file, exactly like ``tests/test_stats_stakes.py``.
_WEEK10_PINNED = {
    "1": (7, 3, 0),
    "2": (9, 1, 0),
    "3": (7, 3, 0),
    "4": (4, 6, 0),
    "5": (6, 4, 0),
    "6": (5, 5, 0),
    "7": (2, 8, 0),
    "8": (4, 6, 0),
    "9": (6, 4, 0),
    "10": (3, 7, 0),
    "11": (2, 8, 0),
    "12": (5, 5, 0),
}


def test_week10_records_match_the_pinned_golden_sides() -> None:
    doc = _week10_facts()
    by_roster = {t.roster_id: t for t in doc.teams}
    for roster_id, (w, losses, t) in _WEEK10_PINNED.items():
        record = by_roster[roster_id].season.record
        assert (record.w, record.l, record.t) == (w, losses, t), roster_id


def test_week10_luck_is_wins_minus_expected_wins_for_every_team() -> None:
    """Story 5.13a: one luck definition, and the fixture agrees with it."""
    doc = _week10_facts().model_dump(mode="json")
    for team in doc["teams"]:
        season = team["season"]
        assert season["luck"] == pytest.approx(
            season["record"]["w"] + season["record"]["t"] / 2 - season["expected_wins"], abs=0.051
        ), team["roster_id"]


@requires_golden
def test_week10_luck_reconciles_with_the_phase0_golden() -> None:
    assert GOLDEN is not None
    doc = _week10_facts().model_dump(mode="json")
    golden_by_roster = {str(t["roster_id"]): t for t in GOLDEN["teams"]}
    for team in doc["teams"]:
        assert team["season"]["luck"] == pytest.approx(
            golden_by_roster[team["roster_id"]]["season"]["luck"], abs=0.05
        ), team["roster_id"]


@requires_golden
def test_week10_reconciles_with_the_phase0_golden() -> None:
    """By roster: record, rank, points for/against, all-play, model rank,
    this-week result and margin equal the golden's — or the row is pinned in
    ``docs/EDGE-CASES.md`` with the raw value and reason. ``season.luck`` is
    asserted in its own test; ``season.coaching_efficiency.pct`` is excluded —
    see the module docstring."""
    assert GOLDEN is not None
    doc = _week10_facts().model_dump(mode="json")
    golden_by_roster = {str(t["roster_id"]): t for t in GOLDEN["teams"]}
    for team in doc["teams"]:
        g = golden_by_roster[team["roster_id"]]
        assert team["season"]["record"] == g["season"]["record"]
        assert team["season"]["rank"] == g["season"]["rank"]
        assert team["season"]["points_for"] == pytest.approx(g["season"]["points_for"], abs=0.01)
        assert team["season"]["points_against"] == pytest.approx(g["season"]["points_against"], abs=0.01)
        assert team["season"]["power"]["model_rank"] == g["season"]["power"]["rank"]
        built_all_play = team["season"]["all_play"]
        golden_all_play = g["season"]["all_play"]
        assert (built_all_play["w"], built_all_play["l"]) == (golden_all_play["w"], golden_all_play["l"])
        assert built_all_play["pct"] == pytest.approx(golden_all_play["pct"], abs=0.01)
        # season.luck and season.coaching_efficiency.pct deliberately not
        # asserted here -- see the module docstring.
        if team["this_week"] is not None and g.get("this_week") is not None:
            assert team["this_week"]["result"] == g["this_week"]["result"]
            assert team["this_week"]["margin"] == pytest.approx(g["this_week"]["margin"], abs=0.01)


# --------------------------------------------------------------------------- #
# Matrix: cold start (week 1)
# --------------------------------------------------------------------------- #


def test_cold_start_week_one_carries_no_all_play_power_or_luck() -> None:
    week_model, league, players, names = _synthetic(12, week=1, playoff_week_start=10)
    doc = build_weekly_facts(week_model, league, players, names, generated_at=GENERATED_AT)
    for team in doc.teams:
        assert team.season.all_play is None
        assert team.season.expected_wins is None
        assert team.season.luck is None
        assert team.season.power.model_rank is None
        assert team.season.power.prev_model_rank is None
        assert team.season.power.week_delta is None
        assert [row.week for row in team.history.weekly] == [1]
        assert team.history.weekly[0].power_rank is None
        assert team.history.weekly[0].all_play is None
        assert team.history.weekly[0].luck is None


# --------------------------------------------------------------------------- #
# Matrix: bye / eliminated roster
# --------------------------------------------------------------------------- #


def test_a_roster_with_no_game_this_week_still_gets_a_row() -> None:
    week_model, league, players, names = _synthetic(12, week=5, playoff_week_start=10)
    # Drop roster 12's week-5 row (and its opponent's) — an eliminated roster.
    trimmed = [m for m in week_model.matchups if not (m.week == 5 and m.roster_id in {"11", "12"})]
    week_model = week_model.model_copy(update={"matchups": trimmed})
    doc = build_weekly_facts(week_model, league, players, names, generated_at=GENERATED_AT)

    by_roster = {t.roster_id: t for t in doc.teams}
    assert by_roster["12"].this_week is None
    assert by_roster["12"].next_week is None
    assert by_roster["12"].season.rank >= 1  # still in the standings
    assert "12" in doc.standings.overall


# --------------------------------------------------------------------------- #
# Matrix: bracket week
# --------------------------------------------------------------------------- #


def test_a_bracket_week_has_no_next_week_cards() -> None:
    bundle = _bundle("week17-playoffs.json")
    week = build_week_model(bundle)
    league = build_league_model(bundle)
    doc = build_weekly_facts(
        week,
        league,
        build_player_snapshot(bundle),
        build_player_names(bundle),
        generated_at=GENERATED_AT,
    )
    assert doc.matchups.next_week == []
    assert doc.period.type == "playoff"
    for team in doc.teams:
        assert team.next_week is None


# --------------------------------------------------------------------------- #
# Matrix: 32-team narration cap
# --------------------------------------------------------------------------- #


def test_a_32_team_league_keeps_the_narration_under_the_cap() -> None:
    from commishdesk.facts.schema import NARRATION_TOKEN_CAP

    week_model, league, players, names = _synthetic(32, week=5, playoff_week_start=10)
    doc = build_weekly_facts(week_model, league, players, names, generated_at=GENERATED_AT)
    assert len(doc.narration.model_dump_json()) <= NARRATION_TOKEN_CAP
    # the ladder must not have fired for a normal league
    assert doc.narration.luck != []
    assert doc.narration.power != []
    assert doc.narration.standings != []
    assert doc.narration.games != []


# --------------------------------------------------------------------------- #
# Matrix: unreducible
# --------------------------------------------------------------------------- #


def test_the_weekly_ladder_raises_when_it_cannot_reduce(monkeypatch) -> None:
    from commishdesk.facts import build as build_mod

    monkeypatch.setattr(build_mod, "NARRATION_TOKEN_CAP", 50)
    week_model, league, players, names = _synthetic(12, week=5, playoff_week_start=10)
    with pytest.raises(SchemaValidationError) as excinfo:
        build_weekly_facts(week_model, league, players, names, generated_at=GENERATED_AT)
    assert "NARRATION_TOKEN_CAP" in str(excinfo.value)


def test_the_ladder_drops_luck_then_the_power_block(monkeypatch) -> None:
    """Tier 1 drops ``luck[]`` and every power row's ``all_play``/``luck``;
    tier 3 drops ``power[]``. ``standings`` and ``games`` survive all of it."""
    from commishdesk.facts import build as build_mod

    full = _week10_facts()
    # A cap that forces every tier.
    monkeypatch.setattr(build_mod, "NARRATION_TOKEN_CAP", len(full.narration.model_dump_json()) - 1)
    trimmed = _week10_facts()

    assert trimmed.narration.standings == full.narration.standings
    assert trimmed.narration.games == full.narration.games
    assert trimmed.narration.lead_candidates == full.narration.lead_candidates
    assert len(trimmed.narration.model_dump_json()) < len(full.narration.model_dump_json())


# --------------------------------------------------------------------------- #
# Matrix: determinism
# --------------------------------------------------------------------------- #


def test_determinism_equal_model_dump() -> None:
    first = _week10_facts()
    second = _week10_facts()
    assert first.model_dump() == second.model_dump()
    assert first.model_dump_json() == second.model_dump_json()


# --------------------------------------------------------------------------- #
# Matrix: schema violation
# --------------------------------------------------------------------------- #


def test_a_missing_roster_stage_result_raises_a_typed_error(monkeypatch) -> None:
    """A malformed stage result inside the merge becomes a chained
    ``SchemaValidationError`` naming ``weekly``."""
    from commishdesk.facts import weekly as weekly_mod

    def _boom(*_a: Any, **_k: Any) -> None:
        raise KeyError("standings row missing")

    monkeypatch.setattr(weekly_mod, "_season_block", _boom)
    week_model, league, players, names = _synthetic(4, week=3, playoff_week_start=10)
    with pytest.raises(SchemaValidationError) as excinfo:
        build_weekly_facts(week_model, league, players, names, generated_at=GENERATED_AT)
    assert isinstance(excinfo.value, CommishDeskError)
    assert "weekly" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #


def test_an_unresolved_player_id_falls_back_to_the_team_then_the_id() -> None:
    week_model, league, players, names = _synthetic(4, week=3, playoff_week_start=10)
    # no name for p2 -> falls back to its snapshot nfl_team ("BUF")
    doc = build_weekly_facts(week_model, league, players, {}, generated_at=GENERATED_AT)
    names_seen = {starter.name for team in doc.teams for starter in (team.this_week.starters if team.this_week else [])}
    assert "BUF" in names_seen  # p2's team
    assert "KC" in names_seen  # p1's team


def test_a_player_missing_from_the_snapshot_falls_back_to_the_id() -> None:
    week_model, league, players, _names = _synthetic(4, week=3, playoff_week_start=10)
    doc = build_weekly_facts(week_model, league, players, {}, generated_at=GENERATED_AT)
    # every starter has *some* label (name, team, or id)
    for team in doc.teams:
        if team.this_week is None:
            continue
        for starter in team.this_week.starters:
            if starter.player_id is not None:
                assert starter.name


# --------------------------------------------------------------------------- #
# Fences
# --------------------------------------------------------------------------- #


def _imported_dotted_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module)
            elif node.level == 1:
                names.add(f"commishdesk.facts.{node.module}" if node.module else "commishdesk.facts")
        elif isinstance(node, ast.Call):
            func = node.func
            fname = getattr(func, "attr", None) or getattr(func, "id", None)
            if fname in {"import_module", "__import__"}:
                names.update(
                    a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)
                )
    return names


def test_weekly_never_reaches_the_network_adapters_store_or_a_later_stage() -> None:
    imported = _imported_dotted_names(STATS_WEEKLY)
    forbidden_roots = {"adapters", "store", "narrate", "render", "deliver", "statmods", "consensus"}
    offenders = [
        name
        for name in imported
        if name.split(".")[:1] == ["commishdesk"]
        and len(name.split(".")) > 1
        and name.split(".")[1] in forbidden_roots
    ]
    assert not offenders, offenders
    assert "commishdesk.ingest" in imported
    assert any(name.startswith("commishdesk.stats") for name in imported)
    assert "commishdesk.errors" in imported


def test_weekly_imports_no_clock_prng_or_filesystem_module() -> None:
    imported = _imported_dotted_names(STATS_WEEKLY)
    banned = {"os", "pathlib", "random", "secrets"}
    hits = {name for name in imported if name.split(".")[0] in banned}
    assert not hits, hits


def test_weekly_source_carries_no_hardcoded_league_shape() -> None:
    """AC: no team count, bracket size or slot list appears as a literal in
    ``facts/weekly.py`` — every one of them is read from the league's own
    ``format``."""
    tree = ast.parse(STATS_WEEKLY.read_text(encoding="utf-8"))
    integers = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool)
    }
    assert 12 not in integers  # a 12-team league is the fixture default
    assert 11 not in integers  # an 11-slot starting lineup
    assert 6 not in integers  # a six-team bracket
    assert 14 not in integers  # a 14-week regular season


def test_weekly_names_are_reachable_via_the_package_re_export() -> None:
    import commishdesk.facts as facts

    assert facts.build_weekly_facts is build_weekly_facts
    assert facts.WeeklyFacts is WeeklyFacts
    for name in ("WeeklyFacts", "build_weekly_facts"):
        assert name in facts.__all__


# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #


def test_tunables_are_named_module_constants() -> None:
    from commishdesk.facts import weekly

    assert weekly.TOP_PERFORMERS == 3
    assert weekly.DUD_POINTS == 6.0
    assert weekly.DUD_LIMIT == 3
    assert weekly.TOP_PLAYERS == 5
    assert weekly.WORST_STARTERS == 5


def test_generated_at_rule_is_shared_with_the_draft_recap_builder() -> None:
    from commishdesk.facts.weekly import _generated_at

    assert _generated_at("2026-09-07T00:00:00Z") == "2026-09-07T00:00:00Z"
    with pytest.raises(SchemaValidationError):
        _generated_at("")


# --------------------------------------------------------------------------- #
# Published-rank resolution (Story 5.12) — AC4 / the matrix's "first published
# week" and "held/skipped prior week" rows. ``_published_lookback`` is pure and
# unit-tested directly; the end-to-end wiring through ``build_weekly_facts`` is
# covered once below so a future refactor of the call site cannot silently stop
# threading ``previous_published_ranks`` through to ``WeeklyPower``.
# --------------------------------------------------------------------------- #


def test_published_lookback_with_no_history_is_all_none() -> None:
    from commishdesk.facts.weekly import _published_lookback

    assert _published_lookback("1", 10, None) == (None, None)
    assert _published_lookback("1", 10, {}) == (None, None)


def test_published_lookback_resolves_the_immediately_prior_week() -> None:
    from commishdesk.facts.weekly import _published_lookback

    history = {9: {"1": 3}}
    assert _published_lookback("1", 10, history) == (3, 3)


def test_published_lookback_falls_back_across_a_gap_but_delta_stays_null() -> None:
    """Week 9 (the immediately prior week) never published; week 7 did. The
    fallback still finds week 7's rank for ``prev_published_rank``, but
    ``published_week_delta`` is only ever computed from the *immediate* prior
    week, so the caller sees ``None`` for the second element here (the matrix's
    "held/skipped prior week" row)."""
    from commishdesk.facts.weekly import _published_lookback

    history = {7: {"1": 5}, 9: {"2": 1}}  # week 9 exists but never ranked roster "1"
    assert _published_lookback("1", 10, history) == (5, None)


def test_published_lookback_ignores_weeks_at_or_after_the_target() -> None:
    from commishdesk.facts.weekly import _published_lookback

    history = {10: {"1": 1}, 11: {"1": 2}}
    assert _published_lookback("1", 10, history) == (None, None)


def test_published_lookback_skips_a_roster_the_found_week_never_ranked() -> None:
    from commishdesk.facts.weekly import _published_lookback

    history = {9: {"2": 4}}  # roster "1" absent from the one week that exists
    assert _published_lookback("1", 10, history) == (None, None)


def test_week10_build_resolves_published_rank_from_the_immediately_prior_week() -> None:
    """End-to-end through ``build_weekly_facts``: a caller-supplied
    ``previous_published_ranks`` reaches ``WeeklyPower.prev_published_rank`` /
    ``published_week_delta`` for the roster it names, and leaves every other
    roster's pair ``None`` (matches the committed oracle's all-``None`` baseline
    when no history is supplied at all)."""
    doc = _week10_facts()
    by_roster = {t.roster_id: t for t in doc.teams}
    model_rank_1 = by_roster["1"].season.power.model_rank
    assert model_rank_1 is not None

    bundle = _bundle(WEEK10)
    week = build_week_model(bundle)
    league = build_league_model(bundle)
    players = build_player_snapshot(bundle)
    names = build_player_names(bundle)
    published = build_weekly_facts(
        week,
        league,
        players,
        names,
        generated_at=GENERATED_AT,
        nfl_byes_next_week=BYES_NEXT_WEEK,
        previous_published_ranks={9: {"1": model_rank_1 + 1}},
    )
    by_roster_published = {t.roster_id: t for t in published.teams}
    power_1 = by_roster_published["1"].season.power
    assert power_1.prev_published_rank == model_rank_1 + 1
    assert power_1.published_week_delta == 1
    # A roster the supplied history never mentions stays null, same as the
    # no-history baseline.
    power_2 = by_roster_published["2"].season.power
    assert power_2.prev_published_rank is None
    assert power_2.published_week_delta is None


# --------------------------------------------------------------------------- #
# Story 5.13a: the hand-authored published-rank overlay fixture
# --------------------------------------------------------------------------- #


def _overlay() -> dict[str, Any]:
    return json.loads(PUBLISHED_OVERLAY_PATH.read_text(encoding="utf-8"))


def test_published_overlay_validates_and_differs_from_the_base_only_in_the_rank_layer() -> None:
    overlay = _overlay()
    WeeklyFacts.model_validate({k: v for k, v in overlay.items() if not k.startswith("_")})
    base = json.loads(EXPECTED_WEEKLY_PATH.read_text(encoding="utf-8"))

    def scrub(doc: dict[str, Any]) -> dict[str, Any]:
        doc = json.loads(json.dumps({k: v for k, v in doc.items() if not k.startswith("_")}))
        for team in doc["teams"]:
            for key in ("published_rank", "nudge", "prev_published_rank", "published_week_delta"):
                team["season"]["power"][key] = None
        for row in doc["narration"]["power"]:
            row["published_rank"] = None
            row["nudge_justification"] = None
        return doc

    assert scrub(overlay) == scrub(base)


def test_published_overlay_swaps_model_ranks_7_and_8_with_a_cited_justification() -> None:
    overlay = _overlay()
    power = {t["roster_id"]: t["season"]["power"] for t in overlay["teams"]}
    swapped = {rid: p for rid, p in power.items() if p["nudge"]}
    assert {p["model_rank"]: p["published_rank"] for p in swapped.values()} == {7: 8, 8: 7}
    for p in swapped.values():
        assert p["nudge"] == p["model_rank"] - p["published_rank"]
        assert p["published_week_delta"] == p["prev_published_rank"] - p["model_rank"]
    assert all(p["published_rank"] is None for rid, p in power.items() if rid not in swapped)

    rows = overlay["narration"]["power"]
    payload = json.dumps(
        [{k: v for k, v in r.items() if k != "nudge_justification"} for r in rows]
    )
    for row in rows:
        if row["roster_id"] in swapped:
            reason = row["nudge_justification"]
            assert reason
            # every figure the reason cites is a record present in the payload
            for record in re.findall(r"\d+-\d+", reason):
                assert record in payload, record
        else:
            assert row["nudge_justification"] is None
