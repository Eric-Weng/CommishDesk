"""Per-pick and per-team consensus metrics (Story 2.4b).

One pure function, :func:`compute_consensus_metrics`, turns a stage-1
:class:`~commishdesk.ingest.LeagueModel` plus a plain ``sleeper_id -> rank``
mapping into a frozen :class:`ConsensusMetrics`:

* per pick -- ``consensus_slot`` / ``consensus_label`` / ``delta`` / ``flags``,
  where ``delta = pick_no - consensus_slot``. **Positive = value** (the player
  fell to the picker), **negative = reach** (the picker jumped early). A drafted
  player absent from the mapping gets slot / label / delta ``None`` and
  ``flags == ["no_consensus"]`` -- it never raises and never blocks the rest.
* per team -- ``best_value_pick`` (largest ``delta``) and ``biggest_reach_pick``
  (smallest ``delta``) as raw ``{pick_no, player, delta}`` extremes, or ``None``
  when the team has no ranked pick. Ties break toward the earlier ``pick_no``.

The input mapping is treated as an ordering signal, not a finished rank: the
function filters it to the drafted set and dense-ranks ``1..N`` in ascending-rank
order, so it is correct on a sparse or non-dense mapping and idempotent on an
already-dense one.

Grades and their inputs are **not** here -- Story 2.4c.

This module stays inside the ``stats/`` fence (AD-1): it imports only stdlib,
pydantic, and ``commishdesk.ingest`` -- nothing from ``adapters`` / ``store`` /
``consensus`` / a later pipeline stage. It never sees a ``ConsensusRank``.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from commishdesk.ingest import LeagueModel

__all__ = [
    "ConsensusMetrics",
    "PickConsensus",
    "PickRef",
    "TeamConsensus",
    "compute_consensus_metrics",
]

_NO_CONSENSUS = "no_consensus"


class _Frozen(BaseModel):
    """Immutable, closed to unknown keys -- matches ``ingest/model.py``."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class PickConsensus(_Frozen):
    """One draft pick measured against the consensus board. ``consensus_slot`` /
    ``consensus_label`` / ``delta`` are ``None`` together exactly when
    ``flags == ["no_consensus"]``."""

    pick_no: int
    roster_id: str
    player: str
    consensus_slot: int | None
    consensus_label: str | None
    delta: int | None
    flags: list[str]


class PickRef(_Frozen):
    """A pointer to one pick and its raw ``delta`` -- a per-team extreme."""

    pick_no: int
    player: str
    delta: int


class TeamConsensus(_Frozen):
    """One roster's raw consensus extremes. Both are ``None`` when the roster
    drafted no player present in the consensus board."""

    roster_id: str
    best_value_pick: PickRef | None
    biggest_reach_pick: PickRef | None


class ConsensusMetrics(_Frozen):
    """The whole result: one :class:`PickConsensus` per pick in ascending
    ``pick_no`` order, one :class:`TeamConsensus` per roster in ``league.teams``
    order."""

    picks: list[PickConsensus]
    teams: list[TeamConsensus]


def _consensus_label(slot: int, team_count: int) -> str:
    """Positional readout ``f"{rnd}.{col:02d}"`` from a 1-based slot. Snake-draft
    direction is not modelled -- slot 14 with 12 teams reads ``"2.02"``."""
    rnd = (slot - 1) // team_count + 1
    col = (slot - 1) % team_count + 1
    return f"{rnd}.{col:02d}"


def compute_consensus_metrics(
    league: LeagueModel, consensus_slots: Mapping[str, int]
) -> ConsensusMetrics:
    """Compute per-pick and per-team consensus metrics for ``league``.

    Pure, deterministic, offline. A valid ``LeagueModel`` plus a
    ``sleeper_id -> rank`` mapping in, a ``ConsensusMetrics`` out; two calls on
    one input return an equal ``model_dump()``.
    """
    team_count = league.format.team_count
    picks = sorted(league.picks, key=lambda pick: pick.pick_no)

    drafted_ids = {pick.player.sleeper_id for pick in picks if pick.player.sleeper_id}
    present = {
        sleeper_id: consensus_slots[sleeper_id]
        for sleeper_id in drafted_ids
        if sleeper_id in consensus_slots
    }
    ordered = sorted(present, key=lambda sleeper_id: (present[sleeper_id], sleeper_id))
    dense = {sleeper_id: slot for slot, sleeper_id in enumerate(ordered, start=1)}

    pick_rows: list[PickConsensus] = []
    for pick in picks:
        slot = dense.get(pick.player.sleeper_id)
        if slot is None:
            pick_rows.append(
                PickConsensus(
                    pick_no=pick.pick_no,
                    roster_id=pick.roster_id,
                    player=pick.player.name,
                    consensus_slot=None,
                    consensus_label=None,
                    delta=None,
                    flags=[_NO_CONSENSUS],
                )
            )
        else:
            pick_rows.append(
                PickConsensus(
                    pick_no=pick.pick_no,
                    roster_id=pick.roster_id,
                    player=pick.player.name,
                    consensus_slot=slot,
                    consensus_label=_consensus_label(slot, team_count),
                    delta=pick.pick_no - slot,
                    flags=[],
                )
            )

    rows_by_roster: dict[str, list[PickConsensus]] = {}
    for row in pick_rows:
        rows_by_roster.setdefault(row.roster_id, []).append(row)

    team_rows: list[TeamConsensus] = []
    for team in league.teams:
        ranked = [
            (row.delta, row.pick_no, row.player)
            for row in rows_by_roster.get(team.roster_id, [])
            if row.delta is not None
        ]
        if not ranked:
            team_rows.append(
                TeamConsensus(
                    roster_id=team.roster_id,
                    best_value_pick=None,
                    biggest_reach_pick=None,
                )
            )
            continue
        best = max(ranked, key=lambda item: (item[0], -item[1]))
        worst = min(ranked, key=lambda item: (item[0], item[1]))
        team_rows.append(
            TeamConsensus(
                roster_id=team.roster_id,
                best_value_pick=PickRef(pick_no=best[1], player=best[2], delta=best[0]),
                biggest_reach_pick=PickRef(
                    pick_no=worst[1], player=worst[2], delta=worst[0]
                ),
            )
        )

    return ConsensusMetrics(picks=pick_rows, teams=team_rows)
