---
doc_id: history-callbacks
kind: stat
title: History callbacks
schema_min: 0.17.0
facts_keys:
  - weekly.history
fixture: facts/expected-weekly-facts-week10.json
source: commishdesk/stats/history_callbacks.py::_h2h
---
## Definition

`history` turns a league's earlier Sleeper seasons, plus this season through the issue week, into the angles a league talks about: who leads a rivalry, which records fell, and who is doing something for the first time in a while. It is a closed-world block: every callback is a Facts key that names the seasons and weeks it comes from (`sources`), so nothing is said that the Facts do not carry. It is `null` when no history was supplied or history is switched off, and the Issue renders either way.

**Who counts.** Managers are Sleeper user ids (a roster's `owner_id`), never roster ids. A game credits the user who owned the roster *that season*, so when a roster changes hands the new owner starts a fresh record and never inherits the previous owner's games. An unowned roster counts for no one: it is never half of a series and never holds a record. Median wins are not games and never count toward a head-to-head.

**Seasons on record.** The earlier seasons that loaded plus this one (`seasons_covered`, `seasons_on_record`). With two or more, `all_time` is true and `wording` is `all_time`, or `seasons_on_record` when the chain was cut short (`partial`), which is worded "in N seasons on record". With one season on record only this-season callbacks exist (season head-to-head and season highs), `all_time` is false, `wording` is `this_season`, and there are no first-time events. Nothing is guessed for a missing season.

**Head-to-head.** `matchups` has one entry for each pairing this week (`this_week`, with the result) and next week (`next_week`); `a_roster_id` is the lower roster id. `h2h` holds the regular-season record (`a_wins`, `b_wins`, `ties`), the playoff record apart (`playoff_*`), `meetings` (both together), the seasons they met, the `last_meeting` before the game in question (season, week, points) and the current `streak` (who won the last `count` meetings). `h2h` is `null` when the two managers have never met (never "0-0"), or a side is unowned. Consolation games are not counted.

**Records.** `records` lists the five league records over seasons on record, regular season only: highest score, lowest score, biggest margin, longest win streak and longest loss streak (a streak runs across seasons; a bye neither extends nor breaks it, a tie breaks it). Each names its earliest holder, season and week. This week can *set* a record (strictly beat every earlier game on record), *tie* it, or leave a streak *within reach* (the current run is exactly one short of the record). Nothing is set when no earlier game is on record, and a streak record needs at least 3 games.

**First-time events.** Only with two or more seasons: `first_win_ever` (the first win over this opponent after earlier meetings that were all losses or ties), `first_win_since` (the first win after earlier wins, the last in `since_season`, and meetings since that were not wins), and `first_top_ever` / `first_top_since` (a manager newly first in the standings who has never, or not since `since_season`, finished a regular season on top).

**What is kept.** At most one callback per pairing (`matchups[].callback`) and three league lines (`league_lines`). Candidates rank by tier (records set or tied, then first-time events, then a head-to-head streak of at least 3 straight meetings or a streak within reach, then a series lead), then the most recent cited season first, then roster id, then kind and metric. No randomness and no clock, so the same input gives the same lines.

## Worked example

Fixture: `tests/fixtures/facts/expected-weekly-facts-week10.json` (the anonymized league "Trench Warfare", one season on record).

```json
{
  "history": {
    "all_time": false,
    "wording": "this_season",
    "seasons_covered": [
      2025
    ],
    "seasons_on_record": 1,
    "partial": false,
    "records": [
      {
        "metric": "biggest_margin",
        "value": 138.19,
        "manager_id": "id_s9a3queopr",
        "roster_id": "1",
        "season": 2025,
        "week": 10
      }
    ],
    "matchups": [
      {
        "when": "this_week",
        "week": 10,
        "a_roster_id": "1",
        "b_roster_id": "5",
        "a_points": 229.96,
        "b_points": 91.77,
        "winner_roster_id": "1",
        "h2h": {
          "a_wins": 1,
          "b_wins": 0,
          "ties": 0,
          "meetings": 1,
          "seasons": [
            2025
          ],
          "last_meeting": null
        },
        "callback": {
          "kind": "record_set",
          "tier": 0,
          "all_time": false,
          "wording": "this_season",
          "roster_id": "1",
          "metric": "biggest_margin",
          "value": 138.19,
          "record": {
            "metric": "biggest_margin",
            "value": 104.48,
            "roster_id": "10",
            "season": 2025,
            "week": 1
          },
          "sources": [
            {
              "season": 2025,
              "weeks": [
                1,
                10
              ]
            }
          ]
        }
      }
    ]
  }
}
```

The fixture holds one season, so `all_time` is false and `wording` is `this_season`. In week 10 roster 1 beat roster 5, 229.96 to 91.77, a margin of 138.19. The earlier biggest margin was 104.48, set by roster 10 in week 1, so the pairing's one kept callback is `record_set` on `biggest_margin` with tier 0, citing weeks 1 and 10 of 2025. The pairing's series is 1-0 over one meeting this season, and `last_meeting` is `null` because this week's game is the only meeting. `records` shows the margin record as it stands after the week, now held by roster 1.

## Code

Source: `commishdesk/stats/history_callbacks.py::_h2h`

```python
def _h2h(series: list[_Game], a: str, b: str, ma: str, mb: str, now_last: Pos | None) -> HistoryH2H | None:
    """The series of managers ``ma`` (roster *a*) and ``mb`` (roster *b*); ``None`` with no meeting."""
    if not series:
        return None
    mine = {"regular": [0, 0, 0], "playoff": [0, 0, 0]}
    for g in series:
        a_pts, b_pts = (g.pa, g.pb) if g.ma == ma else (g.pb, g.pa)
        slot = 0 if a_pts > b_pts else 1 if b_pts > a_pts else 2
        mine[g.kind][slot] += 1
    run_mgr, count = None, 0
    for g in reversed(series):
        w = g.winner_manager()
        if w is None or (run_mgr is not None and w != run_mgr):
            break
        run_mgr, count = w, count + 1
    streak = (
        HistorySeriesStreak(roster_id=a if run_mgr == ma else b, manager_id=run_mgr, count=count) if run_mgr else None
    )
    earlier = [g for g in series if now_last is None or g.pos < now_last]
    last = earlier[-1] if earlier else None
    meeting = None
    if last is not None:
        a_pts, b_pts = (last.pa, last.pb) if last.ma == ma else (last.pb, last.pa)
        meeting = HistoryMeeting(season=last.season, week=last.week, kind=last.kind, a_points=a_pts, b_points=b_pts)
    return HistoryH2H(
        a_manager_id=ma,
        b_manager_id=mb,
        a_wins=mine["regular"][0],
        b_wins=mine["regular"][1],
        ties=mine["regular"][2],
        playoff_a_wins=mine["playoff"][0],
        playoff_b_wins=mine["playoff"][1],
        playoff_ties=mine["playoff"][2],
        meetings=len(series),
        seasons=sorted({g.season for g in series}),
        last_meeting=meeting,
        streak=streak,
        sources=_cites(g.pos for g in series),
    )
```

## Edge cases

- **One season on record.** Season head-to-head and season highs only; `all_time` is false; no first-time events; the words "all-time" never appear. A pairing that has not met this season has `h2h: null`.
- **Partial chain.** `partial` is true with `partial_reason`, and callbacks use `wording: seasons_on_record` ("in N seasons on record").
- **Owner change.** A roster that changed owner counts for each owner only from the season they owned it.
- **Unowned roster.** No series, no record, no callback for that roster.
- **Playoff meetings.** Counted in `playoff_*` only, and in `meetings`; they never move the regular-season record. A playoff-week game this season counts only when both rosters are in the winners bracket; a consolation game is never counted.
- **Tied record.** A score equal to the record is `record_tied` and cites the record's season and week; a streak equal to the record likewise.
- **Within reach.** A current win or loss streak exactly one short of a record of at least 3 games is `record_within_reach`, citing the record held.
- **No earlier games.** Nothing is "set" or "tied" in a league's very first week on record.
- **Many candidates.** Six pairings and eight league lines still give at most one callback each and three league lines, ranked as described above.
- **No history.** `history` is `null` and the Issue renders as before.
