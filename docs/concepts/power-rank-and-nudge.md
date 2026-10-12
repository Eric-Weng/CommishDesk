---
doc_id: power-rank-and-nudge
kind: stat
title: Power rank and nudge
schema_min: 0.5.0
facts_keys:
  - weekly.teams[].season.power
fixture: facts/expected-weekly-facts-week10-published.json
source: commishdesk/stats/power.py::compute_power_ranks
---
## Definition

**Model score.** The model power score is a weighted sum of three league-relative components, each rescaled to the range 0 to 1:

- `all_play_pct`, weight 0.45: the decisive all-play win rate (see `all-play`);
- `normalized_avg_pf`, weight 0.35: average points for, min-max scaled across the league (0 when every team averages the same);
- `win_pct`, weight 0.20: head-to-head win percentage.

`model_score` is shown to three decimals, but `model_rank` is assigned on the unrounded score. Equal scores break by points for, then roster id. `prev_model_rank` is the rank one week earlier, and `week_delta` is `prev_model_rank - model_rank`, so a positive number means the team rose.

**Published rank and nudge.** The narrator may publish its own ranking, as long as no team sits more than 2 places from its model rank (`POWER_NUDGE_CAP`), and every deviation carries a justification that cites numbers present in the facts. `published_rank` is that ranking. `nudge` is `model_rank - published_rank`, so a positive nudge means the desk placed the team higher than the model did. `prev_published_rank` is the most recent earlier published rank, and `published_week_delta` is `prev_published_rank - model_rank`. Both are `null` until a ranking has been published.

## Worked example

Fixture: `tests/fixtures/facts/expected-weekly-facts-week10-published.json` (the anonymized league "Trench Warfare").

```json
{
  "teams": [
    {
      "roster_id": "4",
      "team_name": "Chip-Block Chinchillas",
      "season": {
        "power": {
          "model_score": 0.433,
          "model_rank": 7,
          "published_rank": 8,
          "nudge": -1,
          "prev_model_rank": 9,
          "week_delta": 2,
          "prev_published_rank": 6,
          "published_week_delta": -1
        }
      }
    },
    {
      "roster_id": "5",
      "team_name": "Nickel Newts",
      "season": {
        "power": {
          "model_score": 0.371,
          "model_rank": 8,
          "published_rank": 7,
          "nudge": 1,
          "prev_model_rank": 6,
          "week_delta": -2,
          "prev_published_rank": 9,
          "published_week_delta": 1
        }
      }
    }
  ]
}
```

This example uses the published variant of the week-10 fixture, in which the desk swapped two teams. Chip-Block Chinchillas (roster 4) have a model score of 0.433 and a model rank of 7, up two places from 9 the week before (`week_delta` 2). Nickel Newts (roster 5) score 0.371 and rank 8, down two from 6. The desk published roster 5 at 7 and roster 4 at 8, a one-place swap inside the cap of 2. The nudge is `model_rank - published_rank`: 7 - 8 = -1 for roster 4 (placed lower than the model) and 8 - 7 = +1 for roster 5 (placed higher). Roster 4's previous published rank was 6, so its `published_week_delta` is 6 - 7 = -1. The written justification for the swap lives in the narration of the document, not here.

## Code

Source: `commishdesk/stats/power.py::compute_power_ranks`

```python
def compute_power_ranks(week: WeekModel, *, through_week: int | None = None) -> PowerRanks:
    """Score and rank every roster through ``through_week``.

    ``through_week`` is clamped to ``1..cutoff`` (``cutoff`` is
    ``stats/weekly.py``'s own ``_all_play_cutoff``); ``None`` means the whole
    regular season folded so far. Pure, deterministic, offline: two calls on
    equal inputs return an equal ``model_dump()``. Never raises."""
    cutoff = max(_all_play_cutoff(week), 0)
    if through_week is None or cutoff < MEANINGFUL_FROM_WEEK:
        effective = cutoff
    else:
        effective = min(max(through_week, 1), cutoff)

    roster_ids = sorted((roster.roster_id for roster in week.rosters), key=_sort_key)

    if effective < MEANINGFUL_FROM_WEEK:
        return PowerRanks(week=effective, teams=[_unranked(roster_id) for roster_id in roster_ids])

    # Reuse the weekly module's own all-play fold rather than reimplementing it:
    # a copy scoped to ``effective`` with no playoff_week_start makes
    # ``_all_play_cutoff`` return exactly ``effective``.
    scoped = week.model_copy(update={"week": effective, "playoff_week_start": None})
    weekly = compute_weekly_stats(scoped)
    records = regular_season_records(week, effective)

    components: dict[str, tuple[float, float, float]] = {}
    points_for: dict[str, float] = {}
    for team_stats in weekly.teams:
        roster_id = team_stats.roster_id
        record = records.get(roster_id)
        if record is None or not record.week_points:
            continue
        all_play = team_stats.all_play
        decisive = (all_play.wins + all_play.losses) if all_play is not None else 0
        all_play_pct = (all_play.wins / decisive) if all_play is not None and decisive else 0.0
        games = record.wins + record.losses + record.ties
        win_pct = ((record.wins + 0.5 * record.ties) / games) if games else 0.0
        components[roster_id] = (all_play_pct, win_pct, record.points_for / effective)
        points_for[roster_id] = record.points_for

    averages = [avg_pf for _, _, avg_pf in components.values()]
    low = min(averages) if averages else 0.0
    high = max(averages) if averages else 0.0

    scored: list[tuple[str, float]] = []
    for roster_id, (all_play_pct, win_pct, avg_pf) in components.items():
        normalized = (avg_pf - low) / (high - low) if high > low else 0.0
        score = (
            POWER_WEIGHTS["all_play_pct"] * all_play_pct
            + POWER_WEIGHTS["normalized_avg_pf"] * normalized
            + POWER_WEIGHTS["win_pct"] * win_pct
        )
        scored.append((roster_id, score))

    # Rank on the unrounded score; break an exact tie by points-for, then id.
    scored.sort(key=lambda item: (-item[1], -points_for[item[0]], _sort_key(item[0])))
    rank_of = {roster_id: index + 1 for index, (roster_id, _) in enumerate(scored)}
    score_of = dict(scored)

    teams: list[TeamPower] = []
    for roster_id in roster_ids:
        if roster_id not in rank_of:
            teams.append(_unranked(roster_id))
            continue
        all_play_pct, win_pct, avg_pf = components[roster_id]
        normalized = (avg_pf - low) / (high - low) if high > low else 0.0
        teams.append(
            TeamPower(
                roster_id=roster_id,
                model_score=round(score_of[roster_id], 3),
                model_rank=rank_of[roster_id],
                all_play_pct=round(all_play_pct, 3),
                normalized_avg_pf=round(normalized, 3),
                win_pct=round(win_pct, 3),
            )
        )

    return PowerRanks(week=effective, teams=teams)
```

## Edge cases

- **Cold start.** Before week 2 there is no all-play, so `model_score`, `model_rank` and `week_delta` are `null`. A team that has not played a game is unranked for the same reason.
- **Playoff weeks.** The model rank is computed on the regular season only, so it stays at the last regular-season week.
- **Unrounded ties.** Two teams can both display 0.193 and still hold different ranks, because ranking uses the unrounded score.
- **Cap.** A published rank more than 2 places from the model rank is rejected by a check. The narrator is asked to regenerate once and then the issue falls back to the template narrator.
- **Gaps in the published trail.** `published_week_delta` is `null` unless the immediately preceding week has a published rank for the team. `prev_published_rank` walks further back, so a held week is a gap and is never interpolated.
- **No published layer.** `published_rank` and `nudge` are `null` in a document built before the narrator has chosen a ranking, which is the case in the base week-10 fixture.
