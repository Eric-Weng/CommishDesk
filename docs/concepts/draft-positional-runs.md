---
doc_id: draft-positional-runs
kind: stat
title: Positional runs and draft shape
schema_min: 0.1.0
facts_keys:
  - draft_recap.draft_summary
fixture: facts/expected-draft-recap-facts.json
source: commishdesk/facts/build.py::_draft_summary
---
## Definition

`draft_summary` describes the shape of the whole board.

- `round1_positional` counts round-1 picks by position, and `round1_qbs` lists the quarterbacks taken there.
- `first_window` is one fewer than the team count: the opening stretch of the draft. `first_window_running_backs` lists the running backs taken inside it.
- `pick_count_rank` ranks managers by number of picks, and `round_concentration` lists managers who made three or more picks in one round, with that round and the count.
- `positional_runs` has a summary for each of QB, RB and TE: totals, the first board label, how many quarterbacks went by the end of round 3 and which managers were left waiting on a first quarterback after round 3, which manager took the most running backs, and the early tight-end window and the first big gap before a third tight end.

## Worked example

Fixture: `tests/fixtures/facts/expected-draft-recap-facts.json` (the anonymized league "Trench Warfare").

```json
{
  "draft_summary": {
    "round1_positional": {
      "RB": 5,
      "WR": 5,
      "QB": 2
    },
    "first_window": 11,
    "first_window_running_backs": [
      "Ashton Jeanty",
      "Omarion Hampton",
      "TreVeyon Henderson",
      "Quinshon Judkins",
      "RJ Harvey"
    ],
    "pick_count_rank": [
      {
        "manager": "Pull-Guard Pumas",
        "pick_count": 12
      }
    ],
    "round_concentration": [
      {
        "manager": "Pull-Guard Pumas",
        "round": 4,
        "count": 7
      }
    ],
    "positional_runs": {
      "QB": {
        "total": 10,
        "first_label": "1.05",
        "by_end_round3": 6,
        "run_labels": [
          "1.05",
          "1.10",
          "2.10",
          "3.02",
          "3.05",
          "3.11"
        ]
      },
      "RB": {
        "total": 26,
        "first_label": "1.01",
        "in_round1": 5,
        "most_by_one_manager": {
          "manager": "Gridiron Gophers",
          "count": 6
        }
      }
    }
  }
}
```

This is a 12-team draft, so the first window is 11 picks. Five of those 11 were running backs, and round 1 as a whole holds five running backs, five receivers and two quarterbacks. Two quarterbacks went in round 1, and six had gone by the end of round 3 (labels 1.05, 1.10, 2.10, 3.02, 3.05 and 3.11) out of 10 in the draft. Pull-Guard Pumas made the most picks, 12, and seven of those came in round 4. Gridiron Gophers took the most running backs of any manager, six.

## Code

Source: `commishdesk/facts/build.py::_draft_summary`

```python
def _draft_summary(league: LeagueModel, board: BoardMetrics, pick_rows: list[PickRow]) -> DraftSummary:
    team_count = league.format.team_count
    ordered = sorted(pick_rows, key=lambda r: r.pick_no)
    managers = {t.roster_id: t.manager for t in board.teams}
    round1 = [r for r in ordered if r.round == 1]

    round1_positional: dict[str, int] = {}
    for row in round1:
        pos = _position(row)
        round1_positional[pos] = round1_positional.get(pos, 0) + 1

    first_window = team_count - 1
    first_window_rb = [row.player.name for row in ordered if row.pick_no <= first_window and _position(row) == "RB"]

    round1_qbs = [_board_pick(row) for row in round1 if _position(row) == "QB"]

    ranked_counts = sorted(board.teams, key=lambda t: -t.pick_count)
    pick_count_rank = [ManagerPickCount(manager=t.manager, pick_count=t.pick_count) for t in ranked_counts]

    per_round: dict[str, dict[int, int]] = {}
    for row in ordered:
        per_round.setdefault(row.roster_id, {})
        per_round[row.roster_id][row.round] = per_round[row.roster_id].get(row.round, 0) + 1
    concentration: list[tuple[str | None, int, int]] = []
    for team in board.teams:
        rounds = per_round.get(team.roster_id, {})
        if not rounds:
            continue
        heaviest = min(rounds, key=lambda rnd: (-rounds[rnd], rnd))
        if rounds[heaviest] >= _MIN_ROUND_CONCENTRATION:
            concentration.append((team.manager, heaviest, rounds[heaviest]))
    concentration.sort(key=lambda item: -item[2])
    round_concentration = [RoundConcentration(manager=mgr, round=rnd, count=count) for mgr, rnd, count in concentration]

    positional_runs = PositionalRunsSummary(
        QB=_qb_run(ordered, league.draft.rounds),
        RB=_rb_run(ordered, round1, board, managers),
        TE=_te_run(ordered, team_count),
    )

    return DraftSummary(
        round1_positional=round1_positional,
        first_window=first_window,
        first_window_running_backs=first_window_rb,
        round1_qbs=round1_qbs,
        pick_count_rank=pick_count_rank,
        round_concentration=round_concentration,
        positional_runs=positional_runs,
    )
```

## Edge cases

- **Unknown positions** count as `UNK` and are left out of the positional summaries.
- **Rounds unknown.** If the draft's round count is not known, the "left waiting" test for quarterbacks has no upper bound.
- **Tight-end window.** A gap only closes the early window once two tight ends precede it, so the "third tight end" can never be the second. The gap threshold is half the team count, with a minimum of one.
- **Concentration** needs at least three picks in one round. If a manager ties across two rounds, the earlier round is reported.
- **Most running backs.** Ties go to the team that comes first in league order.
- **Consensus fields.** `delta` and `consensus_label` inside `round1_qbs` are `null` for a quarterback missing from the consensus ranking.
