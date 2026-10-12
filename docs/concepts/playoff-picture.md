---
doc_id: playoff-picture
kind: stat
title: Playoff picture
schema_min: 0.5.0
facts_keys:
  - weekly.standings.playoff_picture
fixture: facts/expected-weekly-facts-week10.json
source: commishdesk/stats/standings.py::_playoff_picture
---
## Definition

The playoff picture is derived from the standings order and the league's declared bracket size, `N`:

- `in_bracket` is the top `N` teams, clamped to the number of teams;
- `byes` is the top few of those, `2^ceil(log2 N) - N` teams, the number that turns the bracket into a clean power-of-two field (6 teams: 2 byes; 7 teams: 1; 8 teams: 0);
- `first_out` is the team ranked `N + 1`, or `null` when the whole league is in;
- `bubble` is ranks `N - 1` through `N + 2`;
- `cut_line_after_rank` is `N`;
- `consolation` is everyone below the cut.

`source` says where the order came from: `derived` from the standings, `confirmed` by the operator, or `commissioner` when the operator supplied the seed order. `seeding_unconfirmed` is true for a playoff-week issue whose bracket was derived and never confirmed, so the issue can say so.

## Worked example

Fixture: `tests/fixtures/facts/expected-weekly-facts-week10.json` (the anonymized league "Trench Warfare").

```json
{
  "standings": {
    "playoff_picture": {
      "source": "derived",
      "seeding_unconfirmed": false,
      "in_bracket": [
        "2",
        "1",
        "3",
        "9",
        "5",
        "6"
      ],
      "byes": [
        "2",
        "1"
      ],
      "first_out": "12",
      "bubble": [
        "5",
        "6",
        "12",
        "8"
      ],
      "cut_line_after_rank": 6,
      "consolation": [
        "12",
        "8",
        "4",
        "10",
        "11",
        "7"
      ]
    }
  }
}
```

This league has 12 teams and a 6-team bracket with 2 byes, which gives 2^3 - 6 = 2. After week 10 the standings order is 2, 1, 3, 9, 5, 6, 12, 8, 4, 10, 11, 7. The top six are in the bracket and the top two (rosters 2 and 1) hold byes. Roster 12 is seventh, so it is `first_out`. The bubble is ranks 5 through 8: rosters 5, 6, 12 and 8. The cut falls after rank 6, and everyone from rank 7 down is in `consolation`. `seeding_unconfirmed` is false because week 10 is a regular-season week.

## Code

Source: `commishdesk/stats/standings.py::_playoff_picture`

```python
def _playoff_picture(
    ordered: list[str], league: LeagueModel, source: str
) -> PlayoffPicture | None:
    """The playoff picture for a standings order, or ``None`` when the league
    declares no playoff bracket size (``league.format.playoff is None``) or has
    no rosters. ``source`` is ``"derived"``, ``"confirmed"`` or
    ``"commissioner"`` depending on the operative seeding mode."""
    playoff = league.format.playoff
    if playoff is None or not ordered:
        return None

    bracket = min(playoff.bracket_teams, len(ordered))
    in_bracket = ordered[:bracket]
    # The bye count that makes the bracket a clean power-of-two field -- the one
    # shared rule (Story 5.7's stats/stakes.py reads the same helper).
    byes = bye_count(bracket)
    bubble = ordered[max(bracket - 1, 1) - 1 : min(bracket + 2, len(ordered))]
    return PlayoffPicture(
        source=source,
        in_bracket=in_bracket,
        byes=in_bracket[:byes],
        first_out=ordered[bracket] if bracket < len(ordered) else None,
        bubble=bubble,
        cut_line_after_rank=bracket,
        consolation=ordered[bracket:],
    )
```

## Edge cases

- **No playoff settings.** If the league declares no bracket size, or has no teams, `playoff_picture` is `null`.
- **Whole league in.** If the bracket size is at least the number of teams, it is clamped, `first_out` is `null` and `consolation` is empty.
- **Commissioner override.** The supplied seeds go first in the given order and the rest follow in derived order. `source` becomes `commissioner`. An override is only accepted from the week before the playoffs start.
- **Confirmation.** A confirmed order marks `source` as `confirmed` and does not change the order.
- **Short brackets.** The bubble is clipped to valid ranks in a small league.
