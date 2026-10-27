---
doc_id: league-value
kind: guide
title: League Value
schema_min: 0.10.0
---
## What League Value is

League Value is one number per player: how many points per week he is worth over a replacement-level player, in this league's own scoring and this league's lineup. A running back who scores 24 a week in a league that pays for catches is worth more in that league than in one that does not, and League Value shows it.

It is computed from open data, by the engine, with no outside rankings. Trade grades and draft boards read it. It is not a key in the Facts JSON. It lives in a snapshot that the engine saves for each league-week, and the stats read that snapshot as a plain table. Re-issuing an Issue reads the saved snapshot, so the same week always gives the same numbers.

## How a value is made

1. **Points per week.** For every week a player was on a roster in your league, the number is what Sleeper scored him in your league. For any other week, the engine takes his real stats from nflverse and scores them with your league's own scoring settings. Kickers, defenses and individual defensive players come from your league's Sleeper points only. A player with no points at all is left out. He is never shown as zero.
2. **Blend in last season.** This season's average is mixed with last season's, as if last season were 4 extra games: `(points this season + 4 × last season's average) / (games + 4)`. Last season counts only if the player had at least 4 games. Last season comes from nflverse only.
3. **Replacement level.** The engine fills every team's starting lineup, best players first, using your league's own slots, flex and superflex slots included. The last starter at each position is "replacement". Anyone below him is not worth a starting spot.
4. **Value.** `value = max(0, points per week − replacement at his position)`. It is in points per week. A player can be worth nothing, but never less.
5. **Rookies before they play.** A player with fewer than 2 games this season and no last season to lean on is given the value of the player at the same position rank by Sleeper's search rank. This is an estimate, and it is marked **est.**
6. **Future value (dynasty leagues only).** This season's value is carried over the next 3 seasons on an age curve for the position, each season counting 15 percent less than the one before. The age curve is an estimate, and so is Future value, marked **est.**
7. **Draft picks (dynasty leagues only).** A pick is worth the rookie who would go at that spot. Next season's picks are sorted into early, mid and late by where the pick's original owner sits in the standings now (the worst third picks early). Picks two or more seasons out have no tier and lose 20 percent of their value for each extra year. All of it is marked **est.**

## Worked example

This example is from the test league: 3 teams, PPR-style scoring with a 0.5 bonus per tight end catch, and a lineup of 1 QB, 2 RB, 2 WR, 1 TE, 1 FLEX, 1 K and 1 DEF. It is week 4.

- Running back 200 scored 21.0, 19.3, 27.6 and 21.9 in your league, 89.8 in all. Last season he averaged 25.7 over 17 games. Blended: (89.8 + 4 × 25.7) / (4 + 4) = **24.075** points per week.
- The replacement levels from the lineup fill are QB 20.629, RB 10.350, WR 8.667 and TE 7.881 points per week.
- Running back 200's value is 24.075 − 10.350 = **13.725** points per week.
- Running back 202 had only 3 games last season, so last season does not count. His average of **18.350** stands on its own, and his value is **8.000**.
- Wide receiver 300 joined the league in week 3. His first two weeks are scored from nflverse stats with the league's settings. He averages **16.365** and is worth **7.699**.
- Running back 900 is a rookie with one game and no history. He is the second running back in the pool by Sleeper's search rank, so he takes the value of the second-best running back, **13.307**, marked est.
- In a dynasty league, running back 200 (30 years old on September 1) has a Future value of 14.087, marked est.

## The constants

| Name | Value | What it does |
| --- | --- | --- |
| Prior games (K) | 4 | Last season counts as this many games in the blend. |
| Prior minimum games | 4 | Last season is ignored with fewer games than this. |
| Rookie games | 2 | Fewer games than this, and no last season, means a rookie. |
| Future horizon | 3 seasons | How far Future value looks ahead. |
| Future discount | 0.85 a season | Each carried season counts this much of the one before. |
| Pick discount | 0.80 a season | Applied to each season beyond next season's picks. |
| Pick tail decay | 0.90 a slot | Slots deeper than the rookie pool shrink by this much each. |
| Age curves | by position | Year-on-year change in production by age, in `commishdesk/league_value.py` (`AGE_CURVES`). |

These are estimates, not measurements, and changing any of them changes the method version, which is part of the snapshot's save key.

## What it cannot see

- **Play-length bonuses.** nflverse stats do not say how long a play was, so a league that pays a bonus for a 40-yard touchdown or a 20-yard catch is scored without that bonus for weeks the player was not on a roster in your league. Weeks he was rostered use Sleeper's own number, which includes it. The snapshot lists the scoring keys it could not express. In the spike run on a real league the gap averaged 0.8 points a week on a mean of 12.9.
- **Kickers, defenses and defensive players** are valued only from the weeks they were on a roster in your league. A player nobody ever rostered has no value.
- **The pool is your league's players.** The engine values the players that appear on your league's rosters, not the whole NFL. Replacement level is set by the players your league has.
- **Rookies before they play, Future value and draft picks** are estimates. The rookie class used to price next year's picks is this year's class.
- **Id matching.** Players are matched to nflverse by their gsis id, then their ESPN id, then name and birth date, then a name and position that is unique. A player who matches none of them is valued from your league's points alone. The engine logs how many rostered players matched.
- **Sample.** The onboarding sample cannot reach nflverse. It builds a "Sleeper data only" snapshot from your league's points and Sleeper's search rank alone, with no last season, and says so.
- **A failed download** falls back to the same "Sleeper data only" snapshot for that week. The Issue is never held up.

## Where the data comes from

The stats are from [nflverse](https://github.com/nflverse/nflverse-data), published under the [Creative Commons Attribution 4.0 licence](https://creativecommons.org/licenses/by/4.0/) (CC BY 4.0). The engine reads two release files, the weekly player stats and the players table, once per season-week (last season's stats once per season), and saves what it needs. Only numbers and ids are used. Names in the nflverse files are used only to match players and are kept only in the engine's private cache and never appear in a snapshot or an Issue. Every place that shows a League Value number credits nflverse.
