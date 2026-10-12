---
doc_id: all-play
kind: stat
title: All-play record
schema_min: 0.5.0
facts_keys:
  - weekly.teams[].season.all_play
fixture: facts/expected-weekly-facts-week10.json
source: commishdesk/stats/weekly.py::_all_play_totals
---
## Definition

All-play asks how a team would have done if it had played everyone every week. For each week from 1 through the last regular-season week, every team that played is compared by points with every other team that played that week. The higher score wins that comparison, the lower score loses, and an exact tie is recorded as a tie. The results are added across the weeks into `w`, `l` and `t`.

`pct` is the decisive win rate, `w / (w + l)`, rounded to three decimals. Ties are left out of the ratio. It is `0.0` before any decisive comparison exists.

All-play is the base for `expected-wins-and-luck`, and it is the largest input to `power-rank-and-nudge`.

## Worked example

Fixture: `tests/fixtures/facts/expected-weekly-facts-week10.json` (the anonymized league "Trench Warfare").

```json
{
  "teams": [
    {
      "roster_id": "1",
      "team_name": "Flea Flicker Union",
      "season": {
        "all_play": {
          "w": 88,
          "l": 22,
          "t": 0,
          "pct": 0.8
        }
      }
    },
    {
      "roster_id": "2",
      "team_name": "Two Minute Drill",
      "season": {
        "all_play": {
          "w": 87,
          "l": 22,
          "t": 1,
          "pct": 0.798
        }
      }
    }
  ]
}
```

There are 12 teams, so each team makes 11 comparisons a week. Over ten weeks that is 110. Flea Flicker Union (roster 1) won 88 of them and lost 22, so `pct` is 88 / 110 = 0.800. Two Minute Drill (roster 2) went 87-22-1, one comparison being an exact tie, so its 109 decisive comparisons give 87 / 109 = 0.798. The tie raises neither `w` nor `l`, but it is still one of that team's 110 comparisons. Roster 2 is 9-1 in real games, while roster 1 is 7-3. Their all-play records are almost equal, which is why `expected-wins-and-luck` treats roster 1 as the less fortunate team.

## Code

Source: `commishdesk/stats/weekly.py::_all_play_totals`

```python
def _all_play_totals(
    matchups_by_week: dict[int, dict[str, Matchup]], cutoff: int
) -> tuple[dict[str, int], dict[str, int], dict[str, int], dict[str, float]]:
    """Fold weeks ``1..cutoff`` into cumulative all-play wins/losses/ties plus
    the summed per-week win-equivalent fraction (``expected_wins`` before
    rounding), keyed by roster id. A roster with no counterpart in
    :attr:`WeekModel.rosters` (an orphan matchup row) still participates --
    it still affects every other roster's count -- it simply has no
    :class:`TeamWeekStats` entry to be reported on later."""
    wins: dict[str, int] = {}
    losses: dict[str, int] = {}
    ties: dict[str, int] = {}
    expected: dict[str, float] = {}

    for wk in range(1, cutoff + 1):
        rows = matchups_by_week.get(wk, {})
        participants = sorted(rows, key=_sort_key)
        decisions = len(participants) - 1
        if decisions < 1:
            continue
        week_win_equiv: dict[str, float] = dict.fromkeys(participants, 0.0)
        for i, roster_a in enumerate(participants):
            points_a = rows[roster_a].points
            for roster_b in participants[i + 1 :]:
                points_b = rows[roster_b].points
                if points_a > points_b:
                    wins[roster_a] = wins.get(roster_a, 0) + 1
                    losses[roster_b] = losses.get(roster_b, 0) + 1
                    week_win_equiv[roster_a] += 1.0
                elif points_b > points_a:
                    wins[roster_b] = wins.get(roster_b, 0) + 1
                    losses[roster_a] = losses.get(roster_a, 0) + 1
                    week_win_equiv[roster_b] += 1.0
                else:
                    ties[roster_a] = ties.get(roster_a, 0) + 1
                    ties[roster_b] = ties.get(roster_b, 0) + 1
                    week_win_equiv[roster_a] += 0.5
                    week_win_equiv[roster_b] += 0.5
        for roster_id in participants:
            expected[roster_id] = expected.get(roster_id, 0.0) + week_win_equiv[roster_id] / decisions

    return wins, losses, ties, expected
```

## Edge cases

- **Week 1.** A single week has no average week to compare against, so `all_play` is `null` for every team until week 2.
- **Playoff weeks.** The fold stops at the week before the playoffs start. From then on `all_play` stays at its final regular-season value.
- **Byes.** The comparisons per week are the number of teams that played minus one. A team on a bye is neither compared nor counted, and the other teams' totals reflect the smaller field that week.
- **Orphan rows.** A matchup row for a roster that is not in the roster list still counts as an opponent for everyone else. It simply has no all-play row of its own.
- **Exact ties.** Both teams get a tie. For `expected_wins` the tie is worth half a win to each.
