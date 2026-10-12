---
doc_id: draft-team-rows
kind: stat
title: Draft team rows
schema_min: 0.1.0
facts_keys:
  - draft_recap.teams[].roster_id
  - draft_recap.teams[].manager
  - draft_recap.teams[].pick_count
  - draft_recap.teams[].pick_nos
  - draft_recap.teams[].positional_counts
  - draft_recap.teams[].back_to_back
  - draft_recap.teams[].best_value_pick
  - draft_recap.teams[].biggest_reach_pick
fixture: facts/expected-draft-recap-facts.json
source: commishdesk/facts/build.py::_merge_teams
---
## Definition

Each team gets one row that joins three results: the board (what the team drafted), the consensus comparison and the grade (see `draft-grade`).

- `pick_count` and `pick_nos` are the number of picks and their overall pick numbers, in order;
- `positional_counts` counts picks by position, always listing QB, RB, WR and TE in that order with zeros filled in, followed by any other position in sorted order;
- `back_to_back` lists each pair of consecutive overall picks the team made;
- `best_value_pick` is the team's pick with the largest delta, and `biggest_reach_pick` the one with the smallest, each as pick number, board label, player and delta.

## Worked example

Fixture: `tests/fixtures/facts/expected-draft-recap-facts.json` (the anonymized league "Trench Warfare").

```json
{
  "teams": [
    {
      "roster_id": "2",
      "manager": "Pull-Guard Pumas",
      "pick_count": 12,
      "pick_nos": [
        1,
        30,
        37,
        42,
        43,
        44,
        45,
        46,
        47,
        54,
        55,
        66
      ],
      "positional_counts": {
        "QB": 1,
        "RB": 4,
        "WR": 6,
        "TE": 1
      },
      "back_to_back": [
        [
          42,
          43
        ],
        [
          43,
          44
        ],
        [
          44,
          45
        ],
        [
          45,
          46
        ],
        [
          46,
          47
        ],
        [
          54,
          55
        ]
      ],
      "best_value_pick": {
        "pick_no": 54,
        "board_label": "5.06",
        "player": "Tai Felton",
        "delta": 10
      },
      "biggest_reach_pick": {
        "pick_no": 1,
        "board_label": "1.01",
        "player": "Ashton Jeanty",
        "delta": -3
      }
    }
  ]
}
```

Pull-Guard Pumas (roster 2) made 12 picks: 1, 30, 37, 42, 43, 44, 45, 46, 47, 54, 55 and 66. Their counts are 1 QB, 4 RB, 6 WR and 1 TE. They picked at 42 through 47 in a row, giving five adjacent pairs (42 and 43, 43 and 44, 44 and 45, 45 and 46, 46 and 47), plus 54 and 55. Their best value was pick 54 (5.06, Tai Felton, +10) and their biggest reach was pick 1 (1.01, Ashton Jeanty, -3).

## Code

Source: `commishdesk/facts/build.py::_merge_teams`

```python
def _merge_teams(
    league: LeagueModel,
    board: BoardMetrics,
    consensus: ConsensusMetrics,
    grades: DraftGrades,
) -> list[TeamRow]:
    board_by_roster = {t.roster_id: t for t in board.teams}
    cons_by_roster = {t.roster_id: t for t in consensus.teams}
    grade_by_roster = {t.roster_id: t for t in grades.teams}
    label_by_pick = {pick.pick_no: pick.board_label for pick in league.picks}
    rows: list[TeamRow] = []
    for team in league.teams:
        tb = board_by_roster.get(team.roster_id)
        tc = cons_by_roster.get(team.roster_id)
        tg = grade_by_roster.get(team.roster_id)
        rows.append(
            TeamRow(
                roster_id=team.roster_id,
                manager=team.manager,
                pick_count=tb.pick_count if tb else 0,
                pick_nos=list(tb.pick_nos) if tb else [],
                positional_counts=_canon_counts(tb.positional_counts if tb else {}),
                back_to_back=list(tb.back_to_back) if tb else [],
                best_value_pick=_extreme(tc.best_value_pick if tc else None, label_by_pick),
                biggest_reach_pick=_extreme(tc.biggest_reach_pick if tc else None, label_by_pick),
                # ``tg`` is expected to always be present (grades are computed for
                # every roster in ``league.teams``); when it is not, the ``TeamRow(...)``
                # construction below raises pydantic's ``ValidationError`` (``None`` is not
                # a valid ``float``/``int``/``str``) -- caught by the outer try/except in
                # ``build_draft_recap_facts`` and re-raised as ``SchemaValidationError``,
                # just as it did before these casts -- cast() is a no-op at runtime, it
                # only tells mypy the same thing the surrounding merge already assumes.
                draft_score=cast(float, tg.draft_score if tg else None),
                premium_picks=cast(int, tg.premium_picks if tg else None),
                format_fit=cast(int, tg.format_fit if tg else None),
                grade_input=cast(float, tg.grade_input if tg else None),
                grade=GradeRef(
                    letter=cast(str, tg.letter if tg else None),
                    driving_picks=list(tg.driving_picks) if tg else [],
                ),
            )
        )
    return rows
```

## Edge cases

- **Ties for best or worst.** The earlier pick number wins.
- **No ranked pick.** If none of a team's players has a consensus slot, both extremes are `null`.
- **Unknown position.** A pick with no position counts as `UNK`. It still adds to `pick_count` and appears in `positional_counts` after the four standard positions.
- **Team with no picks.** `pick_count` is 0, the lists are empty and the counts are all zero.
- **Back to back** means consecutive overall pick numbers, which in a snake draft happens at the turn between rounds.
