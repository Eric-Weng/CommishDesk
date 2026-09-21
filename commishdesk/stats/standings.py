"""Standings, the derived playoff picture, and Sleeper's own cross-check (Story 5.6).

:func:`compute_standings` folds a :class:`~commishdesk.ingest.WeekModel`'s
head-to-head matchups -- **never** ``Roster.wins`` / ``losses`` / ``ties`` /
``fpts`` -- into one :class:`TeamStanding` per roster, the per-division order,
and (when ``league.format.playoff`` is present) a :class:`PlayoffPicture`.
:func:`cross_check_standings` then compares that fold against Sleeper's own
season totals and raises a typed
:class:`~commishdesk.errors.CrossCheckError` when they disagree.

**Why matchup-derived.** Every committed fixture's ``rosters`` section is one
season-final pull, so a week-10 slice carries week-10 *matchups* next to
season-end *records* (roster 1 reads 10-4 while the week-10 fold is 7-3).
Folding the matchups -- verified against the phase-0 golden -- reproduces the
real week-10 standings; reading the roster totals would not. Those totals feed
exactly one thing: the cross-check.

**Ordering.** ``win_pct`` -- ``(W + 0.5*T) / games``, ``0.0`` with no games --
descending, then points-for descending, then ``roster_id`` (numeric where
parseable, ``stats/weekly.py``'s own ``_sort_key`` convention). The tiebreak is
named by :data:`TIEBREAK` and surfaced as ``Standings.tiebreak`` so a reader
never has to guess which key decided a rank. Division order uses the same key,
restricted to that division's rosters.

**Freeze.** The fold stops at ``min(week.week, playoff_week_start - 1)``
(``stats/weekly.py``'s own ``_all_play_cutoff``), so from ``playoff_week_start``
on a playoff-week Issue reports the final regular-season standings rather than a
live, changing race (FR-10). ``regular_season_complete`` says whether that freeze
has engaged.

**Playoff picture.** Derived, never hardcoded: the bracket is the top
``league.format.playoff.bracket_teams`` ranks clamped to the roster count, the
byes are the top ``2^ceil(log2 N) - N`` of them, ``first_out`` is rank ``N+1``
(``None`` when the whole league is in), ``bubble`` is ranks ``N-1..N+2`` clipped
to valid ranks, ``cut_line_after_rank`` is ``N``, and ``consolation`` is
everything below the cut. The commissioner override is a later story (5.15).

**Cross-check.** ``cross_check_standings(standings, week)`` compares, for every
roster in ``week.rosters``, the folded W-L-T against ``Roster``'s exactly and the
folded points-for against ``Roster.fpts`` within
:data:`POINTS_FOR_ROUNDING_TOLERANCE` per folded week (the half-cent each week's
2-dp points can drift) plus a tiny epsilon. Any divergence collects into **one**
:class:`~commishdesk.errors.CrossCheckError` -- a hold, never a wrong number
shipped.

This module stays inside the ``stats/`` fence (AD-1): it imports only stdlib,
pydantic, ``commishdesk.errors``, ``commishdesk.ingest``, and its sibling
``stats/`` modules -- never ``adapters`` / ``store`` / ``facts`` / ``narrate`` /
``render`` / a later pipeline stage. It reads no file, clock, PRNG, or network.
The one thing it raises is :class:`~commishdesk.errors.CrossCheckError`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from commishdesk.errors import CrossCheckError, CrossCheckMismatch
from commishdesk.ingest import LeagueModel, WeekModel

from .weekly import _all_play_cutoff, _matchups_by_week, _sort_key

__all__ = [
    "POINTS_FOR_ROUNDING_TOLERANCE",
    "TIEBREAK",
    "DivisionOrder",
    "PlayoffPicture",
    "Standings",
    "Streak",
    "TeamRecord",
    "TeamStanding",
    "WeekPoints",
    "compute_standings",
    "cross_check_standings",
    "regular_season_records",
]

#: Each folded week's ``Matchup.points`` is already at 2 decimals while Sleeper's
#: season total is exact, so a roster's points-for can legitimately differ by up
#: to half a hundredth per folded week. The cross-check allows exactly that much
#: drift (times the number of weeks actually folded) plus a tiny float epsilon --
#: no more. The week-17 fixture's real drift is exactly 14 x 0.005.
POINTS_FOR_ROUNDING_TOLERANCE = 0.005

#: The one tiebreak key consulted after win percentage -- named so a reader (or
#: a later story) never has to infer it. Surfaced as ``Standings.tiebreak``.
TIEBREAK = "points_for"

#: The fields ``cross_check_standings`` compares, in the order a mismatch is
#: reported for a single roster.
_CHECKED_FIELDS = ("wins", "losses", "ties", "points_for")


class _Frozen(BaseModel):
    """Immutable, closed to unknown keys -- matches ``stats/weekly.py``."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Streak(_Frozen):
    """One roster's current run of identical results: ``type`` is ``"W"`` /
    ``"L"`` / ``"T"`` and ``count`` how many consecutive games back it goes. A
    bye week is not a game, so it neither extends nor breaks a streak. ``None``
    (not an empty streak) before the roster's first game."""

    type: str
    count: int


class WeekPoints(_Frozen):
    """One roster's points in one week -- the shape of :attr:`TeamStanding.high_week`
    / :attr:`TeamStanding.low_week`."""

    week: int
    points: float


class TeamRecord(_Frozen):
    """One roster's head-to-head regular-season fold through the cutoff week.

    ``wins`` / ``losses`` / ``ties`` count only weeks the roster had a
    resolvable opponent; a bye week still adds to ``points_for`` and appends a
    :class:`WeekPoints`. ``results`` is that game sequence in week order (one
    ``"W"`` / ``"L"`` / ``"T"`` per game -- the raw material for
    :class:`Streak`) and ``week_points`` every folded week, bye included -- the
    raw material for ``high_week`` / ``low_week``. This is the shared fold:
    :func:`compute_standings` reads it for the table and ``stats/power.py``
    reuses it for its head-to-head component."""

    roster_id: str
    wins: int = 0
    losses: int = 0
    ties: int = 0
    points_for: float = 0.0
    points_against: float = 0.0
    results: list[str] = []
    week_points: list[WeekPoints] = []


class TeamStanding(_Frozen):
    """One roster's whole standings row.

    ``win_pct`` is ``(W + 0.5*T) / games`` at 3 decimals (``0.0`` with no
    games); ``points_for`` / ``points_against`` are at 2. ``rank`` is overall,
    ``division_rank`` within the roster's division (``None`` when the league
    declares no divisions). ``streak`` is ``None`` before the roster's first
    game; ``high_week`` / ``low_week`` are ``None`` when the roster played no
    folded week at all, and the earlier week wins a tie."""

    roster_id: str
    wins: int
    losses: int
    ties: int
    win_pct: float
    points_for: float
    points_against: float
    rank: int
    division_rank: int | None
    streak: Streak | None
    high_week: WeekPoints | None
    low_week: WeekPoints | None


class DivisionOrder(_Frozen):
    """One division's rosters in standings order (the same tiebreak as the
    overall table, restricted to this division's rosters). ``Standings.divisions``
    is empty when the league declares none."""

    division_id: int
    roster_ids: list[str]


class PlayoffPicture(_Frozen):
    """The derived playoff picture for the target week (``source`` is always
    ``"derived"`` for this module -- the commissioner override is a later
    story).

    ``in_bracket`` is ranks ``1..N`` (``N = league.format.playoff.bracket_teams``
    clamped to the roster count); ``byes`` the top ``2^ceil(log2 N) - N`` of
    them; ``first_out`` rank ``N+1`` (``None`` when the whole league is in);
    ``bubble`` ranks ``N-1..N+2`` clipped to valid ranks; ``cut_line_after_rank``
    is ``N``; ``consolation`` ranks ``N+1..end``."""

    source: str
    in_bracket: list[str]
    byes: list[str]
    first_out: str | None
    bubble: list[str]
    cut_line_after_rank: int
    consolation: list[str]


class Standings(_Frozen):
    """The whole result: one :class:`TeamStanding` per roster in
    ``week.rosters``, ordered by ``rank`` (1 first), plus the per-division order
    and the derived playoff picture.

    ``through_week`` is the last week folded -- ``min(week.week,
    playoff_week_start - 1)`` once the league declares a ``playoff_week_start``,
    else ``week.week`` -- and ``regular_season_complete`` whether the freeze has
    engaged. ``tiebreak`` names the key consulted after win percentage
    (:data:`TIEBREAK`)."""

    week: int
    through_week: int
    regular_season_complete: bool
    tiebreak: str = TIEBREAK
    teams: list[TeamStanding]
    divisions: list[DivisionOrder]
    playoff_picture: PlayoffPicture | None


# --------------------------------------------------------------------------- #
# The fold
# --------------------------------------------------------------------------- #


def regular_season_records(week: WeekModel, through_week: int | None = None) -> dict[str, TeamRecord]:
    """Head-to-head W-L-T and points for/against, folded over weeks
    ``1..through_week`` (default: the regular-season cutoff, ``stats/weekly.py``'s
    own ``_all_play_cutoff``).

    A week only counts as a *game* when the roster's :class:`~commishdesk.ingest.Matchup`
    names an opponent that has its own row that week. A bye week
    (``opponent_roster_id is None``, or an opponent with no row, or a
    self-paired row) still contributes points-for and a ``week_points`` entry,
    but no W/L/T, no points-against, and no streak result.

    Keys are roster ids, so an orphan matchup row (a roster absent from
    ``week.rosters``) still folds and still counts against its opponent. The
    insertion order is the first week each roster appeared, which makes the
    whole fold deterministic for equal inputs."""
    cutoff = _all_play_cutoff(week) if through_week is None else through_week
    by_week = _matchups_by_week(week)

    wins: dict[str, int] = {}
    losses: dict[str, int] = {}
    ties: dict[str, int] = {}
    points_for: dict[str, float] = {}
    points_against: dict[str, float] = {}
    results: dict[str, list[str]] = {}
    week_points: dict[str, list[WeekPoints]] = {}
    seen: list[str] = []

    for wk in range(1, cutoff + 1):
        rows = by_week.get(wk, {})
        for roster_id, matchup in rows.items():
            if roster_id not in week_points:
                seen.append(roster_id)
                week_points[roster_id] = []
                results[roster_id] = []
            week_points[roster_id].append(WeekPoints(week=wk, points=round(matchup.points, 2)))
            points_for[roster_id] = points_for.get(roster_id, 0.0) + round(matchup.points, 2)

            opponent_id = matchup.opponent_roster_id
            opponent = rows.get(opponent_id) if opponent_id is not None else None
            if opponent is None or opponent_id == roster_id:
                continue
            points_against[roster_id] = points_against.get(roster_id, 0.0) + round(opponent.points, 2)
            if matchup.points > opponent.points:
                wins[roster_id] = wins.get(roster_id, 0) + 1
                results[roster_id].append("W")
            elif matchup.points < opponent.points:
                losses[roster_id] = losses.get(roster_id, 0) + 1
                results[roster_id].append("L")
            else:
                ties[roster_id] = ties.get(roster_id, 0) + 1
                results[roster_id].append("T")

    return {
        roster_id: TeamRecord(
            roster_id=roster_id,
            wins=wins.get(roster_id, 0),
            losses=losses.get(roster_id, 0),
            ties=ties.get(roster_id, 0),
            points_for=round(points_for.get(roster_id, 0.0), 2),
            points_against=round(points_against.get(roster_id, 0.0), 2),
            results=results.get(roster_id, []),
            week_points=week_points.get(roster_id, []),
        )
        for roster_id in seen
    }


def _order_key(roster_id: str, records: dict[str, TeamRecord]) -> tuple[float, float, tuple[int, object]]:
    """The one standings key: win percentage descending, then points-for
    descending, then ``roster_id`` (numeric where parseable). Computed on the
    unrounded win percentage so a 3-decimal display tie is still resolved by the
    real fraction."""
    record = records.get(roster_id)
    wins, losses, ties = (record.wins, record.losses, record.ties) if record is not None else (0, 0, 0)
    games = wins + losses + ties
    win_pct = (wins + 0.5 * ties) / games if games else 0.0
    points_for = record.points_for if record is not None else 0.0
    return (-win_pct, -points_for, _sort_key(roster_id))


def _streak(results: list[str]) -> Streak | None:
    """The run of identical results ending at the most recent game, or ``None``
    when the roster has not played one."""
    if not results:
        return None
    last = results[-1]
    count = 0
    for result in reversed(results):
        if result != last:
            break
        count += 1
    return Streak(type=last, count=count)


def _extremes(week_points: list[WeekPoints]) -> tuple[WeekPoints | None, WeekPoints | None]:
    """The highest- and lowest-scoring folded week. Strict comparison, so the
    earlier week wins a tie."""
    high: WeekPoints | None = None
    low: WeekPoints | None = None
    for entry in week_points:
        if high is None or entry.points > high.points:
            high = entry
        if low is None or entry.points < low.points:
            low = entry
    return high, low


def _team_standing(
    roster_id: str,
    records: dict[str, TeamRecord],
    rank_of: dict[str, int],
    division_rank: dict[str, int],
) -> TeamStanding:
    record = records.get(roster_id) or TeamRecord(roster_id=roster_id)
    games = record.wins + record.losses + record.ties
    win_pct = round((record.wins + 0.5 * record.ties) / games, 3) if games else 0.0
    high_week, low_week = _extremes(record.week_points)
    return TeamStanding(
        roster_id=roster_id,
        wins=record.wins,
        losses=record.losses,
        ties=record.ties,
        win_pct=win_pct,
        points_for=record.points_for,
        points_against=record.points_against,
        rank=rank_of[roster_id],
        division_rank=division_rank.get(roster_id),
        streak=_streak(record.results),
        high_week=high_week,
        low_week=low_week,
    )


def _playoff_picture(ordered: list[str], league: LeagueModel) -> PlayoffPicture | None:
    """The derived playoff picture for a standings order, or ``None`` when the
    league declares no playoff bracket size (``league.format.playoff is None``)
    or has no rosters. Every size here is computed from the league's own declared
    ``bracket_teams`` -- none of them is a literal."""
    playoff = league.format.playoff
    if playoff is None or not ordered:
        return None

    bracket = min(playoff.bracket_teams, len(ordered))
    in_bracket = ordered[:bracket]
    # The bye count that makes the bracket a clean power-of-two field: the
    # smallest power of two at or above the bracket size, minus the bracket size.
    bye_count = (1 << (bracket - 1).bit_length()) - bracket
    bubble = ordered[max(bracket - 1, 1) - 1 : min(bracket + 2, len(ordered))]
    return PlayoffPicture(
        source="derived",
        in_bracket=in_bracket,
        byes=in_bracket[:bye_count],
        first_out=ordered[bracket] if bracket < len(ordered) else None,
        bubble=bubble,
        cut_line_after_rank=bracket,
        consolation=ordered[bracket:],
    )


def compute_standings(week: WeekModel, league: LeagueModel) -> Standings:
    """Fold the target week's matchups into the league's standings, then derive
    the playoff picture.

    Pure, deterministic, offline: two calls on equal inputs return an equal
    ``model_dump()``. Never reads ``Roster.wins`` / ``losses`` / ``ties`` /
    ``fpts`` -- those are Sleeper's own season totals, compared only by
    :func:`cross_check_standings`. Never raises."""
    through_week = max(_all_play_cutoff(week), 0)
    records = regular_season_records(week, through_week)

    roster_ids = [roster.roster_id for roster in week.rosters]
    ordered = sorted(roster_ids, key=lambda roster_id: _order_key(roster_id, records))
    rank_of = {roster_id: index + 1 for index, roster_id in enumerate(ordered)}

    division_of = {team.roster_id: team.division_id for team in league.teams}
    declared = [division.id for division in league.format.divisions]
    groups: dict[int, list[str]] = {division_id: [] for division_id in declared}
    for roster_id in ordered:
        division_id = division_of.get(roster_id)
        if division_id in groups:
            groups[division_id].append(roster_id)
    division_rank = {
        roster_id: index + 1
        for division_id in declared
        for index, roster_id in enumerate(groups[division_id])
    }
    divisions = [
        DivisionOrder(division_id=division_id, roster_ids=groups[division_id])
        for division_id in declared
    ]

    playoff_week_start = week.playoff_week_start
    return Standings(
        week=week.week,
        through_week=through_week,
        regular_season_complete=(
            playoff_week_start is not None and week.week >= playoff_week_start - 1
        ),
        teams=[
            _team_standing(roster_id, records, rank_of, division_rank) for roster_id in ordered
        ],
        divisions=divisions,
        playoff_picture=_playoff_picture(ordered, league),
    )


def cross_check_standings(standings: Standings, week: WeekModel) -> None:
    """Compare the folded standings against Sleeper's own season totals.

    For every roster in ``week.rosters``: ``wins`` / ``losses`` / ``ties`` must
    match exactly, and ``points_for`` must be within
    :data:`POINTS_FOR_ROUNDING_TOLERANCE` times the folded week count plus a tiny
    float epsilon (the half-cent each week's 2-dp points can drift; the week-17
    fixture's real drift is exactly 14 x 0.005).

    Returns ``None`` on agreement. Any divergence is collected -- in
    ``(roster, field)`` order -- into **one**
    :class:`~commishdesk.errors.CrossCheckError` carrying the typed
    :class:`~commishdesk.errors.CrossCheckMismatch` tuple, so a caller sees every
    disagreement at once, never just the first. Pure, deterministic, offline."""
    computed = {team.roster_id: team for team in standings.teams}
    tolerance = POINTS_FOR_ROUNDING_TOLERANCE * standings.through_week + 1e-6

    mismatches: list[CrossCheckMismatch] = []
    for roster in sorted(week.rosters, key=lambda item: _sort_key(item.roster_id)):
        team = computed.get(roster.roster_id)
        if team is None:
            continue
        sleeper_values: dict[str, int | float] = {
            "wins": roster.wins,
            "losses": roster.losses,
            "ties": roster.ties,
            "points_for": roster.fpts,
        }
        computed_values: dict[str, int | float] = {
            "wins": team.wins,
            "losses": team.losses,
            "ties": team.ties,
            "points_for": team.points_for,
        }
        for field in _CHECKED_FIELDS:
            got = computed_values[field]
            want = sleeper_values[field]
            disagreed = abs(got - want) > tolerance if field == "points_for" else got != want
            if disagreed:
                mismatches.append(
                    CrossCheckMismatch(
                        roster_id=roster.roster_id, field=field, computed=got, sleeper=want
                    )
                )

    if mismatches:
        raise CrossCheckError(tuple(mismatches))
