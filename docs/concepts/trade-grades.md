---
doc_id: trade-grades
kind: stat
title: Instant trade grades
schema_min: 0.11.0
facts_keys:
  - weekly.transactions.trade_grades
  - weekly.transactions.trade_revisits
fixture: trade_grades/worked-example.json
source: commishdesk/stats/trades.py::_verdict
---
## Definition

An instant trade grade is a verdict on each side of a trade, given the week the trade settles. The only source is [League Value](/docs/league-value/), the engine's own number for how many points per week a player is worth over a replacement-level player in your league's scoring and lineup. No market price is fetched, and no outside ranking is used.

**What is added up.** Each side's received assets are valued in points per week over replacement.

- **This season** adds the received players' League Value. Draft picks never count toward it.
- **Future** (dynasty leagues only) adds the received players' Future value and the received draft picks' value. This season and Future are different units of time, so they are never added together.

A redraft trade gets one verdict (This season). A dynasty trade gets two. A pick in a redraft league is listed but not counted.

**The fair band and the scale.** For a horizon, a side's total is `a` and the rest of the trade's total is `b`. In a three-team trade the other two sides count as one counter-side. The share is `|a - b| / (a + b)`.

| Share of the total | Leader | Trailing side |
| --- | --- | --- |
| up to 5% (10% on a "Sleeper data only" snapshot) | B, Even | B, Even |
| up to 15% | B+, Slight edge | C+, Slightly behind |
| up to 30% | A-, Clear edge | C-, Clearly behind |
| more than 30% | A, Lopsided | D, Far behind |

D is the lowest letter and F is never given. One side's verdict mirrors the other's. The words describe the numbers in the trade, not the managers.

**est.** A verdict is marked est. when more than half of the value it rests on comes from estimated assets: rookies who have not played enough, and draft picks.

**Lineup impact.** Each side also gets the change in its best starting lineup, in points per week over replacement, using the same lineup rules as the coaching efficiency stat. It is worked out on the week's roster: the "after" lineup is the roster as it stands, and the "before" lineup puts back what the side gave up and takes out what it received. A later add or drop can shift it slightly. The lineup impact can disagree with the letter. Both are facts and both are shown.

**No roster-spot multiplier.** A trade of two players for one is not adjusted. A player's value is already floored at replacement, so extra bodies add nothing, and the lineup impact is where the cost of giving up depth shows.

**The context flag.** When one side is out of the playoff race and another is still in it, the trade carries `context: contender_vs_eliminated` and lists the eliminated rosters. This is a fact about the standings. It never changes a grade.

**Ungraded assets.** FAAB, a player with no League Value (a kicker or defender nobody rostered, for example), and a pick the snapshot cannot price are listed with a reason: `faab`, `not_valued`, `pick_not_priced`, `redraft_pick`, or `no_snapshot`. The grade is made on the rest. A trade with nothing valued, or a week with no League Value snapshot, is `ungraded` with the reason and has no letter.

**What "instant" means.** The grade is made from the League Value snapshot of the week the trade settles, the same week the Issue is published. It says who received more value, for the rest of this season and in dynasty for the future, as the numbers stand today. It does not say who won in the end.

**Trade revisited is a different, later verdict.** For a trade of this season that is three or more weeks old, `trade_revisits` carries the numbers for a later look: the weeks since the trade, each side's received players' points per week in the league since the trade, and the leading side (`null` on a tie). A full revisited verdict is not part of this stat.

**Where the numbers come from.** League Value is built from your league's own Sleeper points and, for weeks a player was not on a roster, from [nflverse](https://github.com/nflverse/nflverse-data) stats (CC BY 4.0). Only numbers and Sleeper ids reach the trade facts.

## Worked example

Fixture: `tests/fixtures/trade_grades/worked-example.json`. A dynasty league. Roster 1 receives running back `rbA`. Roster 2 receives running back `rbB` and a 2027 first-round pick that began with roster 4.

```json
{
  "transactions": {
    "trade_grades": [
      {
        "status": "graded",
        "dynasty": true,
        "fair_band": 0.05,
        "context": null,
        "sides": [
          {
            "roster_id": "1",
            "this_season_total": 10.0,
            "future_total": 12.0,
            "lineup_impact": 4.0,
            "verdicts": [
              {
                "horizon": "this_season",
                "total": 10.0,
                "counter_total": 4.0,
                "share": 0.4286,
                "letter": "A",
                "word": "Lopsided",
                "ahead": true
              },
              {
                "horizon": "future",
                "total": 12.0,
                "counter_total": 11.0,
                "share": 0.0435,
                "letter": "B",
                "word": "Even",
                "ahead": null
              }
            ]
          },
          {
            "roster_id": "2",
            "assets": [
              {
                "kind": "pick",
                "pick_season": "2027",
                "pick_round": 1,
                "future": 8.0,
                "est": true
              }
            ],
            "verdicts": [
              {
                "horizon": "this_season",
                "letter": "D",
                "word": "Far behind"
              }
            ]
          }
        ]
      }
    ]
  }
}
```

This season, roster 1 receives 10.0 points a week over replacement and roster 2 receives 4.0. The share is (10 - 4) / (10 + 4) = 0.4286, above 30%, so roster 1 reads A, Lopsided, and roster 2 reads D, Far behind. The pick does not count here.

In the Future, roster 1 receives 12.0. Roster 2 receives the other running back's 3.0 plus the pick's 8.0, which is 11.0. The share is (12 - 11) / (12 + 11) = 0.0435, inside the 5% band, so both sides read B, Even. Only 8.0 of the 23.0 comes from an estimate, which is less than half, so the verdict itself is not marked est.

The two verdicts can disagree. That is the point of having two horizons. Roster 1's lineup improves by 4.0 points a week.

## Code

Source: `commishdesk/stats/trades.py::_verdict`

```python
def _verdict(horizon: str, total: float, counter: float, band: float, est: bool) -> Verdict:
    """The letter-and-word verdict for a side holding *total* against *counter* over a horizon."""
    gross = total + counter
    share = round(abs(total - counter) / gross, 6) if gross > 0 else 0.0
    if share <= band:
        return Verdict(
            horizon=horizon, total=round(total, 3), counter_total=round(counter, 3), share=round(share, 4),
            tier="even", letter=_EVEN_LETTER, word=_EVEN_WORD, ahead=None, est=est,
        )
    ahead = total > counter
    for bound, tier, lead_letter, lead_word, trail_letter, trail_word in SCALE:
        if share <= bound:
            return Verdict(
                horizon=horizon, total=round(total, 3), counter_total=round(counter, 3), share=round(share, 4),
                tier=tier, letter=lead_letter if ahead else trail_letter,
                word=lead_word if ahead else trail_word, ahead=ahead, est=est,
            )
    raise AssertionError("SCALE ends at infinity")  # pragma: no cover
```

## Edge cases

- **Only this week's trades are graded.** A trade from an earlier week is not graded again. After three weeks it can appear in `trade_revisits`.
- **No League Value snapshot.** Every trade of the week is listed `ungraded` with the reason `no_snapshot`, and the Issue is built as usual.
- **Nothing valued.** A trade of only FAAB, picks in a redraft league, or players with no League Value is `ungraded` with the reason `nothing_valued`. It never gets a letter.
- **Fault.** If grading one trade raises unexpectedly, that trade is `ungraded` with the reason `fault`; the rest of the Issue is unaffected.
- **Partly valued.** The grade rests on the valued assets, and each unvalued asset is listed with its reason.
- **A Sleeper-data-only snapshot.** The sample cannot reach nflverse, so its band is wider (10%) and the source says so.
- **Three teams.** Each side is graded against the other two taken together, so two sides can both read behind.
- **All values at replacement.** When nothing in a horizon is worth more than a replacement player, the totals are zero and both sides read Even.
- **A player with no age on file** counts nothing toward Future.
- **Lineup impact is not available** when a roster has no matchup that week, or when the league's lineup format cannot be solved. The grades are unaffected.
- **Revisit ties.** When the two sides' players scored the same points per week, `leader` is `null`.
