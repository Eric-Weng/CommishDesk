"""Story 5.6: ``compute_standings`` -- the matchup-derived standings table, the
derived playoff picture, and the Sleeper cross-check.

One test per row of the spec's I/O & Edge-Case Matrix on hand-built
:class:`~commishdesk.ingest.WeekModel` / :class:`~commishdesk.ingest.LeagueModel`
scenarios, plus the committed fixtures run end to end, determinism, the ordering
rule, the "no hardcoded bracket size" guard, and the import-fence pair that keeps
``stats/standings.py`` off the network, the store, and every later pipeline
stage.

Reconciliation mirrors ``tests/test_stats_lineup.py``. The always-on CI oracle is
the committed fixtures themselves, plus the week-10 rank/division/playoff-picture
order the story's spec pins verbatim (it is copied from the golden, so it stays
useful without the golden). The **phase-0 golden**
(``brief/phase-0/week10-facts.json``) is a private planning artifact that is not
committed to this repo (CLAUDE.md s1), so the ``@requires_golden`` checks below
only run in a workspace that has the sibling ``../brief/`` directory.

The cross-check has a real always-on pass path (``week17-playoffs.json``'s roster
totals are regular-season-final, so W-L-T matches exactly and points-for matches
within the documented per-week rounding tolerance) and a real always-on mismatch
path (``week10-blowout.json``'s roster totals are season-final, so the fold
disagrees by construction).

Story 5.15 adds the ``--seeding`` confirm/override tests: the parse/validate
unit rows on hand-built inputs, and the compute_standings reorder/confirm rows.
"""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from commishdesk.errors import CommishDeskError, CrossCheckError, CrossCheckMismatch
from commishdesk.ingest import (
    Division,
    Draft,
    LeagueFormat,
    LeagueModel,
    Matchup,
    PlayoffFormat,
    Roster,
    Team,
    WeekModel,
    build_league_model,
    build_week_model,
)
from commishdesk.stats import (
    POINTS_FOR_ROUNDING_TOLERANCE,
    TIEBREAK,
    DivisionOrder,
    PlayoffPicture,
    Standings,
    Streak,
    TeamStanding,
    WeekPoints,
    compute_standings,
    cross_check_standings,
    regular_season_records,
)
from tests.conftest import REPO_ROOT

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
STATS_STANDINGS = REPO_ROOT / "commishdesk" / "stats" / "standings.py"

FIXTURES = (
    "week01-openers.json",
    "week08-median.json",
    "week10-blowout.json",
    "week10-superflex.json",
    "week17-playoffs.json",
)

WEEK10 = "week10-blowout.json"
WEEK17 = "week17-playoffs.json"

# The phase-0 golden lives in the sibling ``../brief/`` tree -- deliberately not
# in this repo (CLAUDE.md s1). Every check that reads it is skip-gated, exactly
# like ``requires_golden`` in ``tests/test_stats_lineup.py``.
_GOLDEN_DIR = REPO_ROOT.parent / "brief" / "phase-0"
_GOLDEN_PATH = _GOLDEN_DIR / "week10-facts.json"
GOLDEN = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8")) if _GOLDEN_PATH.is_file() else None
requires_golden = pytest.mark.skipif(
    GOLDEN is None,
    reason="phase-0 golden is a private planning artifact, not in the tree",
)

# The week-10 order copied verbatim from the golden ``week10-facts.json``: overall
# rank, division order, and the derived playoff picture. Always-on, so an
# ordering, fold or playoffs regression on the real fixture cannot hide behind the
# skip-gated golden checks.
_WEEK10_OVERALL = ["2", "1", "3", "9", "5", "6", "12", "8", "4", "10", "11", "7"]
_WEEK10_DIVISIONS = {
    1: ["3", "5", "4", "11"],
    2: ["2", "9", "6", "7"],
    3: ["1", "12", "8", "10"],
}
_WEEK10_IN_BRACKET = ["2", "1", "3", "9", "5", "6"]
_WEEK10_BYES = ["2", "1"]
_WEEK10_FIRST_OUT = "12"
_WEEK10_BUBBLE = ["5", "6", "12", "8"]
_WEEK10_CONSOLATION = ["12", "8", "4", "10", "11", "7"]
_WEEK10_CUT_LINE = 6

# Per-roster week-10 table, copied from the phase-0 golden (values independently
# hand-verified): (W, L, T, PF, PA, streak type, streak count, high (week, pts),
# low (week, pts)). Always-on, so the matrix row does not depend on the golden.
_WEEK10_TABLE = {
    "1": (7, 3, 0, 2100.85, 1683.24, "W", 2, (1, 232.86), (5, 178.99)),
    "2": (9, 1, 0, 2028.72, 1727.66, "W", 2, (10, 242.03), (8, 135.0)),
    "3": (7, 3, 0, 1816.51, 1561.37, "W", 1, (10, 247.2), (8, 149.82)),
    "4": (4, 6, 0, 1681.22, 1618.53, "W", 3, (8, 211.93), (1, 123.35)),
    "5": (6, 4, 0, 1558.93, 1740.42, "L", 1, (5, 179.9), (10, 91.77)),
    "6": (5, 5, 0, 1665.19, 1835.06, "L", 1, (8, 236.84), (6, 93.14)),
    "7": (2, 8, 0, 1436.99, 1695.09, "L", 3, (1, 179.14), (9, 114.61)),
    "8": (4, 6, 0, 1723.63, 1789.08, "W", 1, (5, 224.67), (7, 136.79)),
    "9": (6, 4, 0, 2037.57, 1746.36, "L", 2, (5, 263.99), (10, 117.13)),
    "10": (3, 7, 0, 1632.78, 1771.63, "L", 2, (1, 196.58), (10, 112.37)),
    "11": (2, 8, 0, 1475.64, 1839.21, "L", 6, (2, 208.38), (8, 68.39)),
    "12": (5, 5, 0, 1401.41, 1551.79, "W", 2, (2, 193.52), (1, 92.1)),
}


# --------------------------------------------------------------------------- #
# Helpers -- hand-build a WeekModel / LeagueModel from compact rows
# --------------------------------------------------------------------------- #


def _roster(
    roster_id: str,
    *,
    wins: int = 0,
    losses: int = 0,
    ties: int = 0,
    fpts: float = 0.0,
) -> Roster:
    return Roster(roster_id=roster_id, wins=wins, losses=losses, ties=ties, fpts=fpts)


def _matchup(week: int, roster_id: str, points: float, opponent: str | None = None) -> Matchup:
    return Matchup(week=week, roster_id=roster_id, opponent_roster_id=opponent, points=points)


def _week_model(
    week: int,
    rosters: list[Roster],
    matchups: list[Matchup],
    *,
    playoff_week_start: int | None = None,
) -> WeekModel:
    return WeekModel(
        week=week,
        rosters=list(rosters),
        matchups=list(matchups),
        transactions=[],
        playoff_week_start=playoff_week_start,
    )


def _league_model(
    roster_slots: list[str],
    flex_eligibility: dict[str, list[str]] | None = None,
    league_id: str = "id_league",
    *,
    divisions: list[Division] | None = None,
    teams: list[Team] | None = None,
    playoff_teams: int | None = None,
) -> LeagueModel:
    return LeagueModel(
        league_id=league_id,
        name="Test League",
        season=2025,
        format=LeagueFormat(
            team_count=2,
            roster_slots=list(roster_slots),
            flex_eligibility=dict(flex_eligibility or {}),
            scoring_label="PPR",
            is_superflex_or_2qb=False,
            te_premium=False,
            divisions=list(divisions or []),
            playoff=None if playoff_teams is None else PlayoffFormat(bracket_teams=playoff_teams),
        ),
        teams=list(teams or []),
        picks=[],
        draft=Draft(id="id_draft"),
    )


def _team(standings: Standings, roster_id: str) -> TeamStanding:
    return next(team for team in standings.teams if team.roster_id == roster_id)


def _bundle(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _run(name: str) -> Standings:
    bundle = _bundle(name)
    return compute_standings(build_week_model(bundle), build_league_model(bundle))


# --------------------------------------------------------------------------- #
# Row: the ordering rule -- win %, then points-for, then roster_id
# --------------------------------------------------------------------------- #


def test_points_for_decides_a_record_tie() -> None:
    league = _league_model(["QB"])
    week = _week_model(
        1,
        [_roster("1"), _roster("2"), _roster("3"), _roster("4")],
        [
            _matchup(1, "1", 90.0, "3"),
            _matchup(1, "3", 10.0, "1"),
            _matchup(1, "2", 110.0, "4"),
            _matchup(1, "4", 20.0, "2"),
        ],
    )
    result = compute_standings(week, league)

    assert [team.roster_id for team in result.teams] == ["2", "1", "4", "3"]
    assert [team.rank for team in result.teams] == [1, 2, 3, 4]
    assert _team(result, "2").points_for == 110.0
    assert _team(result, "2").points_against == 20.0
    assert _team(result, "1").points_against == 10.0
    assert result.tiebreak == TIEBREAK == "points_for"


def test_roster_id_breaks_an_equal_record_and_equal_points_for() -> None:
    league = _league_model(["QB"])
    week = _week_model(
        1,
        [_roster("1"), _roster("2"), _roster("3"), _roster("4")],
        [
            _matchup(1, "1", 100.0, "3"),
            _matchup(1, "3", 10.0, "1"),
            _matchup(1, "2", 100.0, "4"),
            _matchup(1, "4", 10.0, "2"),
        ],
    )
    result = compute_standings(week, league)
    assert [team.roster_id for team in result.teams] == ["1", "2", "3", "4"]


def test_a_tied_game_is_half_a_win_and_a_t_streak() -> None:
    league = _league_model(["QB"])
    week = _week_model(
        1,
        [_roster("1"), _roster("2")],
        [_matchup(1, "1", 100.0, "2"), _matchup(1, "2", 100.0, "1")],
    )
    result = compute_standings(week, league)

    for roster_id in ("1", "2"):
        team = _team(result, roster_id)
        assert (team.wins, team.losses, team.ties) == (0, 0, 1)
        assert team.win_pct == 0.5
        assert team.streak == Streak(type="T", count=1)
        assert team.points_for == 100.0
        assert team.points_against == 100.0


def test_a_bye_week_is_no_game_but_still_counts_toward_points_for() -> None:
    league = _league_model(["QB"])
    week = _week_model(
        1,
        [_roster("1"), _roster("2"), _roster("3")],
        [
            _matchup(1, "1", 80.0),
            _matchup(1, "2", 90.0, "3"),
            _matchup(1, "3", 30.0, "2"),
        ],
    )
    result = compute_standings(week, league)
    bye = _team(result, "1")

    assert (bye.wins, bye.losses, bye.ties) == (0, 0, 0)
    assert bye.win_pct == 0.0
    assert bye.points_for == 80.0
    assert bye.points_against == 0.0
    assert bye.streak is None
    assert bye.high_week == WeekPoints(week=1, points=80.0)
    assert bye.low_week == WeekPoints(week=1, points=80.0)


def test_streak_counts_consecutive_games_and_a_bye_neither_extends_nor_breaks_it() -> None:
    league = _league_model(["QB"])
    week = _week_model(
        4,
        [_roster("1"), _roster("2")],
        [
            _matchup(1, "1", 10.0, "2"),  # L
            _matchup(1, "2", 20.0, "1"),
            _matchup(3, "1", 30.0, "2"),  # W (week 2 is a bye for both)
            _matchup(3, "2", 10.0, "1"),
            _matchup(4, "1", 40.0, "2"),  # W
            _matchup(4, "2", 10.0, "1"),
        ],
    )
    result = compute_standings(week, league)
    assert _team(result, "1").streak == Streak(type="W", count=2)
    assert _team(result, "2").streak == Streak(type="L", count=2)
    assert (_team(result, "1").wins, _team(result, "1").losses) == (2, 1)


def test_high_and_low_week_prefer_the_earlier_week_on_a_tie() -> None:
    league = _league_model(["QB"])
    week = _week_model(
        2,
        [_roster("1")],
        [_matchup(1, "1", 50.0), _matchup(2, "1", 50.0)],
    )
    result = compute_standings(week, league)
    team = _team(result, "1")
    assert team.high_week == WeekPoints(week=1, points=50.0)
    assert team.low_week == WeekPoints(week=1, points=50.0)


def test_a_roster_with_no_matchup_at_all_is_a_zero_row() -> None:
    league = _league_model(["QB"])
    week = _week_model(
        2,
        [_roster("1"), _roster("9")],
        [_matchup(1, "1", 40.0, "2"), _matchup(1, "2", 30.0, "1")],
    )
    result = compute_standings(week, league)
    idle = _team(result, "9")
    assert (idle.wins, idle.losses, idle.ties) == (0, 0, 0)
    assert idle.win_pct == 0.0
    assert idle.points_for == 0.0
    assert idle.streak is None
    assert idle.high_week is None and idle.low_week is None


# --------------------------------------------------------------------------- #
# Row: divisions -- present and absent
# --------------------------------------------------------------------------- #


def test_no_divisions_leaves_division_rank_none() -> None:
    league = _league_model(["QB"])
    week = _week_model(1, [_roster("1")], [_matchup(1, "1", 10.0)])
    result = compute_standings(week, league)

    assert result.divisions == []
    assert _team(result, "1").division_rank is None


def test_divisions_carry_their_own_order_with_the_same_tiebreak() -> None:
    league = _league_model(
        ["QB"],
        divisions=[Division(id=1, name="East"), Division(id=2, name="West")],
        teams=[
            Team(roster_id="1", division_id=1),
            Team(roster_id="2", division_id=2),
            Team(roster_id="3", division_id=1),
        ],
    )
    week = _week_model(
        1,
        [_roster("1"), _roster("2"), _roster("3")],
        [
            _matchup(1, "1", 30.0, "3"),
            _matchup(1, "3", 10.0, "1"),
            _matchup(1, "2", 50.0),
        ],
    )
    result = compute_standings(week, league)

    assert result.divisions == [
        DivisionOrder(division_id=1, roster_ids=["1", "3"]),
        DivisionOrder(division_id=2, roster_ids=["2"]),
    ]
    assert _team(result, "1").division_rank == 1
    assert _team(result, "3").division_rank == 2
    assert _team(result, "2").division_rank == 1


# --------------------------------------------------------------------------- #
# Row: the regular-season freeze
# --------------------------------------------------------------------------- #


def test_regular_season_complete_engages_at_playoff_week_start_minus_one() -> None:
    league = _league_model(["QB"])
    week = _week_model(3, [_roster("1")], [_matchup(1, "1", 10.0)], playoff_week_start=4)
    result = compute_standings(week, league)

    assert result.week == 3
    assert result.through_week == 3
    assert result.regular_season_complete is True


def test_playoff_week_pins_standings_at_the_last_regular_season_week() -> None:
    league = _league_model(["QB"])
    week = _week_model(
        5,
        [_roster("1")],
        [_matchup(1, "1", 10.0), _matchup(5, "1", 999.0)],
        playoff_week_start=4,
    )
    result = compute_standings(week, league)

    assert result.through_week == 3
    assert result.regular_season_complete is True
    assert _team(result, "1").points_for == 10.0


# --------------------------------------------------------------------------- #
# Row: the derived playoff picture
# --------------------------------------------------------------------------- #


def test_playoff_picture_is_none_without_a_playoff_format() -> None:
    league = _league_model(["QB"])
    week = _week_model(1, [_roster("1")], [_matchup(1, "1", 10.0)])
    assert compute_standings(week, league).playoff_picture is None


def test_playoff_picture_handles_a_bracket_larger_than_the_league() -> None:
    league = _league_model(["QB"], playoff_teams=8)
    week = _week_model(
        1,
        [_roster("1"), _roster("2"), _roster("3"), _roster("4")],
        [
            _matchup(1, "1", 30.0, "2"),
            _matchup(1, "2", 20.0, "1"),
            _matchup(1, "3", 10.0, "4"),
            _matchup(1, "4", 5.0, "3"),
        ],
    )
    picture = compute_standings(week, league).playoff_picture

    assert picture == PlayoffPicture(
        source="derived",
        in_bracket=["1", "3", "2", "4"],
        byes=[],
        first_out=None,
        bubble=["2", "4"],
        cut_line_after_rank=4,
        consolation=[],
    )


def test_playoff_picture_byes_and_cut_line_are_derived_from_the_bracket_size() -> None:
    league = _league_model(["QB"], playoff_teams=6)
    roster_ids = [str(index) for index in range(1, 13)]
    matchups: list[Matchup] = []
    for index, roster_id in enumerate(roster_ids):
        matchups.append(_matchup(1, roster_id, 100.0 - index))
    week = _week_model(1, [_roster(roster_id) for roster_id in roster_ids], matchups)
    picture = compute_standings(week, league).playoff_picture

    assert picture is not None
    assert picture.source == "derived"
    assert picture.in_bracket == roster_ids[:6]
    assert picture.byes == roster_ids[:2]
    assert picture.first_out == "7"
    assert picture.bubble == roster_ids[4:8]
    assert picture.cut_line_after_rank == 6
    assert picture.consolation == roster_ids[6:]


@pytest.mark.parametrize(
    ("bracket", "byes"),
    [(1, 0), (2, 0), (3, 1), (4, 0), (5, 3), (6, 2), (7, 1), (8, 0), (12, 4)],
)
def test_bye_count_is_the_gap_to_the_next_power_of_two(bracket: int, byes: int) -> None:
    league = _league_model(["QB"], playoff_teams=bracket)
    roster_ids = [str(index) for index in range(1, 13)]
    matchups = [_matchup(1, roster_id, 100.0 - index) for index, roster_id in enumerate(roster_ids)]
    week = _week_model(1, [_roster(roster_id) for roster_id in roster_ids], matchups)
    picture = compute_standings(week, league).playoff_picture

    assert picture is not None
    assert picture.in_bracket == roster_ids[:bracket]
    assert len(picture.byes) == byes
    assert picture.byes == roster_ids[:byes]
    assert picture.cut_line_after_rank == bracket
    assert picture.consolation == roster_ids[bracket:]
    # ranks N-1..N+2, clipped to the table
    assert picture.bubble == roster_ids[max(bracket - 2, 0) : bracket + 2]


def test_a_dangling_or_self_paired_opponent_is_no_game() -> None:
    league = _league_model(["QB"])
    week = _week_model(
        1,
        [_roster("1"), _roster("2")],
        [_matchup(1, "1", 100.0, "9"), _matchup(1, "2", 50.0, "2")],
    )
    result = compute_standings(week, league)

    for roster_id, points in (("1", 100.0), ("2", 50.0)):
        team = _team(result, roster_id)
        assert (team.wins, team.losses, team.ties) == (0, 0, 0)
        assert team.points_against == 0.0
        assert team.streak is None
        assert team.points_for == points


def test_an_orphan_matchup_row_still_counts_against_its_opponent() -> None:
    """A roster absent from ``week.rosters`` has no standings row, but the game
    it played still counts for the roster it played."""
    league = _league_model(["QB"])
    week = _week_model(
        1,
        [_roster("1")],
        [_matchup(1, "1", 100.0, "9"), _matchup(1, "9", 60.0, "1")],
    )
    result = compute_standings(week, league)

    assert [team.roster_id for team in result.teams] == ["1"]
    team = _team(result, "1")
    assert (team.wins, team.losses, team.ties) == (1, 0, 0)
    assert team.points_against == 60.0


def test_standings_source_carries_no_hardcoded_bracket_size() -> None:
    """AC: no bracket size, bye count, or consolation size appears as a literal
    in ``stats/standings.py`` -- every one of them is read from
    ``league.format.playoff``."""
    tree = ast.parse(STATS_STANDINGS.read_text(encoding="utf-8"))
    integers = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
    }
    assert _WEEK10_CUT_LINE not in integers
    assert len(_WEEK10_OVERALL) not in integers


# --------------------------------------------------------------------------- #
# Story 5.15 — playoff seeding parse/validate/reorder
# --------------------------------------------------------------------------- #


def test_playoff_seeding_parse_handles_syntax_rows() -> None:
    from commishdesk.stats.standings import (
        PlayoffSeedingError,
        parse_playoff_seeding,
    )

    assert parse_playoff_seeding(None) is None
    assert parse_playoff_seeding("") is None
    assert parse_playoff_seeding("   ") is None

    parsed = parse_playoff_seeding("confirm")
    assert parsed is not None and parsed.kind == "confirm"

    parsed = parse_playoff_seeding("4, 2,7 ,1")
    assert parsed is not None
    assert parsed.kind == "override"
    assert parsed.seed_roster_ids == ("4", "2", "7", "1")

    with pytest.raises(PlayoffSeedingError, match="empty entry"):
        parse_playoff_seeding("4,,2")
    with pytest.raises(PlayoffSeedingError, match="cannot be mixed"):
        parse_playoff_seeding("confirm,4")
    with pytest.raises(PlayoffSeedingError, match="duplicate"):
        parse_playoff_seeding("4,2,4")


def test_playoff_seeding_validate_names_unknown_duplicate_count_and_no_playoff() -> None:
    from commishdesk.stats.standings import (
        PlayoffSeeding,
        PlayoffSeedingError,
        validate_playoff_seeding,
    )

    roster_ids = [str(i) for i in range(1, 6)]

    with pytest.raises(PlayoffSeedingError, match="no playoff format"):
        validate_playoff_seeding(
            PlayoffSeeding(kind="confirm"), roster_ids=roster_ids, bracket_teams=None
        )

    with pytest.raises(PlayoffSeedingError, match="needs exactly 4 seeds, got 3"):
        validate_playoff_seeding(
            PlayoffSeeding(kind="override", seed_roster_ids=("4", "2", "3")),
            roster_ids=roster_ids,
            bracket_teams=4,
        )

    with pytest.raises(PlayoffSeedingError, match="99.*not a roster"):
        validate_playoff_seeding(
            PlayoffSeeding(kind="override", seed_roster_ids=("4", "2", "99", "1")),
            roster_ids=roster_ids,
            bracket_teams=4,
        )


def test_playoff_seeding_validate_accepts_confirm_when_a_bracket_exists() -> None:
    from commishdesk.stats.standings import PlayoffSeeding, validate_playoff_seeding

    confirm = PlayoffSeeding(kind="confirm")
    assert validate_playoff_seeding(confirm, roster_ids=["1", "2"], bracket_teams=4) is confirm


def test_seeding_override_reorders_standings_and_picture() -> None:
    from commishdesk.stats.standings import PlayoffSeeding

    league = _league_model(["QB"], playoff_teams=4)
    week = _week_model(
        1,
        [_roster(str(i)) for i in range(1, 6)],
        [],
    )
    standings = compute_standings(
        week,
        league,
        seeding=PlayoffSeeding(
            kind="override", seed_roster_ids=("4", "2", "3", "1")
        ),
    )

    assert [team.roster_id for team in standings.teams] == ["4", "2", "3", "1", "5"]
    picture = standings.playoff_picture
    assert picture is not None
    assert picture.source == "commissioner"
    assert picture.in_bracket == ["4", "2", "3", "1"]
    assert picture.first_out == "5"


def test_seeding_confirm_marks_picture_confirmed() -> None:
    from commishdesk.stats.standings import PlayoffSeeding

    league = _league_model(["QB"], playoff_teams=4)
    week = _week_model(
        1,
        [_roster(str(i)) for i in range(1, 6)],
        [],
    )
    standings = compute_standings(
        week, league, seeding=PlayoffSeeding(kind="confirm")
    )
    assert standings.playoff_picture is not None
    assert standings.playoff_picture.source == "confirmed"
    assert [team.roster_id for team in standings.teams] == [str(i) for i in range(1, 6)]


# --------------------------------------------------------------------------- #
# Row: the cross-check
# --------------------------------------------------------------------------- #


def test_cross_check_mismatch_lists_only_the_offending_roster() -> None:
    league = _league_model(["QB"])
    week = _week_model(
        4,
        [
            _roster("1", wins=1, ties=3, fpts=105.0),  # 5.0 above the fold
            _roster("2", losses=1, ties=3, fpts=90.015),  # inside the rounding tolerance
        ],
        [
            _matchup(1, "1", 100.0, "2"),
            _matchup(1, "2", 90.0, "1"),
            _matchup(2, "2", 0.0, "1"),
            _matchup(2, "1", 0.0, "2"),
            _matchup(3, "2", 0.0, "1"),
            _matchup(3, "1", 0.0, "2"),
            _matchup(4, "2", 0.0, "1"),
            _matchup(4, "1", 0.0, "2"),
        ],
    )
    standings = compute_standings(week, league)

    with pytest.raises(CrossCheckError) as excinfo:
        cross_check_standings(standings, week)

    error = excinfo.value
    assert isinstance(error, CommishDeskError)
    assert error.mismatches == (
        CrossCheckMismatch(roster_id="1", field="points_for", computed=100.0, sleeper=105.0),
    )
    message = str(error)
    assert "tools/anonymize.py" in message
    assert "CONTRIBUTING.md" in message
    assert "100.0" in message and "105.0" in message


def test_cross_check_passes_when_the_totals_agree() -> None:
    league = _league_model(["QB"])
    week = _week_model(
        1,
        [_roster("1", wins=1, fpts=100.0), _roster("2", losses=1, fpts=90.0)],
        [_matchup(1, "1", 100.0, "2"), _matchup(1, "2", 90.0, "1")],
    )
    assert cross_check_standings(compute_standings(week, league), week) is None


def test_cross_check_ignores_a_points_for_drift_inside_the_tolerance() -> None:
    league = _league_model(["QB"])
    week = _week_model(
        2,
        [
            _roster("1", wins=2, fpts=200.0 + 2 * POINTS_FOR_ROUNDING_TOLERANCE),
            _roster("2", losses=2, fpts=90.0),
        ],
        [
            _matchup(1, "1", 100.0, "2"),
            _matchup(1, "2", 50.0, "1"),
            _matchup(2, "1", 100.0, "2"),
            _matchup(2, "2", 40.0, "1"),
        ],
    )
    assert cross_check_standings(compute_standings(week, league), week) is None


def test_cross_check_flags_a_ties_only_disagreement_between_losses_and_points_for() -> None:
    league = _league_model(["QB"])
    week = _week_model(
        1,
        [_roster("1", wins=0, losses=0, ties=0, fpts=100.0), _roster("2", ties=1, fpts=100.0)],
        [_matchup(1, "1", 100.0, "2"), _matchup(1, "2", 100.0, "1")],
    )
    standings = compute_standings(week, league)

    with pytest.raises(CrossCheckError) as excinfo:
        cross_check_standings(standings, week)

    assert [(m.roster_id, m.field, m.computed, m.sleeper) for m in excinfo.value.mismatches] == [
        ("1", "ties", 1, 0),
    ]


def test_cross_check_flags_a_points_for_drift_just_outside_the_tolerance() -> None:
    league = _league_model(["QB"])
    week = _week_model(
        2,
        [
            _roster("1", wins=2, fpts=200.0 + 2 * POINTS_FOR_ROUNDING_TOLERANCE + 0.01),
            _roster("2", losses=2, fpts=90.0),
        ],
        [
            _matchup(1, "1", 100.0, "2"),
            _matchup(1, "2", 50.0, "1"),
            _matchup(2, "1", 100.0, "2"),
            _matchup(2, "2", 40.0, "1"),
        ],
    )
    with pytest.raises(CrossCheckError) as excinfo:
        cross_check_standings(compute_standings(week, league), week)
    assert [(m.roster_id, m.field) for m in excinfo.value.mismatches] == [("1", "points_for")]


def test_cross_check_reports_every_mismatch_in_roster_field_order() -> None:
    league = _league_model(["QB"])
    week = _week_model(
        1,
        [_roster("1", wins=5, losses=5, fpts=1.0), _roster("2", wins=0, losses=1, fpts=90.0)],
        [_matchup(1, "1", 100.0, "2"), _matchup(1, "2", 90.0, "1")],
    )
    standings = compute_standings(week, league)

    with pytest.raises(CrossCheckError) as excinfo:
        cross_check_standings(standings, week)

    assert [(m.roster_id, m.field) for m in excinfo.value.mismatches] == [
        ("1", "wins"),
        ("1", "losses"),
        ("1", "points_for"),
    ]


# --------------------------------------------------------------------------- #
# Committed fixtures
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", FIXTURES)
def test_committed_fixture_produces_a_well_formed_table(name: str) -> None:
    bundle = _bundle(name)
    week = build_week_model(bundle)
    result = compute_standings(week, build_league_model(bundle))

    assert {team.roster_id for team in result.teams} == {roster.roster_id for roster in week.rosters}
    assert sorted(team.rank for team in result.teams) == list(range(1, len(result.teams) + 1))
    assert result.tiebreak == "points_for"
    for team in result.teams:
        games = team.wins + team.losses + team.ties
        expected = round((team.wins + 0.5 * team.ties) / games, 3) if games else 0.0
        assert team.win_pct == expected
        assert team.points_for == round(team.points_for, 2)
        assert team.points_against == round(team.points_against, 2)


@pytest.mark.parametrize("name", FIXTURES)
def test_committed_fixture_is_deterministic(name: str) -> None:
    bundle = _bundle(name)
    first = compute_standings(build_week_model(bundle), build_league_model(bundle))
    second = compute_standings(
        build_week_model(copy.deepcopy(bundle)), build_league_model(copy.deepcopy(bundle))
    )
    assert first.model_dump() == second.model_dump()
    assert first.model_dump_json() == second.model_dump_json()


def test_committed_week10_standings_order_is_pinned() -> None:
    result = _run(WEEK10)

    assert [team.roster_id for team in result.teams] == _WEEK10_OVERALL
    assert result.week == 10
    assert result.through_week == 10
    assert result.regular_season_complete is False
    assert result.tiebreak == "points_for"
    assert {division.division_id: division.roster_ids for division in result.divisions} == (
        _WEEK10_DIVISIONS
    )
    assert {team.roster_id: team.rank for team in result.teams} == {
        roster_id: index + 1 for index, roster_id in enumerate(_WEEK10_OVERALL)
    }
    for division in result.divisions:
        for index, roster_id in enumerate(division.roster_ids, start=1):
            assert _team(result, roster_id).division_rank == index


def test_committed_week10_team_rows_are_pinned() -> None:
    result = _run(WEEK10)
    assert {team.roster_id for team in result.teams} == set(_WEEK10_TABLE)
    for team in result.teams:
        wins, losses, ties, pf, pa, streak_type, streak_count, high, low = _WEEK10_TABLE[team.roster_id]
        assert (team.wins, team.losses, team.ties) == (wins, losses, ties)
        assert team.points_for == pf
        assert team.points_against == pa
        assert team.streak == Streak(type=streak_type, count=streak_count)
        assert team.high_week == WeekPoints(week=high[0], points=high[1])
        assert team.low_week == WeekPoints(week=low[0], points=low[1])


def test_committed_week10_playoff_picture_is_pinned() -> None:
    picture = _run(WEEK10).playoff_picture

    assert picture is not None
    assert picture.source == "derived"
    assert picture.in_bracket == _WEEK10_IN_BRACKET
    assert picture.byes == _WEEK10_BYES
    assert picture.first_out == _WEEK10_FIRST_OUT
    assert picture.bubble == _WEEK10_BUBBLE
    assert picture.cut_line_after_rank == _WEEK10_CUT_LINE
    assert picture.consolation == _WEEK10_CONSOLATION


def test_committed_week10_cross_check_passes_on_the_point_in_time_rosters() -> None:
    """Story 5.13a: ``week10-blowout.json``'s ``rosters`` totals are as of week 10
    (``tools/point_in_time_rosters.py``), so the fold matches them."""
    bundle = _bundle(WEEK10)
    week = build_week_model(bundle)
    cross_check_standings(compute_standings(week, build_league_model(bundle)), week)


def test_cross_check_raises_when_a_roster_carries_season_final_wins() -> None:
    """The always-on mismatch path: roster 1 planted with the season-final 10 wins
    against a week-10 fold of 7."""
    bundle = _bundle(WEEK10)
    next(r for r in bundle["rosters"] if r["roster_id"] == 1)["settings"]["wins"] = 10
    week = build_week_model(bundle)
    standings = compute_standings(week, build_league_model(bundle))

    with pytest.raises(CrossCheckError) as excinfo:
        cross_check_standings(standings, week)

    error = excinfo.value
    assert isinstance(error, CommishDeskError)
    assert error.mismatches
    assert all(isinstance(mismatch, CrossCheckMismatch) for mismatch in error.mismatches)
    message = str(error)
    assert "tools/anonymize.py" in message
    assert "CONTRIBUTING.md" in message
    roster_one_wins = [
        mismatch for mismatch in error.mismatches if mismatch.roster_id == "1" and mismatch.field == "wins"
    ]
    assert roster_one_wins
    assert roster_one_wins[0].computed == 7
    assert roster_one_wins[0].sleeper == 10
    assert "7" in message and "10" in message


def test_committed_week17_cross_check_passes() -> None:
    """``week17-playoffs.json``'s roster totals are regular-season-final, so the
    fold matches W-L-T exactly and points-for within the documented per-week
    rounding tolerance -- the always-on pass path."""
    bundle = _bundle(WEEK17)
    week = build_week_model(bundle)
    standings = compute_standings(week, build_league_model(bundle))
    assert cross_check_standings(standings, week) is None


def test_committed_week17_freezes_at_the_last_regular_season_week() -> None:
    bundle = _bundle(WEEK17)
    week = build_week_model(bundle)
    result = compute_standings(week, build_league_model(bundle))

    assert result.through_week == 14
    assert result.regular_season_complete is True

    picture = result.playoff_picture
    assert picture is not None
    assert week.winners_bracket, "the playoff fixture must carry a winners bracket"

    participants = {roster_id for match in week.winners_bracket for roster_id in match.roster_ids}
    # Sleeper's own round numbering is not assumed -- the "first round" is
    # whichever round is lowest-numbered in the fixture.
    first_round_number = min(match.round for match in week.winners_bracket)
    first_round = {
        roster_id
        for match in week.winners_bracket
        if match.round == first_round_number
        for roster_id in match.roster_ids
    }

    assert set(picture.in_bracket) == participants
    assert set(picture.byes) == participants - first_round


# --------------------------------------------------------------------------- #
# regular_season_records -- the shared fold
# --------------------------------------------------------------------------- #


def test_regular_season_records_folds_only_through_the_requested_week() -> None:
    week = _week_model(
        3,
        [_roster("1"), _roster("2")],
        [
            _matchup(1, "1", 10.0, "2"),
            _matchup(1, "2", 20.0, "1"),
            _matchup(3, "1", 30.0, "2"),
            _matchup(3, "2", 10.0, "1"),
        ],
    )
    through_one = regular_season_records(week, 1)
    assert (through_one["1"].wins, through_one["1"].losses) == (0, 1)
    assert through_one["1"].points_for == 10.0

    through_three = regular_season_records(week, 3)
    assert (through_three["1"].wins, through_three["1"].losses) == (1, 1)
    assert through_three["1"].points_for == 40.0


def test_regular_season_records_never_reads_the_roster_totals() -> None:
    week = _week_model(
        1,
        [_roster("1", wins=99, losses=99, ties=99, fpts=9999.0)],
        [_matchup(1, "1", 10.0)],
    )
    record = regular_season_records(week)["1"]
    assert (record.wins, record.losses, record.ties) == (0, 0, 0)
    assert record.points_for == 10.0


# --------------------------------------------------------------------------- #
# Phase-0 golden reconciliation (skip-gated: private planning artifact)
# --------------------------------------------------------------------------- #


def _golden_standings() -> dict[str, dict[str, Any]]:
    assert GOLDEN is not None
    return {str(entry["roster_id"]): entry["season"] for entry in GOLDEN["teams"]}


@requires_golden
def test_week10_standings_reconcile_with_the_phase0_golden() -> None:
    golden = _golden_standings()
    result = _run(WEEK10)
    assert {team.roster_id for team in result.teams} == set(golden)

    for team in result.teams:
        expected = golden[team.roster_id]
        assert team.wins == expected["record"]["w"]
        assert team.losses == expected["record"]["l"]
        assert team.ties == expected["record"]["t"]
        assert team.points_for == expected["points_for"]
        assert team.points_against == expected["points_against"]
        assert team.rank == expected["rank"]
        assert team.streak == Streak(
            type=expected["streak"]["type"], count=expected["streak"]["count"]
        )
        assert team.high_week == WeekPoints(
            week=expected["high_week"]["week"], points=expected["high_week"]["points"]
        )
        assert team.low_week == WeekPoints(
            week=expected["low_week"]["week"], points=expected["low_week"]["points"]
        )


# --------------------------------------------------------------------------- #
# Import fence: stats/standings.py never reaches a later pipeline stage
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
                names.add(f"commishdesk.stats.{node.module}" if node.module else "commishdesk.stats")
            elif node.level == 2:
                names.add(node.module or "commishdesk")
        elif isinstance(node, ast.Call):
            func = node.func
            fname = getattr(func, "attr", None) or getattr(func, "id", None)
            if fname in {"import_module", "__import__"}:
                names.update(a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str))
    return names


def test_standings_never_reaches_the_network_adapters_store_or_a_later_stage() -> None:
    imported = _imported_dotted_names(STATS_STANDINGS)
    forbidden_roots = {"adapters", "store", "facts", "narrate", "render", "deliver", "statmods"}
    offenders = [
        name
        for name in imported
        if name.split(".")[:1] == ["commishdesk"] and len(name.split(".")) > 1 and name.split(".")[1] in forbidden_roots
    ]
    assert not offenders, offenders
    assert "commishdesk.ingest" in imported


def test_standings_imports_no_clock_prng_or_filesystem_module() -> None:
    imported = _imported_dotted_names(STATS_STANDINGS)
    banned = {"datetime", "time", "os", "pathlib", "random", "secrets"}
    hits = {name for name in imported if name.split(".")[0] in banned}
    assert not hits, hits


def test_standings_names_are_reachable_via_the_package_re_export() -> None:
    import commishdesk.stats as stats

    assert stats.compute_standings is compute_standings
    assert stats.cross_check_standings is cross_check_standings
    for name in (
        "POINTS_FOR_ROUNDING_TOLERANCE",
        "TIEBREAK",
        "DivisionOrder",
        "PlayoffPicture",
        "Standings",
        "Streak",
        "TeamRecord",
        "TeamStanding",
        "WeekPoints",
        "regular_season_records",
    ):
        assert name in stats.__all__
