---
doc_id: blowout-and-week-shape
kind: stat
title: Blowouts and the shape of the week
schema_min: 0.5.0
facts_keys:
  - weekly.period.summary
  - weekly.matchups.this_week
fixture: facts/expected-weekly-facts-week10.json
source: commishdesk/stats/weekly.py::_week_summary
---
## Definition

**Blowout rule.** A completed head-to-head game is a blowout when the loser's score is at most 0.65 times the winner's (`BLOWOUT_RATIO`, exposed as `blowout_threshold`). A team on a bye can never be part of one.

**Week summary.** `period.summary` describes the league's week:

- `games` is the number of head-to-head games, and `total_points` and `avg_team_score` fold in every team's score;
- `high` and `low` are the highest and lowest score among teams that had a real opponent;
- `closest` is the game with the smallest margin;
- `biggest_blowout` is the widest-margin game, but only when that game is itself a blowout, otherwise `null`;
- `blowout_count` is the number of blowouts that week.

**Matchups.** `matchups.this_week` lists each pairing once, with the lower roster id as `home`. It carries both scores, `winner_roster_id` (`null` for a tie), the absolute `margin`, `is_blowout`, and `headline_players`, the top scorer on each side.

## Worked example

Fixture: `tests/fixtures/facts/expected-weekly-facts-week10.json` (the anonymized league "Trench Warfare").

```json
{
  "period": {
    "summary": {
      "games": 6,
      "total_points": 1910.9,
      "avg_team_score": 159.24,
      "high": {
        "roster_id": "3",
        "points": 247.2
      },
      "low": {
        "roster_id": "5",
        "points": 91.77
      },
      "closest": {
        "roster_ids": [
          "12",
          "7"
        ],
        "margin": 6.06
      },
      "biggest_blowout": {
        "roster_ids": [
          "1",
          "5"
        ],
        "margin": 138.19
      },
      "blowout_count": 3,
      "blowout_threshold": 0.65
    }
  },
  "matchups": {
    "this_week": [
      {
        "matchup_id": 2,
        "home_roster_id": "1",
        "away_roster_id": "5",
        "home_points": 229.96,
        "away_points": 91.77,
        "winner_roster_id": "1",
        "margin": 138.19,
        "is_blowout": true
      }
    ]
  }
}
```

Week 10 had 6 games and 1,910.90 points in total, an average of 159.24 per team. The widest margin was matchup 2, roster 1 (229.96) over roster 5 (91.77) by 138.19. Roster 5's score is below 0.65 x 229.96 = 149.47, so the game is a blowout and `biggest_blowout` names it. The league had three blowouts in all. The closest game was roster 12 over roster 7 by 6.06. The lowest score of the week, 91.77, is roster 5's, and the high is roster 3's 247.20.

## Code

Source: `commishdesk/stats/weekly.py::_week_summary`

```python
def _week_summary(target_week_rows: dict[str, Matchup]) -> WeekSummary:
    # A first-round playoff-bye row (opponent_roster_id is None) has no real
    # game this week -- its raw points must not compete for the week's
    # high/low (Epic 5's retro, S4).
    scores = [
        (roster_id, matchup.points)
        for roster_id, matchup in target_week_rows.items()
        if matchup.opponent_roster_id is not None
    ]

    # total_points / avg_team_score are the week's raw scoring, unaffected by
    # who has a real opponent -- only high/low/games use the opponent filter.
    all_points = [matchup.points for matchup in target_week_rows.values()]
    total_points = round(sum(all_points), 2)
    avg_team_score = round(total_points / len(all_points), 2) if all_points else 0.0

    high = None
    low = None
    if scores:
        best = min(scores, key=lambda kv: (-kv[1], _sort_key(kv[0])))
        worst = min(scores, key=lambda kv: (kv[1], _sort_key(kv[0])))
        high = TeamPoints(roster_id=best[0], points=best[1])
        low = TeamPoints(roster_id=worst[0], points=worst[1])

    seen_pairs: set[frozenset[str]] = set()
    games: list[tuple[str, str, float, bool]] = []  # (winner, loser, margin, is_blowout)
    for roster_id, matchup in target_week_rows.items():
        opponent_id = matchup.opponent_roster_id
        if opponent_id is None or opponent_id not in target_week_rows:
            continue
        pair = frozenset((roster_id, opponent_id))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)

        opponent_points = target_week_rows[opponent_id].points
        if matchup.points > opponent_points:
            winner, loser = roster_id, opponent_id
        elif opponent_points > matchup.points:
            winner, loser = opponent_id, roster_id
        else:
            winner, loser = sorted((roster_id, opponent_id), key=_sort_key)
        winner_points = target_week_rows[winner].points
        loser_points = target_week_rows[loser].points
        margin = round(winner_points - loser_points, 2)
        is_blowout = winner_points != loser_points and _is_blowout(winner_points, loser_points)
        games.append((winner, loser, margin, is_blowout))

    closest = None
    biggest_blowout = None
    if games:
        closest_game = min(games, key=lambda g: (g[2], _sort_key(g[0])))
        closest = WeekMargin(roster_ids=[closest_game[0], closest_game[1]], margin=closest_game[2])
        # The widest margin is only "the week's biggest blowout" when that
        # game itself clears the blowout ratio -- a lopsided-looking score in
        # an otherwise tight week is not a blowout (Epic 5's retro, S5:
        # decided as a silent gate, not a relabeled non-blowout lead).
        # Two games can tie for the widest margin while only one is an actual
        # blowout (is_blowout depends on the winner/loser ratio, not margin
        # alone) -- prefer the blowout on a margin tie so it isn't dropped by
        # an unrelated roster-id tiebreak.
        biggest_game = min(games, key=lambda g: (-g[2], not g[3], _sort_key(g[0])))
        if biggest_game[3]:
            biggest_blowout = WeekMargin(roster_ids=[biggest_game[0], biggest_game[1]], margin=biggest_game[2])

    return WeekSummary(
        games=len(games),
        total_points=total_points,
        avg_team_score=avg_team_score,
        high=high,
        low=low,
        closest=closest,
        biggest_blowout=biggest_blowout,
        blowout_count=sum(1 for *_, is_blowout in games if is_blowout),
        blowout_threshold=BLOWOUT_RATIO,
    )
```

## Edge cases

- **Bye rows.** A first-round playoff bye has no game. Its score is left out of `high`, `low` and `games`, but it still counts toward `total_points` and `avg_team_score`.
- **Wide margin without a blowout.** If the widest-margin game does not clear the ratio, `biggest_blowout` is `null`. A lopsided score in a high-scoring week is not mislabeled.
- **Margin ties.** If two games share the widest margin and only one is a blowout, the blowout is chosen. Otherwise the lower winner roster id wins.
- **Tied game.** `winner_roster_id` is `null`, the margin is 0.0 and the game is never a blowout.
- **Ties for high or low.** The lower roster id is reported.
- **Playoff weeks.** The summary and matchups describe the playoff week itself.
