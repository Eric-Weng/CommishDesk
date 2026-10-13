---
doc_id: coaching-efficiency-and-bench-regret
kind: stat
title: Coaching efficiency and bench regret
schema_min: 0.5.0
facts_keys:
  - weekly.teams[].season.coaching_efficiency
  - weekly.teams[].this_week.coaching_efficiency
  - weekly.teams[].this_week.bench_regret
  - weekly.teams[].this_week.points_left_on_bench
fixture: facts/expected-weekly-facts-week10.json
source: commishdesk/stats/lineup.py::_team_lineup
---
## Definition

**Optimal lineup.** For each team and week the engine finds the highest-scoring legal lineup that could have been started from the players the team had that week. This is an assignment problem, not a greedy fill. Flex slots overlap the strict slots, and filling slots in order can waste the only player a later slot could use.

**Coaching efficiency.** `actual` is the points the starters scored. `optimal` is the solved lineup's total, never lower than `actual`. `pct` is `actual / optimal` to three decimals and is `null` when `optimal` is zero or less. `season.coaching_efficiency` adds `actual` and `optimal` over weeks 1 through the issue week, then takes the same ratio.

**Points left on the bench** is `optimal - actual` for the week: what a perfect lineup would have added to the score. It is the gap between two lineups, not the total the bench scored, so the Issue words it as "a perfect lineup adds".

**Bench regret** names the highest-scoring player the manager could have started but did not, with that player's points. It is `null` when no benched player scored zero or more.

## Worked example

Fixture: `tests/fixtures/facts/expected-weekly-facts-week10.json` (the anonymized league "Trench Warfare").

```json
{
  "teams": [
    {
      "roster_id": "1",
      "team_name": "Flea Flicker Union",
      "season": {
        "coaching_efficiency": {
          "actual": 2100.85,
          "optimal": 2215.34,
          "pct": 0.948
        }
      },
      "this_week": {
        "coaching_efficiency": {
          "actual": 229.96,
          "optimal": 244.97,
          "pct": 0.939
        },
        "points_left_on_bench": 15.01,
        "bench_regret": {
          "player_id": "2307",
          "name": "Marcus Mariota",
          "pos": "QB",
          "points": 26.74
        }
      }
    }
  ]
}
```

Flea Flicker Union (roster 1) started 229.96 points in week 10. The best legal lineup from its roster was worth 244.97, so `pct` is 229.96 / 244.97 = 0.939 and 244.97 - 229.96 = 15.01 points were left on the bench. The single biggest regret was Marcus Mariota, a quarterback who scored 26.74 on the bench. Over the ten weeks the team started 2,100.85 and could have started 2,215.34, a season efficiency of 2,100.85 / 2,215.34 = 0.948.

## Code

Source: `commishdesk/stats/lineup.py::_team_lineup`

```python
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
```

## Edge cases

- **Floor at actual.** If a player has no known position he cannot be placed, and the solver cannot find a better lineup that uses him. `optimal` is floored at `actual`, so `pct` never exceeds 1.0.
- **Injured reserve.** IR players are not placeable unless they actually started that week. The fixtures hold season-end rosters, and a strict exclusion would push some weeks above 100 percent.
- **Taxi squad.** Taxi players can be activated at any time, so they count as available bench players.
- **Negative bench scores.** A benched player who scored below zero is not a regret. If the best benched player is below zero, `bench_regret` is `null`.
- **Empty slots.** If nobody eligible is left, a slot is simply empty and scores 0.0.
- **No game.** A team with no matchup row for the week has no `this_week` block, so no lineup result is reported for it.
