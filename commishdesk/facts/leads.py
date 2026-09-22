"""Stage 3 — board-only lead angles (Story 2.6, extended Story 5.9).

:func:`build_lead_candidates` ranks the story angles a draft-recap lead could open
on, computed **in code** from the stage results the builder already holds — no
LLM, no prompt (delta D7: keep editorial judgement in code, not the prompt). Each
angle is a :class:`~commishdesk.facts.schema.LeadCandidate`
(``{rank, kind, roster_ids, hook}``); the ``hook`` is a deterministic sentence
built from the numbers, factual and terminal, which the narrator may later
rewrite in voice.

Story 5.9 adds :func:`build_weekly_lead_candidates` — the weekly counterpart,
reading the weekly document's own ``teams`` / ``period`` blocks and firing in
:data:`WEEKLY_LEAD_KIND_PRIORITY` order (a team losing a game it should have won
leads; the week's high score is only the floor).

Pure / deterministic / offline: a valid stage-result set in, a ranked list out.
Two calls on one input return equal ``model_dump()`` output. Every board number
is read off ``board`` / ``draft_summary`` / ``superlatives`` — nothing is
re-derived from ``league.picks`` except the single first-overall pick that
``draft_summary`` does not carry.

Ranking is the fixed kind priority in :data:`LEAD_KIND_PRIORITY`: a cross-cutting
board observation (``positional_run``) and a manager-approach angle both outrank
the single-name ``biggest_score`` floor. Only detectors that fire are emitted,
then ``rank`` is renumbered ``1..N``.

This module imports only stdlib + ``commishdesk.ingest`` + ``commishdesk.stats``
+ ``.schema`` — it stays inside the ``commishdesk/facts/*.py`` import fence
(``tests/test_facts.py::test_facts_package_import_fence``).
"""

from __future__ import annotations

from commishdesk.ingest import LeagueModel
from commishdesk.stats import BoardMetrics, ConsensusMetrics, DraftGrades

from .schema import (
    DraftSummary,
    LeadCandidate,
    Superlatives,
    WeeklyPeriod,
    WeeklyTeam,
    WeekMarginRef,
)

__all__ = [
    "LEAD_KIND_PRIORITY",
    "WEEKLY_LEAD_KIND_PRIORITY",
    "build_lead_candidates",
    "build_weekly_lead_candidates",
]

LEAD_KIND_PRIORITY: tuple[str, ...] = (
    "positional_run",
    "manager_approach",
    "positional_hoard",
    "biggest_score",
)
"""Editorial priority order for lead angles (delta D7). A cross-cutting board
read leads; the marquee name is the floor, never the default. Every ``kind`` a
detector below emits must appear here."""

_MIN_R1_RB_RUN = 4
"""Running backs among the first ``team_count - 1`` picks, at or above this many,
is the cross-cutting board read. Gated on the same list the hook counts."""

_MIN_POSITION_HOARD = 4
"""One roster taking this many of a single real position is a lead angle."""

_UNK = "UNK"

_POSITION_PLURAL = {
    "QB": "quarterbacks",
    "RB": "running backs",
    "WR": "wide receivers",
    "TE": "tight ends",
    "K": "kickers",
    "DEF": "defenses",
    "DST": "defenses",
    "DL": "defensive linemen",
    "LB": "linebackers",
    "DB": "defensive backs",
    "IDP": "defensive players",
}

_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
)


def _spell(n: int) -> str:
    """Small non-negative integers as words (``5`` -> ``"five"``); anything out of
    range falls back to the digits. Keeps the hooks reading like prose without a
    dependency."""
    return _ONES[n] if 0 <= n < len(_ONES) else str(n)


def _roster_key(roster_id: str) -> tuple[int, object]:
    """Numeric-aware ordering for a roster id string (mirrors
    ``facts/weekly.py``'s ``_sort_key``) so a 10+-roster league orders ``2``
    before ``10``. Ids that do not parse as an int sort lexically after the
    numeric ones."""
    try:
        return (0, int(roster_id))
    except (TypeError, ValueError):
        return (1, roster_id)


def _team_label(team: WeeklyTeam) -> str:
    """The label a narrator reads for one weekly roster: the team name, else the
    manager, else a roster-id fallback."""
    return team.team_name or team.manager or f"Roster {team.roster_id}"


def _points(value: float) -> str:
    """Two-decimal point total — the weekly hooks' numeric formatter."""
    return f"{value:.2f}"


def build_lead_candidates(
    league: LeagueModel,
    board: BoardMetrics,
    consensus: ConsensusMetrics,
    grades: DraftGrades,
    draft_summary: DraftSummary,
    superlatives: Superlatives,
) -> list[LeadCandidate]:
    """Ranked board-derived lead angles for the draft recap.

    ``consensus`` / ``grades`` / ``superlatives`` are accepted for symmetry with
    :func:`~commishdesk.facts.build.build_draft_recap_facts` and are reserved for
    the Story 3.1 storyline angles; the board angles here are derived from
    ``board`` / ``draft_summary`` / ``league``. Returns ``[]`` only for a draft
    with no picks; otherwise the ``biggest_score`` floor guarantees one entry.
    """
    approach = _manager_approach(board, draft_summary)
    angles = [
        _positional_run(draft_summary),
        approach,
        _positional_hoard(
            board, exclude=approach.roster_ids[0] if approach else None
        ),
        _biggest_score(league),
    ]
    fired = [a for a in angles if a is not None]
    fired.sort(key=lambda a: LEAD_KIND_PRIORITY.index(a.kind))
    return [a.model_copy(update={"rank": i}) for i, a in enumerate(fired, start=1)]


# --------------------------------------------------------------------------- #
# Detectors — each returns a LeadCandidate with a placeholder rank, or None
# --------------------------------------------------------------------------- #


def _candidate(kind: str, roster_ids: list[str], hook: str) -> LeadCandidate:
    return LeadCandidate(rank=0, kind=kind, roster_ids=roster_ids, hook=hook)


def _positional_run(draft_summary: DraftSummary) -> LeadCandidate | None:
    """A running-back cluster in the opening picks — the cross-cutting board
    read. The hook also names the round-1 quarterbacks, the counter-signal a
    human editor reaches for (the board and the format pulling opposite ways).

    The window size is read off ``draft_summary.first_window`` (``team_count -
    1``, retro finding A2) rather than re-derived from ``league.format`` here —
    one source of truth instead of two."""
    rb_count = len(draft_summary.first_window_running_backs)
    if rb_count < _MIN_R1_RB_RUN:
        return None
    window = draft_summary.first_window
    qb_count = len(draft_summary.round1_qbs)
    hook = (
        f"{_spell(rb_count).capitalize()} of the first {_spell(window)} picks "
        f"were running backs"
    )
    if qb_count:
        noun = "manager" if qb_count == 1 else "managers"
        hook += f", and {_spell(qb_count)} {noun} took a quarterback in round 1."
    else:
        hook += "."
    return _candidate("positional_run", [], hook)


def _manager_approach(
    board: BoardMetrics, draft_summary: DraftSummary
) -> LeadCandidate | None:
    """The one manager who drafted differently — the unique pick-count leader who
    also poured an outsized share of the draft into a single round. Both signals
    are read straight off ``draft_summary`` (``round_concentration``) and
    ``board`` (``pick_count``); nothing is re-derived from ``league.picks``."""
    if not board.teams:
        return None
    top = max(t.pick_count for t in board.teams)
    leaders = [t for t in board.teams if t.pick_count == top]
    if len(leaders) != 1 or leaders[0].manager is None:
        return None
    leader = leaders[0]

    concentration = next(
        (rc for rc in draft_summary.round_concentration if rc.manager == leader.manager),
        None,
    )
    if concentration is None:
        return None

    hook = (
        f"{leader.manager} made {_spell(leader.pick_count)} picks, "
        f"{_spell(concentration.count)} of them in round {_spell(concentration.round)}."
    )
    return _candidate("manager_approach", [leader.roster_id], hook)


def _positional_hoard(
    board: BoardMetrics, *, exclude: str | None
) -> LeadCandidate | None:
    """The roster that stacked one real position higher than anyone else. The
    ``manager_approach`` subject and orphan rosters are skipped during the scan,
    so the angle lands on a real, distinct manager whenever one clears the bar."""
    best_count = 0
    best_position: str | None = None
    best_roster: str | None = None
    best_manager: str | None = None
    for team in board.teams:  # league.teams order breaks ties between rosters
        if team.roster_id == exclude or team.manager is None:
            continue
        for position in sorted(team.positional_counts):  # alpha breaks intra-team ties
            if position == _UNK:
                continue
            count = team.positional_counts[position]
            if count > best_count:
                best_count = count
                best_position = position
                best_roster = team.roster_id
                best_manager = team.manager
    if best_count < _MIN_POSITION_HOARD or best_roster is None or best_manager is None:
        return None
    noun = _POSITION_PLURAL.get(best_position or "", f"{best_position}s")
    hook = f"{best_manager} drafted {_spell(best_count)} {noun}."
    return _candidate("positional_hoard", [best_roster], hook)


def _biggest_score(league: LeagueModel) -> LeadCandidate | None:
    """The marquee name — the D7 floor, so the narrator always has a lead. The
    first overall pick; ``None`` only when the board is empty (matching the
    empty-draft contract: no picks, no angles)."""
    if not league.picks:
        return None
    pick = min(league.picks, key=lambda p: p.pick_no)
    return _candidate(
        "biggest_score",
        [pick.roster_id],
        f"{pick.player.name} went {pick.board_label}.",
    )


# --------------------------------------------------------------------------- #
# Story 5.9 — weekly lead angles
# --------------------------------------------------------------------------- #

WEEKLY_LEAD_KIND_PRIORITY: tuple[str, ...] = (
    "lineup_loss",
    "biggest_blowout",
    "closest_game",
    "week_high_score",
)
"""Editorial priority order for the weekly lead angles (FR-14). The team that
lost a game it should have won leads; the week's shape (its biggest blowout, its
closest game) follows; the week's high score is the floor, never the default.
Every ``kind`` a detector below emits must appear here."""


def build_weekly_lead_candidates(
    teams: list[WeeklyTeam], period: WeeklyPeriod
) -> list[LeadCandidate]:
    """Ranked lead angles for the weekly recap (Story 5.9).

    Mirrors :func:`build_lead_candidates`'s shape: only detectors that fire are
    emitted, in :data:`WEEKLY_LEAD_KIND_PRIORITY` order, then ``rank`` is
    renumbered ``1..N``. Returns ``[]`` only when no roster played a game this
    week — the ``week_high_score`` floor otherwise guarantees at least one angle,
    so a ``lineup_loss`` read is never buried behind a single high score (FR-14).
    """
    angles = [
        _lineup_loss(teams),
        _biggest_blowout(teams, period),
        _closest_game(teams, period),
        _week_high_score(teams),
    ]
    fired = [a for a in angles if a is not None]
    fired.sort(key=lambda a: WEEKLY_LEAD_KIND_PRIORITY.index(a.kind))
    return [a.model_copy(update={"rank": i}) for i, a in enumerate(fired, start=1)]


def _lineup_loss(teams: list[WeeklyTeam]) -> LeadCandidate | None:
    """The roster that lost a game it ought to have won — more points left on the
    bench than it lost by. Among qualifiers the largest such gap leads; ties break
    to the lower roster id."""
    best: WeeklyTeam | None = None
    best_gap = 0.0
    for team in sorted(teams, key=lambda t: _roster_key(t.roster_id)):
        game = team.this_week
        if game is None or game.result != "L":
            continue
        if game.margin is None or game.points_left_on_bench is None:
            continue
        margin = abs(game.margin)
        if game.points_left_on_bench <= margin:
            continue
        gap = game.points_left_on_bench - margin
        if best is None or gap > best_gap:
            best, best_gap = team, gap
    if best is None:
        return None

    game = best.this_week
    assert game is not None and game.margin is not None and game.points_left_on_bench is not None
    label = _team_label(best)
    regret = game.bench_regret
    if regret is not None:
        hook = (
            f"{label} lost by {_points(abs(game.margin))} with "
            f"{_points(game.points_left_on_bench)} on the bench, including {regret.name}."
        )
    else:
        hook = (
            f"{label} lost by {_points(abs(game.margin))} with "
            f"{_points(game.points_left_on_bench)} on the bench."
        )
    return _candidate("lineup_loss", [best.roster_id], hook)


def _biggest_blowout(teams: list[WeeklyTeam], period: WeeklyPeriod) -> LeadCandidate | None:
    """The week's widest margin, off ``period.summary.biggest_blowout``."""
    return _game_angle(
        teams,
        period.summary.biggest_blowout,
        kind="biggest_blowout",
        verb="beat",
        tail="the week's biggest blowout",
    )


def _closest_game(teams: list[WeeklyTeam], period: WeeklyPeriod) -> LeadCandidate | None:
    """The week's narrowest margin, off ``period.summary.closest``. Skipped when
    it names the same pairing as :func:`_biggest_blowout` (only reachable when
    the week has exactly one game, so the two summary fields degenerate to the
    same matchup) -- the two angles would otherwise describe one score as both
    the week's biggest blowout and its closest game."""
    blowout = period.summary.biggest_blowout
    closest = period.summary.closest
    if blowout is not None and closest is not None and set(closest.roster_ids) == set(blowout.roster_ids):
        return None
    return _game_angle(
        teams,
        closest,
        kind="closest_game",
        verb="edged",
        tail="the week's closest game",
    )


def _game_angle(
    teams: list[WeeklyTeam],
    margin_ref: WeekMarginRef | None,
    *,
    kind: str,
    verb: str,
    tail: str,
) -> LeadCandidate | None:
    """A game-shaped angle (``biggest_blowout`` / ``closest_game``) built from a
    :class:`~commishdesk.facts.schema.WeekMarginRef`. ``None`` when the reference
    is absent (``None`` skips, per the spec), does not name exactly two distinct
    rosters, or either participant is missing from *teams*. ``roster_ids`` is
    winner-first, matching ``_lineup_loss``/``_week_high_score``'s
    subject-first convention. A tie (``margin == 0``, reachable per
    ``stats/weekly.py``'s own tie handling) gets its own wording rather than
    being misdescribed as one side edging the other."""
    if margin_ref is None or len(set(margin_ref.roster_ids)) != 2:
        return None
    by_roster = {team.roster_id: team for team in teams}
    ids = sorted(margin_ref.roster_ids, key=_roster_key)
    first = by_roster.get(ids[0])
    second = by_roster.get(ids[1])
    if first is None or second is None:
        return None
    first_points = first.this_week.points if first.this_week is not None else None
    second_points = second.this_week.points if second.this_week is not None else None
    if first_points is None or second_points is None:
        return None
    if first_points == second_points:
        hook = (
            f"{_team_label(first)} and {_team_label(second)} tied at {_points(first_points)}, "
            f"{tail}."
        )
        return _candidate(kind, [first.roster_id, second.roster_id], hook)
    winner, loser = (first, second) if first_points > second_points else (second, first)
    hook = f"{_team_label(winner)} {verb} {_team_label(loser)} by {_points(margin_ref.margin)}, {tail}."
    return _candidate(kind, [winner.roster_id, loser.roster_id], hook)


def _week_high_score(teams: list[WeeklyTeam]) -> LeadCandidate | None:
    """The floor: the week's highest-scoring roster among those that played a
    game. ``None`` only when no roster has a game (matching the empty-week
    contract: no games, no angles). Ties break to the lower roster id."""
    best: WeeklyTeam | None = None
    best_points = 0.0
    for team in sorted(teams, key=lambda t: _roster_key(t.roster_id)):
        game = team.this_week
        if game is None:
            continue
        if best is None or game.points > best_points:
            best, best_points = team, game.points
    if best is None:
        return None
    game = best.this_week
    assert game is not None
    hook = f"{_team_label(best)} posted the week's high score, {_points(game.points)}."
    return _candidate("week_high_score", [best.roster_id], hook)
