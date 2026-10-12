---
doc_id: byes
kind: stat
title: NFL byes
schema_min: 0.5.0
facts_keys:
  - weekly.period.nfl_byes
  - weekly.period.nfl_byes_next_week
  - weekly.teams[].this_week.started_on_bye
  - weekly.teams[].next_week.starters_on_bye
fixture: facts/expected-weekly-facts-week10.json
source: commishdesk/stats/lineup.py::_bye_starters
---
## Definition

A bye is a week an NFL team does not play. The engine does not hold a schedule. The caller supplies the set of NFL team abbreviations that are on bye, and the document records what it was given:

- `period.nfl_byes` is the set of teams on bye in the issue week, and `period.nfl_byes_next_week` is next week's set. Each is a sorted list of abbreviations, or `null` when the caller supplied no bye data.
- `teams[].this_week.started_on_bye` lists the team's starters whose NFL team was on bye in the issue week and who scored nothing. They are ordered by points, then player id.
- `teams[].next_week.starters_on_bye` lists this week's starters whose NFL team is on bye next week. It is the per-team view of the `bye_impact` list on each next-week card (see `stakes-and-next-week`).

## Worked example

Fixture: `tests/fixtures/facts/expected-weekly-facts-week10.json` (the anonymized league "Trench Warfare").

```json
{
  "period": {
    "nfl_byes": null,
    "nfl_byes_next_week": [
      "IND",
      "NO"
    ]
  },
  "teams": [
    {
      "roster_id": "1",
      "this_week": {
        "started_on_bye": null
      },
      "next_week": {
        "starters_on_bye": [
          {
            "player_id": "12518",
            "name": "Tyler Warren",
            "pos": "TE",
            "nfl_team": "IND"
          }
        ]
      }
    }
  ]
}
```

The week-10 fixture was built with byes known for next week only: Indianapolis and New Orleans (`IND`, `NO`). `nfl_byes` is therefore `null` (unknown), and so is every team's `started_on_bye`. Flea Flicker Union (roster 1) started Tyler Warren, a tight end for Indianapolis, in week 10. He will be on bye in week 11, so he appears in roster 1's `starters_on_bye` for next week.

## Code

Source: `commishdesk/stats/lineup.py::_bye_starters`

```python
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
```

## Edge cases

- **Unknown is not empty.** `null` means the caller supplied no bye data. `[]` means there were byes and none of the team's starters were affected. The two are never merged.
- **Points settle it.** A starter is flagged for the issue week only if his team is in the bye set and he scored 0.0. Player snapshots can be newer than the week, so a starter who scored is never flagged.
- **Display.** Player names are joined from the player map. A player without a known position keeps a `null` `pos`.
- **Next week's starters** are the current week's starters, since next week's lineups are not set yet.
