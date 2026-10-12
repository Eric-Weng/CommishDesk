---
doc_id: record-and-standings
kind: stat
title: Record and standings
schema_min: 0.5.0
facts_keys:
  - weekly.teams[].season.record
  - weekly.teams[].season.rank
  - weekly.teams[].season.division_rank
  - weekly.standings.overall
  - weekly.standings.divisions
  - weekly.standings.through_week
  - weekly.standings.regular_season_complete
fixture: facts/expected-weekly-facts-week10.json
source: commishdesk/stats/standings.py::_order_key
---
## Definition

A team's record is its wins, losses and ties in head-to-head games, folded from the week-by-week matchups from week 1 through the last regular-season week. A tie counts as half a win. The engine does not read the win and loss counters the platform keeps on each roster for this. Those totals are used for one thing only: a cross-check that stops the run if they disagree with the fold.

`season.rank` orders the league by one key, in this order:

1. win percentage, `(wins + 0.5 * ties) / games`, highest first, compared before rounding;
2. points for, highest first;
3. roster id, lowest first (numeric where the id is a number).

`season.division_rank` applies the same key inside each division and is `null` when the league declares no divisions. `standings.overall` lists every roster id from first to last in that order. `standings.divisions` maps each division id to its roster ids in division order. `standings.through_week` is the last week folded in, and `standings.regular_season_complete` turns true once the issue week reaches the week before the playoffs start.

## Worked example

Fixture: `tests/fixtures/facts/expected-weekly-facts-week10.json` (the anonymized league "Trench Warfare").

```json
{
  "teams": [
    {
      "roster_id": "1",
      "team_name": "Flea Flicker Union",
      "season": {
        "record": {
          "w": 7,
          "l": 3,
          "t": 0
        },
        "rank": 2,
        "division_rank": 1,
        "points_for": 2100.85
      }
    },
    {
      "roster_id": "3",
      "team_name": "Backpedal Buffalo",
      "season": {
        "record": {
          "w": 7,
          "l": 3,
          "t": 0
        },
        "rank": 3,
        "division_rank": 1,
        "points_for": 1816.51
      }
    },
    {
      "roster_id": "9",
      "team_name": "No-Huddle Narwhals",
      "season": {
        "record": {
          "w": 6,
          "l": 4,
          "t": 0
        },
        "rank": 4,
        "points_for": 2037.57
      }
    }
  ],
  "standings": {
    "overall": [
      "2",
      "1",
      "3",
      "9",
      "5",
      "6",
      "12",
      "8",
      "4",
      "10",
      "11",
      "7"
    ],
    "through_week": 10,
    "regular_season_complete": false
  }
}
```

After week 10, Flea Flicker Union (roster 1) and Backpedal Buffalo (roster 3) are both 7-3, a win percentage of 0.700 each. The first key is level, so points for decides: 2,100.85 against 1,816.51 puts roster 1 at rank 2 and roster 3 at rank 3. No-Huddle Narwhals (roster 9) have scored more than roster 3 (2,037.57 against 1,816.51), but they are 6-4 (0.600), so win percentage ranks them 4th. Points only break ties in win percentage. The `overall` list reads the same way: roster 2 (9-1) first, then 1, 3 and 9. Roster 1 is first in its division (`division_rank` 1) and second overall. `regular_season_complete` is false because week 10 is before the final regular-season week.

## Code

Source: `commishdesk/stats/standings.py::_order_key`

```python
def _order_key(roster_id: str, records: dict[str, TeamRecord]) -> tuple[float, float, tuple[int, object]]:
    """The one standings key: win percentage descending, then points-for
    descending, then ``roster_id`` (numeric where parseable). Computed on the
    unrounded win percentage so a 3-decimal display tie is still resolved by the
    real fraction."""
    record = records.get(roster_id)
    wins, losses, ties = (record.wins, record.losses, record.ties) if record is not None else (0, 0, 0)
    games = wins + losses + ties
    win_pct = (wins + 0.5 * ties) / games if games else 0.0
    points_for = record.points_for if record is not None else 0.0
    return (-win_pct, -points_for, _sort_key(roster_id))
```

## Edge cases

- **Byes and orphans.** A week only counts as a game when the roster's matchup names an opponent that has its own row that week. A bye adds points for the week but no win, loss, tie or points against, and it does not break a streak.
- **Ties in percentage.** The comparison uses the unrounded fraction, so two teams that both display 0.667 are still ordered by their real values before points for is consulted.
- **Playoff weeks.** The fold stops at the week before the playoffs start, so a playoff-week issue reports the final regular-season standings, not a changing race. `regular_season_complete` reports whether that freeze has engaged.
- **Commissioner seeding.** When the operator overrides the playoff seeding, the standings order and `rank` follow the supplied seed order. Each team's record is unchanged. See `playoff-picture`.
- **No divisions.** `division_rank` is `null` and `standings.divisions` is empty.
- **Cross-check.** If the folded record or points for disagree with the platform's own season totals beyond a rounding allowance of half a cent per week, the run stops with a mismatch report instead of publishing.
