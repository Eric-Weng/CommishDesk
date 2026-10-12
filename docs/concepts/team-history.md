---
doc_id: team-history
kind: stat
title: Team history
schema_min: 0.5.0
facts_keys:
  - weekly.teams[].history
fixture: facts/expected-weekly-facts-week10.json
source: commishdesk/facts/weekly.py::_history_row
---
## Definition

`history.weekly` has one row for each week from week 1 through the issue week in which the team has a matchup row. It is the trail behind the season numbers, and it is what the charts plot. Each row holds:

- `points`, `opponent_roster_id`, `result` and `margin` for that week's game;
- `cum_record`, the team's record through that week (fixed at the last regular-season week once the playoffs begin);
- `power_rank`, the model rank computed through that week (see `power-rank-and-nudge`), and `published_rank`, the rank a previous confirmed issue published for that week, if any;
- `all_play` and `luck`, the cumulative figures through that week (see `all-play` and `expected-wins-and-luck`).

The weekly `luck` is the cumulative win equivalents minus the cumulative expected wins at that point, rounded to one decimal. The season-level `luck` stays the authoritative figure, and the two can differ by at most 0.1.

## Worked example

Fixture: `tests/fixtures/facts/expected-weekly-facts-week10.json` (the anonymized league "Trench Warfare").

```json
{
  "teams": [
    {
      "roster_id": "11",
      "team_name": "Bubble-Screen Bobcats",
      "history": {
        "weekly": [
          {
            "week": 1,
            "points": 119.65,
            "opponent_roster_id": "5",
            "result": "L",
            "margin": -44.46,
            "cum_record": {
              "w": 0,
              "l": 1,
              "t": 0
            },
            "power_rank": null,
            "published_rank": null,
            "all_play": null,
            "luck": null
          },
          {
            "week": 2,
            "points": 208.38,
            "opponent_roster_id": "4",
            "result": "W",
            "margin": 0.72,
            "cum_record": {
              "w": 1,
              "l": 1,
              "t": 0
            },
            "power_rank": 6,
            "published_rank": null,
            "all_play": {
              "w": 11,
              "l": 10,
              "t": 1,
              "pct": 0.524
            },
            "luck": 0.0
          },
          {
            "week": 10,
            "points": 154.38,
            "opponent_roster_id": "8",
            "result": "L",
            "margin": -28.76,
            "cum_record": {
              "w": 2,
              "l": 8,
              "t": 0
            },
            "power_rank": 11,
            "published_rank": null,
            "all_play": {
              "w": 33,
              "l": 76,
              "t": 1,
              "pct": 0.303
            },
            "luck": -1.0
          }
        ]
      }
    }
  ]
}
```

Bubble-Screen Bobcats (roster 11) lost week 1 by 44.46 (119.65 against 164.11), so the first row is `L` with a 0-1 record, and `power_rank`, `all_play` and `luck` are all `null`, since a single week has nothing to compare. In week 2 they won by 0.72 for a 1-1 record, ranked 6th, and their cumulative all-play was 11-10-1. They had won 1 game against about 1.0 expected (the stored figure is already rounded to one decimal), so luck is 0.0. By week 10 the record is 2-8, the model rank is 11th, all-play is 33-76-1 and luck is -1.0, the same value as the season block.

## Code

Source: `commishdesk/facts/weekly.py::_history_row`

```python
def _history_row(
    rid: str,
    k: int,
    all_rows: Mapping[tuple[int, str], object],
    records_at: Mapping[int, Mapping[str, object]],
    power_at: Mapping[int, Mapping[str, int | None]],
    allplay_at: Mapping[int, Mapping[str, TeamWeekStats]],
    regular_cutoff: int,
    previous_published_ranks: Mapping[int, Mapping[str, int]] | None,
) -> WeeklyHistoryRow:
    matchup = all_rows[(k, rid)]
    points = matchup.points  # type: ignore[attr-defined]
    opponent_id = matchup.opponent_roster_id  # type: ignore[attr-defined]
    opponent_points = None
    if opponent_id is not None:
        opponent = all_rows.get((k, opponent_id))
        opponent_points = opponent.points if opponent is not None else None  # type: ignore[attr-defined]

    result = None
    margin = None
    if opponent_points is not None:
        margin = round(points - opponent_points, 2)
        if points > opponent_points:
            result = "W"
        elif points < opponent_points:
            result = "L"
        else:
            result = "T"

    # Frozen past regular_cutoff (Epic 5's retro, S3): a playoff/consolation
    # week's record never rolls into cum_record -- the history fold stays
    # pinned at the last regular-season week, same as the in_range-gated
    # fields below.
    record = records_at[min(k, regular_cutoff)].get(rid)
    cum = WeeklyRecord(
        w=record.wins if record is not None else 0,  # type: ignore[attr-defined]
        l=record.losses if record is not None else 0,  # type: ignore[attr-defined]
        t=record.ties if record is not None else 0,  # type: ignore[attr-defined]
    )

    power_rank = None
    published_rank = None
    all_play = None
    luck = None
    in_range = MEANINGFUL_FROM_WEEK <= k <= regular_cutoff
    if in_range:
        power_rank = power_at[k].get(rid)
        # Story 5B.3: the rank a prior confirmed week persisted for this roster,
        # plotted by the power-rank bump chart alongside the model trail.
        published_rank = (
            previous_published_ranks.get(k, {}).get(rid)
            if previous_published_ranks is not None
            else None
        )
        wrow = allplay_at[k].get(rid)
        if wrow is not None and wrow.all_play is not None:
            ap = wrow.all_play
            all_play = WeeklyAllPlay(w=ap.wins, l=ap.losses, t=ap.ties, pct=ap.pct)
            win_equiv = (cum.w + 0.5 * cum.t)
            if wrow.expected_wins is not None:
                luck = round(win_equiv - wrow.expected_wins, 1)

    return WeeklyHistoryRow(
        week=k,
        points=points,
        opponent_roster_id=opponent_id,
        result=result,
        margin=margin,
        cum_record=cum,
        power_rank=power_rank,
        published_rank=published_rank,
        all_play=all_play,
        luck=luck,
    )
```

## Edge cases

- **Week 1.** `power_rank`, `published_rank`, `all_play` and `luck` are `null`. They start in week 2.
- **Playoff and consolation weeks.** `points`, `opponent_roster_id`, `result` and `margin` still describe the game. `cum_record` stays pinned at the final regular-season record, and `power_rank`, `all_play` and `luck` are `null`.
- **Bye weeks.** No opponent, so `opponent_roster_id`, `result` and `margin` are `null`.
- **Published rank.** `published_rank` is only filled in for weeks where an earlier confirmed issue stored a ranking.
- **Rounding.** Row luck is the win equivalent minus the already-rounded expected wins, so a tiny shortfall such as -0.04 never appears: the week-2 row shows 0.0.
