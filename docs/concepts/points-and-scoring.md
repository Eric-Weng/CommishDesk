---
doc_id: points-and-scoring
kind: stat
title: Points and scoring
schema_min: 0.5.0
facts_keys:
  - weekly.teams[].season.points_for
  - weekly.teams[].season.points_against
  - weekly.teams[].season.avg_for
  - weekly.teams[].season.high_week
  - weekly.teams[].season.low_week
  - weekly.teams[].this_week.points
  - weekly.teams[].this_week.opponent_roster_id
  - weekly.teams[].this_week.opponent_points
  - weekly.teams[].this_week.margin
  - weekly.teams[].this_week.result
  - weekly.teams[].this_week.starters
  - weekly.teams[].this_week.top_performers
  - weekly.teams[].this_week.duds
fixture: facts/expected-weekly-facts-week10.json
source: commishdesk/facts/weekly.py::_this_week
---
## Definition

**Season totals.** `points_for` is the sum of the team's weekly scores through the last regular-season week, each week rounded to two decimals. `points_against` is the sum of its opponents' scores in the weeks it had a game. `avg_for` is `points_for` divided by the number of weeks scored. `high_week` and `low_week` name the best and worst weekly score and the week it happened; the earlier week wins a tie.

**This week.** `points` is the team's score for the issue week. `opponent_roster_id` and `opponent_points` identify the other side. `margin` is `points - opponent_points`, signed from this team's side, so a loss is negative. `result` is `W`, `L` or `T` from the same comparison. `starters` is the lineup the manager actually started, in roster-slot order, as the platform recorded it. This is not the optimal lineup, which is covered in `coaching-efficiency-and-bench-regret`.

**Top performers and duds.** `top_performers` are the team's three highest-scoring starters (`TOP_PERFORMERS = 3`). `duds` are started players who scored strictly below 6.0 points (`DUD_POINTS`), lowest first, at most three (`DUD_LIMIT`). Ties on points break by player id.

## Worked example

Fixture: `tests/fixtures/facts/expected-weekly-facts-week10.json` (the anonymized league "Trench Warfare").

```json
{
  "teams": [
    {
      "roster_id": "1",
      "team_name": "Flea Flicker Union",
      "season": {
        "points_for": 2100.85,
        "points_against": 1683.24,
        "avg_for": 210.08,
        "high_week": {
          "week": 1,
          "points": 232.86
        },
        "low_week": {
          "week": 5,
          "points": 178.99
        }
      },
      "this_week": {
        "opponent_roster_id": "5",
        "points": 229.96,
        "opponent_points": 91.77,
        "result": "W",
        "margin": 138.19,
        "top_performers": [
          {
            "player_id": "9226",
            "name": "De'Von Achane",
            "pos": "RB",
            "points": 56.52
          },
          {
            "player_id": "8112",
            "name": "Drake London",
            "pos": "WR",
            "points": 29.75
          },
          {
            "player_id": "9488",
            "name": "Jaxon Smith-Njigba",
            "pos": "WR",
            "points": 25.81
          }
        ],
        "duds": []
      }
    }
  ]
}
```

Flea Flicker Union (roster 1) scored 229.96 in week 10 against Nickel Newts (roster 5), who scored 91.77. The margin is 229.96 - 91.77 = 138.19 and the result is `W`. Its three best starters were De'Von Achane (56.52), Drake London (29.75) and Jaxon Smith-Njigba (25.81). `duds` is empty because nobody it started scored below 6.0. Across ten games the team has 2,100.85 points for, so `avg_for` is 2,100.85 / 10, about 210.08 after rounding to two decimals. Its best week was week 1 (232.86) and its worst was week 5 (178.99).

## Code

Source: `commishdesk/facts/weekly.py::_this_week`

```python
def _this_week(
    wrow: TeamWeekStats,
    lrow: TeamLineup,
    starters: list[WeeklyStarter],
    name_of: Callable[[str], str],
    players: Mapping[str, PlayerSnapshot],
) -> WeeklyTeamGame:
    game = wrow.this_week
    result = None
    margin = None
    opponent_points = None
    opponent_roster_id = None
    if game is not None:
        opponent_roster_id = game.opponent_roster_id
        opponent_points = game.opponent_points
        margin = game.margin
        if opponent_points is not None:
            if game.points > opponent_points:
                result = "W"
            elif game.points < opponent_points:
                result = "L"
            else:
                result = "T"

    # Filtered to a non-None player_id above; cast() only tells mypy what the
    # filter already guarantees, no runtime check added (facts/build.py precedent).
    ranked = [s for s in starters if s.player_id is not None]
    top_performers = [
        WeeklyPerformer(
            player_id=cast(str, s.player_id), name=s.name or cast(str, s.player_id), pos=s.pos, points=s.points
        )
        for s in sorted(ranked, key=lambda s: (-s.points, s.player_id or ""))[:TOP_PERFORMERS]
    ]
    duds = [
        WeeklyPerformer(
            player_id=cast(str, s.player_id), name=s.name or cast(str, s.player_id), pos=s.pos, points=s.points
        )
        for s in sorted(
            (s for s in ranked if s.points < DUD_POINTS), key=lambda s: (s.points, s.player_id or "")
        )[:DUD_LIMIT]
    ]

    bench_regret = None
    if lrow.best_benched is not None:
        pid = lrow.best_benched.player_id
        bench_regret = WeeklyBenchRegret(
            player_id=pid, name=name_of(pid), pos=_pos(players, pid), points=lrow.best_benched.points
        )

    started_on_bye = None
    if lrow.started_on_bye is not None:
        started_on_bye = [
            WeeklyByeStarter(
                player_id=entry.player_id,
                name=name_of(entry.player_id),
                pos=_pos(players, entry.player_id),
                nfl_team=entry.nfl_team,
                points=entry.points,
            )
            for entry in lrow.started_on_bye
        ]

    return WeeklyTeamGame(
        opponent_roster_id=opponent_roster_id,
        points=game.points if game is not None else 0.0,
        opponent_points=opponent_points,
        result=result,
        margin=margin,
        coaching_efficiency=_coaching(lrow.actual, lrow.optimal),
        points_left_on_bench=lrow.points_left_on_bench,
        starters=starters,
        top_performers=top_performers,
        duds=duds,
        bench_regret=bench_regret,
        started_on_bye=started_on_bye,
    )
```

## Edge cases

- **Bye or no game.** `opponent_roster_id` is `null`, and so are `opponent_points`, `margin` and `result`. A team with no matchup row for the issue week has no `this_week` block at all (`null`). Bye weeks still count toward `points_for`, but they add nothing to `points_against`.
- **Tied score.** `margin` is 0.0 and `result` is `T`.
- **Empty slot.** A roster slot with no player appears in `starters` with a `null` player. It is left out of `top_performers` and `duds`.
- **Dud threshold.** A starter at exactly 6.0 is not a dud.
- **Frozen season totals.** From the first playoff week on, the season totals stay at their final regular-season values. `this_week` keeps following the issue week.
- **Played-through weeks.** `avg_for` divides by weeks with a score, so a team that missed a week is not penalised for it.
