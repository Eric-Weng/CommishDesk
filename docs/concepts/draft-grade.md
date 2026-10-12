---
doc_id: draft-grade
kind: stat
title: Draft grade
schema_min: 0.1.0
facts_keys:
  - draft_recap.grade_method
  - draft_recap.teams[].draft_score
  - draft_recap.teams[].premium_picks
  - draft_recap.teams[].format_fit
  - draft_recap.teams[].grade_input
  - draft_recap.teams[].grade
fixture: facts/expected-draft-recap-facts.json
source: commishdesk/stats/grades.py::compute_draft_grades
---
## Definition

The grade is editorial, not the raw pick delta, which would over-reward late-round picks. Each team gets four numbers and a letter:

- `draft_score` is the tier-weighted sum of its pick deltas. Each delta is multiplied by `max(0.15, (60 - consensus_slot) / 60)`, so early slots weigh close to 1.0 and anything past about slot 51 weighs 0.15. Picks without a consensus slot add nothing.
- `premium_picks` counts the team's picks with a consensus slot of 10 or better.
- `format_fit` adds league-format credit. In a superflex or two-QB league it is +2 for at least one QB and +2 more for at least two. In a tight-end-premium league it is +1 per drafted TE with consensus slot 12 or better.
- `grade_input = draft_score + 3 * premium_picks + format_fit`, rounded to one decimal.

The letter is a fixed cut table on `grade_input`: A+ at 16.0 and above, A from 12.0, A- from 9.0, B+ from 6.0, B from 4.5, B- from 3.0, C+ from 1.0, C from -1.0, C- from -4.0, D+ from -8.0, D from -13.0, D- from -18.0, and F below that. A team with two or more premium picks never grades below C. `driving_picks` lists up to four pick numbers whose weighted contribution is at least 1.5 in size. `grade_method` is the same description for every team, and `grade.rationale` is prose written later by the narrator.

## Worked example

Fixture: `tests/fixtures/facts/expected-draft-recap-facts.json` (the anonymized league "Trench Warfare").

```json
{
  "grade_method": {
    "letter_scale": "A-F+/-",
    "floor_rule": ">=2 premium picks -> minimum C"
  },
  "teams": [
    {
      "roster_id": "2",
      "draft_score": 14.9,
      "premium_picks": 1,
      "format_fit": 2,
      "grade_input": 19.9,
      "grade": {
        "letter": "A+",
        "driving_picks": [
          1,
          43,
          44,
          46
        ]
      }
    }
  ]
}
```

Pull-Guard Pumas (roster 2) have a weighted `draft_score` of 14.9, driven by large positive deltas on picks 43, 44 and 46 (+9 each, with consensus slots in the 30s, so each weighs about 0.4), and the first pick's reach of -3. One pick, Jeanty at consensus slot 4, is a premium pick, worth 3 points. The league is two-QB and the team drafted one quarterback, so `format_fit` is +2. Then `grade_input = 14.9 + 3 x 1 + 2 = 19.9`, which clears the 16.0 cut for an A+. The `driving_picks` are 1, 43, 44 and 46.

## Code

Source: `commishdesk/stats/grades.py::compute_draft_grades`

```python
def compute_draft_grades(
    league: LeagueModel, consensus: ConsensusMetrics
) -> DraftGrades:
    """Compute one editorial :class:`TeamGrade` per roster in ``league``.

    Pure, deterministic, offline. A valid ``LeagueModel`` plus its
    ``ConsensusMetrics`` in, a ``DraftGrades`` out; two calls on one input return
    an equal ``model_dump()``. A roster whose every pick is ``no_consensus`` gets
    ``draft_score`` ``0.0``, ``premium_picks`` ``0``, ``driving_picks`` ``[]`` --
    ``format_fit`` still comes from the drafted positions -- and never raises.
    """
    superflex = league.format.is_superflex_or_2qb
    te_premium = league.format.te_premium

    position_by_pick_no = {pick.pick_no: (pick.player.position or "") for pick in league.picks}

    picks_by_roster: dict[str, list] = {}
    for pick_row in consensus.picks:
        picks_by_roster.setdefault(pick_row.roster_id, []).append(pick_row)

    qb_by_roster: dict[str, int] = {}
    for pick in league.picks:
        if pick.player.position == "QB":
            qb_by_roster[pick.roster_id] = qb_by_roster.get(pick.roster_id, 0) + 1

    team_grades: list[TeamGrade] = []
    for team in league.teams:
        roster_picks = picks_by_roster.get(team.roster_id, [])

        total = 0.0
        premium_picks = 0
        premium_te = 0
        contributions: list[tuple[float, int]] = []  # (abs contribution, pick_no)
        for pick_row in roster_picks:
            slot = pick_row.consensus_slot
            # premium_picks / premium_te are counted from consensus_slot alone
            # (spec: "count of picks with consensus_slot <= 10", no delta dependency).
            if slot is None:
                continue
            if slot <= PREMIUM_SLOT:
                premium_picks += 1
            if (
                slot <= PREMIUM_TE_SLOT
                and position_by_pick_no.get(pick_row.pick_no) == "TE"
            ):
                premium_te += 1
            # draft_score contribution + driving-pick math need delta.
            if pick_row.delta is None:
                continue
            contribution = pick_row.delta * _weight(slot)
            total += contribution
            if abs(contribution) >= DRIVING_CUTOFF:
                contributions.append((abs(contribution), pick_row.pick_no))

        draft_score = round(total, 1) or 0.0

        format_fit = 0
        if superflex:
            qb_count = qb_by_roster.get(team.roster_id, 0)
            if qb_count >= 1:
                format_fit += 2
            if qb_count >= 2:
                format_fit += 2
        if te_premium:
            format_fit += premium_te

        grade_input = round(draft_score + PREMIUM_BONUS * premium_picks + format_fit, 1) or 0.0

        letter = _letter_for(grade_input)
        if premium_picks >= 2 and letter in _FLOOR_LETTERS:
            letter = _FLOOR_LETTER

        contributions.sort(key=lambda item: (-item[0], item[1]))
        driving_picks = sorted(
            {pick_no for _, pick_no in contributions[:MAX_DRIVING_PICKS]}
        )

        team_grades.append(
            TeamGrade(
                roster_id=team.roster_id,
                draft_score=draft_score,
                premium_picks=premium_picks,
                format_fit=format_fit,
                grade_input=grade_input,
                letter=letter,
                driving_picks=driving_picks,
            )
        )

    return DraftGrades(teams=team_grades, grade_method=GRADE_METHOD)
```

## Edge cases

- **No consensus picks.** A team whose every pick lacks a consensus slot has a `draft_score` of 0.0, no premium picks and no driving picks. Its `format_fit` still comes from the positions it drafted.
- **Floor.** Two or more premium picks lift a C- or lower to a C. The floor never raises a grade above C.
- **Negative side.** The scale is coarser below C. A team that reached on nearly every pick bottoms out near -7, so the D- and F cuts are rarely met.
- **Driving picks.** The cutoff is on the absolute weighted value, the cap is four, and the list is returned in pick order. Ties in size go to the earlier pick.
- **Rounding.** The letter is chosen from the already-rounded one-decimal `grade_input`.
- **Rationale.** `grade.rationale` is `null` in the engine's output and the narrator fills it in.
