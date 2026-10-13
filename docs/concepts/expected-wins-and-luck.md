---
doc_id: expected-wins-and-luck
kind: stat
title: Expected wins and luck
schema_min: 0.5.0
facts_keys:
  - weekly.teams[].season.expected_wins
  - weekly.teams[].season.luck
fixture: facts/expected-weekly-facts-week10.json
source: commishdesk/stats/weekly.py::compute_weekly_stats
---
## Definition

**Expected wins** is the number of games a team would have won if its scores had been played against the whole league each week. For each week it takes the share of that week's all-play comparisons the team won (a tie counts half), then adds those shares across the weeks. Over a season with a steady number of teams and no ties this equals `all_play.pct` times the number of weeks, and the weekly share form also holds when a bye changes the field size.

**Luck** is wins actually earned minus expected wins: `luck = (wins + 0.5 * ties) - expected_wins`. The win count is the team's record as the platform reports it. A positive number means the team won more games than its scores deserved, and a negative number means fewer. Both numbers are rounded to one decimal, and `luck` is computed from the unrounded expected wins, so the displayed values can differ in the last digit from a subtraction of the rounded figures.

## Worked example

Fixture: `tests/fixtures/facts/expected-weekly-facts-week10.json` (the anonymized league "Trench Warfare").

```json
{
  "teams": [
    {
      "roster_id": "9",
      "team_name": "No-Huddle Narwhals",
      "season": {
        "record": {
          "w": 6,
          "l": 4,
          "t": 0
        },
        "all_play": {
          "w": 86,
          "l": 24,
          "t": 0,
          "pct": 0.782
        },
        "expected_wins": 7.8,
        "luck": -1.8
      }
    },
    {
      "roster_id": "12",
      "team_name": "Yardage Yaks",
      "season": {
        "record": {
          "w": 5,
          "l": 5,
          "t": 0
        },
        "all_play": {
          "w": 28,
          "l": 82,
          "t": 0,
          "pct": 0.255
        },
        "expected_wins": 2.5,
        "luck": 2.5
      }
    }
  ]
}
```

Yardage Yaks (roster 12) are 5-5 with an all-play record of 28-82, so they won 28 of 110 comparisons. Their expected wins are about 28 / 110 x 10 weeks = 2.5, and their luck is 5 - 2.5 = +2.5. They have been the luckiest team in the league. No-Huddle Narwhals (roster 9) are 6-4 with an all-play record of 86-24. They scored well enough to be expected to win 7.8 games, and they won 6, so their luck is 6 - 7.8 = -1.8, the worst in the league.

## Code

Source: `commishdesk/stats/weekly.py::compute_weekly_stats`

```python
def compute_weekly_stats(week: WeekModel) -> WeeklyStats:
    """Compute all-play, expected wins, luck, and blowout flags for one
    league-week.

    Pure, deterministic, offline. A valid :class:`~commishdesk.ingest.WeekModel`
    in, a :class:`WeeklyStats` out; two calls on one input return an equal
    ``model_dump()``. Never raises."""
    period = _period(week)
    matchups_by_week = _matchups_by_week(week)
    target_week_rows = matchups_by_week.get(week.week, {})

    all_play_wins: dict[str, int] = {}
    all_play_losses: dict[str, int] = {}
    all_play_ties: dict[str, int] = {}
    expected_wins_acc: dict[str, float] = {}
    has_meaningful_all_play = period.has_prior_week and _all_play_cutoff(week) >= 1
    if has_meaningful_all_play:
        all_play_wins, all_play_losses, all_play_ties, expected_wins_acc = _all_play_totals(
            matchups_by_week, _all_play_cutoff(week)
        )

    teams: list[TeamWeekStats] = []
    for roster in sorted(week.rosters, key=lambda r: _sort_key(r.roster_id)):
        roster_id = roster.roster_id
        matchup = target_week_rows.get(roster_id)
        this_week = _team_game(matchup, target_week_rows) if matchup is not None else None

        all_play: AllPlayRecord | None = None
        expected_wins: float | None = None
        luck: float | None = None
        if has_meaningful_all_play:
            wins = all_play_wins.get(roster_id, 0)
            losses = all_play_losses.get(roster_id, 0)
            ties = all_play_ties.get(roster_id, 0)
            pct = round(wins / (wins + losses), 3) if (wins + losses) else 0.0
            all_play = AllPlayRecord(wins=wins, losses=losses, ties=ties, pct=pct)
            raw_expected_wins = expected_wins_acc.get(roster_id, 0.0)
            expected_wins = round(raw_expected_wins, 1)
            actual_wins_equivalent = roster.wins + 0.5 * roster.ties
            luck = round(actual_wins_equivalent - raw_expected_wins, 1)

        teams.append(
            TeamWeekStats(
                roster_id=roster_id,
                wins=roster.wins,
                losses=roster.losses,
                ties=roster.ties,
                this_week=this_week,
                all_play=all_play,
                expected_wins=expected_wins,
                luck=luck,
                bracket=_bracket_for(roster_id, week, period),
            )
        )

    return WeeklyStats(period=period, summary=_week_summary(target_week_rows), teams=teams)
```

## Edge cases

- **Week 1.** There is no earlier week to measure against, so `expected_wins` and `luck` are `null`, never 0.
- **Playoff weeks.** Both values stay at their last regular-season figure.
- **Ties.** A real tie counts half a win in the record term, and an all-play tie counts half a win in the expected term.
- **Byes in the regular season.** Each week's share is wins earned divided by that week's decisions, so a smaller field does not skew the total.
- **Platform record.** The record is read as the platform reports it and is never recomputed from matchup pairs. If the platform folds median-scoring wins into the record, luck reflects them.
- **History rows.** `teams[].history.weekly[].luck` is recomputed for each earlier week from the cumulative record at that time. It can differ from the season value by at most 0.1. The season value is the authoritative one. See `team-history`.
