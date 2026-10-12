---
doc_id: streaks
kind: stat
title: Streaks
schema_min: 0.5.0
facts_keys:
  - weekly.teams[].season.streak
fixture: facts/expected-weekly-facts-week10.json
source: commishdesk/stats/standings.py::_streak
---
## Definition

A streak is the run of identical results that ends at the team's most recent game. `type` is `W`, `L` or `T`, and `count` is the length of the run. Results come from the same head-to-head fold as the record, in week order, through the last regular-season week. `streak` is `null` when the team has not played a game.

## Worked example

Fixture: `tests/fixtures/facts/expected-weekly-facts-week10.json` (the anonymized league "Trench Warfare").

```json
{
  "teams": [
    {
      "roster_id": "4",
      "team_name": "Chip-Block Chinchillas",
      "season": {
        "streak": {
          "type": "W",
          "count": 3
        }
      }
    },
    {
      "roster_id": "11",
      "team_name": "Bubble-Screen Bobcats",
      "season": {
        "streak": {
          "type": "L",
          "count": 6
        }
      }
    }
  ]
}
```

Bubble-Screen Bobcats (roster 11) lost in weeks 5 through 10 after a week-4 win, so the run that ends at week 10 is six losses: `L`, 6. Chip-Block Chinchillas (roster 4) have won their last three games, so `W`, 3. A streak of three or more is one of the signals the engine looks for when it offers storylines, as described in `leads-and-storylines`.

## Code

Source: `commishdesk/stats/standings.py::_streak`

```python
def _streak(results: list[str]) -> Streak | None:
    """The run of identical results ending at the most recent game, or ``None``
    when the roster has not played one."""
    if not results:
        return None
    last = results[-1]
    count = 0
    for result in reversed(results):
        if result != last:
            break
        count += 1
    return Streak(type=last, count=count)
```

## Edge cases

- **Byes.** A bye is not a game. It adds no result and does not break or extend a streak.
- **Ties.** A tie is its own result type, `T`. It ends a win or loss streak and starts a streak of one.
- **Week 1.** After one game the streak is a run of one.
- **Playoff weeks.** The fold stops at the last regular-season week, so the streak does not change in the playoffs.
- **Unplayed.** A team with no games has a `null` streak.
