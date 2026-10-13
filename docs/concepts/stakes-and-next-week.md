---
doc_id: stakes-and-next-week
kind: stat
title: Stakes and next week
schema_min: 0.5.0
facts_keys:
  - weekly.matchups.next_week
  - weekly.teams[].next_week.opponent_roster_id
  - weekly.teams[].next_week.power_rank_opp
  - weekly.teams[].next_week.power_rank_self
  - weekly.teams[].next_week.stakes
fixture: facts/expected-weekly-facts-week10.json
source: commishdesk/stats/stakes.py::_outlooks
---
## Definition

Next week's preview pairs the teams that play each other and says what each game is worth. Each pairing is a card with an `a` side and a `b` side. A card carries both records, both model power ranks, the stakes tags, and the `game_of_week` flag for the single most important card. A card's `bye_impact` list names starters on an NFL bye next week. It is described in `byes`. Each team's own `next_week` block repeats its opponent, both power ranks and the stakes for its card.

**Outlook rules.** Everything is counted in win equivalents. `cur` is `wins + 0.5 * ties` already banked, and `max` is `cur` plus the regular-season games every team still has. With `N` playoff teams and `B` byes:

- `eliminated`: at least `N` other teams have a `cur` strictly above this team's `max`;
- `clinched_playoff`: at most `N - 1` other teams could still reach this team's `cur`;
- `clinched_bye`: the same test with `B - 1`;
- `clinched_division`: no division rival's `max` reaches this team's `cur`.

These tests are conservative: tiebreaks are ignored, so a flag is only set when no outcome can change it.

**Stakes tags** are the sorted union of both sides' tags: `bye_seed`, `wildcard_race`, `division_race`, `draft_position` (eliminated) and `elimination` (a loss would eliminate).

## Worked example

Fixture: `tests/fixtures/facts/expected-weekly-facts-week10.json` (the anonymized league "Trench Warfare").

```json
{
  "matchups": {
    "next_week": [
      {
        "matchup_id": 1,
        "a_roster_id": "1",
        "b_roster_id": "11",
        "a_power_rank": 2,
        "b_power_rank": 11,
        "a_record": "7-3",
        "b_record": "2-8",
        "a_clinched_playoff": false,
        "a_clinched_bye": false,
        "a_eliminated": false,
        "a_clinched_division": false,
        "b_eliminated": false,
        "stakes": [
          "bye_seed",
          "wildcard_race",
          "division_race"
        ],
        "game_of_week": false
      }
    ]
  },
  "teams": [
    {
      "roster_id": "1",
      "next_week": {
        "opponent_roster_id": "11",
        "power_rank_self": 2,
        "power_rank_opp": 11,
        "stakes": [
          "bye_seed",
          "wildcard_race",
          "division_race"
        ]
      }
    }
  ]
}
```

The league plays 14 regular-season weeks, so after week 10 each team has 4 games left. Flea Flicker Union (roster 1, 7-3) has `cur` 7 and `max` 11 and meets Bubble-Screen Bobcats (roster 11, 2-8), whose `max` is 6. Roster 11 is not eliminated, because only three teams have a `cur` above 6 and elimination needs six. Roster 1 cannot clinch yet, since many teams can still reach 7. The union of tags for this card is `bye_seed` (roster 1 can still reach a bye), `wildcard_race` (roster 11 has neither clinched nor been eliminated) and `division_race` (roster 1's division is still open). The card is not the game of the week, and the power ranks shown are 2 and 11.

## Code

Source: `commishdesk/stats/stakes.py::_outlooks`

```python
def _outlooks(week: WeekModel, league: LeagueModel, standings: Standings) -> dict[str, _Outlook]:
    """Every roster's :class:`_Outlook`, keyed by ``roster_id``.

    All-empty when the league declares no rosters or no ``playoff_week_start``.
    Once the previewed week reaches ``playoff_week_start`` the clinch and
    elimination flags are still computed (with no games left they are the final
    regular-season picture) but no tag is emitted. A league that declares no
    ``league.format.playoff`` keeps the division picture but emits no playoff
    tag."""
    roster_ids = sorted((roster.roster_id for roster in week.rosters), key=_sort_key)
    if not roster_ids or week.playoff_week_start is None:
        return {roster_id: _NO_OUTLOOK for roster_id in roster_ids}
    emit_tags = not _stand_down(week)

    records = {team.roster_id: team for team in standings.teams}
    cur: dict[str, float] = {}
    for roster_id in roster_ids:
        record = records.get(roster_id)
        cur[roster_id] = _win_equivalent(record.wins, record.ties) if record is not None else 0.0
    remaining = _remaining_games(week, standings)
    ceiling = {roster_id: cur[roster_id] + remaining for roster_id in roster_ids}

    playoff = league.format.playoff
    bracket = min(playoff.bracket_teams, len(roster_ids)) if playoff is not None else None
    byes = bye_count(bracket) if bracket is not None else 0
    picture = standings.playoff_picture
    seeded = picture if picture is not None and picture.source == "commissioner" else None

    division_of = {team.roster_id: team.division_id for team in league.teams}
    declared = {division.id for division in league.format.divisions}

    outlooks: dict[str, _Outlook] = {}
    for roster_id in roster_ids:
        others = [other for other in roster_ids if other != roster_id]
        others_cur = [cur[other] for other in others]
        others_max = [ceiling[other] for other in others]

        tags: set[str] = set()
        clinched_playoff = False
        clinched_bye = False
        eliminated = False

        if bracket is not None and bracket > 0:
            ahead_cur = sum(1 for value in others_cur if value > ceiling[roster_id])
            catchers = sum(1 for value in others_max if value >= cur[roster_id])
            eliminated = ahead_cur >= bracket
            clinched_playoff = catchers <= bracket - 1
            clinched_bye = byes > 0 and catchers <= byes - 1

            if byes > 0 and not clinched_bye and ahead_cur <= byes - 1:
                tags.add("bye_seed")
            if not clinched_playoff and not eliminated:
                tags.add("wildcard_race")
            if eliminated:
                tags.add("draft_position")
            elif sum(1 for value in others_cur if value > ceiling[roster_id] - 1) >= bracket:
                tags.add("elimination")

        if seeded is not None:
            # A commissioner override fixes the bracket: the picture, not the
            # records, decides membership, so stakes must agree with it.
            in_bracket = roster_id in seeded.in_bracket
            clinched_playoff = in_bracket
            clinched_bye = roster_id in seeded.byes
            eliminated = not in_bracket
            tags -= {"bye_seed", "wildcard_race", "elimination", "draft_position"}
            if eliminated:
                tags.add("draft_position")

        division_id = division_of.get(roster_id)
        clinched_division = False
        if division_id is not None and division_id in declared:
            rivals = [
                other for other in others if division_of.get(other) == division_id
            ]
            if rivals:
                rival_cur = [cur[rival] for rival in rivals]
                rival_max = [ceiling[rival] for rival in rivals]
                clinched_division = not any(value >= cur[roster_id] for value in rival_max)
                if not clinched_division and not any(value > ceiling[roster_id] for value in rival_cur):
                    tags.add("division_race")

        outlooks[roster_id] = _Outlook(
            tags=frozenset(tags) if emit_tags else frozenset(),
            clinched_playoff=clinched_playoff,
            clinched_bye=clinched_bye,
            eliminated=eliminated,
            clinched_division=clinched_division,
        )
    return outlooks
```

## Edge cases

- **Stand-down.** When the previewed week is the first playoff week or later, or the league declares no playoff start, `stakes` is `[]` on every card. When the previewed week is the first playoff week or later, the clinch and elimination flags are still computed; when the league declares no playoff start, they are all false.
- **No playoff settings.** Without a bracket size the three playoff flags are all false and no playoff tag is emitted. Without divisions there is no `division_race`.
- **Commissioner seeding.** An override fixes the bracket, so the supplied picture decides membership and the flags agree with it.
- **Byes and odd groups.** A team on a bye, or a group that is not exactly two teams, produces no card.
- **Unknown bye data.** `bye_impact` is `null`, not an empty list, when the caller supplied no bye information.
- **Game of the week.** Exactly one card is flagged: the card with the most stakes tags across its two sides, then the lowest sum of the two model ranks (an unranked team counts as worst), then the lowest matchup id.
