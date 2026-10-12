---
doc_id: transactions-and-trade-market
kind: stat
title: Transactions and the trade market
schema_min: 0.5.0
facts_keys:
  - weekly.transactions
fixture: facts/expected-weekly-facts-week10.json
source: commishdesk/stats/transactions.py::_market_note
---
## Definition

The transactions block is the weekly market desk.

- `this_week` lists every settled move of the issue week, such as a waiver claim or a trade. Each carries its `transaction_id`, `type`, the roster ids involved, the players added and dropped (with the roster each belongs to) and the FAAB bid.
- `recent_trades` spells out each trade from the last three weeks, including the issue week (`RECENT_TRADE_WEEKS = 3`), newest week first. A trade has one side per roster, and a side is what that roster receives: players, draft picks (season, round and the roster that originally owned the pick) and FAAB.
- `market_note` says when the league last traded. `last_trade_week` is the most recent week with a trade, `weeks_since_last_trade` is the issue week minus that, and `complete` says whether every earlier week is available to check.

## Worked example

Fixture: `tests/fixtures/facts/expected-weekly-facts-week10.json` (the anonymized league "Trench Warfare").

```json
{
  "transactions": {
    "this_week": [
      {
        "transaction_id": "id_qtiw68uvsq",
        "type": "waiver",
        "roster_ids": [
          "1"
        ],
        "adds": [
          {
            "player_id": "8180",
            "name": "Jalen Nailor",
            "pos": "WR",
            "roster_id": "1"
          }
        ],
        "drops": [
          {
            "player_id": "827",
            "name": "Tyrod Taylor",
            "pos": "QB",
            "roster_id": "1"
          }
        ],
        "faab": 25
      }
    ],
    "recent_trades": [
      {
        "week": 9,
        "transaction_id": "id_v51mvor6g7",
        "sides": [
          {
            "roster_id": "2",
            "players": [
              {
                "player_id": "8132",
                "name": "Tyler Allgeier",
                "pos": "RB",
                "roster_id": "2"
              }
            ],
            "picks": [
              {
                "season": "2026",
                "round": 4,
                "from_roster_id": "4"
              }
            ],
            "faab": 0
          },
          {
            "roster_id": "4",
            "players": [
              {
                "player_id": "8408",
                "name": "Jordan Mason",
                "pos": "RB",
                "roster_id": "4"
              }
            ],
            "picks": [],
            "faab": 0
          }
        ]
      }
    ],
    "market_note": {
      "last_trade_week": 9,
      "weeks_since_last_trade": 1,
      "complete": true
    }
  }
}
```

In week 10, roster 1 won a waiver claim for Jalen Nailor (WR) with a 25 FAAB bid and dropped Tyrod Taylor (QB). The one trade in the last three weeks was in week 9: roster 2 received Tyler Allgeier and a 2026 fourth-round pick that began with roster 4, and roster 4 received Jordan Mason. The market note reads `last_trade_week` 9 and `weeks_since_last_trade` 10 - 9 = 1, with `complete` true because weeks 1 through 9 are all on file.

## Code

Source: `commishdesk/stats/transactions.py::_market_note`

```python
def _market_note(week: WeekModel, by_week: dict[int, list[Transaction]]) -> MarketNote:
    if any(wk not in week.past_transactions for wk in range(1, week.week)):
        return MarketNote(last_trade_week=None, weeks_since_last_trade=None, complete=False)
    last_trade_week: int | None = None
    for wk in range(week.week, 0, -1):
        if any(transaction.type == _TRADE_TYPE for transaction in by_week.get(wk, [])):
            last_trade_week = wk
            break
    if last_trade_week is None:
        return MarketNote(last_trade_week=None, weeks_since_last_trade=None, complete=True)
    return MarketNote(
        last_trade_week=last_trade_week,
        weeks_since_last_trade=week.week - last_trade_week,
        complete=True,
    )
```

## Edge cases

- **Incomplete history.** If any earlier week is missing, a partial history must not read as "no trades". Both `last_trade_week` and `weeks_since_last_trade` are `null` and `complete` is false.
- **No trade yet.** With a complete history and no trade, both fields are `null` and `complete` is true.
- **The window.** `recent_trades` covers the issue week and the two before it. An older trade still sets `last_trade_week`, but it does not appear in the list.
- **Ordering.** Moves are ordered by `transaction_id`. Trades are ordered by week, newest first, then `transaction_id`.
