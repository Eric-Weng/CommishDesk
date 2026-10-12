---
doc_id: leaders
kind: stat
title: Weekly leaders
schema_min: 0.5.0
facts_keys:
  - weekly.leaders
fixture: facts/expected-weekly-facts-week10.json
source: commishdesk/facts/weekly.py::_leaders
---
## Definition

The leaders block ranks players and lineups across the whole league for the week.

- `week_high_player` is the highest-scoring started player, and `is_season_high` says whether that score is at least every player's best week through this one.
- `top_players` are the five best started players (`TOP_PLAYERS = 5`) and `worst_starters` are the five lowest-scoring (`WORST_STARTERS = 5`), each with its roster, position and points.
- `best_coaching` and `worst_coaching` give the highest and lowest weekly coaching efficiency, as a roster id and a `pct` (see `coaching-efficiency-and-bench-regret`).

Only players who were actually started count. Ties break by points, then roster id, then player id.

## Worked example

Fixture: `tests/fixtures/facts/expected-weekly-facts-week10.json` (the anonymized league "Trench Warfare").

```json
{
  "leaders": {
    "week_high_player": {
      "roster_id": "3",
      "player_id": "6813",
      "name": "Jonathan Taylor",
      "pos": "RB",
      "points": 71.37,
      "is_season_high": true
    },
    "top_players": [
      {
        "roster_id": "3",
        "player_id": "6813",
        "name": "Jonathan Taylor",
        "pos": "RB",
        "points": 71.37
      },
      {
        "roster_id": "1",
        "player_id": "9226",
        "name": "De'Von Achane",
        "pos": "RB",
        "points": 56.52
      }
    ],
    "best_coaching": {
      "roster_id": "2",
      "pct": 0.992
    },
    "worst_coaching": {
      "roster_id": "7",
      "pct": 0.524
    }
  }
}
```

Jonathan Taylor, started by roster 3, scored 71.37 in week 10, the best of any starter and a new season high, ahead of De'Von Achane (56.52, roster 1). Roster 2 had the most efficient week, using 99.2 percent of its optimal lineup, and roster 7 the least, at 52.4 percent. Roster 7 left 108.86 points on the bench in a six-point loss, the lead of the issue in `leads-and-storylines`.

## Code

Source: `commishdesk/facts/weekly.py::_leaders`

```python
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
```

## Edge cases

- **Season high.** The comparison is `>=`, so a score that ties the existing best also counts as a season high.
- **Empty slots.** A roster slot with no player is not a starter, so it cannot be a leader or a worst starter.
- **Zero points.** Several starters can score 0.0, for example when a team has no points from players on bye. The five worst are picked by the tie-break order, so the list is stable but not exhaustive.
- **No starters.** If no starter exists, `week_high_player` is `null` and the lists are empty.
- **Coaching leaders** only consider teams whose coaching efficiency has a value.
