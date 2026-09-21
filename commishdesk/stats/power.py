"""The model power rank and its per-week history (Story 5.6).

:func:`compute_power_ranks` scores every roster for the model's own power rank
(FR-11, AD-13) as one weighted sum of three league-relative components, and
:func:`compute_power_history` replays that for every week up to the
regular-season cutoff so a recap can say how the rank has moved.

**Score.** ``model_score = sum(POWER_WEIGHTS[k] * component[k])``, every
component rescaled to ``0..1`` across the league, so the score is comparable
week to week and league to league:

* ``all_play_pct`` -- decisive all-play win rate,
  ``wins / (wins + losses)``, from the cumulative round robin
  :func:`~commishdesk.stats.weekly.compute_weekly_stats` already folds (ties are
  neither a win nor a loss here). This is the component that makes the rank a
  *model* rank: it measures how good the roster was relative to the whole league
  in each week, not just its head-to-head record.
* ``normalized_avg_pf`` -- min-max normalized average points-for across the
  ranked rosters (``0.0`` when every roster averages the same, so a league with
  no spread does not divide by zero).
* ``win_pct`` -- head-to-head win percentage, ``(W + 0.5*T) / games``.

The head-to-head half comes from ``stats/standings.py``'s shared
:func:`~commishdesk.stats.standings.regular_season_records` fold -- the numbers
are never read from ``Roster.wins`` / ``losses`` / ``ties`` / ``fpts``.

**Ranking.** ``model_score`` is *reported* at 3 decimals but the ranks are
assigned on the **unrounded** score; this alone reproduces the phase-0 golden's
week 9, where two rosters both display ``0.193`` and the golden still orders
them. An exact tie then breaks by points-for descending, then ``roster_id``
(numeric where parseable).

**Any prior week.** ``through_week`` (clamped to ``1..cutoff``) folds only
matchups at or below that week, so ``compute_power_ranks(week, through_week=n)``
equals the result for a bundle truncated at week ``n`` -- no later-week leakage.
:func:`compute_power_history` returns exactly that, one :class:`PowerRanks` per
week ``1..cutoff``. Before ``MEANINGFUL_FROM_WEEK`` (week 1) every
``model_score`` / ``model_rank`` is ``None``: FR-10's no-cold-start rule. A
roster with no game through the week is unranked for the same reason -- and
never a spurious ``0.0``.

**What this module does not do.** ``published_rank`` and the narrate-side nudge
are AD-13's narrator concern, not this one: :data:`POWER_NUDGE_CAP` is declared
here only so ``narrate/`` has one named place to read it from.

This module stays inside the ``stats/`` fence (AD-1): it imports only stdlib,
pydantic, ``commishdesk.ingest``, and its sibling ``stats/`` modules -- never
``adapters`` / ``store`` / ``facts`` / ``narrate`` / ``render`` / a later
pipeline stage. It reads no file, clock, PRNG, or network, and never raises.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from commishdesk.ingest import WeekModel

from .standings import regular_season_records
from .weekly import MEANINGFUL_FROM_WEEK, _all_play_cutoff, _sort_key, compute_weekly_stats

__all__ = [
    "POWER_NUDGE_CAP",
    "POWER_WEIGHTS",
    "PowerRanks",
    "TeamPower",
    "compute_power_history",
    "compute_power_ranks",
]

#: The model's power score is a weighted sum of these three league-relative
#: components. PRD v0 placeholders -- deliberately tunable, deliberately all in
#: one place, deliberately summing to 1.0 (a test pins that). Every weight is
#: read by name, so re-weighting is a one-line change here and nothing else.
POWER_WEIGHTS: dict[str, float] = {
    "all_play_pct": 0.45,
    "normalized_avg_pf": 0.35,
    "win_pct": 0.20,
}

#: How many rank positions the narrator's "the model disagrees with the
#: standings" nudge may span (AD-13). Declared here, next to the score it
#: qualifies; consumed by ``narrate/``, never applied to the rank itself.
POWER_NUDGE_CAP = 2


class _Frozen(BaseModel):
    """Immutable, closed to unknown keys -- matches ``stats/weekly.py``."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class TeamPower(_Frozen):
    """One roster's model power row for one week.

    ``model_score`` is the weighted sum at 3 decimals -- what a reader sees.
    ``model_rank`` is assigned on the *unrounded* score, so two rosters whose
    scores both display the same 3-decimal value can still be ordered correctly
    (this alone reproduces the golden's week 9). Both are ``None`` for a roster
    with no game through the week (FR-10's no-cold-start rule).

    The three components are carried rounded to 3 decimals so a reader can see
    *why* a roster ranks where it does."""

    roster_id: str
    model_score: float | None
    model_rank: int | None
    all_play_pct: float | None
    normalized_avg_pf: float | None
    win_pct: float | None


class PowerRanks(_Frozen):
    """The whole result for one week: one :class:`TeamPower` per roster in
    ``week.rosters``, ordered by ``roster_id`` (numeric where parseable --
    matches this package's convention).

    ``week`` is the week the ranking is *for* -- the last week folded, not
    necessarily the input model's own ``week`` -- so a week-10 model asked for
    ``through_week=5`` reports week 5, identically to the model built from a
    bundle truncated at week 5."""

    week: int
    teams: list[TeamPower]


def _unranked(roster_id: str) -> TeamPower:
    """The row for a roster the model cannot rank -- no game through the week,
    or a week before ``MEANINGFUL_FROM_WEEK``."""
    return TeamPower(
        roster_id=roster_id,
        model_score=None,
        model_rank=None,
        all_play_pct=None,
        normalized_avg_pf=None,
        win_pct=None,
    )


def compute_power_ranks(week: WeekModel, *, through_week: int | None = None) -> PowerRanks:
    """Score and rank every roster through ``through_week``.

    ``through_week`` is clamped to ``1..cutoff`` (``cutoff`` is
    ``stats/weekly.py``'s own ``_all_play_cutoff``); ``None`` means the whole
    regular season folded so far. Pure, deterministic, offline: two calls on
    equal inputs return an equal ``model_dump()``. Never raises."""
    cutoff = max(_all_play_cutoff(week), 0)
    if through_week is None or cutoff < MEANINGFUL_FROM_WEEK:
        effective = cutoff
    else:
        effective = min(max(through_week, 1), cutoff)

    roster_ids = sorted((roster.roster_id for roster in week.rosters), key=_sort_key)

    if effective < MEANINGFUL_FROM_WEEK:
        return PowerRanks(week=effective, teams=[_unranked(roster_id) for roster_id in roster_ids])

    # Reuse the weekly module's own all-play fold rather than reimplementing it:
    # a copy scoped to ``effective`` with no playoff_week_start makes
    # ``_all_play_cutoff`` return exactly ``effective``.
    scoped = week.model_copy(update={"week": effective, "playoff_week_start": None})
    weekly = compute_weekly_stats(scoped)
    records = regular_season_records(week, effective)

    components: dict[str, tuple[float, float, float]] = {}
    points_for: dict[str, float] = {}
    for team_stats in weekly.teams:
        roster_id = team_stats.roster_id
        record = records.get(roster_id)
        if record is None or not record.week_points:
            continue
        all_play = team_stats.all_play
        decisive = (all_play.wins + all_play.losses) if all_play is not None else 0
        all_play_pct = (all_play.wins / decisive) if all_play is not None and decisive else 0.0
        games = record.wins + record.losses + record.ties
        win_pct = ((record.wins + 0.5 * record.ties) / games) if games else 0.0
        components[roster_id] = (all_play_pct, win_pct, record.points_for / effective)
        points_for[roster_id] = record.points_for

    averages = [avg_pf for _, _, avg_pf in components.values()]
    low = min(averages) if averages else 0.0
    high = max(averages) if averages else 0.0

    scored: list[tuple[str, float]] = []
    for roster_id, (all_play_pct, win_pct, avg_pf) in components.items():
        normalized = (avg_pf - low) / (high - low) if high > low else 0.0
        score = (
            POWER_WEIGHTS["all_play_pct"] * all_play_pct
            + POWER_WEIGHTS["normalized_avg_pf"] * normalized
            + POWER_WEIGHTS["win_pct"] * win_pct
        )
        scored.append((roster_id, score))

    # Rank on the unrounded score; break an exact tie by points-for, then id.
    scored.sort(key=lambda item: (-item[1], -points_for[item[0]], _sort_key(item[0])))
    rank_of = {roster_id: index + 1 for index, (roster_id, _) in enumerate(scored)}
    score_of = dict(scored)

    teams: list[TeamPower] = []
    for roster_id in roster_ids:
        if roster_id not in rank_of:
            teams.append(_unranked(roster_id))
            continue
        all_play_pct, win_pct, avg_pf = components[roster_id]
        normalized = (avg_pf - low) / (high - low) if high > low else 0.0
        teams.append(
            TeamPower(
                roster_id=roster_id,
                model_score=round(score_of[roster_id], 3),
                model_rank=rank_of[roster_id],
                all_play_pct=round(all_play_pct, 3),
                normalized_avg_pf=round(normalized, 3),
                win_pct=round(win_pct, 3),
            )
        )

    return PowerRanks(week=effective, teams=teams)


def compute_power_history(week: WeekModel) -> list[PowerRanks]:
    """One :class:`PowerRanks` per week ``1..cutoff``, oldest first -- the model
    rank's whole regular-season arc, so a recap can say how far a roster has
    moved. Entry ``n`` (1-indexed) equals ``compute_power_ranks(week,
    through_week=n)`` exactly. Pure, deterministic, offline; never raises."""
    cutoff = max(_all_play_cutoff(week), 0)
    return [compute_power_ranks(week, through_week=n) for n in range(1, cutoff + 1)]
