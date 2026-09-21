"""Optimal-lineup solver and coaching efficiency (Story 5.5).

One pure function, :func:`compute_weekly_lineups`, answers the question the
weekly lead hangs on (FR-7): *how many points did this manager leave on the
bench?* For every roster that played the target week it finds the true
highest-scoring legal assignment of that week's players to
``league.format.roster_slots`` and compares it against the lineup the manager
actually started.

**Why an assignment solver, not a greedy fill.** Slots x players with
overlapping eligibility (``FLEX`` superset of RB/WR/TE, ``SUPER_FLEX`` adds QB,
``WRRB_FLEX``/``REC_FLEX`` overlap each other) is a max-weight bipartite
matching. Filling slots in ``roster_slots`` order and taking the best remaining
eligible player is provably suboptimal the moment a flexible slot precedes a
strict one -- the flex slot takes the only player the strict slot could use.
So slots are matching rows, candidates plus one dummy column per slot are
matching columns, and "fill every slot while an eligible player remains" falls
out of the cost matrix: a dummy edge is priced above every real edge (even a
negative score), so a slot is only ever left empty when nothing eligible is
left. The solve is a module-private Kuhn-Munkres (Jonker-Volgenant short form),
integers only -- pure Python, no new dependency, no network, no clock.

**Candidate pool (DECIDED, amended).** ``Roster.ir`` players are excluded from
the placeable pool *except the ones who actually started that week*: an IR slot
is not startable, but the fixtures' IR list is season-end state, not week-N
state, so a strict exclusion can push coaching efficiency above 100% on the
committed week-10 fixture (roster 4). ``Roster.taxi`` players are **not**
excluded -- in this league a taxi player is startable, so he is available. The
one remaining divergence from the phase-0 golden's ``optimal`` (roster 4, a
non-starting player who was on IR by season end) is recorded per the golden-file
rule in ``docs/EDGE-CASES.md``, never bent to match. Availability itself is
"whatever Sleeper scored" (PRD section 17 Q6): an injury tag never removes a
player who was scored.

A player with no known position (absent from *players*, or carrying a ``None``
position) cannot be placed anywhere. To make sure that data gap can never read
as a manager who beat the optimum, ``optimal`` is floored at ``actual`` -- so
``pct`` is at most ``1.0`` by construction.

Result shape. ``actual`` is the sum of ``Matchup.starters_points``. ``optimal``
is the solved lineup's total, floored at ``actual`` (2 decimals).
``pct = actual / optimal`` (3 decimals, ``None`` when ``optimal <= 0``).
``points_left_on_bench = optimal - actual`` (2 decimals). ``bench_regret`` is
the highest-scoring placeable player the manager actually benched -- his
``Matchup`` roster minus his ``starters`` -- with ``best_benched`` naming him;
``None`` when every placeable player started or every one benched scored below
zero (that is not a regret). ``lineup`` walks
``roster_slots`` in order, one :class:`LineupSlot` each, an empty slot scoring
``0.0``. ``started_on_bye`` lists the actual starters whose NFL team is in the
caller-supplied ``nfl_byes`` and who scored nothing (a team on bye cannot score, so a starter
with points is never flagged even when a backfilled snapshot names a bye team)
-- ``None`` (not ``[]``) when the caller did not supply bye data, since "no
byes" and "unknown byes" are different facts.

Scope is the target week only. A roster gets a row only if it has a
:class:`~commishdesk.ingest.Matchup` that week (a roster absent from
``WeekModel.rosters`` still gets one -- it just has no IR list); season-cumulative coaching
efficiency is a later fold over per-week results. Rows are ordered by
``roster_id`` (numeric where parseable, matching
:class:`~commishdesk.ingest.WeekModel`'s own convention).

This module stays inside the ``stats/`` fence (AD-1): it imports only stdlib,
pydantic and ``commishdesk.ingest`` / ``commishdesk.errors`` -- never
``adapters`` / ``store`` / ``facts`` / ``narrate`` / ``render`` / a later
pipeline stage. It reads no bye file, no clock, no PRNG, no filesystem, and
makes no network call. The one thing it does raise is
:class:`~commishdesk.errors.OptimalLineupError`, for a malformed
``roster_slots`` -- a broken *league format*, not a broken league-week.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from commishdesk.errors import OptimalLineupError
from commishdesk.ingest import LeagueModel, Matchup, PlayerSnapshot, Roster, WeekModel

__all__ = [
    "BenchedPlayer",
    "ByeStarter",
    "LineupSlot",
    "TeamLineup",
    "WeeklyLineups",
    "compute_weekly_lineups",
]

#: Points are scaled to integers before the solve so the Kuhn-Munkres potentials
#: stay exact (no float-comparison drift) and the assignment needs no
#: third-party solver. A thousandth of a point is far below any scoring delta a
#: real league can produce.
_POINT_SCALE = 1000

#: Stand-in for "unreached" inside the assignment potentials. A plain int (not
#: ``float("inf")``) keeps the whole solve in integer arithmetic, so no
#: ``inf - inf`` can ever produce a ``NaN`` potential.
_INFINITY = 1 << 62


class _Frozen(BaseModel):
    """Immutable, closed to unknown keys -- matches ``stats/weekly.py``."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class LineupSlot(_Frozen):
    """One slot of the optimal lineup, in ``league.format.roster_slots`` order.
    ``player_id`` is ``None`` and ``points`` is ``0.0`` when no eligible player
    remained for this slot."""

    slot: str
    player_id: str | None
    points: float


class BenchedPlayer(_Frozen):
    """One player who was on the week's roster but not in the manager's starting
    lineup, with his points for the week."""

    player_id: str
    points: float


class ByeStarter(_Frozen):
    """One of the roster's actual starters whose NFL team was on bye that week
    (as told by the caller's ``nfl_byes``). ``nfl_team`` is ``None`` when the
    player's snapshot carries no team, in which case he can never be flagged."""

    player_id: str
    nfl_team: str | None
    points: float


class TeamLineup(_Frozen):
    """One roster's optimal-lineup row for the target week.

    ``lineup`` is the solved assignment in slot order. ``actual`` is what the
    manager started; ``optimal`` is the best legal lineup's total, floored at
    ``actual`` so a player the solver could not place (no known position) never
    reads as a manager beating the optimum. ``pct`` is ``actual / optimal``
    (``None`` when ``optimal <= 0``, never above ``1.0``).
    ``points_left_on_bench`` is ``optimal - actual``. ``best_benched`` /
    ``bench_regret`` are the manager's single worst benching decision -- the
    highest-scoring placeable player on the roster who did not start -- both
    ``None`` when every placeable player started or every one benched scored below
    zero. ``started_on_bye`` is ``None``
    when the caller supplied no bye data, never ``[]``."""

    roster_id: str
    lineup: list[LineupSlot]
    optimal: float
    actual: float
    pct: float | None
    points_left_on_bench: float
    best_benched: BenchedPlayer | None
    bench_regret: float | None
    started_on_bye: list[ByeStarter] | None


class WeeklyLineups(_Frozen):
    """The whole result: one :class:`TeamLineup` per roster that played the
    target week, ordered by ``roster_id`` (numeric where parseable -- matches
    :class:`~commishdesk.ingest.WeekModel`'s own convention)."""

    week: int
    teams: list[TeamLineup]


def _sort_key(value: str) -> tuple[int, object]:
    """Order ids numerically when they parse as ints, lexically otherwise --
    mirrors ``stats/weekly.py``'s own ``_sort_key``."""
    try:
        return (0, int(value))
    except (TypeError, ValueError):
        return (1, value)


# --------------------------------------------------------------------------- #
# Format validation
# --------------------------------------------------------------------------- #


def _malformed_slots(league: LeagueModel, detail: str) -> OptimalLineupError:
    """The one typed fault this module raises. Names the league, the offending
    slot list, and the two documents that explain how to fix it -- so an
    operator reading a one-line stderr message knows both what broke and where
    to go."""
    return OptimalLineupError(
        f"league {league.league_id!r} has a malformed roster_slots ({detail}): "
        f"{list(league.format.roster_slots)!r}. Regenerate the fixture's "
        "league.roster_positions with tools/anonymize.py, or see CONTRIBUTING.md "
        "for how to contribute the league that exposed it."
    )


def _slot_eligibility(league: LeagueModel) -> tuple[list[str], list[list[str]]]:
    """Resolve ``league.format`` into per-slot eligible positions.

    A slot named in ``flex_eligibility`` is a flex slot and uses that list; any
    other slot matches its own position literally (``QB``, ``K``, ``DEF``,
    ``DL``, ...). Three shapes are structurally unsolvable and raise
    :class:`~commishdesk.errors.OptimalLineupError`: no slots at all, a slot
    with an empty name, and a flex slot that declares no eligible position (an
    empty list, or no ``flex_eligibility`` entry at all)."""
    slots = [str(slot) for slot in league.format.roster_slots]
    if not slots:
        raise _malformed_slots(league, "roster_slots is empty")

    flex = league.format.flex_eligibility
    eligibility: list[list[str]] = []
    for slot in slots:
        if not slot.strip():
            raise _malformed_slots(league, "a roster slot has an empty name")
        if slot in flex:
            positions = [str(position) for position in flex[slot] if str(position).strip()]
            if not positions:
                raise _malformed_slots(league, f"flex slot {slot!r} declares no eligible positions")
            eligibility.append(positions)
        elif "FLEX" in slot.upper():
            # A flex-named slot the builder recorded no eligibility for at all
            # (a hand-built format, or a stale one): unsolvable, never a
            # literal-position guess that would silently leave the slot empty.
            raise _malformed_slots(league, f"flex slot {slot!r} has no flex_eligibility entry")
        else:
            eligibility.append([slot])
    return slots, eligibility


# --------------------------------------------------------------------------- #
# The assignment solve
# --------------------------------------------------------------------------- #


def _min_cost_assignment(cost: list[list[int]]) -> list[int]:
    """Minimum-cost perfect assignment of the rows of a square integer matrix to
    its columns. Returns ``assignment[row] = column``.

    Kuhn-Munkres (the Jonker-Volgenant short form), O(n^3), integer arithmetic
    only: exact, deterministic, and dependency-free. Determinism across runs
    comes from the caller: candidates arrive in a canonical ``(-points,
    player_id)`` order, so equal inputs always build the same matrix and get the
    same assignment. Which of several equal-scoring players fills which slot is
    a fixed but arbitrary consequence of that order, not a "lower id wins" rule."""
    size = len(cost)
    if size == 0:
        return []

    row_potential = [0] * (size + 1)
    column_potential = [0] * (size + 1)
    column_row = [0] * (size + 1)
    way = [0] * (size + 1)

    for row in range(1, size + 1):
        column_row[0] = row
        column = 0
        min_reduced = [_INFINITY] * (size + 1)
        visited = [False] * (size + 1)
        while True:
            visited[column] = True
            current = column_row[column]
            delta = _INFINITY
            next_column = 0
            for candidate in range(1, size + 1):
                if visited[candidate]:
                    continue
                reduced = cost[current - 1][candidate - 1] - row_potential[current] - column_potential[candidate]
                if reduced < min_reduced[candidate]:
                    min_reduced[candidate] = reduced
                    way[candidate] = column
                if min_reduced[candidate] < delta:
                    delta = min_reduced[candidate]
                    next_column = candidate
            for candidate in range(size + 1):
                if visited[candidate]:
                    row_potential[column_row[candidate]] += delta
                    column_potential[candidate] -= delta
                else:
                    min_reduced[candidate] -= delta
            column = next_column
            if column_row[column] == 0:
                break
        while column:
            previous = way[column]
            column_row[column] = column_row[previous]
            column = previous

    assignment = [0] * size
    for column in range(1, size + 1):
        if column_row[column]:
            assignment[column_row[column] - 1] = column - 1
    return assignment


def _solve(eligibility: list[list[str]], candidates: list[tuple[str, str, float]]) -> list[tuple[int | None, float]]:
    """Assign *candidates* to slots, returning ``(candidate_index | None,
    points)`` in slot order.

    The matrix is square: ``len(eligibility)`` slot rows plus one padding row
    per candidate, against one column per candidate plus one dummy column per
    slot. A dummy edge costs more than any real edge times the number of slots
    a single swap could touch, so leaving a slot empty is only ever chosen when
    no eligible player remains -- the "fill while an eligible player remains,
    even negative-scoring" rule. Ineligible (slot, player) pairs cost more than
    every dummy edge combined, so the optimum never touches one. The padding
    rows cost ``0`` anywhere: they absorb whatever the slot rows leave -- an
    unused candidate column, or a slot's dummy column (i.e. that slot was
    filled by a candidate) -- and so never influence which lineup is optimal."""
    slot_count = len(eligibility)
    if slot_count == 0:
        return []
    candidate_count = len(candidates)
    if candidate_count == 0:
        return [(None, 0.0)] * slot_count

    scaled = [round(points * _POINT_SCALE) for _, _, points in candidates]
    bound = max((abs(value) for value in scaled), default=0)
    dummy_cost = (2 * slot_count + 1) * bound + 1
    forbidden = dummy_cost * (slot_count + candidate_count + 1)

    size = slot_count + candidate_count
    cost = [[forbidden] * size for _ in range(size)]
    for row, positions in enumerate(eligibility):
        allowed = set(positions)
        for column, (_, position, _) in enumerate(candidates):
            if position in allowed:
                cost[row][column] = -scaled[column]
        cost[row][candidate_count + row] = dummy_cost
    for row in range(slot_count, size):
        cost[row] = [0] * size

    assignment = _min_cost_assignment(cost)
    solved: list[tuple[int | None, float]] = []
    for row in range(slot_count):
        column = assignment[row]
        if column < candidate_count:
            solved.append((column, candidates[column][2]))
        else:
            solved.append((None, 0.0))
    return solved


# --------------------------------------------------------------------------- #
# Per-roster rows
# --------------------------------------------------------------------------- #


def _candidates(
    matchup: Matchup,
    roster: Roster | None,
    players: Mapping[str, PlayerSnapshot],
    placeable: frozenset[str],
) -> list[tuple[str, str, float]]:
    """``(player_id, position, points)`` for every player who can legally be
    placed this week, ordered by ``(-points, player_id)``.

    The pool is the whole week roster (``starters`` + ``bench`` +
    ``players_points``). ``Roster.ir`` occupants are dropped *unless they
    started*; taxi occupants are kept -- see the module docstring's DECIDED note. A player
    with no known position, or one whom no slot in this league accepts (a ``K``
    in a league with no ``K`` slot), is dropped too: he cannot be put in a slot,
    so he can neither be placed nor be named the best benched player."""
    starters = set(matchup.starters)
    excluded: set[str] = set()
    if roster is not None:
        excluded = set(roster.ir)

    roster_ids: list[str] = []
    for player_id in (*matchup.starters, *matchup.bench, *matchup.players_points):
        if player_id not in roster_ids:
            roster_ids.append(player_id)

    pool: list[tuple[str, str, float]] = []
    for player_id in roster_ids:
        if player_id in excluded and player_id not in starters:
            continue
        snapshot = players.get(player_id)
        position = snapshot.position if snapshot is not None else None
        if position is None or str(position) not in placeable:
            continue
        pool.append((player_id, str(position), float(matchup.players_points.get(player_id, 0.0))))
    pool.sort(key=lambda row: (-row[2], row[0]))
    return pool


def _bye_starters(
    matchup: Matchup, players: Mapping[str, PlayerSnapshot], nfl_byes: frozenset[str] | None
) -> list[ByeStarter] | None:
    """The actual starters whose NFL team is on bye, ordered by ``(-points,
    player_id)``. ``None`` when no bye data was supplied -- "none on bye" and
    "byes unknown" are different facts and must not collapse."""
    if nfl_byes is None:
        return None
    bye_teams = set(nfl_byes)
    flagged: list[tuple[str, str, float]] = []
    for player_id in matchup.starters:
        snapshot = players.get(player_id)
        nfl_team = snapshot.nfl_team if snapshot is not None else None
        points = float(matchup.players_points.get(player_id, 0.0))
        # A player whose team was on bye cannot have scored. Snapshot teams can
        # be newer than the week (a backfill -- docs/EDGE-CASES.md), so a starter
        # with points is never a bye starter, whatever his snapshot says.
        if nfl_team is not None and nfl_team in bye_teams and points == 0.0:
            flagged.append((player_id, nfl_team, points))
    flagged.sort(key=lambda row: (-row[2], row[0]))
    return [
        ByeStarter(player_id=player_id, nfl_team=nfl_team, points=round(points, 2))
        for player_id, nfl_team, points in flagged
    ]


def _team_lineup(
    matchup: Matchup,
    roster: Roster | None,
    slots: list[str],
    eligibility: list[list[str]],
    players: Mapping[str, PlayerSnapshot],
    nfl_byes: frozenset[str] | None,
) -> TeamLineup:
    placeable = frozenset(position for positions in eligibility for position in positions)
    candidates = _candidates(matchup, roster, players, placeable)
    solved = _solve(eligibility, candidates)

    lineup: list[LineupSlot] = []
    optimal_raw = 0.0
    for slot, (index, points) in zip(slots, solved, strict=True):
        if index is None:
            lineup.append(LineupSlot(slot=slot, player_id=None, points=0.0))
            continue
        optimal_raw += points
        lineup.append(LineupSlot(slot=slot, player_id=candidates[index][0], points=round(points, 2)))

    actual = round(sum(matchup.starters_points), 2)
    optimal = round(max(optimal_raw, actual), 2)
    pct = round(min(actual / optimal, 1.0), 3) if optimal > 0 else None

    starters = set(matchup.starters)
    # ``candidates`` is already ordered by ``(-points, player_id)``, so the first
    # non-starter is the manager's worst benching decision.
    benched = [
        BenchedPlayer(player_id=player_id, points=round(points, 2))
        for player_id, _, points in candidates
        if player_id not in starters
    ]
    # A benched player who scored below zero is not a "regret" -- leaving him out
    # was right -- so he is never named the best benched player.
    best_benched = benched[0] if benched and benched[0].points >= 0 else None

    return TeamLineup(
        roster_id=matchup.roster_id,
        lineup=lineup,
        optimal=optimal,
        actual=actual,
        pct=pct,
        points_left_on_bench=round(optimal - actual, 2),
        best_benched=best_benched,
        bench_regret=round(best_benched.points, 2) if best_benched is not None else None,
        started_on_bye=_bye_starters(matchup, players, nfl_byes),
    )


def compute_weekly_lineups(
    week: WeekModel,
    league: LeagueModel,
    players: Mapping[str, PlayerSnapshot],
    nfl_byes: frozenset[str] | None = None,
) -> WeeklyLineups:
    """Solve every roster's best legal lineup for one league-week and compare it
    against what the manager actually started.

    Pure, deterministic, offline: two calls on equal inputs return an equal
    ``model_dump()``. *nfl_byes* is the set of NFL team abbreviations on bye
    that week, supplied by the caller -- this module never reads the bye file.
    Raises :class:`~commishdesk.errors.OptimalLineupError` when
    ``league.format.roster_slots`` cannot be solved; a roster with no
    :class:`~commishdesk.ingest.Matchup` for the target week simply gets no
    row."""
    slots, eligibility = _slot_eligibility(league)

    target_rows: dict[str, Matchup] = {}
    for row in week.matchups:
        if row.week == week.week:
            target_rows[row.roster_id] = row

    # Driven by the target week's matchups, not by ``week.rosters``: an orphan
    # roster that played (absent from the roster list) still gets a row, it just
    # carries no IR information to exclude by.
    rosters = {roster.roster_id: roster for roster in week.rosters}
    teams: list[TeamLineup] = []
    for roster_id in sorted(target_rows, key=_sort_key):
        teams.append(
            _team_lineup(target_rows[roster_id], rosters.get(roster_id), slots, eligibility, players, nfl_byes)
        )

    return WeeklyLineups(week=week.week, teams=teams)
