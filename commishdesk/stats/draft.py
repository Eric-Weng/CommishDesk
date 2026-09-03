"""Board-only draft metrics (Story 2.4a).

One pure function, :func:`compute_board_metrics`, turns a stage-1
:class:`~commishdesk.ingest.LeagueModel` into a frozen :class:`BoardMetrics`
using ``league.picks`` and ``league.teams`` alone -- no consensus rank, no
:class:`~commishdesk.store.Store`, no network, no clock, no filesystem. Two calls
on one model return equal ``model_dump()`` output.

Per roster it derives: pick count, ascending pick numbers, per-position counts,
consecutive-``pick_no`` back-to-back pairs, and the board-wide real positions the
roster drafted none of. Board-wide it derives the maximal spans of consecutive
same-position picks (``length >= MIN_RUN``).

A pick whose ``player.position`` is falsy counts as ``"UNK"``: it still adds to
``pick_count`` and ``positional_counts`` but is excluded from the
``zero_positions`` universe and from run detection.

This module imports only stdlib, pydantic, and ``commishdesk.ingest`` -- nothing
from ``adapters`` / ``facts`` / ``narrate`` / ``render`` / ``deliver`` (AD-1).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from commishdesk.ingest import LeagueModel, Pick

__all__ = [
    "MIN_RUN",
    "BoardMetrics",
    "PositionalRun",
    "TeamBoard",
    "compute_board_metrics",
]

MIN_RUN = 3
"""Shortest span of consecutive same-position picks reported as a run."""

_UNK = "UNK"


class _Frozen(BaseModel):
    """Immutable, closed to unknown keys -- matches ``ingest/model.py``."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class PositionalRun(_Frozen):
    """A maximal span of ``length`` consecutive same-position picks on the board,
    in ``pick_no`` order. ``from_label`` / ``to_label`` are the ``board_label``
    of the span's first / last pick (e.g. ``"1.06"``)."""

    position: str
    from_pick_no: int
    to_pick_no: int
    from_label: str
    to_label: str
    length: int


class TeamBoard(_Frozen):
    """One roster's board-only draft profile. ``manager`` is carried verbatim
    from the :class:`~commishdesk.ingest.Team` (``None`` for an orphan)."""

    roster_id: str
    manager: str | None
    pick_count: int
    pick_nos: list[int]
    positional_counts: dict[str, int]
    back_to_back: list[tuple[int, int]]
    zero_positions: list[str]


class BoardMetrics(_Frozen):
    """The whole board-only result: one :class:`TeamBoard` per roster in
    ``league.teams`` order, plus every board-wide positional run in ascending
    ``from_pick_no`` order."""

    teams: list[TeamBoard]
    positional_runs: list[PositionalRun]


def _position(pick: Pick) -> str:
    return pick.player.position or _UNK


def compute_board_metrics(league: LeagueModel) -> BoardMetrics:
    """Compute board-only draft metrics for every roster in ``league``.

    Pure, deterministic, offline. A valid ``LeagueModel`` in, a ``BoardMetrics``
    out; malformed input is a programming error, not a typed domain fault.
    """
    picks = sorted(league.picks, key=lambda p: p.pick_no)

    # Board-wide universe of real (non-UNK) positions actually drafted.
    universe = sorted({_position(p) for p in picks} - {_UNK})

    picks_by_roster: dict[str, list[Pick]] = {}
    for pick in picks:
        picks_by_roster.setdefault(pick.roster_id, []).append(pick)

    teams: list[TeamBoard] = []
    for team in league.teams:
        roster_picks = picks_by_roster.get(team.roster_id, [])
        pick_nos = sorted(p.pick_no for p in roster_picks)

        counts: dict[str, int] = {}
        for pick in roster_picks:
            pos = _position(pick)
            counts[pos] = counts.get(pos, 0) + 1
        positional_counts = {key: counts[key] for key in sorted(counts)}

        back_to_back = [
            (pick_nos[i], pick_nos[i + 1])
            for i in range(len(pick_nos) - 1)
            if pick_nos[i + 1] == pick_nos[i] + 1
        ]

        drafted_real = {_position(p) for p in roster_picks} - {_UNK}
        zero_positions = [pos for pos in universe if pos not in drafted_real]

        teams.append(
            TeamBoard(
                roster_id=team.roster_id,
                manager=team.manager,
                pick_count=len(roster_picks),
                pick_nos=pick_nos,
                positional_counts=positional_counts,
                back_to_back=back_to_back,
                zero_positions=zero_positions,
            )
        )

    return BoardMetrics(teams=teams, positional_runs=_scan_runs(picks))


def _scan_runs(picks: list[Pick]) -> list[PositionalRun]:
    """Maximal spans of consecutive same-position picks (``pick_no`` order) of
    length ``>= MIN_RUN``; ``UNK`` spans are skipped. A pick of a different
    position ends the current span. The scan is board-wide -- it never resets on
    a roster or round boundary."""
    runs: list[PositionalRun] = []
    start = 0
    for i in range(1, len(picks) + 1):
        if i < len(picks) and _position(picks[i]) == _position(picks[start]):
            continue
        span = picks[start:i]
        position = _position(span[0])
        if position != _UNK and len(span) >= MIN_RUN:
            runs.append(
                PositionalRun(
                    position=position,
                    from_pick_no=span[0].pick_no,
                    to_pick_no=span[-1].pick_no,
                    from_label=span[0].board_label,
                    to_label=span[-1].board_label,
                    length=len(span),
                )
            )
        start = i
    return runs  # already ascending by from_pick_no: picks are pre-sorted, spans are disjoint
