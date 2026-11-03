---
doc_id: playoff-odds
kind: stat
title: Playoff odds
schema_min: 0.16.0
facts_keys:
  - weekly.playoff_odds
fixture: playoff_odds/worked-example.json
source: commishdesk/statmods/playoff_odds.py::_team_models
---
## Definition

Playoff odds answer one question for every team: out of 10,000 ways the rest of the regular season could play out, in how many does this team make the playoffs? The answer is a number from 0 to 1 with four decimal places, so 0.7283 means 7,283 of 10,000 simulated seasons.

**What is simulated.** Every game still to be played is in the Facts JSON (`matchups.remaining`), so the engine plays each one 10,000 times. A season starts from the real records and the real points scored so far. Each simulated game draws a score for each team, and the higher score wins.

**How a team's score is drawn.** The method belongs to this stat alone, and it is deliberately plain.

- A team's expected score is its average so far, pulled toward the league average as if it had also played three games at exactly that average. A team that has played only a few games is pulled harder, and a hot streak does not run away with the odds.
- The spread of a score around that expectation is one number for the whole league: how far teams typically land from their own average. Every team uses the same spread.
- Nothing else is used. Not rosters, injuries, byes, or last season. The odds are about the standings and how teams have scored.

**Who makes it.** After the last game, teams are sorted by wins, then by total points scored (the same tiebreak as the standings). The top `bracket_teams` make the playoffs, and the top `byes` of those get a first-round bye. Because exactly that many teams get in each simulated season, the make-playoffs odds of all teams add up to the bracket size.

**Division odds.** Where the league declares divisions, `division` is the share of seasons in which the team finishes first in its division, by the same wins-then-points order. A division winner is not guaranteed a place in the bracket here; the bracket is the top teams overall.

**Bye odds.** `bye` appears only when the league has first-round byes.

**Seeded and repeatable.** The simulation is seeded from the league id, the week and the module version, so building the same week again gives byte-identical numbers.

**Clinched and eliminated.** A team that the standings already show as clinched (or clinched a bye) is held in that position in every simulated season, a team that clinched its division is held as that division's winner, and an eliminated team is held out. So a team that clinched a playoff spot reads exactly 1.0, an eliminated team exactly 0.0, and the odds can never disagree with the clinch and elimination flags on the next-week cards. `status` carries those words (`clinched`, `eliminated`, or `in_race`), because an Issue says "clinched", not "100%".

**Commissioner brackets.** When the commissioner has set the playoff field, the odds do not guess it: listed teams read 1.0 to make the playoffs and the rest read 0.0, and the same goes for listed byes.

**Week over week.** Each team's `change` is how many percentage points its make-playoffs odds moved since the previous week's issue, for example 4.5 for 0.3248 against 0.2798. It is left out in the first week the odds exist, or when last week's issue cannot be read. The Issue's header names the biggest mover (the largest move, a tie going to the lower roster id) and says nothing when no team moved a full point.

**How the Issue shows them.** The web Issue (and the league page, which is the same page) lists every team, ranked by make-playoffs odds, with a bar and the percent as text. A Bye column appears only when the league has byes and a Division column only when it has divisions. A team that has clinched or is eliminated shows that word in a pill, with no bar or percent. A live team under one half of a percent reads "<1%", never 0%. The email shows the top six teams (four on a phone) plus the biggest mover and links to the full list; plain text and Discord show every team up to twelve, and past twelve teams the top eight and "and N more". The header reads "Top 6 of 12 make it. Top 2 get a bye." Colour never carries the odds: every row also has an accessible name that reads its numbers. The narrator is given whole percents only, so a printed "43%" is always a number the engine produced.

**When there are no odds.** The block is absent, never zero, when the league has no playoff format, before the first game has been played, after the regular season ends, or when the remaining schedule could not be fully fetched. Week 1 is a cold start, so no surface shows odds there.

## Worked example

Fixture: `tests/fixtures/playoff_odds/worked-example.json`. A twelve-team league after week 10 with divisions, six playoff spots and two byes. Roster 4 is 4-6 and roster 5 is 6-4, both in division 1.

```json
{
  "playoff_odds": {
    "simulations": 10000,
    "teams": [
      {
        "roster_id": "4",
        "make_playoffs": 0.3248,
        "status": "in_race",
        "bye": 0.0,
        "division": 0.0085
      },
      {
        "roster_id": "5",
        "make_playoffs": 0.7283,
        "status": "in_race",
        "bye": 0.0036,
        "division": 0.0852
      }
    ]
  }
}
```

Roster 5 made the playoffs in 7,283 of the 10,000 simulated seasons, so its odds are 0.7283. Roster 4, at 4-6 with four to play, made it in 3,248 (0.3248). A first-round bye is a long shot for roster 5 (36 seasons) and effectively out of reach for roster 4 (none). Division 1 is led by another team, so roster 5 wins it in 852 seasons and roster 4 in 85. Across all twelve teams the make-playoffs odds add up to 6, the size of the bracket.

## Code

Source: `commishdesk/statmods/playoff_odds.py::_team_models`

```python
def _team_models(
    histories: Mapping[str, Sequence[float]],
) -> tuple[dict[str, float], float]:
    """Each team's expected score and the one league-pooled spread.

    A team's mean is pulled toward the league mean as if it had also played ``PRIOR_GAMES``
    games at the league average, so a hot week or two does not run away with the odds."""
    scores = [x for xs in histories.values() for x in xs]
    league_mean = sum(scores) / len(scores)
    squares, dof = 0.0, 0
    for xs in histories.values():
        if len(xs) > 1:
            mean = sum(xs) / len(xs)
            squares += sum((x - mean) ** 2 for x in xs)
            dof += len(xs) - 1
    if dof:
        spread = math.sqrt(squares / dof)
    else:  # one game each: the spread across teams is the only evidence there is
        spread = math.sqrt(sum((x - league_mean) ** 2 for x in scores) / len(scores))
    if spread <= 0:
        spread = max(1.0, 0.2 * abs(league_mean))
    means = {
        rid: (sum(xs) + PRIOR_GAMES * league_mean) / (len(xs) + PRIOR_GAMES) if xs else league_mean
        for rid, xs in histories.items()
    }
    return means, spread
```

## Edge cases

- **Odd or unpaired rosters.** A roster in no game some week plays fewer games, and the simulation still runs.
- **Ties in the standings.** A tie counts as half a win. Teams level on wins are ordered by points scored.
- **Few games played.** After one game each there is no spread within a team to measure, so the spread across teams is used instead.
- **Absent block.** No playoff format, no game played yet, the regular season over, or an incomplete schedule: the `playoff_odds` key is not in the Facts JSON at all.
- **Flags win.** A clinched or eliminated flag always decides the team's make-playoffs odds, whatever the simulation would say.
- **Not a promise.** The odds describe the standings and scoring so far. A team can read 0.0 without being flagged eliminated when it cannot realistically get in but the standings cannot yet prove it.
