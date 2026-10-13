---
doc_id: leads-and-storylines
kind: stat
title: Lead angles and storylines
schema_min: 0.10.0
facts_keys:
  - weekly.lead_candidates
  - weekly.storyline_candidates
  - draft_recap.lead_candidates
  - draft_recap.storyline_candidates
fixture: facts/expected-weekly-facts-week10.json
source: commishdesk/facts/leads.py::build_weekly_lead_candidates
---
## Definition

These two lists are the engine's own suggestions for what an issue could open on. They are chosen in code from the numbers, not by the narrator, and each carries a factual one-sentence `hook` built from those numbers.

**Lead candidates** are ranked angles for the lead of this issue. Weekly kinds, in priority order:

1. `lineup_loss`: a team lost a game it should have won: a perfect lineup would have added more points than the team lost by;
2. `biggest_blowout`: the week's blowout (see `blowout-and-week-shape`);
3. `closest_game`: the smallest margin;
4. `week_high_score`: the best score of the week, which is always available as the floor.

An angle that led one of the last few issues steps behind the fresh ones, so leads rotate. Draft-recap kinds are `positional_run`, `manager_approach`, `positional_hoard` and `biggest_score`.

**Storyline candidates** are threads that run across weeks. A thread opens the first week its signal fires, keeps its `weeks_running` count while the signal continues, and closes the week the signal stops. Its `id` is `<kind>:<roster_id>`. Weekly kinds are `luck_extreme` (season luck of at least 1.5 in either direction), `streak` (the longest active streak, at least 3) and `power_climb` (a model-rank move of at least 3). Draft-recap kinds are `grade_extreme`, `boldest_swing` and `round_stack`.

## Worked example

Fixture: `tests/fixtures/facts/expected-weekly-facts-week10.json` (the anonymized league "Trench Warfare").

```json
{
  "lead_candidates": [
    {
      "rank": 1,
      "kind": "lineup_loss",
      "roster_ids": [
        "7"
      ],
      "hook": "Kneel-Down Koalas lost by 6.06, and a perfect lineup adds 108.86, including TreVeyon Henderson."
    },
    {
      "rank": 2,
      "kind": "biggest_blowout",
      "roster_ids": [
        "1",
        "5"
      ],
      "hook": "Flea Flicker Union beat Nickel Newts by 138.19, the week's biggest blowout."
    }
  ],
  "storyline_candidates": [
    {
      "id": "luck_extreme:12",
      "kind": "luck_extreme",
      "roster_ids": [
        "12"
      ],
      "hook": "Yardage Yaks has run the league's best luck, +2.5 wins over expectation.",
      "weeks_running": 1
    },
    {
      "id": "streak:11",
      "kind": "streak",
      "roster_ids": [
        "11"
      ],
      "hook": "Bubble-Screen Bobcats has lost six straight.",
      "weeks_running": 1
    }
  ]
}
```

Roster 7 (Kneel-Down Koalas) lost by 6.06, and a perfect lineup adds 108.86 points, so `lineup_loss` leads: 108.86 is more than 6.06, and the gap is the largest of any losing team. The week's biggest blowout, roster 1 over roster 5 by 138.19, ranks second. For storylines, Yardage Yaks (roster 12) carry the league's best luck, +2.5, which clears the 1.5 threshold, and Bubble-Screen Bobcats (roster 11) have a six-game losing streak, well over the length of three. Both have run for one week.

## Code

Source: `commishdesk/facts/leads.py::build_weekly_lead_candidates`

```python
def build_weekly_lead_candidates(
    teams: list[WeeklyTeam],
    period: WeeklyPeriod,
    *,
    recent_kinds: Sequence[str] = (),
) -> list[LeadCandidate]:
    """Ranked lead angles for the weekly recap (Story 5.9).

    Mirrors :func:`build_lead_candidates`'s shape: only detectors that fire are
    emitted, in :data:`WEEKLY_LEAD_KIND_PRIORITY` order, then ``rank`` is
    renumbered ``1..N``. Returns ``[]`` only when no roster played a game this
    week — the ``week_high_score`` floor otherwise guarantees at least one angle,
    so a ``lineup_loss`` read is never buried behind a single high score (FR-14).

    ``recent_kinds`` are the angles the league's last Issues led on. An angle that
    just led steps behind the fresh ones (the kind priority still orders each
    group), so a season rotates its leads instead of opening on the same story
    every week; when every fired angle is recent, or none is, the plain priority
    order stands. ``lineup_loss`` still leads any week that has not just used it.
    """
    angles = [
        _lineup_loss(teams),
        _biggest_blowout(teams, period),
        _closest_game(teams, period),
        _week_high_score(teams),
    ]
    fired = [a for a in angles if a is not None]
    fired.sort(key=lambda a: WEEKLY_LEAD_KIND_PRIORITY.index(a.kind))
    fired = [a for a in fired if a.kind not in recent_kinds] + [a for a in fired if a.kind in recent_kinds]
    return [a.model_copy(update={"rank": i}) for i, a in enumerate(fired, start=1)]
```

## Edge cases

- **Empty lists.** A weekly document has no lead candidates only when no team played. Otherwise the high-score angle is a guaranteed floor. A draft with no picks has none either.
- **Rotation.** An angle that just led steps behind the others, and if every angle is recent the plain priority order stands.
- **Playoffs.** `luck_extreme` and `streak` storylines stop firing in playoff weeks, because those values are frozen. `power_climb` still fires.
- **Pruning.** A resolved storyline is dropped four weeks after it closes.
- **Draft recap storylines** `grade_extreme` fire only for a grade at the very top (A or A+) or bottom (D- or F) of the scale, so a middling "lowest grade" never becomes a thread.
- **Hooks are plain.** They are factual sentences. The narrator may rewrite them in voice, and the safety checks verify the numbers it uses.
