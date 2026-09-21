"""The transactions desk: this week's moves, the recent trades, the market note
(Story 5.7).

:func:`compute_transactions_desk` reads a
:class:`~commishdesk.ingest.WeekModel` into the weekly Issue's market section:
``this_week`` -- every settled move from the target week, ids only -- the
``recent_trades`` of the last :data:`RECENT_TRADE_WEEKS` weeks, each spelled out
side by side, and a :class:`MarketNote` saying when the league last traded.

**Trades, not just this week.** Sleeper files a move under the week it settled;
``WeekModel.past_transactions`` (Story 5.7) carries every earlier week the bundle
holds. A trade's *sides* are built from what each participating roster
*receives*: the player ids ``adds`` maps to it, the :class:`TradedPick` s it now
owns, and the FAAB it was sent. Nothing here is a player display name -- Story
5.8 joins those, as it does for ``stats/lineup.py``.

**The market note is honest about what it does not know.** It reads weeks
``1..n``. When every week before the target is present in the bundle's history,
``complete`` is ``True`` and it reports the last trade week (or ``None`` when the
league has simply never traded). When any earlier week is missing, a partial
history must not read as "no trades": both ``last_trade_week`` and
``weeks_since_last_trade`` are ``None`` and ``complete`` is ``False``.

This module stays inside the ``stats/`` fence (AD-1): it imports only stdlib,
pydantic, ``commishdesk.ingest``, and its sibling ``stats/`` modules -- never
``adapters`` / ``store`` / ``facts`` / ``narrate`` / ``render`` / a later
pipeline stage. It reads no file, clock, PRNG, or network, and never raises.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from commishdesk.ingest import TradedPick, Transaction, WeekModel

from .weekly import _sort_key

__all__ = [
    "RECENT_TRADE_WEEKS",
    "MarketNote",
    "Move",
    "Trade",
    "TradeSide",
    "TransactionsDesk",
    "compute_transactions_desk",
]

#: How many weeks back -- the target week included -- :attr:`TransactionsDesk.recent_trades`
#: looks. Named and tunable on purpose: it is the desk's one "lately" knob.
RECENT_TRADE_WEEKS = 3

#: The one transaction type this module treats as a trade.
_TRADE_TYPE = "trade"


class _Frozen(BaseModel):
    """Immutable, closed to unknown keys -- matches ``stats/standings.py``."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Move(_Frozen):
    """One settled transaction from the target week, ids only: the assets moved
    (``adds`` / ``drops``, both ``{player_id: roster_id}``) and the winning
    waiver bid when the move had one."""

    transaction_id: str
    type: str
    roster_ids: list[str]
    adds: dict[str, str]
    drops: dict[str, str]
    faab: int | None


class TradeSide(_Frozen):
    """One participating roster's receipts from a trade: the player ids it
    received, the draft picks it now owns, and the FAAB sent its way."""

    roster_id: str
    player_ids: list[str]
    picks: list[TradedPick]
    faab: int


class Trade(_Frozen):
    """One settled trade: its week, its id, and one :class:`TradeSide` per
    participating roster (ordered by ``roster_id``)."""

    week: int
    transaction_id: str
    sides: list[TradeSide]


class MarketNote(_Frozen):
    """When the league last traded, over weeks ``1..n``.

    ``complete`` is ``False`` when any week in ``1..n-1`` is missing from the
    bundle's past transactions -- a partial history must not read as "no
    trades". When it is ``False`` both ``last_trade_week`` and
    ``weeks_since_last_trade`` are ``None`` (unknown); both are also ``None``,
    with ``complete`` ``True``, when the history is complete and no trade has
    happened yet."""

    last_trade_week: int | None
    weeks_since_last_trade: int | None
    complete: bool


class TransactionsDesk(_Frozen):
    """The whole result: the target week's :class:`Move` s, the recent
    :class:`Trade` s (``RECENT_TRADE_WEEKS`` back), and the :class:`MarketNote`."""

    week: int
    this_week: list[Move]
    recent_trades: list[Trade]
    market_note: MarketNote


def _move(transaction: Transaction) -> Move:
    return Move(
        transaction_id=transaction.transaction_id,
        type=transaction.type,
        roster_ids=list(transaction.roster_ids),
        adds=dict(transaction.adds),
        drops=dict(transaction.drops),
        faab=transaction.waiver_bid,
    )


def _trade(week: int, transaction: Transaction) -> Trade:
    sides: list[TradeSide] = []
    for roster_id in sorted(transaction.roster_ids, key=_sort_key):
        player_ids = sorted(
            (
                player_id
                for player_id, to_roster in transaction.adds.items()
                if to_roster == roster_id
            ),
            key=_sort_key,
        )
        picks = sorted(
            (pick for pick in transaction.draft_picks if pick.owner_id == roster_id),
            key=lambda pick: (pick.season, pick.round, _sort_key(pick.roster_id)),
        )
        faab = sum(transfer.amount for transfer in transaction.faab if transfer.receiver == roster_id)
        sides.append(
            TradeSide(roster_id=roster_id, player_ids=player_ids, picks=picks, faab=faab)
        )
    return Trade(week=week, transaction_id=transaction.transaction_id, sides=sides)


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


def compute_transactions_desk(week: WeekModel) -> TransactionsDesk:
    """Build the transactions desk for one league-week.

    Pure, deterministic, offline: two calls on equal inputs return an equal
    ``model_dump()``. ``this_week`` is the target week's own settled
    :class:`~commishdesk.ingest.Transaction` log; ``recent_trades`` spans the
    target week and the :data:`RECENT_TRADE_WEEKS` - 1 before it, newest week
    first and each week's trades by ``transaction_id``. Never raises."""
    by_week: dict[int, list[Transaction]] = {week.week: list(week.transactions)}
    for past_week, transactions in week.past_transactions.items():
        by_week.setdefault(past_week, []).extend(transactions)

    recent_trades: list[Trade] = []
    window_start = max(1, week.week - RECENT_TRADE_WEEKS + 1)
    for wk in range(week.week, window_start - 1, -1):
        for transaction in sorted(by_week.get(wk, []), key=lambda txn: txn.transaction_id):
            if transaction.type == _TRADE_TYPE:
                recent_trades.append(_trade(wk, transaction))

    return TransactionsDesk(
        week=week.week,
        this_week=[
            _move(transaction)
            for transaction in sorted(week.transactions, key=lambda txn: txn.transaction_id)
        ],
        recent_trades=recent_trades,
        market_note=_market_note(week, by_week),
    )
