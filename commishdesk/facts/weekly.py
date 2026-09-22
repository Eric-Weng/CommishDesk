"""Stage 3 builder — the standalone weekly Facts document (Story 5.8).

:func:`build_weekly_facts` is the one place a stage-1 :class:`WeekModel` +
:class:`LeagueModel`, a ``{player_id: PlayerSnapshot}`` map and a
``{player_id: name}`` map become the published ``weekly`` contract (AD-2). It
runs the weekly stats modules (5.4-5.7), joins names, builds per-week history,
projects and caps the :class:`~commishdesk.facts.schema.WeeklyNarration`, and
self-validates.

Story 5.9 wires the weekly lead angles
(``facts/leads.py::build_weekly_lead_candidates``) and the ``kind="weekly"``
branch of the storyline lifecycle (``facts/storylines.py``) into both the
document and its narration projection, replacing the two ``[]`` placeholders.

Pure, deterministic, offline: no network, no clock, no filesystem.
``generated_at`` is a caller argument, so two builds of one input produce an
equal ``model_dump()`` modulo nothing.

**Sources of truth.** Every computed number comes from ``commishdesk.stats``
(``compute_weekly_stats``, ``compute_weekly_lineups``, ``compute_power_ranks``,
``compute_standings``, ``compute_next_week``, ``compute_transactions_desk``,
``regular_season_records``) or a direct read of ``WeekModel`` — nothing is
recomputed a second way. History ``power_rank`` is
``compute_power_ranks(week, through_week=k)``; ``all_play`` is the cumulative
fold from ``compute_weekly_stats`` on a copy scoped to ``week=k,
playoff_week_start=None`` (the ``stats/power.py`` trick); per-week ``luck`` is
``round(win-equivalents_k - expected_wins_k, 1)`` — the season ``luck`` stays
authoritative and the two can differ by at most 0.1.

**Tunables.** :data:`TOP_PERFORMERS`, :data:`DUD_POINTS` / :data:`DUD_LIMIT`,
:data:`TOP_PLAYERS` and :data:`WORST_STARTERS` are named module constants.
Every tie breaks by points, then numeric roster id, then player id.

**Fence.** This module imports only stdlib, pydantic,
``commishdesk.errors``, ``commishdesk.ingest``, ``commishdesk.stats`` and
sibling ``facts/`` — never ``adapters`` / ``store`` / ``narrate`` / ``render``
/ ``deliver`` / ``httpx`` / ``statmods`` / ``consensus``, and reads no file,
clock, PRNG or network.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import cast

from pydantic import ValidationError

from commishdesk.errors import SchemaValidationError
from commishdesk.ingest import LeagueModel, Matchup, PlayerSnapshot, WeekModel
from commishdesk.stats.lineup import TeamLineup, compute_weekly_lineups
from commishdesk.stats.power import compute_power_ranks
from commishdesk.stats.stakes import compute_next_week
from commishdesk.stats.standings import (
    PlayoffPicture,
    bye_count,
    compute_standings,
    regular_season_records,
)
from commishdesk.stats.transactions import TransactionsDesk, compute_transactions_desk
from commishdesk.stats.weekly import (
    MEANINGFUL_FROM_WEEK,
    TeamWeekStats,
    compute_weekly_stats,
)

from .build import _current_cap, _generated_at, _violation_message, _within_cap
from .leads import build_weekly_lead_candidates
from .schema import (
    DivisionRef,
    LeadCandidate,
    Source,
    Storyline,
    StorylineCandidate,
    TeamPointsRef,
    WeeklyAllPlay,
    WeeklyBenchRegret,
    WeeklyByeImpact,
    WeeklyByeStarter,
    WeeklyCoachingEfficiency,
    WeeklyFacts,
    WeeklyFormatRef,
    WeeklyHeadlinePlayer,
    WeeklyHistory,
    WeeklyHistoryRow,
    WeeklyLeaderPlayer,
    WeeklyLeaderRef,
    WeeklyLeaders,
    WeeklyLeagueRef,
    WeeklyMarketNote,
    WeeklyMatchup,
    WeeklyMatchups,
    WeeklyMove,
    WeeklyMovePlayer,
    WeeklyNarration,
    WeeklyNarrationGame,
    WeeklyNarrationLeague,
    WeeklyNarrationLuck,
    WeeklyNarrationNextWeek,
    WeeklyNarrationPlayoff,
    WeeklyNarrationPower,
    WeeklyNarrationStanding,
    WeeklyNarrationTransactions,
    WeeklyNextWeekCard,
    WeeklyPerformer,
    WeeklyPeriod,
    WeeklyPickRef,
    WeeklyPlayoffPicture,
    WeeklyPlayoffRef,
    WeeklyPower,
    WeeklyRecord,
    WeeklySeason,
    WeeklyStandings,
    WeeklyStarter,
    WeeklyStreak,
    WeeklyTeam,
    WeeklyTeamGame,
    WeeklyTeamNextWeek,
    WeeklyTrade,
    WeeklyTradeSide,
    WeeklyTransactions,
    WeeklyWeekHighPlayer,
    WeeklyWeekPoints,
    WeekMarginRef,
    WeekSummaryRef,
)
from .storylines import advance_storylines, project_storyline_candidates

__all__ = [
    "DUD_LIMIT",
    "DUD_POINTS",
    "TOP_PERFORMERS",
    "TOP_PLAYERS",
    "WORST_STARTERS",
    "build_weekly_facts",
]

TOP_PERFORMERS = 3
"""How many of a roster's best starters the week block names."""

DUD_POINTS = 6.0
"""A started player strictly below this is a dud."""

DUD_LIMIT = 3
"""At most this many duds per roster, worst first."""

TOP_PLAYERS = 5
"""How many of the week's best starters league-wide the leaders block names."""

WORST_STARTERS = 5
"""How many of the week's worst starters league-wide the leaders block names."""


def _sort_key(value: str) -> tuple[int, object]:
    """Order ids numerically when they parse as ints, lexically otherwise."""
    try:
        return (0, int(value))
    except (TypeError, ValueError):
        return (1, value)


def _rec_str(wins: int, losses: int, ties: int) -> str:
    return f"{wins}-{losses}-{ties}"


def _team_label(team_name: str | None, manager: str | None, roster_id: str) -> str:
    """The label a narrator reads: the team name, else the manager, else a
    roster-id fallback — the ``roster_id`` is always carried alongside."""
    return team_name or manager or f"Roster {roster_id}"


def _regular_cutoff(week: WeekModel) -> int:
    """The last regular-season week, ``playoff_week_start - 1`` once declared."""
    if week.playoff_week_start is None:
        return week.week
    return max(0, week.playoff_week_start - 1)


def _week_shape(week: WeekModel, period_type: str) -> str:
    """A non-prose machine label for the week's shape. Playoff status wins
    over cold-start when both apply (a ``playoff_week_start`` of 1 declares
    week 1 itself the first playoff week)."""
    if period_type == "playoff":
        return "playoff"
    if week.week == 1:
        return "cold_start"
    if week.playoff_week_start is not None and week.week == week.playoff_week_start - 1:
        return "final_regular"
    return "regular"


def _name_resolver(
    players: Mapping[str, PlayerSnapshot], player_names: Mapping[str, str]
) -> Callable[[str], str]:
    """Resolve a player id to a display name: the joined name, else the
    snapshot's NFL team (a DST / blank record), else the id itself."""

    def resolve(player_id: str) -> str:
        name = player_names.get(player_id)
        if name:
            return name
        snapshot = players.get(player_id)
        if snapshot is not None and snapshot.nfl_team:
            return snapshot.nfl_team
        return player_id

    return resolve


def _pos(players: Mapping[str, PlayerSnapshot], player_id: str) -> str | None:
    snapshot = players.get(player_id)
    return snapshot.position if snapshot is not None else None


def _nfl_team(players: Mapping[str, PlayerSnapshot], player_id: str) -> str | None:
    snapshot = players.get(player_id)
    return snapshot.nfl_team if snapshot is not None else None


def build_weekly_facts(
    week: WeekModel,
    league: LeagueModel,
    players: Mapping[str, PlayerSnapshot],
    player_names: Mapping[str, str],
    *,
    generated_at: datetime | str,
    nfl_byes: frozenset[str] | None = None,
    nfl_byes_next_week: frozenset[str] | None = None,
    fetched_at: str | None = None,
    provisional: bool = True,
    previous_storylines: Sequence[Storyline] = (),
) -> WeeklyFacts:
    """Merge the weekly stats modules into a validated :class:`WeeklyFacts`.

    Pure / deterministic / offline. ``generated_at`` follows the
    ``build_draft_recap_facts`` rule. ``previous_storylines`` is the league's
    persisted narrative memory (Story 5.9), advanced by the ``kind="weekly"``
    branch of :func:`~commishdesk.facts.storylines.advance_storylines`. Raises
    :class:`~commishdesk.errors.SchemaValidationError` (chained from the
    underlying error, message naming ``weekly``) when the merged document does
    not satisfy the schema; returns no partial document. A malformed
    ``roster_slots`` propagates the stats layer's
    :class:`~commishdesk.errors.OptimalLineupError` unwrapped."""
    generated_at_str = _generated_at(generated_at)
    try:
        return _build(
            week,
            league,
            players,
            player_names,
            generated_at=generated_at_str,
            nfl_byes=nfl_byes,
            nfl_byes_next_week=nfl_byes_next_week,
            fetched_at=fetched_at,
            provisional=provisional,
            previous_storylines=previous_storylines,
        )
    except (ValidationError, KeyError, AttributeError, TypeError, ValueError) as exc:
        raise SchemaValidationError(_violation_message(exc, document="weekly")) from exc


def _build(
    week: WeekModel,
    league: LeagueModel,
    players: Mapping[str, PlayerSnapshot],
    player_names: Mapping[str, str],
    *,
    generated_at: str,
    nfl_byes: frozenset[str] | None,
    nfl_byes_next_week: frozenset[str] | None,
    fetched_at: str | None,
    provisional: bool,
    previous_storylines: Sequence[Storyline],
) -> WeeklyFacts:
    weekly = compute_weekly_stats(week)
    lineups = compute_weekly_lineups(week, league, players, nfl_byes)
    power = compute_power_ranks(week)
    standings = compute_standings(week, league)
    next_week = compute_next_week(week, league, standings, power, players, nfl_byes_next_week)
    desk = compute_transactions_desk(week)
    records = regular_season_records(week, standings.through_week)

    name_of = _name_resolver(players, player_names)

    team_by_roster = {t.roster_id: t for t in league.teams}
    weekly_by_roster = {t.roster_id: t for t in weekly.teams}
    power_by_roster = {t.roster_id: t for t in power.teams}
    standings_by_roster = {t.roster_id: t for t in standings.teams}
    lineups_by_roster = {t.roster_id: t for t in lineups.teams}

    rosters = sorted(week.rosters, key=lambda r: _sort_key(r.roster_id))
    roster_ids = [r.roster_id for r in rosters]
    labels = {
        rid: _team_label(
            team_by_roster[rid].team_name if rid in team_by_roster else None,
            team_by_roster[rid].manager if rid in team_by_roster else None,
            rid,
        )
        for rid in roster_ids
    }

    # The prior week's model rank, for each roster's rank movement.
    prev_ranks: dict[str, int | None] = {}
    if week.week > 1:
        prev_ranks = {t.roster_id: t.model_rank for t in compute_power_ranks(week, through_week=week.week - 1).teams}

    # Seasonal coaching efficiency: the sum of each week's actual / optimal.
    seasonal_actual, seasonal_optimal = _season_coaching(week, league, players, nfl_byes)

    # Per-week cumulative folds, computed once and shared across rosters.
    weeks = list(range(1, week.week + 1))
    records_at = {k: regular_season_records(week, k) for k in weeks}
    power_at: dict[int, dict[str, int | None]] = {}
    allplay_at: dict[int, dict[str, TeamWeekStats]] = {}
    for k in weeks:
        power_at[k] = {t.roster_id: t.model_rank for t in compute_power_ranks(week, through_week=k).teams}
        scoped = week.model_copy(update={"week": k, "playoff_week_start": None})
        allplay_at[k] = {t.roster_id: t for t in compute_weekly_stats(scoped).teams}

    regular_cutoff = _regular_cutoff(week)
    all_rows = {(m.week, m.roster_id): m for m in week.matchups}

    card_by_roster: dict[str, object] = {}
    for card in next_week.cards:
        card_by_roster[card.a.roster_id] = card
        card_by_roster[card.b.roster_id] = card

    starters_by_roster: dict[str, list[WeeklyStarter]] = {}
    teams_block: list[WeeklyTeam] = []
    for rid in roster_ids:
        info = team_by_roster.get(rid)
        standing = standings_by_roster.get(rid)
        powrow = power_by_roster.get(rid)
        wrow = weekly_by_roster.get(rid)
        lrow = lineups_by_roster.get(rid)
        record = records.get(rid)

        season = _season_block(
            rid,
            standing,
            powrow,
            wrow,
            record,
            prev_ranks,
            seasonal_actual.get(rid, 0.0),
            seasonal_optimal.get(rid, 0.0),
        )

        this_week: WeeklyTeamGame | None = None
        target_matchup = all_rows.get((week.week, rid))
        if wrow is not None and lrow is not None and wrow.this_week is not None and target_matchup is not None:
            starters = _starters(target_matchup, league.format.roster_slots, name_of, players)
            starters_by_roster[rid] = starters
            this_week = _this_week(wrow, lrow, starters, name_of, players)
        else:
            starters_by_roster[rid] = []

        history = WeeklyHistory(
            weekly=[
                _history_row(rid, k, all_rows, records_at, power_at, allplay_at, regular_cutoff)
                for k in weeks
                if (k, rid) in all_rows
            ]
        )

        team_next = _team_next_week(rid, card_by_roster.get(rid), name_of, players)

        teams_block.append(
            WeeklyTeam(
                roster_id=rid,
                manager=info.manager if info else None,
                team_name=info.team_name if info else None,
                division_id=info.division_id if info else None,
                co_owners=list(info.co_owners) if info else [],
                season=season,
                this_week=this_week,
                history=history,
                next_week=team_next,
            )
        )

    matchups_block = _matchups_block(week, weekly_by_roster, starters_by_roster, next_week, name_of, players)
    standings_block = _standings_block(standings)
    transactions_block = _transactions_block(desk, name_of, players)
    leaders_block = _leaders(roster_ids, starters_by_roster, lineups_by_roster, week)
    period_block = _period_block(week, weekly, nfl_byes, nfl_byes_next_week)

    # Story 5.9: the weekly lead angles, and the weekly-kind storyline lifecycle
    # advanced from the league's persisted narrative memory.
    lead_candidates = build_weekly_lead_candidates(teams_block, period_block)
    storyline_candidates = project_storyline_candidates(
        advance_storylines(
            previous_storylines,
            kind="weekly",
            week=week.week,
            teams=teams_block,
            period=period_block,
        ),
        kind="weekly",
    )

    narration = _weekly_narration(
        week,
        league,
        labels,
        roster_ids,
        teams_block,
        matchups_block,
        standings_block,
        transactions_block,
        weekly,
        standings,
        period_block.nfl_byes_next_week,
        lead_candidates=lead_candidates,
        storyline_candidates=storyline_candidates,
    )

    doc = WeeklyFacts(
        generated_at=generated_at,
        provisional=provisional,
        week=week.week,
        source=Source(
            platform=league.platform,
            league_id=league.league_id,
            fetched_at=fetched_at,
        ),
        league=_league_block(league, week),
        period=period_block,
        teams=teams_block,
        matchups=matchups_block,
        standings=standings_block,
        transactions=transactions_block,
        leaders=leaders_block,
        lead_candidates=lead_candidates,
        storyline_candidates=storyline_candidates,
        narration=narration,
    )
    WeeklyFacts.model_validate(doc.model_dump())
    return doc


def _season_coaching(
    week: WeekModel,
    league: LeagueModel,
    players: Mapping[str, PlayerSnapshot],
    nfl_byes: frozenset[str] | None,
) -> tuple[dict[str, float], dict[str, float]]:
    """Sum ``compute_weekly_lineups`` over weeks ``1..n`` (scoped copies; the
    one supplied player snapshot applies to every week; byes only for week
    ``n``). Returns the per-roster ``(actual, optimal)`` totals."""
    actual: dict[str, float] = {}
    optimal: dict[str, float] = {}
    for k in range(1, week.week + 1):
        scoped = week.model_copy(update={"week": k})
        result = compute_weekly_lineups(scoped, league, players, nfl_byes if k == week.week else None)
        for row in result.teams:
            actual[row.roster_id] = actual.get(row.roster_id, 0.0) + row.actual
            optimal[row.roster_id] = optimal.get(row.roster_id, 0.0) + row.optimal
    return actual, optimal


def _coaching(actual: float, optimal: float) -> WeeklyCoachingEfficiency:
    pct = round(min(actual / optimal, 1.0), 3) if optimal > 0 else None
    return WeeklyCoachingEfficiency(actual=round(actual, 2), optimal=round(optimal, 2), pct=pct)


def _league_block(league: LeagueModel, week: WeekModel) -> WeeklyLeagueRef:
    fmt = league.format
    bracket: int | None = None
    byes = 0
    if fmt.playoff is not None:
        bracket = fmt.playoff.bracket_teams
        byes = bye_count(bracket)
    start_week = week.playoff_week_start
    playoff = (
        WeeklyPlayoffRef(bracket_teams=bracket, byes=byes, start_week=start_week)
        if bracket is not None and start_week is not None
        else None
    )
    return WeeklyLeagueRef(
        id=league.league_id,
        name=league.name,
        season=str(league.season),
        platform=league.platform,
        format=WeeklyFormatRef(
            team_count=fmt.team_count,
            roster_slots=list(fmt.roster_slots),
            flex_eligibility={k: list(v) for k, v in fmt.flex_eligibility.items()},
            scoring_label=fmt.scoring_label,
            is_superflex_or_2qb=fmt.is_superflex_or_2qb,
            te_premium=fmt.te_premium,
            divisions=[DivisionRef(id=d.id, name=d.name) for d in fmt.divisions],
            playoff=playoff,
            regular_season_weeks=max(0, start_week - 1) if start_week is not None else None,
        ),
    )


def _period_block(
    week: WeekModel,
    weekly: object,
    nfl_byes: frozenset[str] | None,
    nfl_byes_next_week: frozenset[str] | None,
) -> WeeklyPeriod:
    summary = weekly.summary  # type: ignore[attr-defined]
    return WeeklyPeriod(
        week=weekly.period.week,  # type: ignore[attr-defined]
        type=weekly.period.type,  # type: ignore[attr-defined]
        has_prior_week=weekly.period.has_prior_week,  # type: ignore[attr-defined]
        nfl_byes=sorted(nfl_byes) if nfl_byes is not None else None,
        nfl_byes_next_week=sorted(nfl_byes_next_week) if nfl_byes_next_week is not None else None,
        summary=WeekSummaryRef(
            games=summary.games,
            total_points=summary.total_points,
            avg_team_score=summary.avg_team_score,
            high=TeamPointsRef(roster_id=summary.high.roster_id, points=summary.high.points)
            if summary.high is not None
            else None,
            low=TeamPointsRef(roster_id=summary.low.roster_id, points=summary.low.points)
            if summary.low is not None
            else None,
            closest=WeekMarginRef(roster_ids=list(summary.closest.roster_ids), margin=summary.closest.margin)
            if summary.closest is not None
            else None,
            biggest_blowout=WeekMarginRef(
                roster_ids=list(summary.biggest_blowout.roster_ids), margin=summary.biggest_blowout.margin
            )
            if summary.biggest_blowout is not None
            else None,
            blowout_count=summary.blowout_count,
            blowout_threshold=summary.blowout_threshold,
        ),
    )


def _season_block(
    rid: str,
    standing: object,
    powrow: object,
    wrow: object,
    record: object,
    prev_ranks: Mapping[str, int | None],
    seasonal_actual: float,
    seasonal_optimal: float,
) -> WeeklySeason:
    wins = standing.wins if standing is not None else 0  # type: ignore[attr-defined]
    losses = standing.losses if standing is not None else 0  # type: ignore[attr-defined]
    ties = standing.ties if standing is not None else 0  # type: ignore[attr-defined]
    points_for = round(standing.points_for, 2) if standing is not None else 0.0  # type: ignore[attr-defined]
    games = len(record.week_points) if record is not None else 0  # type: ignore[attr-defined]
    avg_for = round(points_for / games, 2) if games else 0.0

    model_rank = powrow.model_rank if powrow is not None else None  # type: ignore[attr-defined]
    prev_rank = prev_ranks.get(rid)
    week_delta = (
        prev_rank - model_rank if prev_rank is not None and model_rank is not None else None
    )

    high_week = _week_points(standing.high_week) if standing is not None else None  # type: ignore[attr-defined]
    low_week = _week_points(standing.low_week) if standing is not None else None  # type: ignore[attr-defined]
    streak = (
        WeeklyStreak(type=standing.streak.type, count=standing.streak.count)  # type: ignore[attr-defined]
        if standing is not None and standing.streak is not None  # type: ignore[attr-defined]
        else None
    )

    all_play = None
    expected_wins = None
    luck = None
    if wrow is not None and wrow.all_play is not None:  # type: ignore[attr-defined]
        ap = wrow.all_play  # type: ignore[attr-defined]
        all_play = WeeklyAllPlay(w=ap.wins, l=ap.losses, t=ap.ties, pct=ap.pct)
    if wrow is not None:
        expected_wins = wrow.expected_wins  # type: ignore[attr-defined]
        luck = wrow.luck  # type: ignore[attr-defined]

    return WeeklySeason(
        record=WeeklyRecord(w=wins, l=losses, t=ties),
        rank=standing.rank if standing is not None else 0,  # type: ignore[attr-defined]
        division_rank=standing.division_rank if standing is not None else None,  # type: ignore[attr-defined]
        points_for=points_for,
        points_against=round(standing.points_against, 2) if standing is not None else 0.0,  # type: ignore[attr-defined]
        avg_for=avg_for,
        high_week=high_week,
        low_week=low_week,
        streak=streak,
        all_play=all_play,
        expected_wins=expected_wins,
        luck=luck,
        power=WeeklyPower(
            model_score=powrow.model_score if powrow is not None else None,  # type: ignore[attr-defined]
            model_rank=model_rank,
            prev_model_rank=prev_rank,
            week_delta=week_delta,
        ),
        coaching_efficiency=_coaching(seasonal_actual, seasonal_optimal),
    )


def _week_points(entry: object) -> WeeklyWeekPoints | None:
    if entry is None:
        return None
    return WeeklyWeekPoints(week=entry.week, points=entry.points)  # type: ignore[attr-defined]


def _starters(
    matchup: Matchup,
    roster_slots: list[str],
    name_of: Callable[[str], str],
    players: Mapping[str, PlayerSnapshot],
) -> list[WeeklyStarter]:
    """The manager's actual week-``n`` lineup — ``Matchup.starters`` /
    ``starters_points`` (Sleeper's own arrays, already ordered to match
    ``roster_slots``), never :class:`TeamLineup`'s *optimal* solve. A starter
    id with no counterpart in either array (a malformed bundle) gets an empty
    slot rather than a mismatched one."""
    out: list[WeeklyStarter] = []
    for index, slot in enumerate(roster_slots):
        pid = matchup.starters[index] if index < len(matchup.starters) else None
        points = matchup.starters_points[index] if index < len(matchup.starters_points) else 0.0
        out.append(
            WeeklyStarter(
                player_id=pid,
                name=name_of(pid) if pid else None,
                pos=_pos(players, pid) if pid else None,
                nfl_team=_nfl_team(players, pid) if pid else None,
                slot=slot,
                points=points,
            )
        )
    return out


def _this_week(
    wrow: TeamWeekStats,
    lrow: TeamLineup,
    starters: list[WeeklyStarter],
    name_of: Callable[[str], str],
    players: Mapping[str, PlayerSnapshot],
) -> WeeklyTeamGame:
    game = wrow.this_week
    result = None
    margin = None
    opponent_points = None
    opponent_roster_id = None
    if game is not None:
        opponent_roster_id = game.opponent_roster_id
        opponent_points = game.opponent_points
        margin = game.margin
        if opponent_points is not None:
            if game.points > opponent_points:
                result = "W"
            elif game.points < opponent_points:
                result = "L"
            else:
                result = "T"

    # Filtered to a non-None player_id above; cast() only tells mypy what the
    # filter already guarantees, no runtime check added (facts/build.py precedent).
    ranked = [s for s in starters if s.player_id is not None]
    top_performers = [
        WeeklyPerformer(
            player_id=cast(str, s.player_id), name=s.name or cast(str, s.player_id), pos=s.pos, points=s.points
        )
        for s in sorted(ranked, key=lambda s: (-s.points, s.player_id or ""))[:TOP_PERFORMERS]
    ]
    duds = [
        WeeklyPerformer(
            player_id=cast(str, s.player_id), name=s.name or cast(str, s.player_id), pos=s.pos, points=s.points
        )
        for s in sorted(
            (s for s in ranked if s.points < DUD_POINTS), key=lambda s: (s.points, s.player_id or "")
        )[:DUD_LIMIT]
    ]

    bench_regret = None
    if lrow.best_benched is not None:
        pid = lrow.best_benched.player_id
        bench_regret = WeeklyBenchRegret(
            player_id=pid, name=name_of(pid), pos=_pos(players, pid), points=lrow.best_benched.points
        )

    started_on_bye = None
    if lrow.started_on_bye is not None:
        started_on_bye = [
            WeeklyByeStarter(
                player_id=entry.player_id,
                name=name_of(entry.player_id),
                pos=_pos(players, entry.player_id),
                nfl_team=entry.nfl_team,
                points=entry.points,
            )
            for entry in lrow.started_on_bye
        ]

    return WeeklyTeamGame(
        opponent_roster_id=opponent_roster_id,
        points=game.points if game is not None else 0.0,
        opponent_points=opponent_points,
        result=result,
        margin=margin,
        coaching_efficiency=_coaching(lrow.actual, lrow.optimal),
        points_left_on_bench=lrow.points_left_on_bench,
        starters=starters,
        top_performers=top_performers,
        duds=duds,
        bench_regret=bench_regret,
        started_on_bye=started_on_bye,
    )


def _history_row(
    rid: str,
    k: int,
    all_rows: Mapping[tuple[int, str], object],
    records_at: Mapping[int, Mapping[str, object]],
    power_at: Mapping[int, Mapping[str, int | None]],
    allplay_at: Mapping[int, Mapping[str, TeamWeekStats]],
    regular_cutoff: int,
) -> WeeklyHistoryRow:
    matchup = all_rows[(k, rid)]
    points = matchup.points  # type: ignore[attr-defined]
    opponent_id = matchup.opponent_roster_id  # type: ignore[attr-defined]
    opponent_points = None
    if opponent_id is not None:
        opponent = all_rows.get((k, opponent_id))
        opponent_points = opponent.points if opponent is not None else None  # type: ignore[attr-defined]

    result = None
    margin = None
    if opponent_points is not None:
        margin = round(points - opponent_points, 2)
        if points > opponent_points:
            result = "W"
        elif points < opponent_points:
            result = "L"
        else:
            result = "T"

    record = records_at[k].get(rid)
    cum = WeeklyRecord(
        w=record.wins if record is not None else 0,  # type: ignore[attr-defined]
        l=record.losses if record is not None else 0,  # type: ignore[attr-defined]
        t=record.ties if record is not None else 0,  # type: ignore[attr-defined]
    )

    power_rank = None
    all_play = None
    luck = None
    in_range = MEANINGFUL_FROM_WEEK <= k <= regular_cutoff
    if in_range:
        power_rank = power_at[k].get(rid)
        wrow = allplay_at[k].get(rid)
        if wrow is not None and wrow.all_play is not None:
            ap = wrow.all_play
            all_play = WeeklyAllPlay(w=ap.wins, l=ap.losses, t=ap.ties, pct=ap.pct)
            win_equiv = (cum.w + 0.5 * cum.t)
            if wrow.expected_wins is not None:
                luck = round(win_equiv - wrow.expected_wins, 1)

    return WeeklyHistoryRow(
        week=k,
        points=points,
        opponent_roster_id=opponent_id,
        result=result,
        margin=margin,
        cum_record=cum,
        power_rank=power_rank,
        all_play=all_play,
        luck=luck,
    )


def _team_next_week(
    rid: str,
    card: object,
    name_of: Callable[[str], str],
    players: Mapping[str, PlayerSnapshot],
) -> WeeklyTeamNextWeek | None:
    if card is None:
        return None
    if card.a.roster_id == rid:  # type: ignore[attr-defined]
        self_side = card.a  # type: ignore[attr-defined]
        opp_side = card.b  # type: ignore[attr-defined]
    else:
        self_side = card.b  # type: ignore[attr-defined]
        opp_side = card.a  # type: ignore[attr-defined]

    starters_on_bye: list[WeeklyByeStarter] = []
    if card.bye_impact is not None:  # type: ignore[attr-defined]
        for entry in card.bye_impact:  # type: ignore[attr-defined]
            if entry.roster_id == rid:
                starters_on_bye.append(
                    WeeklyByeStarter(
                        player_id=entry.player_id,
                        name=name_of(entry.player_id),
                        pos=_pos(players, entry.player_id),
                        nfl_team=entry.nfl_team,
                    )
                )

    return WeeklyTeamNextWeek(
        opponent_roster_id=opp_side.roster_id,
        power_rank_self=self_side.model_rank,
        power_rank_opp=opp_side.model_rank,
        stakes=list(card.stakes),  # type: ignore[attr-defined]
        starters_on_bye=starters_on_bye,
    )


def _matchups_block(
    week: WeekModel,
    weekly_by_roster: Mapping[str, TeamWeekStats],
    starters_by_roster: Mapping[str, list[WeeklyStarter]],
    next_week: object,
    name_of: Callable[[str], str],
    players: Mapping[str, PlayerSnapshot],
) -> WeeklyMatchups:
    groups: dict[int, list[object]] = {}
    for m in week.matchups:
        if m.week != week.week or m.matchup_id is None:
            continue
        groups.setdefault(m.matchup_id, []).append(m)

    this_week: list[WeeklyMatchup] = []
    for mid in sorted(groups):
        rows = groups[mid]
        unique = sorted({m.roster_id for m in rows}, key=_sort_key)  # type: ignore[attr-defined]
        if len(unique) != 2:
            continue
        home, away = unique
        home_row = next(m for m in rows if m.roster_id == home)  # type: ignore[attr-defined]
        away_row = next(m for m in rows if m.roster_id == away)  # type: ignore[attr-defined]
        home_points = home_row.points  # type: ignore[attr-defined]
        away_points = away_row.points  # type: ignore[attr-defined]
        margin = round(abs(home_points - away_points), 2)
        if home_points > away_points:
            winner = home
        elif away_points > home_points:
            winner = away
        else:
            winner = None

        home_week = weekly_by_roster.get(home)
        is_blowout = bool(home_week is not None and home_week.this_week is not None and home_week.this_week.is_blowout)

        headline: list[WeeklyHeadlinePlayer] = []
        for side in (home, away):
            cands = [s for s in starters_by_roster.get(side, []) if s.player_id is not None]
            if not cands:
                continue
            best = min(cands, key=lambda s: (-s.points, s.player_id or ""))
            headline.append(
                WeeklyHeadlinePlayer(
                    roster_id=side,
                    player_id=best.player_id or "",
                    name=best.name or best.player_id or "",
                    pos=best.pos,
                    points=best.points,
                )
            )

        this_week.append(
            WeeklyMatchup(
                matchup_id=mid,
                home_roster_id=home,
                away_roster_id=away,
                home_points=home_points,
                away_points=away_points,
                winner_roster_id=winner,
                margin=margin,
                is_blowout=is_blowout,
                headline_players=headline,
            )
        )

    cards: list[WeeklyNextWeekCard] = []
    for card in next_week.cards:  # type: ignore[attr-defined]
        bye_impact: list[WeeklyByeImpact] | None = None
        if card.bye_impact is not None:
            bye_impact = [
                WeeklyByeImpact(
                    roster_id=entry.roster_id,
                    player_id=entry.player_id,
                    name=name_of(entry.player_id),
                    pos=_pos(players, entry.player_id),
                    nfl_team=entry.nfl_team,
                )
                for entry in card.bye_impact
            ]
        cards.append(
            WeeklyNextWeekCard(
                matchup_id=card.matchup_id,
                a_roster_id=card.a.roster_id,
                b_roster_id=card.b.roster_id,
                a_power_rank=card.a.model_rank,
                b_power_rank=card.b.model_rank,
                a_record=_rec_str(card.a.wins, card.a.losses, card.a.ties),
                b_record=_rec_str(card.b.wins, card.b.losses, card.b.ties),
                a_clinched_playoff=card.a.clinched_playoff,
                a_clinched_bye=card.a.clinched_bye,
                a_eliminated=card.a.eliminated,
                a_clinched_division=card.a.clinched_division,
                b_clinched_playoff=card.b.clinched_playoff,
                b_clinched_bye=card.b.clinched_bye,
                b_eliminated=card.b.eliminated,
                b_clinched_division=card.b.clinched_division,
                stakes=list(card.stakes),
                bye_impact=bye_impact,
                game_of_week=card.game_of_week,
            )
        )

    return WeeklyMatchups(this_week=this_week, next_week=cards)


def _standings_block(standings: object) -> WeeklyStandings:
    picture = standings.playoff_picture  # type: ignore[attr-defined]
    return WeeklyStandings(
        overall=[t.roster_id for t in standings.teams],  # type: ignore[attr-defined]
        divisions={d.division_id: list(d.roster_ids) for d in standings.divisions},  # type: ignore[attr-defined]
        playoff_picture=_playoff_picture(picture),
        through_week=standings.through_week,  # type: ignore[attr-defined]
        regular_season_complete=standings.regular_season_complete,  # type: ignore[attr-defined]
    )


def _playoff_picture(picture: PlayoffPicture | None) -> WeeklyPlayoffPicture | None:
    if picture is None:
        return None
    return WeeklyPlayoffPicture(
        source=picture.source,
        in_bracket=list(picture.in_bracket),
        byes=list(picture.byes),
        first_out=picture.first_out,
        bubble=list(picture.bubble),
        cut_line_after_rank=picture.cut_line_after_rank,
        consolation=list(picture.consolation),
    )


def _transactions_block(
    desk: TransactionsDesk, name_of: Callable[[str], str], players: Mapping[str, PlayerSnapshot]
) -> WeeklyTransactions:
    moves: list[WeeklyMove] = []
    for move in desk.this_week:
        adds = [
            WeeklyMovePlayer(
                player_id=pid, name=name_of(pid), pos=_pos(players, pid), roster_id=to_roster
            )
            for pid, to_roster in sorted(move.adds.items(), key=lambda kv: _sort_key(kv[0]))
        ]
        drops = [
            WeeklyMovePlayer(
                player_id=pid, name=name_of(pid), pos=_pos(players, pid), roster_id=from_roster
            )
            for pid, from_roster in sorted(move.drops.items(), key=lambda kv: _sort_key(kv[0]))
        ]
        moves.append(
            WeeklyMove(
                transaction_id=move.transaction_id,
                type=move.type,
                roster_ids=list(move.roster_ids),
                adds=adds,
                drops=drops,
                faab=move.faab,
            )
        )

    trades: list[WeeklyTrade] = []
    for trade in desk.recent_trades:
        sides = [
            WeeklyTradeSide(
                roster_id=side.roster_id,
                players=[
                    WeeklyMovePlayer(
                        player_id=pid, name=name_of(pid), pos=_pos(players, pid), roster_id=side.roster_id
                    )
                    for pid in side.player_ids
                ],
                picks=[
                    WeeklyPickRef(season=pick.season, round=pick.round, from_roster_id=pick.roster_id)
                    for pick in side.picks
                ],
                faab=side.faab,
            )
            for side in trade.sides
        ]
        trades.append(WeeklyTrade(week=trade.week, transaction_id=trade.transaction_id, sides=sides))

    note = desk.market_note
    return WeeklyTransactions(
        this_week=moves,
        recent_trades=trades,
        market_note=WeeklyMarketNote(
            last_trade_week=note.last_trade_week,
            weeks_since_last_trade=note.weeks_since_last_trade,
            complete=note.complete,
        ),
    )


def _leaders(
    roster_ids: list[str],
    starters_by_roster: Mapping[str, list[WeeklyStarter]],
    lineups_by_roster: Mapping[str, TeamLineup],
    week: WeekModel,
) -> WeeklyLeaders:
    all_starters: list[tuple[str, WeeklyStarter]] = []
    for rid in roster_ids:
        for starter in starters_by_roster.get(rid, []):
            if starter.player_id is not None:
                all_starters.append((rid, starter))

    def rank_key(item: tuple[str, WeeklyStarter]) -> tuple[float, tuple[int, object], str]:
        rid, starter = item
        return (-starter.points, _sort_key(rid), starter.player_id or "")

    def worst_key(item: tuple[str, WeeklyStarter]) -> tuple[float, tuple[int, object], str]:
        rid, starter = item
        return (starter.points, _sort_key(rid), starter.player_id or "")

    def to_leader(item: tuple[str, WeeklyStarter]) -> WeeklyLeaderPlayer:
        rid, starter = item
        return WeeklyLeaderPlayer(
            roster_id=rid,
            player_id=starter.player_id or "",
            name=starter.name or starter.player_id or "",
            pos=starter.pos,
            points=starter.points,
        )

    ordered = sorted(all_starters, key=rank_key)
    week_high = _week_high_player(ordered, week)

    coaching: list[tuple[str, float]] = []
    for rid in roster_ids:
        row = lineups_by_roster.get(rid)
        if row is not None and row.pct is not None:
            coaching.append((rid, row.pct))
    coaching.sort(key=lambda kv: (-kv[1], _sort_key(kv[0])))
    best_coaching = WeeklyLeaderRef(roster_id=coaching[0][0], pct=coaching[0][1]) if coaching else None
    worst_coaching = WeeklyLeaderRef(roster_id=coaching[-1][0], pct=coaching[-1][1]) if coaching else None

    return WeeklyLeaders(
        week_high_player=week_high,
        top_players=[to_leader(item) for item in ordered[:TOP_PLAYERS]],
        worst_starters=[to_leader(item) for item in sorted(all_starters, key=worst_key)[:WORST_STARTERS]],
        best_coaching=best_coaching,
        worst_coaching=worst_coaching,
    )


def _week_high_player(
    ordered: list[tuple[str, WeeklyStarter]], week: WeekModel
) -> WeeklyWeekHighPlayer | None:
    """The week's highest-scoring started player, flagged against every
    player's best week through and including this one (ties count as a new
    season high, matching the golden's ``is_season_high``)."""
    if not ordered:
        return None
    rid, starter = ordered[0]
    season_best = 0.0
    for matchup in week.matchups:
        if matchup.week > week.week:
            continue
        for points in matchup.players_points.values():
            season_best = max(season_best, points)
    return WeeklyWeekHighPlayer(
        roster_id=rid,
        player_id=starter.player_id or "",
        name=starter.name or starter.player_id or "",
        pos=starter.pos,
        points=starter.points,
        is_season_high=starter.points >= season_best,
    )


def _weekly_narration(
    week: WeekModel,
    league: LeagueModel,
    labels: Mapping[str, str],
    roster_ids: list[str],
    teams_block: list[WeeklyTeam],
    matchups_block: WeeklyMatchups,
    standings_block: WeeklyStandings,
    transactions_block: WeeklyTransactions,
    weekly: object,
    standings: object,
    nfl_byes_next_week: list[str] | None,
    *,
    lead_candidates: list[LeadCandidate],
    storyline_candidates: list[StorylineCandidate],
) -> WeeklyNarration:
    by_roster = {t.roster_id: t for t in teams_block}

    league_block = WeeklyNarrationLeague(
        name=league.name,
        season=str(league.season),
        week=week.week,
        team_count=league.format.team_count,
        divisions=[d.name for d in league.format.divisions if d.name],
    )

    games: list[WeeklyNarrationGame] = []
    for game in matchups_block.this_week:
        winner = game.winner_roster_id
        loser = game.away_roster_id if winner == game.home_roster_id else game.home_roster_id
        winner_pts = game.home_points if winner == game.home_roster_id else game.away_points
        loser_pts = game.away_points if winner == game.home_roster_id else game.home_points
        top = None
        best = None
        for player in game.headline_players:
            if best is None or player.points > best.points:
                best = player
        if best is not None:
            top = best.name
        games.append(
            WeeklyNarrationGame(
                matchup_id=game.matchup_id,
                winner=labels.get(winner) if winner else None,
                winner_pts=winner_pts,
                loser=labels.get(loser) if loser else None,
                loser_pts=loser_pts,
                margin=game.margin,
                top=top,
            )
        )

    standings_narration: list[WeeklyNarrationStanding] = []
    for _rank, rid in enumerate(standings_block.overall, start=1):
        team = by_roster.get(rid)
        if team is None:
            continue
        standings_narration.append(
            WeeklyNarrationStanding(
                rank=team.season.rank,
                team=labels.get(rid, rid),
                roster_id=rid,
                rec=_rec_str(team.season.record.w, team.season.record.l, team.season.record.t),
                pf=team.season.points_for,
                model_rank=team.season.power.model_rank,
            )
        )

    picture = standings_block.playoff_picture
    playoff_narration = None
    if picture is not None:
        playoff_narration = WeeklyNarrationPlayoff(
            format=f"{picture.cut_line_after_rank} teams",
            in_bracket=[labels.get(rid, rid) for rid in picture.in_bracket],
            byes=[labels.get(rid, rid) for rid in picture.byes],
            first_out=labels.get(picture.first_out, picture.first_out) if picture.first_out else None,
            bubble=[labels.get(rid, rid) for rid in picture.bubble],
        )

    power_narration: list[WeeklyNarrationPower] = []
    luck_narration: list[WeeklyNarrationLuck] = []
    for team in sorted(teams_block, key=lambda t: _sort_key(t.roster_id)):
        rec = _rec_str(team.season.record.w, team.season.record.l, team.season.record.t)
        all_play = None
        if team.season.all_play is not None:
            ap = team.season.all_play
            all_play = _rec_str(ap.w, ap.l, ap.t)
        power_narration.append(
            WeeklyNarrationPower(
                model_rank=team.season.power.model_rank,
                team=labels.get(team.roster_id, team.roster_id),
                roster_id=team.roster_id,
                rec=rec,
                avg_pf=team.season.avg_for,
                all_play=all_play,
                luck=team.season.luck,
                week_delta=team.season.power.week_delta,
            )
        )
        luck_narration.append(
            WeeklyNarrationLuck(
                team=labels.get(team.roster_id, team.roster_id),
                roster_id=team.roster_id,
                rec=rec,
                all_play=all_play,
                earned_wins=team.season.expected_wins,
                luck=team.season.luck,
            )
        )

    next_week_narration: list[WeeklyNarrationNextWeek] = []
    for card in matchups_block.next_week:
        next_week_narration.append(
            WeeklyNarrationNextWeek(
                matchup_id=card.matchup_id,
                a=labels.get(card.a_roster_id, card.a_roster_id),
                a_roster_id=card.a_roster_id,
                a_rec=card.a_record,
                a_model_rank=card.a_power_rank,
                b=labels.get(card.b_roster_id, card.b_roster_id),
                b_roster_id=card.b_roster_id,
                b_rec=card.b_record,
                b_model_rank=card.b_power_rank,
                stakes=list(card.stakes),
                game_of_week=card.game_of_week,
            )
        )

    note = transactions_block.market_note
    narration = WeeklyNarration(
        league=league_block,
        week_shape=_week_shape(week, weekly.period.type),  # type: ignore[attr-defined]
        games=games,
        standings=standings_narration,
        playoff_picture=playoff_narration,
        power=power_narration,
        luck=luck_narration,
        next_week=next_week_narration,
        next_week_nfl_byes=nfl_byes_next_week,
        transactions=WeeklyNarrationTransactions(
            last_trade_week=note.last_trade_week,
            weeks_since_last_trade=note.weeks_since_last_trade,
            complete=note.complete,
            this_week_count=len(transactions_block.this_week),
            recent_trade_count=len(transactions_block.recent_trades),
        ),
        lead_candidates=lead_candidates,
        storyline_candidates=storyline_candidates,
    )
    return _apply_weekly_narration_cap(narration)


def _apply_weekly_narration_cap(narration: WeeklyNarration) -> WeeklyNarration:
    """Hold the weekly narration under :data:`NARRATION_TOKEN_CAP` via the fixed
    reduction ladder. Each tier is applied only while the projection is still
    over the cap:

    1. drop ``luck[]`` and every power row's ``all_play`` and ``luck``;
    2. keep only the game-of-the-week card in ``next_week[]`` (``transactions``
       already carries only a ``recent_trade_count`` int, never a list, so
       there is nothing further to drop there);
    3. drop ``power[]``.

    ``lead_candidates``, ``standings[]`` and ``games[]`` are never touched. If
    the projection is still over the cap after the final tier, this raises
    :class:`~commishdesk.errors.SchemaValidationError` (retro item 33)."""
    if _within_cap(narration):
        return narration

    narration = narration.model_copy(
        update={
            "luck": [],
            "power": [
                row.model_copy(update={"all_play": None, "luck": None}) for row in narration.power
            ],
        }
    )
    if _within_cap(narration):
        return narration

    marquee = [card for card in narration.next_week if card.game_of_week][:1]
    narration = narration.model_copy(update={"next_week": marquee})
    if _within_cap(narration):
        return narration

    narration = narration.model_copy(update={"power": []})
    if _within_cap(narration):
        return narration

    raise SchemaValidationError(
        f"weekly narration cannot be reduced under NARRATION_TOKEN_CAP "
        f"({len(narration.model_dump_json())} > {_current_cap()})"
    )
