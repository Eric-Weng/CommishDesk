"""All-play, expected wins, luck, and blowouts (Story 5.4).

One pure function, :func:`compute_weekly_stats`, turns a stage-1
:class:`~commishdesk.ingest.WeekModel` into a frozen :class:`WeeklyStats`: the
week's :class:`Period`, a league-wide :class:`WeekSummary`, and one
:class:`TeamWeekStats` per roster in ``week.rosters`` order. It is the first
consumer of Story 5.3a/5.3b's weekly-ingest family and needs no second input
model -- ``WeekModel.matchups`` already carries every week ``1..week``, so the
cumulative all-play round robin is a pure fold over data already in hand.

A team's real record answers "who did they play"; all-play answers "how many
teams would they have beaten in an average week" -- the gap between the two is
luck. This module never touches ``Roster.wins``/``losses``/``ties`` beyond
reading them verbatim: Sleeper's own count (which may already fold in
median-scoring wins) is never recomputed from head-to-head :class:`Matchup`
comparisons.

All-play round robin. For each week ``1..cutoff`` (``cutoff`` is
``playoff_week_start - 1`` once the league has one and it is at or before the
current week, else the current week itself -- see :func:`_all_play_cutoff`),
every roster with a :class:`Matchup` that week is compared, by ``points``
only, against every other roster with a :class:`Matchup` that week: higher
score wins, lower loses, an exact tie is neither (but still one of that
week's ``participants - 1`` decisions, counted as half a win for
``expected_wins`` purposes, symmetrically with how an *actual* tied matchup
counts half a win in the luck-facing term below). ``expected_wins`` is the sum,
across those weeks, of each week's win-equivalent fraction (``wins earned that
week / decisions that week``) -- robust to a bye changing that week's
participant count, and numerically equal to ``all_play_pct * weeks_played``
whenever the participant count is constant. ``luck = actual_wins_equivalent -
expected_wins``, where ``actual_wins_equivalent = record.wins + 0.5 *
record.ties`` (the Sleeper-sourced record, read verbatim, never recomputed).
``luck`` is computed from the *raw*, unrounded ``expected_wins`` accumulator
-- only the displayed ``TeamWeekStats.expected_wins`` field and the final
``luck`` result are each independently rounded to 1 decimal, avoiding a
double-rounding drift.

``has_prior_week = week.week >= MEANINGFUL_FROM_WEEK``; when false (week 1),
``all_play``/``expected_wins``/``luck`` are ``None`` for every roster -- a
single week has no "average week" to compare against. Once
``week.week >= playoff_week_start``, these three stay pinned at their last
regular-season value (the cutoff formula above simply stops advancing), per
this story's Boundaries & Constraints.

Blowout: a completed head-to-head game (both sides have a :class:`Matchup`
naming each other as opponent) is a blowout when the loser's points are
``<= BLOWOUT_RATIO`` times the winner's. A roster on a bye that week (no
opponent) can never be flagged.

Bracket classification (playoff weeks only): ``round = week.week -
playoff_week_start + 1``; a roster is ``"playoff"`` when a
``winners_bracket`` entry at that round names it, ``"consolation"`` when a
``losers_bracket`` entry does, else ``None`` (eliminated in an earlier round,
or -- pre-playoff -- not applicable).

This module stays inside the ``stats/`` fence (AD-1): it imports only stdlib
and ``commishdesk.ingest`` -- never ``adapters`` / ``store`` / ``facts`` /
``narrate`` / ``render`` / a later pipeline stage. It makes no network call,
reads no clock, no PRNG, no filesystem, and never raises: a malformed or
degenerate :class:`~commishdesk.ingest.WeekModel` (already validated at the
ingest boundary) simply produces a materialized-but-empty result.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from commishdesk.ingest import Matchup, WeekModel

__all__ = [
    "BLOWOUT_RATIO",
    "MEANINGFUL_FROM_WEEK",
    "AllPlayRecord",
    "Period",
    "TeamGame",
    "TeamPoints",
    "TeamWeekStats",
    "WeekMargin",
    "WeekSummary",
    "WeeklyStats",
    "compute_weekly_stats",
]

MEANINGFUL_FROM_WEEK = 2
"""``all_play``/``expected_wins``/``luck`` are ``None`` before this week --
there is no "average week" to compare a single week's results against."""

BLOWOUT_RATIO = 0.65
"""A completed head-to-head game is a blowout when the loser's points are at
or below this fraction of the winner's."""


class _Frozen(BaseModel):
    """Immutable, closed to unknown keys -- matches ``stats/grades.py``."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Period(_Frozen):
    """The week this result covers. ``type`` is one week-level string --
    ``"regular"`` before ``playoff_week_start``, ``"playoff"`` once any
    bracket round is active this week (matches the only golden-file
    precedent; per-roster granularity lives on
    :attr:`TeamWeekStats.bracket` instead)."""

    week: int
    type: str
    has_prior_week: bool


class TeamPoints(_Frozen):
    """One roster's points for the week -- the shape of
    :attr:`WeekSummary.high` / :attr:`WeekSummary.low`."""

    roster_id: str
    points: float


class WeekMargin(_Frozen):
    """One game's margin, identified by its two participants directly (no
    index into a separate games list -- this story has no other reason to
    build one). ``roster_ids`` is ``[winner, loser]``; ``margin`` is
    ``winner.points - loser.points`` (non-negative; ``0.0`` for a tied
    game -- ordering is then by ``roster_id``)."""

    roster_ids: list[str]
    margin: float


class WeekSummary(_Frozen):
    """League-wide shape of the target week: how many games were played, the
    scoring spread, and the blowout count against the documented rule.
    ``high``/``low`` break a tied top/bottom score toward the lower
    ``roster_id`` (numeric where parseable, matching this module's own
    ordering convention)."""

    games: int
    total_points: float
    avg_team_score: float
    high: TeamPoints | None
    low: TeamPoints | None
    closest: WeekMargin | None
    biggest_blowout: WeekMargin | None
    blowout_count: int
    blowout_threshold: float


class AllPlayRecord(_Frozen):
    """One roster's cumulative all-play round-robin result through the
    all-play cutoff week (see the module docstring). ``pct`` is the decisive
    win rate ``wins / (wins+losses)`` -- ties are still tallied (and still
    count toward that week's decisions for ``expected_wins``) but, matching
    the standard win-percentage convention, don't enter this ratio -- rounded
    to 3 decimals, or ``0.0`` before any decisive game has been played."""

    wins: int
    losses: int
    ties: int
    pct: float


class TeamGame(_Frozen):
    """One roster's matchup for the target week. Present whenever the roster
    has a :class:`~commishdesk.ingest.Matchup` row that week. ``opponent_points``/
    ``margin`` are ``None`` and ``is_blowout`` is ``False`` whenever there is no
    resolvable opponent score to compare against -- a bye (``opponent_roster_id
    is None``), or (an inconsistent/malformed bundle) a named
    ``opponent_roster_id`` that has no matchup row of its own that week.
    ``margin`` is signed from this roster's perspective: positive when it
    outscored its opponent."""

    opponent_roster_id: str | None
    points: float
    opponent_points: float | None
    margin: float | None
    is_blowout: bool


class TeamWeekStats(_Frozen):
    """One roster's whole weekly-stats row. ``wins``/``losses``/``ties`` are
    :class:`~commishdesk.ingest.Roster`'s season-cumulative record, read
    verbatim (never recomputed from head-to-head :class:`Matchup`
    comparisons). ``this_week`` is ``None`` for a roster absent from the
    target week's matchups (bye, orphan, or eliminated from the playoffs) --
    such a roster also never enters that week's all-play round robin.
    ``all_play``/``expected_wins``/``luck`` are ``None`` before
    :data:`MEANINGFUL_FROM_WEEK`, never ``0`` -- also ``None`` in the
    degenerate case where a declared ``playoff_week_start`` of 1 or 2 pins
    the all-play cutoff below week 1, since there is then no regular-season
    week to fold into the round robin either. ``bracket`` is
    ``"playoff"``/``"consolation"``/``None`` (see the module docstring)."""

    roster_id: str
    wins: int
    losses: int
    ties: int
    this_week: TeamGame | None
    all_play: AllPlayRecord | None
    expected_wins: float | None
    luck: float | None
    bracket: str | None


class WeeklyStats(_Frozen):
    """The whole result: the week's :class:`Period`, its :class:`WeekSummary`,
    and one :class:`TeamWeekStats` per roster, ordered by ``roster_id``
    (numeric where parseable -- matches :class:`~commishdesk.ingest.WeekModel`'s
    own convention)."""

    period: Period
    summary: WeekSummary
    teams: list[TeamWeekStats]


def _sort_key(value: str) -> tuple[int, object]:
    """Order roster ids numerically when they parse as ints, lexically
    otherwise -- mirrors ``ingest/build.py``'s own ``_sort_key``."""
    try:
        return (0, int(value))
    except (TypeError, ValueError):
        return (1, value)


def _is_blowout(winner_points: float, loser_points: float) -> bool:
    return loser_points <= BLOWOUT_RATIO * winner_points


def _all_play_cutoff(week: WeekModel) -> int:
    """The last week folded into the cumulative all-play round robin. Once
    the league has a ``playoff_week_start`` at or before the current week,
    this pins at ``playoff_week_start - 1`` -- the "freeze at playoff_week_start"
    rule -- regardless of how far past it ``week.week`` has advanced."""
    if week.playoff_week_start is not None:
        return min(week.week, week.playoff_week_start - 1)
    return week.week


def _period(week: WeekModel) -> Period:
    is_playoff = week.playoff_week_start is not None and week.week >= week.playoff_week_start
    return Period(
        week=week.week,
        type="playoff" if is_playoff else "regular",
        has_prior_week=week.week >= MEANINGFUL_FROM_WEEK,
    )


def _matchups_by_week(week: WeekModel) -> dict[int, dict[str, Matchup]]:
    by_week: dict[int, dict[str, Matchup]] = {}
    for matchup in week.matchups:
        by_week.setdefault(matchup.week, {})[matchup.roster_id] = matchup
    return by_week


def _all_play_totals(
    matchups_by_week: dict[int, dict[str, Matchup]], cutoff: int
) -> tuple[dict[str, int], dict[str, int], dict[str, int], dict[str, float]]:
    """Fold weeks ``1..cutoff`` into cumulative all-play wins/losses/ties plus
    the summed per-week win-equivalent fraction (``expected_wins`` before
    rounding), keyed by roster id. A roster with no counterpart in
    :attr:`WeekModel.rosters` (an orphan matchup row) still participates --
    it still affects every other roster's count -- it simply has no
    :class:`TeamWeekStats` entry to be reported on later."""
    wins: dict[str, int] = {}
    losses: dict[str, int] = {}
    ties: dict[str, int] = {}
    expected: dict[str, float] = {}

    for wk in range(1, cutoff + 1):
        rows = matchups_by_week.get(wk, {})
        participants = sorted(rows, key=_sort_key)
        decisions = len(participants) - 1
        if decisions < 1:
            continue
        week_win_equiv: dict[str, float] = dict.fromkeys(participants, 0.0)
        for i, roster_a in enumerate(participants):
            points_a = rows[roster_a].points
            for roster_b in participants[i + 1 :]:
                points_b = rows[roster_b].points
                if points_a > points_b:
                    wins[roster_a] = wins.get(roster_a, 0) + 1
                    losses[roster_b] = losses.get(roster_b, 0) + 1
                    week_win_equiv[roster_a] += 1.0
                elif points_b > points_a:
                    wins[roster_b] = wins.get(roster_b, 0) + 1
                    losses[roster_a] = losses.get(roster_a, 0) + 1
                    week_win_equiv[roster_b] += 1.0
                else:
                    ties[roster_a] = ties.get(roster_a, 0) + 1
                    ties[roster_b] = ties.get(roster_b, 0) + 1
                    week_win_equiv[roster_a] += 0.5
                    week_win_equiv[roster_b] += 0.5
        for roster_id in participants:
            expected[roster_id] = expected.get(roster_id, 0.0) + week_win_equiv[roster_id] / decisions

    return wins, losses, ties, expected


def _team_game(matchup: Matchup, week_rows: dict[str, Matchup]) -> TeamGame:
    opponent = week_rows.get(matchup.opponent_roster_id) if matchup.opponent_roster_id is not None else None
    opponent_points = opponent.points if opponent is not None else None
    margin = round(matchup.points - opponent_points, 2) if opponent_points is not None else None
    is_blowout = False
    if opponent_points is not None and matchup.points != opponent_points:
        winner_points = max(matchup.points, opponent_points)
        loser_points = min(matchup.points, opponent_points)
        is_blowout = _is_blowout(winner_points, loser_points)
    return TeamGame(
        opponent_roster_id=matchup.opponent_roster_id,
        points=matchup.points,
        opponent_points=opponent_points,
        margin=margin,
        is_blowout=is_blowout,
    )


def _bracket_for(roster_id: str, week: WeekModel, period: Period) -> str | None:
    if period.type != "playoff" or week.playoff_week_start is None:
        return None
    round_no = week.week - week.playoff_week_start + 1
    if any(match.round == round_no and roster_id in match.roster_ids for match in week.winners_bracket):
        return "playoff"
    if any(match.round == round_no and roster_id in match.roster_ids for match in week.losers_bracket):
        return "consolation"
    return None


def _week_summary(target_week_rows: dict[str, Matchup]) -> WeekSummary:
    scores = [(roster_id, matchup.points) for roster_id, matchup in target_week_rows.items()]

    total_points = round(sum(points for _, points in scores), 2)
    avg_team_score = round(total_points / len(scores), 2) if scores else 0.0

    high = None
    low = None
    if scores:
        best = min(scores, key=lambda kv: (-kv[1], _sort_key(kv[0])))
        worst = min(scores, key=lambda kv: (kv[1], _sort_key(kv[0])))
        high = TeamPoints(roster_id=best[0], points=best[1])
        low = TeamPoints(roster_id=worst[0], points=worst[1])

    seen_pairs: set[frozenset[str]] = set()
    games: list[tuple[str, str, float, bool]] = []  # (winner, loser, margin, is_blowout)
    for roster_id, matchup in target_week_rows.items():
        opponent_id = matchup.opponent_roster_id
        if opponent_id is None or opponent_id not in target_week_rows:
            continue
        pair = frozenset((roster_id, opponent_id))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)

        opponent_points = target_week_rows[opponent_id].points
        if matchup.points > opponent_points:
            winner, loser = roster_id, opponent_id
        elif opponent_points > matchup.points:
            winner, loser = opponent_id, roster_id
        else:
            winner, loser = sorted((roster_id, opponent_id), key=_sort_key)
        winner_points = target_week_rows[winner].points
        loser_points = target_week_rows[loser].points
        margin = round(winner_points - loser_points, 2)
        is_blowout = winner_points != loser_points and _is_blowout(winner_points, loser_points)
        games.append((winner, loser, margin, is_blowout))

    closest = None
    biggest_blowout = None
    if games:
        closest_game = min(games, key=lambda g: (g[2], _sort_key(g[0])))
        biggest_game = min(games, key=lambda g: (-g[2], _sort_key(g[0])))
        closest = WeekMargin(roster_ids=[closest_game[0], closest_game[1]], margin=closest_game[2])
        biggest_blowout = WeekMargin(roster_ids=[biggest_game[0], biggest_game[1]], margin=biggest_game[2])

    return WeekSummary(
        games=len(games),
        total_points=total_points,
        avg_team_score=avg_team_score,
        high=high,
        low=low,
        closest=closest,
        biggest_blowout=biggest_blowout,
        blowout_count=sum(1 for *_, is_blowout in games if is_blowout),
        blowout_threshold=BLOWOUT_RATIO,
    )


def compute_weekly_stats(week: WeekModel) -> WeeklyStats:
    """Compute all-play, expected wins, luck, and blowout flags for one
    league-week.

    Pure, deterministic, offline. A valid :class:`~commishdesk.ingest.WeekModel`
    in, a :class:`WeeklyStats` out; two calls on one input return an equal
    ``model_dump()``. Never raises."""
    period = _period(week)
    matchups_by_week = _matchups_by_week(week)
    target_week_rows = matchups_by_week.get(week.week, {})

    all_play_wins: dict[str, int] = {}
    all_play_losses: dict[str, int] = {}
    all_play_ties: dict[str, int] = {}
    expected_wins_acc: dict[str, float] = {}
    has_meaningful_all_play = period.has_prior_week and _all_play_cutoff(week) >= 1
    if has_meaningful_all_play:
        all_play_wins, all_play_losses, all_play_ties, expected_wins_acc = _all_play_totals(
            matchups_by_week, _all_play_cutoff(week)
        )

    teams: list[TeamWeekStats] = []
    for roster in sorted(week.rosters, key=lambda r: _sort_key(r.roster_id)):
        roster_id = roster.roster_id
        matchup = target_week_rows.get(roster_id)
        this_week = _team_game(matchup, target_week_rows) if matchup is not None else None

        all_play: AllPlayRecord | None = None
        expected_wins: float | None = None
        luck: float | None = None
        if has_meaningful_all_play:
            wins = all_play_wins.get(roster_id, 0)
            losses = all_play_losses.get(roster_id, 0)
            ties = all_play_ties.get(roster_id, 0)
            pct = round(wins / (wins + losses), 3) if (wins + losses) else 0.0
            all_play = AllPlayRecord(wins=wins, losses=losses, ties=ties, pct=pct)
            raw_expected_wins = expected_wins_acc.get(roster_id, 0.0)
            expected_wins = round(raw_expected_wins, 1)
            actual_wins_equivalent = roster.wins + 0.5 * roster.ties
            luck = round(actual_wins_equivalent - raw_expected_wins, 1)

        teams.append(
            TeamWeekStats(
                roster_id=roster_id,
                wins=roster.wins,
                losses=roster.losses,
                ties=roster.ties,
                this_week=this_week,
                all_play=all_play,
                expected_wins=expected_wins,
                luck=luck,
                bracket=_bracket_for(roster_id, week, period),
            )
        )

    return WeeklyStats(period=period, summary=_week_summary(target_week_rows), teams=teams)
