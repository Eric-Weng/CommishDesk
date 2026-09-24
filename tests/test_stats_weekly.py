"""Story 5.4: ``compute_weekly_stats`` -- all-play, expected wins, luck, and
blowouts.

One test per row of the spec's I/O & Edge-Case Matrix on hand-built
:class:`~commishdesk.ingest.WeekModel`/:class:`~commishdesk.ingest.Roster`/
:class:`~commishdesk.ingest.Matchup` scenarios, plus the four committed
fixtures reconciled against a new ``tests/fixtures/weekly-stats/expected-*.json``
per fixture, determinism, and import-fence checks that ``stats/weekly.py``
reaches no network / clock / PRNG / filesystem module.

Reconciliation runs two ways, mirroring ``tests/test_facts.py``'s established
split. The **CI oracle** is the committed ``tests/fixtures/weekly-stats/
expected-*.json`` per fixture -- a frozen ``model_dump(mode="json")`` snapshot;
it runs everywhere, including CI, but only proves the code is consistent with
itself across changes. The **phase-0 golden** (``brief/phase-0/week10-facts.json``)
is a private planning artifact not committed to this repo (CLAUDE.md ss1); the
``@requires_golden``-gated checks below match ``all_play``/``expected_wins``
per roster and the whole :class:`~commishdesk.stats.weekly.WeekSummary`
field-by-field against it and only run in a workspace that has the sibling
``../brief/`` directory -- this is the actual AC1 "golden-file rule"
reconciliation, not just the always-on CI oracle.

``luck`` and the season ``wins``/``losses``/``ties`` on
:class:`~commishdesk.stats.weekly.TeamWeekStats` are sourced verbatim from
:class:`~commishdesk.ingest.Roster` (Sleeper's *current* count), and Sleeper's
``/rosters`` endpoint has no historical-per-week view. So a fixture built after
the season finished carries season-final totals beside mid-season matchups --
which is still true of ``week17-playoffs.json``. Story 5.13a rewrote
``week10-blowout.json``'s roster totals as of week 10
(``tools/point_in_time_rosters.py``), so its ``luck`` now reconciles with the
golden's ``season.luck``.
"""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from commishdesk.ingest import BracketMatch, Matchup, Roster, WeekModel, build_week_model
from commishdesk.stats import (
    BLOWOUT_RATIO,
    MEANINGFUL_FROM_WEEK,
    TeamWeekStats,
    WeeklyStats,
    compute_weekly_stats,
)
from tests.conftest import REPO_ROOT

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
WEEKLY_DIR = FIXTURE_DIR / "weekly-stats"
STATS_WEEKLY = REPO_ROOT / "commishdesk" / "stats" / "weekly.py"

FIXTURES = (
    "week01-openers.json",
    "week08-median.json",
    "week10-blowout.json",
    "week17-playoffs.json",
)

# The phase-0 golden (``week10-facts.json``) is a *private* planning artifact in
# the sibling ``../brief/`` directory -- deliberately not in this repo (CLAUDE.md
# ss1). Every check that reads it is skip-gated, exactly like ``requires_golden``
# in ``tests/test_facts.py``.
_GOLDEN_PATH = REPO_ROOT.parent / "brief" / "phase-0" / "week10-facts.json"
GOLDEN = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8")) if _GOLDEN_PATH.is_file() else None
requires_golden = pytest.mark.skipif(
    GOLDEN is None,
    reason="phase-0 golden is a private planning artifact, not in the tree",
)


# --------------------------------------------------------------------------- #
# Helpers -- hand-build a WeekModel from compact rows
# --------------------------------------------------------------------------- #


def _roster(roster_id: str, wins: int = 0, losses: int = 0, ties: int = 0) -> Roster:
    return Roster(roster_id=roster_id, wins=wins, losses=losses, ties=ties)


def _matchup(week: int, roster_id: str, opponent: str | None, points: float) -> Matchup:
    return Matchup(week=week, roster_id=roster_id, opponent_roster_id=opponent, points=points)


def _week_model(
    week: int,
    rosters: list[Roster],
    matchups: list[Matchup],
    *,
    playoff_week_start: int | None = None,
    winners_bracket: list[BracketMatch] = (),  # type: ignore[assignment]
    losers_bracket: list[BracketMatch] = (),  # type: ignore[assignment]
) -> WeekModel:
    return WeekModel(
        week=week,
        rosters=rosters,
        matchups=matchups,
        transactions=[],
        playoff_week_start=playoff_week_start,
        winners_bracket=list(winners_bracket),
        losers_bracket=list(losers_bracket),
    )


def _team(stats: WeeklyStats, roster_id: str) -> TeamWeekStats:
    return next(t for t in stats.teams if t.roster_id == roster_id)


# --------------------------------------------------------------------------- #
# Row: week-1 fixture -- has_prior_week False, all_play/expected_wins/luck None
# --------------------------------------------------------------------------- #


def test_week_one_has_no_prior_week_and_every_derived_stat_is_none_not_zero() -> None:
    week = _week_model(
        1,
        [_roster("1", wins=1, losses=0), _roster("2", wins=0, losses=1)],
        [_matchup(1, "1", "2", 120.0), _matchup(1, "2", "1", 100.0)],
    )
    stats = compute_weekly_stats(week)

    assert stats.period.has_prior_week is False
    assert stats.period.week == 1 < MEANINGFUL_FROM_WEEK
    for team in stats.teams:
        assert team.all_play is None
        assert team.expected_wins is None
        assert team.luck is None


def test_committed_week01_openers_fixture_has_no_prior_week() -> None:
    """Row: Week-1 fixture (``week01-openers.json``) -- ``has_prior_week`` is
    false and every history-dependent value is null, not zero, on the actual
    committed fixture (not just a hand-built stand-in)."""
    model = build_week_model(_bundle("week01-openers.json"))
    stats = compute_weekly_stats(model)
    assert stats.period.has_prior_week is False
    for team in stats.teams:
        assert team.all_play is None
        assert team.expected_wins is None
        assert team.luck is None


def test_meaningful_from_week_boundary_is_inclusive_at_two() -> None:
    week = _week_model(
        2,
        [_roster("1", wins=2, losses=0), _roster("2", wins=0, losses=2)],
        [
            _matchup(1, "1", "2", 120.0),
            _matchup(1, "2", "1", 100.0),
            _matchup(2, "1", "2", 110.0),
            _matchup(2, "2", "1", 90.0),
        ],
    )
    stats = compute_weekly_stats(week)
    assert stats.period.has_prior_week is True
    assert _team(stats, "1").all_play is not None


def test_degenerate_playoff_week_start_below_meaningful_week_keeps_stats_none() -> None:
    """``playoff_week_start=1`` pins the all-play cutoff at week 0 -- below
    the first real week -- so even though ``has_prior_week`` is true at week
    3, there is no regular-season week to fold into the round robin.
    ``all_play``/``expected_wins``/``luck`` must stay ``None``, not surface
    an all-zero ``AllPlayRecord`` with a nonzero ``luck``."""
    week = _week_model(
        3,
        [_roster("1", wins=2, losses=0), _roster("2", wins=0, losses=2)],
        [
            _matchup(1, "1", "2", 120.0),
            _matchup(1, "2", "1", 100.0),
            _matchup(2, "1", "2", 110.0),
            _matchup(2, "2", "1", 90.0),
            _matchup(3, "1", "2", 130.0),
            _matchup(3, "2", "1", 80.0),
        ],
        playoff_week_start=1,
    )
    stats = compute_weekly_stats(week)
    assert stats.period.has_prior_week is True
    for team in stats.teams:
        assert team.all_play is None
        assert team.expected_wins is None
        assert team.luck is None


# --------------------------------------------------------------------------- #
# Row: tied matchup -- half a win in the luck-facing term; record untouched
# --------------------------------------------------------------------------- #


def test_tied_actual_matchup_counts_half_a_win_toward_luck_record_is_verbatim() -> None:
    # Week 3: rosters 1 and 2 play each other and tie at 100.0. Both rosters'
    # season record (2-0-1 each) is untouched -- it is Roster-sourced, never
    # recomputed from this head-to-head equality.
    week = _week_model(
        3,
        [_roster("1", wins=2, losses=0, ties=1), _roster("2", wins=1, losses=1, ties=1)],
        [
            _matchup(1, "1", "2", 120.0),
            _matchup(1, "2", "1", 100.0),
            _matchup(2, "1", "2", 130.0),
            _matchup(2, "2", "1", 90.0),
            _matchup(3, "1", "2", 100.0),
            _matchup(3, "2", "1", 100.0),
        ],
    )
    stats = compute_weekly_stats(week)

    team1 = _team(stats, "1")
    team2 = _team(stats, "2")
    assert team1.wins == 2 and team1.losses == 0 and team1.ties == 1
    assert team2.wins == 1 and team2.losses == 1 and team2.ties == 1

    # actual_wins_equivalent = wins + 0.5*ties: team1 = 2.5, team2 = 1.5
    assert team1.luck == round(2.5 - team1.expected_wins, 1)
    assert team2.luck == round(1.5 - team2.expected_wins, 1)

    # Week 3's own game was a tie: is_blowout False, margin 0.0.
    assert team1.this_week is not None
    assert team1.this_week.margin == 0.0
    assert team1.this_week.is_blowout is False


# --------------------------------------------------------------------------- #
# Row: all-play tie -- equal points this week counts for neither side, still
# one of the team_count(that week) - 1 decisions
# --------------------------------------------------------------------------- #


def test_all_play_tie_counts_for_neither_side_but_is_still_a_decision() -> None:
    # A 4-team week: rosters 1 and 2 tie at 100.0; roster 3 (110) beats both;
    # roster 4 (90) loses to both. Each roster logs exactly 3 decisions
    # (team_count - 1 = 4 - 1), the two ties among them.
    # Only week 2 carries any matchups here (week 1 is empty and contributes
    # no decisions) so each roster's cumulative total is exactly this one
    # week's 3 decisions.
    week = _week_model(
        2,
        [_roster(str(i)) for i in range(1, 5)],
        [
            _matchup(2, "1", "2", 100.0),
            _matchup(2, "2", "1", 100.0),
            _matchup(2, "3", "4", 110.0),
            _matchup(2, "4", "3", 90.0),
        ],
    )
    stats = compute_weekly_stats(week)

    team1 = _team(stats, "1")
    team2 = _team(stats, "2")
    assert team1.all_play is not None and team2.all_play is not None
    # roster 1 vs roster 2 tie, roster 1 loses to roster 3, beats roster 4:
    # wins=1, losses=1, ties=1 -- 3 total decisions.
    assert (team1.all_play.wins, team1.all_play.losses, team1.all_play.ties) == (1, 1, 1)
    assert team1.all_play.wins + team1.all_play.losses + team1.all_play.ties == 3
    assert (team2.all_play.wins, team2.all_play.losses, team2.all_play.ties) == (1, 1, 1)


def test_week08_median_fixture_record_is_sourced_verbatim_from_roster() -> None:
    """AC: "each team's result against the league median is counted in its
    record exactly as Sleeper counts it" -- i.e. this code never re-derives a
    record from median-scoring rules itself, it trusts ``Roster`` verbatim.
    Proven directly on the named fixture: every roster's ``TeamWeekStats``
    wins/losses/ties equal that roster's raw ``Roster`` fields, unchanged."""
    bundle = _bundle("week08-median.json")
    model = build_week_model(bundle)
    stats = compute_weekly_stats(model)
    rosters_by_id = {r.roster_id: r for r in model.rosters}
    for team in stats.teams:
        roster = rosters_by_id[team.roster_id]
        assert (team.wins, team.losses, team.ties) == (roster.wins, roster.losses, roster.ties)


def test_committed_fixture_has_a_real_all_play_tie_week10_blowout() -> None:
    """``week10-blowout.json``'s own week 2 has rosters 2 and 11 scoring an
    exact match (208.38) -- a real, non-hand-built all-play tie. Both
    rosters carry exactly one cumulative all-play tie by week 10."""
    model = build_week_model(json.loads((FIXTURE_DIR / "week10-blowout.json").read_text(encoding="utf-8")))
    stats = compute_weekly_stats(model)
    assert _team(stats, "2").all_play.ties == 1
    assert _team(stats, "11").all_play.ties == 1


# --------------------------------------------------------------------------- #
# Row: orphan/no-opponent roster, empty starter slot -- never raises
# --------------------------------------------------------------------------- #


def test_bye_matchup_still_enters_the_all_play_round_robin() -> None:
    # Roster 3 has a Matchup row this week with no opponent (a bye) -- it
    # still gets a this_week entry and still enters the round robin.
    week = _week_model(
        2,
        [_roster("1"), _roster("2"), _roster("3")],
        [
            _matchup(1, "1", "2", 100.0),
            _matchup(1, "2", "1", 90.0),
            _matchup(1, "3", None, 80.0),
            _matchup(2, "1", "2", 100.0),
            _matchup(2, "2", "1", 90.0),
            _matchup(2, "3", None, 120.0),
        ],
    )
    stats = compute_weekly_stats(week)
    team3 = _team(stats, "3")
    assert team3.this_week is not None
    assert team3.this_week.opponent_roster_id is None
    assert team3.this_week.opponent_points is None
    assert team3.this_week.margin is None
    assert team3.this_week.is_blowout is False
    # Week 2: roster 3 scored 120, beating both 1 (100) and 2 (90).
    assert team3.all_play.wins == 2


def test_opponent_roster_id_named_but_no_matchup_row_that_week_never_raises() -> None:
    """An inconsistent/malformed bundle: roster 1's ``Matchup`` names roster
    ``9`` as its opponent, but roster 9 has no matchup row of its own that
    week. This must never raise, and produces the same null
    ``opponent_points``/``margin``/``is_blowout`` shape as a genuine bye."""
    week = _week_model(2, [_roster("1")], [_matchup(1, "1", None, 100.0), _matchup(2, "1", "9", 100.0)])
    stats = compute_weekly_stats(week)
    team1 = _team(stats, "1")
    assert team1.this_week is not None
    assert team1.this_week.opponent_roster_id == "9"
    assert team1.this_week.opponent_points is None
    assert team1.this_week.margin is None
    assert team1.this_week.is_blowout is False


def test_absent_roster_gets_this_week_none_and_no_all_play_entry_that_week() -> None:
    # Roster 2 has no Matchup row at all for week 2 (eliminated/absent).
    week = _week_model(
        2,
        [_roster("1"), _roster("2")],
        [
            _matchup(1, "1", "2", 100.0),
            _matchup(1, "2", "1", 90.0),
            _matchup(2, "1", None, 100.0),
        ],
    )
    stats = compute_weekly_stats(week)
    assert _team(stats, "2").this_week is None
    # Roster 1 is the sole week-2 participant -- 0 decisions that week, so
    # its cumulative all-play only reflects week 1 (1 win).
    assert _team(stats, "1").all_play.wins == 1
    assert _team(stats, "1").all_play.wins + _team(stats, "1").all_play.losses == 1


def test_orphan_matchup_roster_absent_from_rosters_never_raises() -> None:
    """A ``Matchup`` row referencing a ``roster_id`` with no season-level
    :class:`Roster` counterpart still participates in the all-play round
    robin (affecting the other rosters' counts) but never gets its own
    :class:`TeamWeekStats` row (there is no :class:`Roster` to source
    ``wins``/``losses``/``ties`` from)."""
    week = _week_model(
        2,
        [_roster("1")],
        [
            _matchup(1, "1", None, 100.0),
            _matchup(1, "99", None, 50.0),
            _matchup(2, "1", None, 100.0),
            _matchup(2, "99", None, 150.0),
        ],
    )
    stats = compute_weekly_stats(week)
    assert {t.roster_id for t in stats.teams} == {"1"}
    team1 = _team(stats, "1")
    # Week 1: roster 1 (100) beats orphan 99 (50) -> 1 win.
    # Week 2: roster 1 (100) loses to orphan 99 (150) -> 1 loss.
    assert (team1.all_play.wins, team1.all_play.losses) == (1, 1)


def test_empty_starter_slot_and_default_matchup_fields_never_raise() -> None:
    week = _week_model(2, [_roster("1"), _roster("2")], [_matchup(1, "1", "2", 0.0), _matchup(1, "2", "1", 0.0)])
    stats = compute_weekly_stats(week)
    assert isinstance(stats, WeeklyStats)


# --------------------------------------------------------------------------- #
# Row: playoff-week fixture -- bracket classification, elimination, freezing
# --------------------------------------------------------------------------- #


def test_committed_fixture_week17_playoffs_bracket_classification_and_freeze() -> None:
    model = build_week_model(json.loads((FIXTURE_DIR / "week17-playoffs.json").read_text(encoding="utf-8")))
    stats = compute_weekly_stats(model)

    assert stats.period.type == "playoff"
    playoff_ids = {"1", "2", "4", "5"}
    consolation_ids = {"8", "10", "11", "12"}
    eliminated_ids = {"3", "6", "7", "9"}

    for roster_id in playoff_ids:
        team = _team(stats, roster_id)
        assert team.bracket == "playoff"
        assert team.this_week is not None
    for roster_id in consolation_ids:
        team = _team(stats, roster_id)
        assert team.bracket == "consolation"
        assert team.this_week is not None
    for roster_id in eliminated_ids:
        team = _team(stats, roster_id)
        assert team.bracket is None
        assert team.this_week is None

    # Regular-season-derived stats are frozen at their week-14 (playoff_week_start
    # - 1) value: recomputing on a model truncated to week 14 reproduces them.
    matchups_through_14 = [m for m in model.matchups if m.week <= 14]
    frozen_model = WeekModel(
        week=14,
        rosters=model.rosters,
        matchups=matchups_through_14,
        transactions=[],
        playoff_week_start=model.playoff_week_start,
    )
    frozen_stats = compute_weekly_stats(frozen_model)
    for roster_id in {t.roster_id for t in stats.teams}:
        assert _team(stats, roster_id).expected_wins == _team(frozen_stats, roster_id).expected_wins
        assert _team(stats, roster_id).luck == _team(frozen_stats, roster_id).luck
        assert _team(stats, roster_id).all_play == _team(frozen_stats, roster_id).all_play


def test_bracket_none_pre_playoff_even_with_bracket_data_present() -> None:
    """A roster is never classified before the playoff period, even if (an
    unusual bundle) already carries bracket rows."""
    week = _week_model(
        5,
        [_roster("1"), _roster("2")],
        [_matchup(wk, str(r), None, 100.0) for wk in range(1, 6) for r in (1, 2)],
        playoff_week_start=15,
        winners_bracket=[BracketMatch(round=1, roster_ids=["1", "2"])],
    )
    stats = compute_weekly_stats(week)
    assert stats.period.type == "regular"
    assert _team(stats, "1").bracket is None


def test_round_with_no_bracket_match_and_no_matchup_is_eliminated() -> None:
    # Round = 16 - 15 + 1 = 2: only a round-1 bracket entry exists (roster 1
    # lost in round 1 and dropped out of both brackets); roster 1 also has
    # no week-16 Matchup row -- eliminated, per the spec's own rule.
    week = _week_model(
        16,
        [_roster("1")],
        [_matchup(wk, "1", None, 100.0) for wk in range(1, 15)],
        playoff_week_start=15,
        winners_bracket=[BracketMatch(round=1, roster_ids=["1", "2"])],
    )
    stats = compute_weekly_stats(week)
    assert _team(stats, "1").bracket is None
    assert _team(stats, "1").this_week is None


def test_round_with_a_bracket_match_at_that_round_classifies_playoff() -> None:
    week = _week_model(
        16,
        [_roster("1"), _roster("2")],
        [_matchup(wk, str(r), None, 100.0) for wk in range(1, 15) for r in (1, 2)]
        + [_matchup(16, "1", "2", 100.0), _matchup(16, "2", "1", 90.0)],
        playoff_week_start=15,
        winners_bracket=[BracketMatch(round=2, roster_ids=["1", "2"])],
    )
    stats = compute_weekly_stats(week)
    assert _team(stats, "1").bracket == "playoff"
    assert _team(stats, "1").this_week is not None


# --------------------------------------------------------------------------- #
# Row: blowout flag -- the 0.65 ratio, inclusive
# --------------------------------------------------------------------------- #


def test_blowout_ratio_boundary_is_inclusive() -> None:
    # loser exactly at 0.65 * winner -> blowout; one point above -> not.
    week = _week_model(
        2,
        [_roster("1"), _roster("2"), _roster("3"), _roster("4")],
        [
            _matchup(2, "1", "2", 100.0),
            _matchup(2, "2", "1", 65.0),  # exactly BLOWOUT_RATIO * 100
            _matchup(2, "3", "4", 100.0),
            _matchup(2, "4", "3", 65.01),
        ],
    )
    stats = compute_weekly_stats(week)
    assert _team(stats, "1").this_week.is_blowout is True
    assert _team(stats, "2").this_week.is_blowout is True
    assert _team(stats, "3").this_week.is_blowout is False
    assert _team(stats, "4").this_week.is_blowout is False
    assert stats.summary.blowout_count == 1


def test_bye_matchup_can_never_be_flagged_a_blowout() -> None:
    week = _week_model(2, [_roster("1")], [_matchup(1, "1", None, 300.0), _matchup(2, "1", None, 300.0)])
    stats = compute_weekly_stats(week)
    assert _team(stats, "1").this_week.is_blowout is False


def test_zero_zero_tie_is_never_a_blowout_at_team_or_summary_level() -> None:
    """Regression: ``loser <= BLOWOUT_RATIO * winner`` is trivially true when
    both sides score 0.0 (``0 <= 0.65*0``); both ``_team_game`` and
    ``_week_summary`` must guard an exact tie out of the blowout check."""
    week = _week_model(
        2,
        [_roster("1"), _roster("2")],
        [_matchup(2, "1", "2", 0.0), _matchup(2, "2", "1", 0.0)],
    )
    stats = compute_weekly_stats(week)
    assert _team(stats, "1").this_week.is_blowout is False
    assert _team(stats, "2").this_week.is_blowout is False
    assert stats.summary.blowout_count == 0
    assert stats.summary.games == 1


# --------------------------------------------------------------------------- #
# Week summary: games, closest, biggest_blowout, high/low
# --------------------------------------------------------------------------- #


def test_week_summary_closest_and_biggest_blowout_identify_the_right_pair() -> None:
    week = _week_model(
        2,
        [_roster(str(i)) for i in range(1, 5)],
        [
            _matchup(2, "1", "2", 150.0),
            _matchup(2, "2", "1", 40.0),  # biggest blowout: margin 110
            _matchup(2, "3", "4", 101.0),
            _matchup(2, "4", "3", 100.0),  # closest: margin 1
        ],
    )
    stats = compute_weekly_stats(week)
    assert stats.summary.games == 2
    assert stats.summary.closest is not None and stats.summary.biggest_blowout is not None
    assert stats.summary.closest.roster_ids == ["3", "4"]
    assert stats.summary.closest.margin == 1.0
    assert stats.summary.biggest_blowout.roster_ids == ["1", "2"]
    assert stats.summary.biggest_blowout.margin == 110.0


def test_week_summary_high_low_and_totals() -> None:
    week = _week_model(
        2,
        [_roster("1"), _roster("2")],
        [_matchup(2, "1", "2", 150.0), _matchup(2, "2", "1", 50.0)],
    )
    stats = compute_weekly_stats(week)
    assert stats.summary.high.roster_id == "1"
    assert stats.summary.low.roster_id == "2"
    assert stats.summary.total_points == 200.0
    assert stats.summary.avg_team_score == 100.0
    assert stats.summary.closest.roster_ids == ["1", "2"]
    assert stats.summary.closest.margin == 100.0
    assert stats.summary.biggest_blowout.roster_ids == ["1", "2"]


def test_week_summary_high_low_tie_break_favors_lower_roster_id() -> None:
    week = _week_model(
        2,
        [_roster("5"), _roster("1"), _roster("3")],
        [_matchup(2, "5", None, 100.0), _matchup(2, "1", None, 100.0), _matchup(2, "3", None, 50.0)],
    )
    stats = compute_weekly_stats(week)
    assert stats.summary.high.roster_id == "1"  # tied at 100.0 with "5" -- lower id wins
    assert stats.summary.low.roster_id == "3"


def test_week_summary_is_none_for_closest_and_blowout_when_no_games() -> None:
    week = _week_model(1, [_roster("1")], [_matchup(1, "1", None, 100.0)])
    stats = compute_weekly_stats(week)
    assert stats.summary.games == 0
    assert stats.summary.closest is None
    assert stats.summary.biggest_blowout is None
    assert stats.summary.blowout_count == 0
    assert stats.summary.blowout_threshold == BLOWOUT_RATIO


# --------------------------------------------------------------------------- #
# Deterministic ordering
# --------------------------------------------------------------------------- #


def test_teams_are_ordered_by_roster_id_numerically() -> None:
    week = _week_model(
        1,
        [_roster("10"), _roster("2"), _roster("1")],
        [_matchup(1, r, None, 100.0) for r in ("10", "2", "1")],
    )
    stats = compute_weekly_stats(week)
    assert [t.roster_id for t in stats.teams] == ["1", "2", "10"]


# --------------------------------------------------------------------------- #
# Determinism -- two calls, one hand-built input
# --------------------------------------------------------------------------- #


def test_deterministic_on_hand_built_input() -> None:
    week = _week_model(
        3,
        [_roster("1", wins=2, losses=1), _roster("2", wins=1, losses=2)],
        [
            _matchup(1, "1", "2", 120.0),
            _matchup(1, "2", "1", 100.0),
            _matchup(2, "1", "2", 90.0),
            _matchup(2, "2", "1", 130.0),
            _matchup(3, "1", "2", 110.0),
            _matchup(3, "2", "1", 95.0),
        ],
    )
    first = compute_weekly_stats(week)
    second = compute_weekly_stats(copy.deepcopy(week))
    assert first.model_dump() == second.model_dump()
    assert first.model_dump_json() == second.model_dump_json()


# --------------------------------------------------------------------------- #
# Both committed fixtures reconciled against the committed expected-*.json
# --------------------------------------------------------------------------- #


def _bundle(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _expected_name(fixture_name: str) -> str:
    return f"expected-{fixture_name.removesuffix('.json')}.json"


@pytest.mark.parametrize("name", FIXTURES)
def test_committed_fixture_reconciles_with_expected(name: str) -> None:
    model = build_week_model(_bundle(name))
    stats = compute_weekly_stats(model)

    expected = json.loads((WEEKLY_DIR / _expected_name(name)).read_text(encoding="utf-8"))
    assert stats.model_dump(mode="json") == expected


@pytest.mark.parametrize("name", FIXTURES)
def test_committed_fixture_is_deterministic(name: str) -> None:
    bundle = _bundle(name)
    first = compute_weekly_stats(build_week_model(bundle))
    second = compute_weekly_stats(build_week_model(copy.deepcopy(bundle)))
    assert first.model_dump() == second.model_dump()
    assert first.model_dump_json() == second.model_dump_json()


def test_week10_blowout_ac_row_three_blowouts_one_close_non_blowout() -> None:
    """AC: 3 games flagged blowout; the 6.06-margin game is not."""
    model = build_week_model(_bundle("week10-blowout.json"))
    stats = compute_weekly_stats(model)
    assert stats.summary.blowout_count == 3
    assert stats.summary.blowout_threshold == 0.65
    assert stats.summary.closest is not None
    assert stats.summary.closest.margin == 6.06
    # the closest game's participants are not flagged a blowout
    for roster_id in stats.summary.closest.roster_ids:
        assert _team(stats, roster_id).this_week.is_blowout is False


def test_week10_blowout_every_roster_logs_team_count_minus_one_decisions() -> None:
    """AC: every roster logs ``team_count(that week) - 1`` all-play decisions
    (12-team league, no byes in weeks 1-10: 11 decisions/week * 10 weeks)."""
    model = build_week_model(_bundle("week10-blowout.json"))
    stats = compute_weekly_stats(model)
    for team in stats.teams:
        decisions = team.all_play.wins + team.all_play.losses + team.all_play.ties
        assert decisions == 11 * 10


@requires_golden
def test_week10_blowout_all_play_and_expected_wins_reconcile_with_phase0_golden() -> None:
    """AC1's actual "golden-file rule" reconciliation: every roster's
    ``all_play`` (wins/losses/pct) and ``expected_wins`` match
    ``brief/phase-0/week10-facts.json``'s ``teams[*].season`` exactly. The
    golden's ``all_play`` carries no ``ties`` field (it simply omits ties from
    the shown w/l pair -- roster 2's 87+22=109 is one short of the 110 total
    decisions, the same all-play tie ``test_committed_fixture_has_a_real_all_play_tie_week10_blowout``
    asserts directly), so ``ties`` is not compared here."""
    model = build_week_model(_bundle("week10-blowout.json"))
    stats = compute_weekly_stats(model)
    golden_by_roster = {str(t["roster_id"]): t["season"] for t in GOLDEN["teams"]}

    assert golden_by_roster, "golden file has no teams"
    for team in stats.teams:
        golden_season = golden_by_roster[team.roster_id]
        golden_all_play = golden_season["all_play"]
        assert (team.all_play.wins, team.all_play.losses, team.all_play.pct) == (
            golden_all_play["w"],
            golden_all_play["l"],
            golden_all_play["pct"],
        )
        assert team.expected_wins == golden_season["expected_wins"]


@requires_golden
def test_week10_blowout_week_summary_reconciles_with_phase0_golden() -> None:
    """AC's week-summary reconciliation: games, totals, high/low, closest,
    biggest_blowout margin, and blowout_count all match the golden's
    ``period.summary`` (the golden identifies closest/biggest_blowout by
    ``matchup_index`` into its own ``matchups`` list rather than by
    ``roster_ids`` -- this story's Design Notes deliberately chose the
    roster-id shape instead, so only the shared ``margin``/count fields are
    compared)."""
    model = build_week_model(_bundle("week10-blowout.json"))
    stats = compute_weekly_stats(model)
    golden_summary = GOLDEN["period"]["summary"]

    assert stats.summary.games == golden_summary["games"]
    assert stats.summary.total_points == golden_summary["total_points"]
    assert stats.summary.avg_team_score == golden_summary["avg_team_score"]
    assert stats.summary.high.roster_id == str(golden_summary["high"]["roster_id"])
    assert stats.summary.high.points == golden_summary["high"]["points"]
    assert stats.summary.low.roster_id == str(golden_summary["low"]["roster_id"])
    assert stats.summary.low.points == golden_summary["low"]["points"]
    assert stats.summary.closest.margin == golden_summary["closest"]["margin"]
    assert stats.summary.biggest_blowout.margin == golden_summary["biggest_blowout"]["margin"]
    assert stats.summary.blowout_count == golden_summary["blowout_count"]

    golden_period = GOLDEN["period"]
    assert stats.period.type == golden_period["type"]
    assert stats.period.has_prior_week == golden_period["has_prior_week"]


# --------------------------------------------------------------------------- #
# Import fence: stats/weekly.py never reaches a later pipeline stage
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
                names.update(
                    a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)
                )
    return names


def test_weekly_never_reaches_the_network_adapters_store_or_a_later_stage() -> None:
    imported = _imported_dotted_names(STATS_WEEKLY)
    forbidden_roots = {"adapters", "store", "facts", "narrate", "render", "deliver", "statmods"}
    offenders = [
        name
        for name in imported
        if name.split(".")[:1] == ["commishdesk"]
        and len(name.split(".")) > 1
        and name.split(".")[1] in forbidden_roots
    ]
    assert not offenders, offenders
    assert "commishdesk.ingest" in imported


def test_weekly_imports_no_clock_prng_or_filesystem_module() -> None:
    imported = _imported_dotted_names(STATS_WEEKLY)
    banned = {"datetime", "time", "os", "pathlib", "random", "secrets"}
    hits = {name for name in imported if name.split(".")[0] in banned}
    assert not hits, hits


def test_weekly_names_are_reachable_via_the_package_re_export() -> None:
    import commishdesk.stats as stats

    assert stats.compute_weekly_stats is compute_weekly_stats
    assert "WeeklyStats" in stats.__all__
    assert "compute_weekly_stats" in stats.__all__
