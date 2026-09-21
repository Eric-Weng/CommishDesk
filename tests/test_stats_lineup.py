"""Story 5.5: ``compute_weekly_lineups`` -- optimal lineup, coaching efficiency,
points left on the bench, the best benched player, and starters on an NFL bye.

One test per row of the spec's I/O & Edge-Case Matrix on hand-built
:class:`~commishdesk.ingest.WeekModel` / :class:`~commishdesk.ingest.LeagueModel`
scenarios, plus the committed fixtures run end to end, determinism, the ordering
rule, and the import-fence pair that keeps ``stats/lineup.py`` off the network,
the store, and every later pipeline stage.

Reconciliation mirrors ``tests/test_stats_weekly.py``. The always-on CI oracle is
the committed fixtures themselves: every roster's ``pct`` must be at most ``1.0``
and every ``points_left_on_bench`` non-negative. The **phase-0 golden**
(``brief/phase-0/week10-facts.json``) is a private planning artifact that is not
committed to this repo (CLAUDE.md s1), so the ``@requires_golden`` checks below
only run in a workspace that has the sibling ``../brief/`` directory. They
reconcile eleven of the twelve rosters against the golden exactly, and for the
one the DECIDED pool rule perturbs (roster 4 -- a non-starting player who was on
IR by season end, which the golden counted and this module does not) they assert
the divergence direction the story's Boundaries require to be recorded rather
than bent to match: this module's ``optimal``, ``points_left_on_bench`` and
``bench_regret`` are each at or below the golden's, and its ``pct`` never
exceeds ``1.0``. The golden's bye-week data
likewise comes from the private tree; the committed ``ingest/nfl_byes.toml`` has
no 2025 season, so every bye-driven assertion is skip-gated too.
"""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from commishdesk.errors import CommishDeskError, OptimalLineupError
from commishdesk.ingest import (
    Draft,
    LeagueFormat,
    LeagueModel,
    Matchup,
    PlayerSnapshot,
    Roster,
    WeekModel,
    build_league_model,
    build_player_snapshot,
    build_week_model,
)
from commishdesk.stats import (
    BenchedPlayer,
    ByeStarter,
    LineupSlot,
    TeamLineup,
    WeeklyLineups,
    compute_weekly_lineups,
)
from tests.conftest import REPO_ROOT

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
STATS_LINEUP = REPO_ROOT / "commishdesk" / "stats" / "lineup.py"

FIXTURES = (
    "week01-openers.json",
    "week08-median.json",
    "week10-blowout.json",
    "week10-superflex.json",
    "week17-playoffs.json",
)

# The phase-0 golden lives in the sibling ``../brief/`` tree -- deliberately not
# in this repo (CLAUDE.md s1). Every check that reads it is skip-gated, exactly
# like ``requires_golden`` in ``tests/test_stats_weekly.py``.
_GOLDEN_DIR = REPO_ROOT.parent / "brief" / "phase-0"
_GOLDEN_PATH = _GOLDEN_DIR / "week10-facts.json"
GOLDEN = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8")) if _GOLDEN_PATH.is_file() else None
requires_golden = pytest.mark.skipif(
    GOLDEN is None,
    reason="phase-0 golden is a private planning artifact, not in the tree",
)

# The rosters whose optimal pool the DECIDED IR rule narrows: roster 4 carried a
# non-starting scorer who is on the season-end IR list, so the golden's optimal
# (which counted him) is above this module's. Taxi players are not excluded.
_PERTURBED_ROSTERS = frozenset({"4"})
_UNPERTURBED_ROSTERS = frozenset({"1", "2", "3", "5", "6", "7", "8", "9", "10", "11", "12"})

WEEK10 = "week10-blowout.json"


# --------------------------------------------------------------------------- #
# Helpers -- hand-build a WeekModel / LeagueModel from compact rows
# --------------------------------------------------------------------------- #


def _roster(roster_id: str, *, ir: list[str] | None = None, taxi: list[str] | None = None) -> Roster:
    return Roster(roster_id=roster_id, ir=list(ir or []), taxi=list(taxi or []))


def _matchup(
    week: int,
    roster_id: str,
    starters: dict[str, float],
    bench: dict[str, float] | None = None,
) -> Matchup:
    benched = dict(bench or {})
    return Matchup(
        week=week,
        roster_id=roster_id,
        opponent_roster_id=None,
        points=round(sum(starters.values()), 2),
        starters=list(starters),
        starters_points=[starters[player_id] for player_id in starters],
        bench=list(benched),
        players_points={**starters, **benched},
    )


def _week_model(week: int, rosters: list[Roster], matchups: list[Matchup]) -> WeekModel:
    return WeekModel(week=week, rosters=list(rosters), matchups=list(matchups), transactions=[])


def _league_model(
    roster_slots: list[str],
    flex_eligibility: dict[str, list[str]] | None = None,
    league_id: str = "id_league",
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
        ),
        teams=[],
        picks=[],
        draft=Draft(id="id_draft"),
    )


def _snapshots(positions: dict[str, str | None], teams: dict[str, str] | None = None) -> dict[str, PlayerSnapshot]:
    nfl_teams = dict(teams or {})
    return {
        player_id: PlayerSnapshot(player_id=player_id, position=position, nfl_team=nfl_teams.get(player_id))
        for player_id, position in positions.items()
    }


def _team(lineups: WeeklyLineups, roster_id: str) -> TeamLineup:
    return next(team for team in lineups.teams if team.roster_id == roster_id)


def _bundle(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _run(bundle: dict[str, Any], byes: frozenset[str] | None = None) -> WeeklyLineups:
    return compute_weekly_lineups(
        build_week_model(bundle), build_league_model(bundle), build_player_snapshot(bundle), byes
    )


# --------------------------------------------------------------------------- #
# Row: the optimum is the solved assignment, not the greedy fill
# --------------------------------------------------------------------------- #


def test_optimal_lineup_plays_the_better_bench_player() -> None:
    league = _league_model(["QB", "RB"])
    week = _week_model(
        2,
        [_roster("1")],
        [_matchup(2, "1", {"p1": 10.0, "p2": 5.0}, {"p3": 20.0})],
    )
    result = compute_weekly_lineups(week, league, _snapshots({"p1": "QB", "p2": "RB", "p3": "RB"}))

    assert isinstance(result, WeeklyLineups)
    team = _team(result, "1")
    assert [slot.player_id for slot in team.lineup] == ["p1", "p3"]
    assert [slot.slot for slot in team.lineup] == ["QB", "RB"]
    assert team.optimal == 30.0
    assert team.actual == 15.0
    assert team.pct == 0.5
    assert team.points_left_on_bench == 15.0
    assert team.best_benched == BenchedPlayer(player_id="p3", points=20.0)
    assert team.bench_regret == 20.0


def test_flex_overlap_trap_requires_assignment_not_greedy() -> None:
    """Slots ``["FLEX", "WR"]``: a greedy fill in slot order gives the FLEX slot
    ``w1`` (10.0) and leaves the WR slot empty -- 10.0. The true optimum is 19.0,
    with ``w1`` in the slot only he can fill and ``r1`` in the flex. Only a
    bipartite assignment finds it."""
    league = _league_model(["FLEX", "WR"], {"FLEX": ["RB", "WR", "TE"]})
    week = _week_model(2, [_roster("1")], [_matchup(2, "1", {"w1": 10.0}, {"r1": 9.0})])
    result = compute_weekly_lineups(week, league, _snapshots({"w1": "WR", "r1": "RB"}))

    team = _team(result, "1")
    assert [(slot.slot, slot.player_id) for slot in team.lineup] == [("FLEX", "r1"), ("WR", "w1")]
    assert team.optimal == 19.0
    assert team.actual == 10.0


def test_two_limited_flex_slots_resolve_to_the_true_optimum() -> None:
    """``REC_FLEX`` (WR/TE) is narrower than ``FLEX`` (RB/WR/TE): the WR must go
    where only he fits, and the flex takes the RB."""
    league = _league_model(["FLEX", "REC_FLEX"], {"FLEX": ["RB", "WR", "TE"], "REC_FLEX": ["WR", "TE"]})
    week = _week_model(
        2,
        [_roster("1")],
        [_matchup(2, "1", {"w1": 10.0}, {"r1": 9.0, "t1": 1.0})],
    )
    result = compute_weekly_lineups(week, league, _snapshots({"w1": "WR", "r1": "RB", "t1": "TE"}))

    team = _team(result, "1")
    assert team.optimal == 19.0
    assert dict((slot.slot, slot.player_id) for slot in team.lineup) == {
        "FLEX": "r1",
        "REC_FLEX": "w1",
    }


def test_a_fillable_slot_is_filled_even_with_a_negative_scorer() -> None:
    """The "fill while an eligible player remains" rule: a negative score still
    beats an empty slot, and ``optimal`` floors at ``actual``."""
    league = _league_model(["QB"])
    week = _week_model(2, [_roster("1")], [_matchup(2, "1", {"p1": 5.0}, {"p2": -3.0})])
    result = compute_weekly_lineups(week, league, _snapshots({"p1": "RB", "p2": "QB"}))

    team = _team(result, "1")
    assert team.lineup == [LineupSlot(slot="QB", player_id="p2", points=-3.0)]
    assert team.optimal == 5.0  # floored at actual
    assert team.pct == 1.0


# --------------------------------------------------------------------------- #
# Row: IDP / K / DEF
# --------------------------------------------------------------------------- #


def test_idp_k_and_def_slots_all_solve() -> None:
    league = _league_model(["K", "DEF", "DL", "IDP_FLEX"], {"IDP_FLEX": ["DL", "LB", "DB"]})
    week = _week_model(
        2,
        [_roster("1")],
        [
            _matchup(
                2,
                "1",
                {"k1": 5.0, "d1": 4.0, "dl1": 1.0, "lb1": 12.0},
                {"dl2": 8.0},
            )
        ],
    )
    snapshots = _snapshots({"k1": "K", "d1": "DEF", "dl1": "DL", "dl2": "DL", "lb1": "LB"})
    result = compute_weekly_lineups(week, league, snapshots)

    team = _team(result, "1")
    assert [(slot.slot, slot.player_id) for slot in team.lineup] == [
        ("K", "k1"),
        ("DEF", "d1"),
        ("DL", "dl2"),
        ("IDP_FLEX", "lb1"),
    ]
    assert team.optimal == 29.0
    assert team.actual == 22.0
    assert team.pct == 0.759


# --------------------------------------------------------------------------- #
# Row: superflex
# --------------------------------------------------------------------------- #


def test_superflex_slot_accepts_a_second_quarterback() -> None:
    league = _league_model(["QB", "SUPER_FLEX"], {"SUPER_FLEX": ["QB", "RB", "WR", "TE"]})
    week = _week_model(
        2,
        [_roster("1")],
        [_matchup(2, "1", {"qb1": 20.0, "rb1": 15.0}, {"qb2": 18.0})],
    )
    snapshots = _snapshots({"qb1": "QB", "qb2": "QB", "rb1": "RB"})
    result = compute_weekly_lineups(week, league, snapshots)

    team = _team(result, "1")
    assert {slot.player_id for slot in team.lineup} == {"qb1", "qb2"}
    assert team.optimal == 38.0
    assert team.actual == 35.0
    assert team.points_left_on_bench == 3.0


# --------------------------------------------------------------------------- #
# Row: malformed slots -- a typed error naming league id, slots, and the docs
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("slots", "flex"),
    [
        ([], {}),
        (["QB", ""], {}),
        (["FLEX"], {"FLEX": []}),
        (["QB", "IDP_FLEX"], {}),
    ],
    ids=["empty-slots", "empty-slot-name", "flex-with-no-eligibility", "flex-missing-from-table"],
)
def test_malformed_roster_slots_raise_a_typed_error(slots: list[str], flex: dict[str, list[str]]) -> None:
    league = _league_model(slots, flex)
    week = _week_model(2, [_roster("1")], [_matchup(2, "1", {"p1": 10.0})])

    with pytest.raises(OptimalLineupError) as excinfo:
        compute_weekly_lineups(week, league, _snapshots({"p1": "QB"}))

    assert isinstance(excinfo.value, CommishDeskError)
    message = str(excinfo.value)
    assert league.league_id in message
    assert repr(list(league.format.roster_slots)) in message
    assert "tools/anonymize.py" in message
    assert "CONTRIBUTING.md" in message


def test_malformed_slots_fail_before_any_roster_is_solved() -> None:
    """The format is validated once, up front -- a roster that would have solved
    fine does not get a row out of a broken league."""
    league = _league_model([], {})
    week = _week_model(2, [_roster("1")], [_matchup(2, "1", {"p1": 10.0})])
    with pytest.raises(OptimalLineupError):
        compute_weekly_lineups(week, league, _snapshots({"p1": "QB"}))


# --------------------------------------------------------------------------- #
# Row: byes unknown vs byes known
# --------------------------------------------------------------------------- #


def test_byes_unknown_is_none_not_an_empty_list() -> None:
    league = _league_model(["QB", "RB"])
    week = _week_model(2, [_roster("1")], [_matchup(2, "1", {"p1": 0.0, "p2": 5.0})])
    snapshots = _snapshots({"p1": "QB", "p2": "RB"}, {"p1": "KC", "p2": "BUF"})

    unknown = _team(compute_weekly_lineups(week, league, snapshots), "1")
    assert unknown.started_on_bye is None

    known = _team(compute_weekly_lineups(week, league, snapshots, frozenset({"KC"})), "1")
    assert known.started_on_bye == [
        ByeStarter(player_id="p1", nfl_team="KC", points=0.0),
    ]


def test_a_starter_who_scored_is_never_flagged_even_if_his_snapshot_team_is_on_bye() -> None:
    """A team on bye cannot score; a scoring starter naming a bye team means the
    snapshot is newer than the week (a backfilled trade) -- never a bye starter."""
    league = _league_model(["QB"])
    week = _week_model(2, [_roster("1")], [_matchup(2, "1", {"p1": 12.5})])
    result = compute_weekly_lineups(week, league, _snapshots({"p1": "QB"}, {"p1": "KC"}), frozenset({"KC"}))
    assert _team(result, "1").started_on_bye == []


def test_bye_flagging_ignores_a_starter_with_no_known_nfl_team() -> None:
    league = _league_model(["QB"])
    week = _week_model(2, [_roster("1")], [_matchup(2, "1", {"p1": 10.0})])
    result = compute_weekly_lineups(week, league, _snapshots({"p1": "QB"}), frozenset({"KC"}))
    assert _team(result, "1").started_on_bye == []


def test_a_bench_player_on_bye_is_not_flagged() -> None:
    league = _league_model(["QB", "RB"])
    week = _week_model(2, [_roster("1")], [_matchup(2, "1", {"p1": 10.0, "p2": 5.0}, {"p3": 30.0})])
    snapshots = _snapshots({"p1": "QB", "p2": "RB", "p3": "RB"}, {"p3": "KC"})
    result = compute_weekly_lineups(week, league, snapshots, frozenset({"KC"}))
    assert _team(result, "1").started_on_bye == []


# --------------------------------------------------------------------------- #
# Row: empty / short roster -- empty slots score 0, never a raise
# --------------------------------------------------------------------------- #


def test_short_roster_leaves_slots_empty_at_zero() -> None:
    league = _league_model(["QB", "RB", "WR"])
    week = _week_model(2, [_roster("1")], [_matchup(2, "1", {"p1": 10.0})])
    result = compute_weekly_lineups(week, league, _snapshots({"p1": "QB"}))

    team = _team(result, "1")
    assert [slot.player_id for slot in team.lineup] == ["p1", None, None]
    assert [slot.points for slot in team.lineup] == [10.0, 0.0, 0.0]
    assert team.optimal == 10.0
    assert team.actual == 10.0
    assert team.pct == 1.0
    assert team.points_left_on_bench == 0.0
    assert team.best_benched is None
    assert team.bench_regret is None


def test_no_played_players_at_all_is_a_zero_lineup_with_no_efficiency() -> None:
    league = _league_model(["QB", "RB"])
    week = _week_model(2, [_roster("1")], [_matchup(2, "1", {})])
    result = compute_weekly_lineups(week, league, {})

    team = _team(result, "1")
    assert team.actual == 0.0
    assert team.optimal == 0.0
    assert team.pct is None
    assert team.points_left_on_bench == 0.0


def test_roster_with_no_matchup_this_week_gets_no_row() -> None:
    league = _league_model(["QB"])
    week = _week_model(2, [_roster("1"), _roster("2")], [_matchup(2, "1", {"p1": 10.0})])
    result = compute_weekly_lineups(week, league, _snapshots({"p1": "QB"}))
    assert [team.roster_id for team in result.teams] == ["1"]


def test_orphan_roster_that_played_still_gets_a_row() -> None:
    """A roster with a target-week matchup but absent from ``WeekModel.rosters``
    (an orphan) is solved like any other -- it just has no IR/taxi list."""
    league = _league_model(["QB"])
    week = _week_model(2, [_roster("1")], [_matchup(2, "1", {"p1": 10.0}), _matchup(2, "9", {"p9": 4.0})])
    result = compute_weekly_lineups(week, league, _snapshots({"p1": "QB", "p9": "QB"}))
    assert [team.roster_id for team in result.teams] == ["1", "9"]
    assert _team(result, "9").actual == 4.0


def test_only_the_target_weeks_matchups_are_solved() -> None:
    league = _league_model(["QB"])
    week = _week_model(
        3,
        [_roster("1")],
        [_matchup(2, "1", {"old": 99.0}), _matchup(3, "1", {"p1": 10.0}, {"p2": 12.0})],
    )
    result = compute_weekly_lineups(week, league, _snapshots({"old": "QB", "p1": "QB", "p2": "QB"}))
    team = _team(result, "1")
    assert result.week == 3
    assert (team.actual, team.optimal) == (10.0, 12.0)


def test_a_position_no_slot_accepts_is_never_the_best_benched_player() -> None:
    league = _league_model(["QB"])
    week = _week_model(2, [_roster("1")], [_matchup(2, "1", {"p1": 10.0}, {"k1": 25.0, "q2": 3.0})])
    result = compute_weekly_lineups(week, league, _snapshots({"p1": "QB", "k1": "K", "q2": "QB"}))
    team = _team(result, "1")
    assert team.best_benched == BenchedPlayer(player_id="q2", points=3.0)
    assert team.bench_regret == 3.0
    assert team.optimal == 10.0


def test_a_bench_of_only_negative_scorers_has_no_best_benched_player() -> None:
    league = _league_model(["QB"])
    week = _week_model(2, [_roster("1")], [_matchup(2, "1", {"p1": 10.0}, {"p2": -2.0})])
    team = _team(compute_weekly_lineups(week, league, _snapshots({"p1": "QB", "p2": "QB"})), "1")
    assert team.best_benched is None
    assert team.bench_regret is None


def test_multi_slot_negative_scorers_still_fill_every_eligible_slot() -> None:
    """Both slots have an eligible player, so both fill -- even with negative
    scorers, and even when the flex could hold the better-scoring positive."""
    league = _league_model(["FLEX", "WR"], {"FLEX": ["RB", "WR", "TE"]})
    week = _week_model(
        2,
        [_roster("1")],
        [_matchup(2, "1", {"w1": -1.0}, {"r1": -4.0, "t1": -0.5})],
    )
    snapshots = _snapshots({"w1": "WR", "r1": "RB", "t1": "TE"})
    team = _team(compute_weekly_lineups(week, league, snapshots), "1")
    assert all(slot.player_id is not None for slot in team.lineup)
    assert {slot.slot: slot.player_id for slot in team.lineup} == {"FLEX": "t1", "WR": "w1"}


def test_equal_scores_resolve_to_the_lower_player_id_regardless_of_input_order() -> None:
    league = _league_model(["RB"])
    snapshots = _snapshots({"a": "RB", "b": "RB", "c": "RB", "d": "RB"})
    forward = _week_model(2, [_roster("1")], [_matchup(2, "1", {"d": 7.0}, {"b": 9.0, "a": 9.0, "c": 9.0})])
    reverse = _week_model(2, [_roster("1")], [_matchup(2, "1", {"d": 7.0}, {"c": 9.0, "a": 9.0, "b": 9.0})])
    first = _team(compute_weekly_lineups(forward, league, snapshots), "1")
    second = _team(compute_weekly_lineups(reverse, league, snapshots), "1")
    assert first == second
    assert first.lineup[0].player_id == "a"
    assert first.best_benched == BenchedPlayer(player_id="a", points=9.0)


# --------------------------------------------------------------------------- #
# Row: the DECIDED IR pool rule (taxi is startable, so taxi players count)
# --------------------------------------------------------------------------- #


def test_non_starting_ir_players_are_not_placeable() -> None:
    league = _league_model(["QB", "RB"])
    week = _week_model(
        2,
        [_roster("1", ir=["p9"])],
        [_matchup(2, "1", {"p1": 10.0, "p2": 5.0}, {"p3": 20.0, "p9": 50.0})],
    )
    snapshots = _snapshots({"p1": "QB", "p2": "RB", "p3": "RB", "p9": "RB"})
    result = compute_weekly_lineups(week, league, snapshots)

    team = _team(result, "1")
    assert team.optimal == 30.0  # p3, not the 50.0 sitting on IR
    assert team.best_benched == BenchedPlayer(player_id="p3", points=20.0)


def test_non_starting_taxi_players_are_placeable() -> None:
    """A taxi player is startable in this league, so he counts as available --
    unlike an IR player."""
    league = _league_model(["QB", "RB"])
    week = _week_model(
        2,
        [_roster("1", taxi=["p8"])],
        [_matchup(2, "1", {"p1": 10.0, "p2": 5.0}, {"p3": 20.0, "p8": 40.0})],
    )
    snapshots = _snapshots({"p1": "QB", "p2": "RB", "p3": "RB", "p8": "RB"})
    team = _team(compute_weekly_lineups(week, league, snapshots), "1")

    assert team.optimal == 50.0  # p1 + the 40.0 taxi RB
    assert team.best_benched == BenchedPlayer(player_id="p8", points=40.0)


def test_an_ir_player_who_actually_started_is_still_placeable() -> None:
    league = _league_model(["QB", "RB"])
    week = _week_model(
        2,
        [_roster("1", ir=["p3"])],
        [_matchup(2, "1", {"p1": 10.0, "p3": 5.0}, {"p2": 20.0})],
    )
    snapshots = _snapshots({"p1": "QB", "p2": "RB", "p3": "RB"})
    result = compute_weekly_lineups(week, league, snapshots)

    team = _team(result, "1")
    assert team.optimal == 30.0  # QB p1 + RB p2
    assert team.actual == 15.0
    assert team.pct == 0.5


# --------------------------------------------------------------------------- #
# Row: a player the solver cannot place never reads as exceeding the optimum
# --------------------------------------------------------------------------- #


def test_a_player_with_no_known_position_cannot_be_placed_efficiency_is_capped() -> None:
    league = _league_model(["QB"])
    week = _week_model(2, [_roster("1")], [_matchup(2, "1", {"p1": 5.0}, {"p2": 30.0})])
    result = compute_weekly_lineups(week, league, _snapshots({"p2": None}))

    team = _team(result, "1")
    assert team.lineup == [LineupSlot(slot="QB", player_id=None, points=0.0)]
    assert team.optimal == 5.0  # floored at actual
    assert team.actual == 5.0
    assert team.pct == 1.0
    assert team.best_benched is None


# --------------------------------------------------------------------------- #
# Ordering + determinism
# --------------------------------------------------------------------------- #


def test_teams_are_ordered_by_roster_id_numerically() -> None:
    league = _league_model(["QB"])
    week = _week_model(
        2,
        [_roster("10"), _roster("2"), _roster("1")],
        [_matchup(2, roster_id, {f"p{roster_id}": 1.0}) for roster_id in ("10", "2", "1")],
    )
    snapshots = _snapshots({f"p{roster_id}": "QB" for roster_id in ("10", "2", "1")})
    result = compute_weekly_lineups(week, league, snapshots)
    assert [team.roster_id for team in result.teams] == ["1", "2", "10"]


def test_deterministic_on_hand_built_input() -> None:
    league = _league_model(["QB", "RB", "FLEX"], {"FLEX": ["RB", "WR", "TE"]})
    week = _week_model(
        2,
        [_roster("1"), _roster("2")],
        [
            _matchup(2, "1", {"p1": 10.0, "p2": 5.0}, {"p3": 20.0}),
            _matchup(2, "2", {"p4": 1.0, "p5": 2.0}, {"p6": 3.0}),
        ],
    )
    snapshots = _snapshots({"p1": "QB", "p2": "RB", "p3": "RB", "p4": "QB", "p5": "RB", "p6": "WR"})

    first = compute_weekly_lineups(week, league, snapshots)
    second = compute_weekly_lineups(copy.deepcopy(week), copy.deepcopy(league), copy.deepcopy(snapshots))
    assert first.model_dump() == second.model_dump()
    assert first.model_dump_json() == second.model_dump_json()


# --------------------------------------------------------------------------- #
# Committed fixtures
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", FIXTURES)
def test_committed_fixture_never_reports_efficiency_above_one(name: str) -> None:
    """AC3, on the real committed bundles: whatever the data gap, ``pct`` is at
    most ``1.0`` and ``points_left_on_bench`` never goes negative."""
    result = _run(_bundle(name))
    assert result.teams
    for team in result.teams:
        assert team.pct is None or team.pct <= 1.0
        assert team.points_left_on_bench >= 0.0
        assert team.optimal >= team.actual


@pytest.mark.parametrize("name", FIXTURES)
def test_committed_fixture_is_deterministic(name: str) -> None:
    bundle = _bundle(name)
    first = _run(bundle)
    second = _run(copy.deepcopy(bundle))
    assert first.model_dump() == second.model_dump()
    assert first.model_dump_json() == second.model_dump_json()


def test_committed_superflex_fixture_solves_every_slot() -> None:
    """The superflex fixture feeds the solver end to end. ``SUPER_FLEX`` is a
    flex slot whose eligibility includes ``QB`` (so a second quarterback may fill
    it -- pinned directly on a hand-built league by
    ``test_superflex_slot_accepts_a_second_quarterback``), and a 12-team roster
    carries far more placeable players than its 11 slots, so every slot fills
    and the optimum never falls below what the manager actually started."""
    bundle = _bundle("week10-superflex.json")
    league = build_league_model(bundle)
    assert "SUPER_FLEX" in league.format.flex_eligibility
    assert "QB" in league.format.flex_eligibility["SUPER_FLEX"]

    result = _run(bundle)
    assert result.teams
    slot_count = len(league.format.roster_slots)
    for team in result.teams:
        assert len(team.lineup) == slot_count
        assert all(slot.player_id is not None for slot in team.lineup)
        assert team.optimal >= team.actual
        assert team.pct is not None and team.pct <= 1.0


# roster_id -> (actual, optimal, pct, points_left_on_bench, bench_regret) for the
# committed week-10 bundle under the DECIDED pool rule. Every roster but 4 equals
# the phase-0 golden; 4 sits below it (a non-starting IR scorer excluded).
# Always-on: a solver, eligibility or pool regression on real slot/position
# names cannot hide behind the ``optimal >= actual`` floor.
_WEEK10_PINNED = {
    "1": (229.96, 244.97, 0.939, 15.01, 26.74),
    "2": (242.03, 243.98, 0.992, 1.95, 18.39),
    "3": (247.2, 280.24, 0.882, 33.04, 22.31),
    "4": (172.23, 178.37, 0.966, 6.14, 15.63),
    "5": (91.77, 135.49, 0.677, 43.72, 18.33),
    "6": (114.69, 126.9, 0.904, 12.21, 5.88),
    "7": (119.97, 228.83, 0.524, 108.86, 51.66),
    "8": (183.14, 209.73, 0.873, 26.59, 33.25),
    "9": (117.13, 173.67, 0.674, 56.54, 23.58),
    "10": (112.37, 116.13, 0.968, 3.76, 3.76),
    "11": (154.38, 168.6, 0.916, 14.22, 12.74),
    "12": (126.03, 136.46, 0.924, 10.43, 13.38),
}


def test_committed_week10_blowout_pins_every_rosters_optimum() -> None:
    result = _run(_bundle(WEEK10))
    assert {team.roster_id for team in result.teams} == set(_WEEK10_PINNED)
    for team in result.teams:
        actual, optimal, pct, left, regret = _WEEK10_PINNED[team.roster_id]
        assert (team.actual, team.optimal, team.pct, team.points_left_on_bench, team.bench_regret) == (
            actual,
            optimal,
            pct,
            left,
            regret,
        ), team.roster_id


def test_committed_week17_fixture_only_rows_rosters_that_played() -> None:
    """Four rosters were eliminated in an earlier playoff round and carry no
    week-17 matchup -- they get no lineup row at all."""
    bundle = _bundle("week17-playoffs.json")
    week = build_week_model(bundle)
    played = {matchup.roster_id for matchup in week.matchups if matchup.week == week.week}
    result = _run(bundle)
    assert {team.roster_id for team in result.teams} == played
    assert len(result.teams) < len(week.rosters)


def test_committed_week10_blowout_started_on_bye_is_none_without_bye_data() -> None:
    """The committed ``ingest/nfl_byes.toml`` has no 2025 season, so a real run
    without caller-supplied byes reports "unknown", never a misleading ``[]``."""
    result = _run(_bundle(WEEK10))
    for team in result.teams:
        assert team.started_on_bye is None


# --------------------------------------------------------------------------- #
# Phase-0 golden reconciliation (skip-gated: private planning artifact)
# --------------------------------------------------------------------------- #


def _golden_this_week() -> dict[str, dict[str, Any]]:
    """``{roster_id: this_week}`` from the golden's ``teams`` section."""
    assert GOLDEN is not None
    return {str(entry["roster_id"]): entry["this_week"] for entry in GOLDEN["teams"]}


def _golden_byes() -> frozenset[str]:
    """The golden's own week-10 NFL bye teams (``period.nfl_byes``) -- the
    committed ``ingest/nfl_byes.toml`` has no 2025 season."""
    assert GOLDEN is not None
    return frozenset(GOLDEN["period"]["nfl_byes"])


@requires_golden
def test_week10_blowout_lineups_reconcile_with_the_phase0_golden() -> None:
    golden_weeks = _golden_this_week()
    result = _run(_bundle(WEEK10), _golden_byes())
    assert {team.roster_id for team in result.teams} == set(golden_weeks)

    for roster_id, golden_week in golden_weeks.items():
        team = _team(result, roster_id)
        golden = golden_week["coaching_efficiency"]
        assert team.actual == golden["actual"]
        assert team.optimal >= team.actual
        assert team.pct is not None
        assert team.pct <= 1.0
        # This module's pool is the golden's minus the non-starting IR
        # scorers, so optimal, bench gap and bench regret can only be at or
        # below the golden's -- never above (the golden-file rule: the divergence
        # is recorded in docs/EDGE-CASES.md, not bent to match).
        assert team.optimal <= golden["optimal"]
        assert team.points_left_on_bench <= golden_week["points_left_on_bench"]
        assert team.bench_regret is not None
        assert team.bench_regret <= golden_week["bench_regret"]["points"]
        if roster_id in _UNPERTURBED_ROSTERS:
            assert team.optimal == golden["optimal"]
            assert team.pct == golden["pct"]
            assert team.points_left_on_bench == golden_week["points_left_on_bench"]
            assert team.bench_regret == golden_week["bench_regret"]["points"]
        else:
            assert roster_id in _PERTURBED_ROSTERS
            assert team.optimal < golden["optimal"]


@requires_golden
def test_week10_blowout_started_on_bye_reconciles_with_the_phase0_golden() -> None:
    result = _run(_bundle(WEEK10), _golden_byes())
    for roster_id, golden_week in _golden_this_week().items():
        team = _team(result, roster_id)
        assert team.started_on_bye is not None
        assert sorted(starter.nfl_team for starter in team.started_on_bye if starter.nfl_team) == sorted(
            entry["nfl_team"] for entry in golden_week["started_on_bye"]
        )


# --------------------------------------------------------------------------- #
# Optimality cross-check against brute force
# --------------------------------------------------------------------------- #


def test_solver_matches_brute_force_on_randomized_overlapping_flex_rosters() -> None:
    import itertools
    import random

    flex = {
        "FLEX": ["RB", "WR", "TE"],
        "SUPER_FLEX": ["QB", "RB", "WR", "TE"],
        "REC_FLEX": ["WR", "TE"],
    }
    slots = ["QB", "RB", "FLEX", "SUPER_FLEX", "REC_FLEX", "WR"]
    positions = ["QB", "RB", "WR", "TE"]
    rng = random.Random(5)
    league = _league_model(slots, flex)

    for _ in range(40):
        players = {f"p{index}": rng.choice(positions) for index in range(rng.randint(3, 9))}
        points = {player_id: float(rng.randint(1, 40)) for player_id in players}
        week = _week_model(2, [_roster("1")], [_matchup(2, "1", {}, points)])
        team = _team(compute_weekly_lineups(week, league, _snapshots(players)), "1")

        def allowed(slot: str) -> set[str]:
            return set(flex.get(slot, [slot]))

        best = 0.0
        ids = list(players)
        # Every partial injective slot -> player map (None = empty slot).
        for choice in itertools.product([None, *ids], repeat=len(slots)):
            used = [player_id for player_id in choice if player_id is not None]
            if len(set(used)) != len(used):
                continue
            if any(
                player_id is not None and players[player_id] not in allowed(slot)
                for slot, player_id in zip(slots, choice, strict=True)
            ):
                continue
            best = max(best, sum(points[player_id] for player_id in used))
        assert team.optimal == best


# --------------------------------------------------------------------------- #
# Import fence: stats/lineup.py never reaches a later pipeline stage
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


def test_lineup_never_reaches_the_network_adapters_store_or_a_later_stage() -> None:
    imported = _imported_dotted_names(STATS_LINEUP)
    forbidden_roots = {"adapters", "store", "facts", "narrate", "render", "deliver", "statmods"}
    offenders = [
        name
        for name in imported
        if name.split(".")[:1] == ["commishdesk"] and len(name.split(".")) > 1 and name.split(".")[1] in forbidden_roots
    ]
    assert not offenders, offenders
    assert "commishdesk.ingest" in imported


def test_lineup_imports_no_clock_prng_or_filesystem_module() -> None:
    imported = _imported_dotted_names(STATS_LINEUP)
    banned = {"datetime", "time", "os", "pathlib", "random", "secrets"}
    hits = {name for name in imported if name.split(".")[0] in banned}
    assert not hits, hits


def test_lineup_names_are_reachable_via_the_package_re_export() -> None:
    import commishdesk.stats as stats

    assert stats.compute_weekly_lineups is compute_weekly_lineups
    for name in ("BenchedPlayer", "ByeStarter", "LineupSlot", "TeamLineup", "WeeklyLineups"):
        assert name in stats.__all__
