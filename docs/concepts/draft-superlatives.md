---
doc_id: draft-superlatives
kind: stat
title: Draft superlatives
schema_min: 0.1.0
facts_keys:
  - draft_recap.superlatives
fixture: facts/expected-draft-recap-facts.json
source: commishdesk/facts/build.py::_superlatives
---
## Definition

Superlatives are the extremes of `delta` across the draft (see `pick-delta-and-consensus`). Only picks that have a consensus slot take part.

- `best_value` and `best_value_runner_up` are the two largest deltas, and `biggest_reach` and `biggest_reach_runner_up` the two smallest. Ties go to the earlier pick.
- `boldest_swing` is the manager whose own picks have the widest spread between their best and worst delta. It lists those two picks, in pick order. A manager needs at least two ranked picks to qualify.
- `note` on each is prose the narrator may add. The engine leaves it `null`.

## Worked example

Fixture: `tests/fixtures/facts/expected-draft-recap-facts.json` (the anonymized league "Trench Warfare").

```json
{
  "superlatives": {
    "best_value": {
      "pick_no": 71,
      "board_label": "6.11",
      "manager": "Blitz Alpacas",
      "player": "Lan Larison",
      "delta": 17
    },
    "best_value_runner_up": {
      "pick_no": 65,
      "player": "Gunnar Helm",
      "delta": 12
    },
    "biggest_reach": {
      "pick_no": 1,
      "player": "Ashton Jeanty",
      "delta": -3
    },
    "boldest_swing": {
      "roster_id": "7",
      "manager": "Blitz Alpacas",
      "picks": [
        {
          "pick_no": 23,
          "player": "Jalen Royals",
          "delta": -1
        },
        {
          "pick_no": 71,
          "player": "Lan Larison",
          "delta": 17
        }
      ]
    }
  }
}
```

Lan Larison, taken 6.11 (pick 71) by Blitz Alpacas, is the best value at +17, ahead of Gunnar Helm (pick 65, +12). The biggest reach is Ashton Jeanty at pick 1 (-3), tied on delta with Tetairoa McMillan at pick 4, and the earlier pick takes the top spot. Blitz Alpacas (roster 7) also have the boldest swing: Jalen Royals at 2.11 (-1) against Larison at 6.11 (+17), a spread of 18.

## Code

Source: `commishdesk/facts/build.py::_superlatives`

```python
def _superlatives(league: LeagueModel, pick_rows: list[PickRow]) -> Superlatives:
    ranked = [r for r in pick_rows if r.delta is not None]
    if not ranked:
        return Superlatives()

    # ``ranked`` is already filtered to rows with a non-None delta (above); cast()
    # only tells mypy what the filter already guarantees, no runtime check added.
    by_value = sorted(ranked, key=lambda r: (-cast(int, r.delta), r.pick_no))
    by_reach = sorted(ranked, key=lambda r: (r.delta, r.pick_no))

    return Superlatives(
        best_value=_superlative_pick(by_value[0]),
        best_value_runner_up=(_superlative_pick(by_value[1]) if len(by_value) > 1 else None),
        biggest_reach=_superlative_pick(by_reach[0]),
        biggest_reach_runner_up=(_superlative_pick(by_reach[1]) if len(by_reach) > 1 else None),
        boldest_swing=_boldest_swing(league, ranked),
    )
```

## Edge cases

- **No ranked picks.** If no pick has a delta, every superlative is empty.
- **One ranked pick.** The runner-up fields are `null`.
- **Swing ties.** If two managers have the same spread, the first in league order wins. A zero spread is no swing, so `boldest_swing` is `null`.
- **Distinct picks.** The swing always shows two different picks. If the best and worst were the same pick, the manager's earliest and latest ranked picks are used.
- **Players without consensus** never appear in a superlative, even if they were the most surprising pick in a human sense.
