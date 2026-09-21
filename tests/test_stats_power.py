"""Story 5.6: ``compute_power_ranks`` / ``compute_power_history`` -- the model
power rank.

One test per row of the spec's I/O & Edge-Case Matrix, the two named module
constants, the unrounded-rank rule, the per-week history, recomputability for any
prior week, determinism, the committed fixtures, and the import-fence pair that
keeps ``stats/power.py`` off the network, the store, and every later pipeline
stage.

The phase-0 golden (``brief/phase-0/week10-facts.json``) is a private planning
artifact, not committed to this repo, so the ``@requires_golden`` checks below
only run in a workspace that has the sibling ``../brief/`` directory -- exactly
like ``tests/test_stats_lineup.py``.
"""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from commishdesk.ingest import (
    Draft,
    LeagueFormat,
    LeagueModel,
    Matchup,
    Roster,
    WeekModel,
    build_week_model,
)
from commishdesk.stats import (
    MEANINGFUL_FROM_WEEK,
    POWER_NUDGE_CAP,
    POWER_WEIGHTS,
    PowerRanks,
    TeamPower,
    compute_power_history,
    compute_power_ranks,
)
from tests.conftest import REPO_ROOT

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
STATS_POWER = REPO_ROOT / "commishdesk" / "stats" / "power.py"

FIXTURES = (
    "week01-openers.json",
    "week08-median.json",
    "week10-blowout.json",
    "week10-superflex.json",
    "week17-playoffs.json",
)

WEEK10 = "week10-blowout.json"

# The phase-0 golden lives in the sibling ``../brief/`` tree -- deliberately not
# in this repo (CLAUDE.md s1). Every check that reads it is skip-gated.
_GOLDEN_DIR = REPO_ROOT.parent / "brief" / "phase-0"
_GOLDEN_PATH = _GOLDEN_DIR / "week10-facts.json"
GOLDEN = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8")) if _GOLDEN_PATH.is_file() else None
requires_golden = pytest.mark.skipif(
    GOLDEN is None,
    reason="phase-0 golden is a private planning artifact, not in the tree",
)


# --------------------------------------------------------------------------- #
# Helpers -- hand-build a WeekModel / LeagueModel from compact rows
# --------------------------------------------------------------------------- #


def _roster(roster_id: str) -> Roster:
    return Roster(roster_id=roster_id)


def _matchup(week: int, roster_id: str, points: float, opponent: str | None = None) -> Matchup:
    return Matchup(week=week, roster_id=roster_id, opponent_roster_id=opponent, points=points)


def _week_model(week: int, rosters: list[Roster], matchups: list[Matchup]) -> WeekModel:
    return WeekModel(week=week, rosters=list(rosters), matchups=list(matchups), transactions=[])


def _league_model() -> LeagueModel:
    return LeagueModel(
        league_id="id_league",
        name="Test League",
        season=2025,
        format=LeagueFormat(
            team_count=2,
            roster_slots=["QB"],
            flex_eligibility={},
            scoring_label="PPR",
            is_superflex_or_2qb=False,
            te_premium=False,
        ),
        teams=[],
        picks=[],
        draft=Draft(id="id_draft"),
    )


def _bundle(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _by_id(ranks: PowerRanks) -> dict[str, TeamPower]:
    return {team.roster_id: team for team in ranks.teams}


# --------------------------------------------------------------------------- #
# Named module constants (AC)
# --------------------------------------------------------------------------- #


def test_power_weights_are_named_and_sum_to_one() -> None:
    assert set(POWER_WEIGHTS) == {"all_play_pct", "normalized_avg_pf", "win_pct"}
    assert sum(POWER_WEIGHTS.values()) == pytest.approx(1.0)


def test_power_nudge_cap_is_a_named_module_constant() -> None:
    assert POWER_NUDGE_CAP == 2
    source = STATS_POWER.read_text(encoding="utf-8")
    assert "POWER_NUDGE_CAP = 2" in source
    assert "POWER_WEIGHTS: dict[str, float] = {" in source


# --------------------------------------------------------------------------- #
# Row: week 1 -- no cold-start power rank
# --------------------------------------------------------------------------- #


def test_week_one_has_no_power_rank() -> None:
    week = _week_model(
        1,
        [_roster("1"), _roster("2")],
        [_matchup(1, "1", 100.0, "2"), _matchup(1, "2", 90.0, "1")],
    )
    ranks = compute_power_ranks(week)
    assert ranks.week == 1
    for team in ranks.teams:
        assert team.model_score is None
        assert team.model_rank is None
        assert team.all_play_pct is None
        assert team.normalized_avg_pf is None
        assert team.win_pct is None


def test_week_one_is_unranked_even_when_the_bundle_has_later_weeks() -> None:
    week = build_week_model(_bundle(WEEK10))
    ranks = compute_power_ranks(week, through_week=1)
    assert ranks.week == MEANINGFUL_FROM_WEEK - 1
    assert all(team.model_rank is None for team in ranks.teams)


# --------------------------------------------------------------------------- #
# Row: a roster with no game is unranked
# --------------------------------------------------------------------------- #


def test_a_roster_with_no_game_is_unranked() -> None:
    week = _week_model(
        3,
        [_roster("1"), _roster("2"), _roster("9")],
        [
            _matchup(1, "1", 100.0, "2"),
            _matchup(1, "2", 90.0, "1"),
            _matchup(2, "1", 80.0, "2"),
            _matchup(2, "2", 70.0, "1"),
        ],
    )
    by_id = _by_id(compute_power_ranks(week))

    assert by_id["9"].model_rank is None
    assert by_id["9"].model_score is None
    assert by_id["1"].model_rank == 1
    assert by_id["2"].model_rank == 2
    assert by_id["1"].model_score is not None
    assert by_id["2"].model_score is not None
    assert by_id["1"].model_score > by_id["2"].model_score


def test_teams_are_ordered_by_roster_id() -> None:
    week = _week_model(
        2,
        [_roster("10"), _roster("2"), _roster("1")],
        [
            _matchup(1, "1", 100.0, "2"),
            _matchup(1, "2", 90.0, "1"),
            _matchup(2, "10", 0.0),
        ],
    )
    ranks = compute_power_ranks(week)
    assert [team.roster_id for team in ranks.teams] == ["1", "2", "10"]


# --------------------------------------------------------------------------- #
# Row: the score is a weighted sum of league-relative components
# --------------------------------------------------------------------------- #


def test_components_are_league_relative_and_the_score_weights_them() -> None:
    week = _week_model(
        2,
        [_roster("1"), _roster("2")],
        [
            _matchup(1, "1", 200.0, "2"),
            _matchup(1, "2", 100.0, "1"),
            _matchup(2, "1", 200.0, "2"),
            _matchup(2, "2", 100.0, "1"),
        ],
    )
    by_id = _by_id(compute_power_ranks(week))

    assert by_id["1"].all_play_pct == 1.0
    assert by_id["2"].all_play_pct == 0.0
    assert by_id["1"].normalized_avg_pf == 1.0
    assert by_id["2"].normalized_avg_pf == 0.0
    assert by_id["1"].win_pct == 1.0
    assert by_id["2"].win_pct == 0.0
    assert by_id["1"].model_score == pytest.approx(1.0)
    assert by_id["2"].model_score == pytest.approx(0.0)
    assert by_id["1"].model_rank == 1
    assert by_id["2"].model_rank == 2


def test_all_equal_average_points_for_normalizes_to_zero_not_a_division_by_zero() -> None:
    week = _week_model(
        2,
        [_roster("1"), _roster("2")],
        [
            _matchup(1, "1", 100.0, "2"),
            _matchup(1, "2", 100.0, "1"),
            _matchup(2, "1", 100.0, "2"),
            _matchup(2, "2", 100.0, "1"),
        ],
    )
    by_id = _by_id(compute_power_ranks(week))
    assert by_id["1"].normalized_avg_pf == 0.0
    assert by_id["2"].normalized_avg_pf == 0.0
    assert by_id["1"].all_play_pct == 0.0
    assert by_id["2"].all_play_pct == 0.0


# --------------------------------------------------------------------------- #
# Row: through_week recomputability, history, clamping
# --------------------------------------------------------------------------- #


def test_through_week_matches_the_bundle_truncated_at_that_week() -> None:
    bundle = _bundle(WEEK10)
    truncated = {
        **bundle,
        "matchups": {key: rows for key, rows in bundle["matchups"].items() if int(key) <= 5},
    }
    week = build_week_model(bundle)

    assert compute_power_ranks(week, through_week=5) == compute_power_ranks(build_week_model(truncated))
    assert compute_power_ranks(week, through_week=5).week == 5


def test_history_entry_n_equals_through_week_n() -> None:
    week = build_week_model(_bundle(WEEK10))
    history = compute_power_history(week)

    assert [entry.week for entry in history] == list(range(1, 11))
    for index, entry in enumerate(history, start=1):
        assert entry == compute_power_ranks(week, through_week=index)
        assert entry.week == index


def test_history_first_entry_is_the_unranked_cold_start() -> None:
    history = compute_power_history(build_week_model(_bundle(WEEK10)))
    assert all(team.model_rank is None for team in history[0].teams)
    assert any(team.model_rank is not None for team in history[1].teams)


def test_through_week_is_clamped_to_the_cutoff() -> None:
    week = build_week_model(_bundle(WEEK10))
    assert compute_power_ranks(week, through_week=999) == compute_power_ranks(week)
    assert compute_power_ranks(week, through_week=999).week == 10


def test_no_later_week_leaks_into_an_earlier_ranking() -> None:
    bundle = _bundle(WEEK10)
    week = build_week_model(bundle)
    truncated = {
        **bundle,
        "matchups": {key: rows for key, rows in bundle["matchups"].items() if int(key) <= 2},
    }
    early = compute_power_ranks(week, through_week=2)
    assert early == compute_power_ranks(build_week_model(truncated))
    assert [entry.week for entry in compute_power_history(build_week_model(truncated))] == [1, 2]


# --------------------------------------------------------------------------- #
# Row: ranks are assigned on the unrounded score
# --------------------------------------------------------------------------- #


def test_ranks_use_the_unrounded_score_when_the_displayed_scores_tie() -> None:
    """Week 9: rosters 11 and 12 both display ``0.193`` but the golden still
    orders them (12 above 11). Pinned on the committed fixture, always on."""
    week = build_week_model(_bundle(WEEK10))
    by_id = _by_id(compute_power_ranks(week, through_week=9))

    assert by_id["11"].model_score == 0.193
    assert by_id["12"].model_score == 0.193
    assert by_id["12"].model_rank == 10
    assert by_id["11"].model_rank == 11


# Week-10 model scores and ranks, copied from the phase-0 golden's
# ``season.power`` -- always-on, independent of the golden being present.
_WEEK10_POWER = {
    "1": (0.85, 2),
    "2": (0.853, 1),
    "3": (0.597, 4),
    "4": (0.433, 7),
    "5": (0.371, 8),
    "6": (0.453, 6),
    "7": (0.164, 12),
    "8": (0.466, 5),
    "9": (0.79, 3),
    "10": (0.368, 9),
    "11": (0.213, 11),
    "12": (0.215, 10),
}


def test_committed_week10_power_scores_and_ranks_are_pinned() -> None:
    ranks = compute_power_ranks(build_week_model(_bundle(WEEK10)))
    assert ranks.week == 10
    assert {team.roster_id: (team.model_score, team.model_rank) for team in ranks.teams} == _WEEK10_POWER


# Mid-season model ranks (weeks 3 and 6), copied from the phase-0 golden's
# ``history.weekly[].power_rank`` -- always-on, so a bug that only bites for
# ``through_week`` between 2 and 9 cannot pass CI on the golden's absence.
_MIDSEASON_RANKS = {
    3: {"1": 1, "2": 2, "3": 4, "4": 10, "5": 6, "6": 11, "7": 7, "8": 9, "9": 3, "10": 5, "11": 8, "12": 12},
    6: {"1": 3, "2": 1, "3": 4, "4": 10, "5": 7, "6": 9, "7": 11, "8": 5, "9": 2, "10": 6, "11": 8, "12": 12},
}


@pytest.mark.parametrize("through_week", sorted(_MIDSEASON_RANKS))
def test_committed_midseason_model_ranks_are_pinned(through_week: int) -> None:
    ranks = compute_power_ranks(build_week_model(_bundle(WEEK10)), through_week=through_week)
    assert ranks.week == through_week
    assert {team.roster_id: team.model_rank for team in ranks.teams} == _MIDSEASON_RANKS[through_week]


# --------------------------------------------------------------------------- #
# Committed fixtures
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", FIXTURES)
def test_committed_fixture_produces_well_formed_ranks(name: str) -> None:
    week = build_week_model(_bundle(name))
    history = compute_power_history(week)

    assert history
    for entry in history:
        assert {team.roster_id for team in entry.teams} == {roster.roster_id for roster in week.rosters}
        ranks = [team.model_rank for team in entry.teams if team.model_rank is not None]
        assert sorted(ranks) == list(range(1, len(ranks) + 1))
        for team in entry.teams:
            assert (team.model_rank is None) == (team.model_score is None)


@pytest.mark.parametrize("name", FIXTURES)
def test_committed_fixture_is_deterministic(name: str) -> None:
    bundle = _bundle(name)
    first = compute_power_ranks(build_week_model(bundle))
    second = compute_power_ranks(build_week_model(copy.deepcopy(bundle)))
    assert first.model_dump() == second.model_dump()
    assert first.model_dump_json() == second.model_dump_json()


def test_committed_week17_freezes_at_the_regular_season_cutoff() -> None:
    week = build_week_model(_bundle("week17-playoffs.json"))
    ranks = compute_power_ranks(week)
    assert ranks.week == 14
    assert compute_power_ranks(week, through_week=17) == ranks


# --------------------------------------------------------------------------- #
# Phase-0 golden reconciliation (skip-gated: private planning artifact)
# --------------------------------------------------------------------------- #


def _golden_history() -> dict[int, dict[str, int]]:
    """``{week: {roster_id: power_rank}}`` -- the golden stores the arc per team;
    week 1 is ``null`` (FR-10: no cold-start rank), so it is left out."""
    assert GOLDEN is not None
    history: dict[int, dict[str, int]] = {}
    for team in GOLDEN["teams"]:
        for entry in team["history"]["weekly"]:
            if entry["power_rank"] is not None:
                history.setdefault(int(entry["week"]), {})[str(team["roster_id"])] = entry[
                    "power_rank"
                ]
    return history


def _golden_season_scores() -> dict[str, float]:
    assert GOLDEN is not None
    return {str(entry["roster_id"]): entry["season"]["power"]["score"] for entry in GOLDEN["teams"]}


@requires_golden
def test_week10_power_ranks_reconcile_with_the_phase0_golden() -> None:
    week = build_week_model(_bundle(WEEK10))
    golden_history = _golden_history()
    golden_scores = _golden_season_scores()

    for entry in compute_power_history(week):
        if entry.week == 1:
            assert all(team.model_rank is None for team in entry.teams)
            continue
        expected = golden_history[entry.week]
        assert {team.roster_id for team in entry.teams} == set(expected)
        for team in entry.teams:
            assert team.model_rank == expected[team.roster_id]
        if entry.week == 10:
            for team in entry.teams:
                assert team.model_score == pytest.approx(golden_scores[team.roster_id], abs=0.005)


# --------------------------------------------------------------------------- #
# Import fence: stats/power.py never reaches a later pipeline stage
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


def test_power_never_reaches_the_network_adapters_store_or_a_later_stage() -> None:
    imported = _imported_dotted_names(STATS_POWER)
    forbidden_roots = {"adapters", "store", "facts", "narrate", "render", "deliver", "statmods"}
    offenders = [
        name
        for name in imported
        if name.split(".")[:1] == ["commishdesk"] and len(name.split(".")) > 1 and name.split(".")[1] in forbidden_roots
    ]
    assert not offenders, offenders
    assert "commishdesk.ingest" in imported


def test_power_imports_no_clock_prng_or_filesystem_module() -> None:
    imported = _imported_dotted_names(STATS_POWER)
    banned = {"datetime", "time", "os", "pathlib", "random", "secrets"}
    hits = {name for name in imported if name.split(".")[0] in banned}
    assert not hits, hits


def test_power_names_are_reachable_via_the_package_re_export() -> None:
    import commishdesk.stats as stats

    assert stats.compute_power_ranks is compute_power_ranks
    assert stats.compute_power_history is compute_power_history
    for name in ("POWER_NUDGE_CAP", "POWER_WEIGHTS", "PowerRanks", "TeamPower"):
        assert name in stats.__all__
