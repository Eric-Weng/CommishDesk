---
doc_id: pick-delta-and-consensus
kind: stat
title: Pick delta against the consensus board
schema_min: 0.1.0
facts_keys:
  - draft_recap.picks
  - draft_recap.consensus_source
fixture: facts/expected-draft-recap-facts.json
source: commishdesk/stats/consensus.py::compute_consensus_metrics
---
## Definition

Every draft pick is measured against a consensus ranking of the same players. The engine takes the players who were actually drafted, orders them by consensus rank and renumbers them 1 to N, so the consensus slot is correct even when the ranking is sparse or has gaps.

- `consensus_slot` is that renumbered position, and `consensus_label` is the same slot written as a round and column, such as `1.04`, using the league's team count.
- `delta` is `pick_no - consensus_slot`. A positive delta is value: the player fell to the picker. A negative delta is a reach: the picker took him early.
- `flags` holds `no_consensus` when a drafted player is missing from the consensus ranking. His slot, label and delta are then all `null`.

The rest of each pick row is the draft board itself: round, slot, board label, roster, manager and the player. `consensus_source` records which ranking was used, when it was taken, and whether it is provisional.

## Worked example

Fixture: `tests/fixtures/facts/expected-draft-recap-facts.json` (the anonymized league "Trench Warfare").

```json
{
  "picks": [
    {
      "pick_no": 1,
      "board_label": "1.01",
      "consensus_slot": 4,
      "consensus_label": "1.04",
      "delta": -3,
      "flags": []
    },
    {
      "pick_no": 2,
      "board_label": "1.02",
      "consensus_slot": 1,
      "consensus_label": "1.01",
      "delta": 1,
      "flags": []
    },
    {
      "pick_no": 5,
      "board_label": "1.05",
      "consensus_slot": null,
      "consensus_label": null,
      "delta": null,
      "flags": [
        "no_consensus"
      ],
      "player": {
        "name": "Cameron Ward"
      }
    },
    {
      "pick_no": 71,
      "board_label": "6.11",
      "consensus_slot": 54,
      "consensus_label": "5.06",
      "delta": 17,
      "flags": [],
      "player": {
        "name": "Lan Larison"
      }
    }
  ]
}
```

The first pick, 1.01, was Ashton Jeanty. The consensus had him fourth among the drafted players, so the delta is 1 - 4 = -3, a reach of three spots (label `1.04`). Pick 2 was Omarion Hampton, the consensus first, so 2 - 1 = +1 and he fell one spot. Pick 71, at 6.11, was Lan Larison, ranked 54th, so 71 - 54 = +17, the best value on the board. Pick 5, quarterback Cameron Ward, was not in the consensus ranking, so his slot, label and delta are `null` and the flags read `["no_consensus"]`.

## Code

Source: `commishdesk/stats/consensus.py::compute_consensus_metrics`

```python
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
```

## Edge cases

- **Not in the ranking.** The pick is flagged and left out of every value, reach and grade calculation. It never raises and never stops the rest.
- **Sparse rankings.** The renumbering happens after filtering to the drafted set, so a gap in the original rank does not distort the slots.
- **Snake drafts.** The label `round.column` is positional only. Snake direction is not modelled, so slot 14 in a 12-team league reads `2.02`.
- **Ties in rank.** Equal consensus ranks are ordered by player id so the slots are stable.
- **Provisional board.** `consensus_source.provisional` marks a ranking that is not final, so a recap can say its values are preliminary.
