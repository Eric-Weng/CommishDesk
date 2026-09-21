"""Story 5.7: ``compute_transactions_desk`` -- this week's moves, the recent
trades, and the market note.

One test per row of the spec's I/O & Edge-Case Matrix: the week-5 trade fixture
(every side's pick swaps and FAAB equal to the raw bundle), the week-10 desk (the
one waiver, the week-9 trade, the market note), a partial history, plus
determinism, the "no player display name" guard, and the import-fence pair that
keeps ``stats/transactions.py`` off the network, the store, and every later
pipeline stage.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from commishdesk.ingest import (
    FaabTransfer,
    Roster,
    TradedPick,
    Transaction,
    WeekModel,
    build_week_model,
)
from commishdesk.stats import (
    RECENT_TRADE_WEEKS,
    MarketNote,
    Move,
    Trade,
    TradeSide,
    TransactionsDesk,
    compute_transactions_desk,
)
from tests.conftest import REPO_ROOT

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
STATS_TRANSACTIONS = REPO_ROOT / "commishdesk" / "stats" / "transactions.py"

WEEK05 = "week05-trade.json"
WEEK10 = "week10-blowout.json"


def _bundle(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _run(name: str) -> TransactionsDesk:
    return compute_transactions_desk(build_week_model(_bundle(name)))


def _txn(
    transaction_id: str,
    *,
    type: str = "waiver",
    roster_ids: tuple[str, ...] = (),
    adds: dict[str, str] | None = None,
    drops: dict[str, str] | None = None,
    picks: tuple[TradedPick, ...] = (),
    faab: tuple[FaabTransfer, ...] = (),
    waiver_bid: int | None = None,
) -> Transaction:
    return Transaction(
        transaction_id=transaction_id,
        type=type,
        status="complete",
        roster_ids=list(roster_ids),
        adds=dict(adds or {}),
        drops=dict(drops or {}),
        draft_picks=list(picks),
        faab=list(faab),
        waiver_bid=waiver_bid,
    )


def _week(
    week: int,
    *,
    transactions: tuple[Transaction, ...] = (),
    past: dict[int, list[Transaction]] | None = None,
) -> WeekModel:
    return WeekModel(
        week=week,
        rosters=[Roster(roster_id=str(i)) for i in range(1, 3)],
        matchups=[],
        transactions=list(transactions),
        past_transactions=dict(past or {}),
    )


# --------------------------------------------------------------------------- #
# Row: this week's moves
# --------------------------------------------------------------------------- #


def test_this_week_lists_every_settled_move_id_only() -> None:
    week = _week(
        5,
        transactions=(
            _txn("t2", type="free_agent", roster_ids=("1",), adds={"p9": "1"}, drops={"p1": "1"}),
            _txn("t1", type="waiver", roster_ids=("2",), adds={"p2": "2"}, waiver_bid=17),
        ),
    )
    desk = compute_transactions_desk(week)

    assert [move.transaction_id for move in desk.this_week] == ["t1", "t2"]
    first = desk.this_week[0]
    assert first.type == "waiver"
    assert first.roster_ids == ["2"]
    assert first.adds == {"p2": "2"}
    assert first.drops == {}
    assert first.faab == 17
    assert desk.this_week[1].faab is None


# --------------------------------------------------------------------------- #
# Row: recent trades
# --------------------------------------------------------------------------- #


def test_recent_trades_span_the_window_newest_week_first() -> None:
    week = _week(
        5,
        transactions=(
            _txn("t_week5", type="trade", roster_ids=("1", "2"), adds={"p1": "1", "p2": "2"}),
        ),
        past={
            1: [_txn("t_week1", type="trade", roster_ids=("1", "2"))],
            2: [_txn("t_week2", type="trade", roster_ids=("1", "2"))],
            3: [_txn("t_week3", type="trade", roster_ids=("1", "2"))],
            4: [],
        },
    )
    desk = compute_transactions_desk(week)

    assert RECENT_TRADE_WEEKS == 3
    assert [trade.transaction_id for trade in desk.recent_trades] == ["t_week5", "t_week3"]


def test_a_trade_side_holds_what_that_roster_received() -> None:
    week = _week(
        5,
        transactions=(
            _txn(
                "t_trade",
                type="trade",
                roster_ids=("1", "2"),
                adds={"p1": "1", "p2": "2"},
                picks=(
                    TradedPick(season="2026", round=1, roster_id="1", owner_id="2"),
                    TradedPick(season="2027", round=3, roster_id="2", owner_id="1"),
                ),
                faab=(FaabTransfer(sender="2", receiver="1", amount=9),),
            ),
        ),
        past={1: [], 2: [], 3: [], 4: []},
    )
    desk = compute_transactions_desk(week)

    (trade,) = desk.recent_trades
    assert trade == Trade(
        week=5,
        transaction_id="t_trade",
        sides=[
            TradeSide(
                roster_id="1",
                player_ids=["p1"],
                picks=[TradedPick(season="2027", round=3, roster_id="2", owner_id="1")],
                faab=9,
            ),
            TradeSide(
                roster_id="2",
                player_ids=["p2"],
                picks=[TradedPick(season="2026", round=1, roster_id="1", owner_id="2")],
                faab=0,
            ),
        ],
    )


def test_non_trade_moves_never_reach_recent_trades() -> None:
    week = _week(
        5,
        transactions=(_txn("t_waiver", roster_ids=("1",), adds={"p1": "1"}),),
        past={1: [_txn("t2", roster_ids=("1",))], 2: [], 3: [], 4: []},
    )
    assert compute_transactions_desk(week).recent_trades == []


# --------------------------------------------------------------------------- #
# Row: the market note
# --------------------------------------------------------------------------- #


def test_market_note_reports_the_target_weeks_trade() -> None:
    week = _week(
        5,
        transactions=(_txn("t5", type="trade", roster_ids=("1", "2")),),
        past={1: [], 2: [], 3: [], 4: [_txn("t4", type="trade", roster_ids=("1", "2"))]},
    )
    assert compute_transactions_desk(week).market_note == MarketNote(
        last_trade_week=5, weeks_since_last_trade=0, complete=True
    )


def test_market_note_is_known_and_empty_when_the_league_never_traded() -> None:
    week = _week(
        5,
        transactions=(_txn("t5", roster_ids=("1",), adds={"p1": "1"}),),
        past={1: [], 2: [], 3: [], 4: []},
    )
    assert compute_transactions_desk(week).market_note == MarketNote(
        last_trade_week=None, weeks_since_last_trade=None, complete=True
    )


def test_market_note_is_unknown_when_the_history_is_partial() -> None:
    """Row: partial history -- a bundle with week ``n`` transactions only must
    not read as "no trades"."""
    week = _week(
        5,
        transactions=(_txn("t5", type="trade", roster_ids=("1", "2")),),
        past={4: [_txn("t4", type="trade", roster_ids=("1", "2"))]},
    )
    assert compute_transactions_desk(week).market_note == MarketNote(
        last_trade_week=None, weeks_since_last_trade=None, complete=False
    )


# --------------------------------------------------------------------------- #
# Row: the week-5 trade fixture
# --------------------------------------------------------------------------- #


def test_week05_trade_fixture_desk_matches_the_raw_bundle() -> None:
    bundle = _bundle(WEEK05)
    week = build_week_model(bundle)
    desk = compute_transactions_desk(week)

    raw_week = {
        str(w): [txn for txn in (bundle["transactions"].get(str(w)) or []) if txn.get("status") == "complete"]
        for w in range(1, 6)
    }
    assert [move.transaction_id for move in desk.this_week] == sorted(
        txn["transaction_id"] for txn in raw_week["5"]
    )

    raw_trades = [
        (w, txn)
        for w in range(1, 6)
        for txn in raw_week[str(w)]
        if txn.get("type") == "trade"
    ]
    assert raw_trades, "the week05-trade fixture must carry a trade"

    seasons = {pick["season"] for _, txn in raw_trades for pick in (txn.get("draft_picks") or [])}
    assert {"2026", "2027"} <= seasons

    by_id = {trade.transaction_id: trade for trade in desk.recent_trades}
    for w, txn in raw_trades:
        if w < 5 - RECENT_TRADE_WEEKS + 1:
            continue
        trade = by_id[txn["transaction_id"]]
        assert trade.week == w
        for side in trade.sides:
            roster_id = side.roster_id
            assert side.player_ids == sorted(
                (
                    player_id
                    for player_id, to_roster in (txn.get("adds") or {}).items()
                    if str(to_roster) == roster_id
                ),
                key=int,
            )
            assert {pick.season for pick in side.picks} == {
                pick["season"]
                for pick in (txn.get("draft_picks") or [])
                if str(pick.get("owner_id")) == roster_id
            }
            assert side.faab == sum(
                transfer["amount"]
                for transfer in (txn.get("waiver_budget") or [])
                if str(transfer.get("receiver")) == roster_id
            )


# --------------------------------------------------------------------------- #
# Row: the week-10 desk
# --------------------------------------------------------------------------- #


def test_week10_desk_matches_the_spec() -> None:
    desk = _run(WEEK10)

    assert desk.market_note == MarketNote(
        last_trade_week=9, weeks_since_last_trade=1, complete=True
    )

    assert len(desk.this_week) == 1
    move = desk.this_week[0]
    assert move.type == "waiver"
    assert move.faab == 25
    assert move.adds
    assert move.drops

    assert len(desk.recent_trades) == 1
    trade = desk.recent_trades[0]
    assert trade.week == 9
    sides = {side.roster_id: side for side in trade.sides}
    assert set(sides) == {"2", "4"}
    assert sides["4"].player_ids
    assert sides["2"].player_ids
    # Raw data wins over the spec matrix's wording: the fixture's 2026 R4 pick
    # is owned by roster 2 (owner_id 2, previous_owner_id 4), not roster 4.
    assert any(pick.season == "2026" and pick.round == 4 for pick in sides["2"].picks)
    assert not sides["4"].picks


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def test_deterministic_on_hand_built_input() -> None:
    week = _week(
        5,
        transactions=(_txn("t5", type="trade", roster_ids=("1", "2"), adds={"p1": "1"}),),
        past={1: [], 2: [_txn("t2", type="trade", roster_ids=("1", "2"))], 3: [], 4: []},
    )
    first = compute_transactions_desk(week)
    second = compute_transactions_desk(week)
    assert first.model_dump() == second.model_dump()
    assert first.model_dump_json() == second.model_dump_json()


@pytest.mark.parametrize("name", [WEEK05, WEEK10])
def test_deterministic_on_committed_fixtures(name: str) -> None:
    first = _run(name)
    second = _run(name)
    assert first.model_dump() == second.model_dump()
    assert first.model_dump_json() == second.model_dump_json()


# --------------------------------------------------------------------------- #
# Import fence: stats/transactions.py never reaches a later pipeline stage
# --------------------------------------------------------------------------- #


def _imported_dotted_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module)
            elif node.level == 1:
                names.add(f"commishdesk.stats.{node.module}" if node.module else "commishdesk.stats")
            elif node.level == 2:
                names.add(node.module or "commishdesk")
        elif isinstance(node, ast.Call):
            func = node.func
            fname = getattr(func, "attr", None) or getattr(func, "id", None)
            if fname in {"import_module", "__import__"}:
                names.update(a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str))
    return names


def test_transactions_never_reaches_the_network_adapters_store_or_a_later_stage() -> None:
    imported = _imported_dotted_names(STATS_TRANSACTIONS)
    forbidden_roots = {"adapters", "store", "facts", "narrate", "render", "deliver", "statmods"}
    offenders = [
        name
        for name in imported
        if name.split(".")[:1] == ["commishdesk"] and len(name.split(".")) > 1 and name.split(".")[1] in forbidden_roots
    ]
    assert not offenders, offenders
    assert "commishdesk.ingest" in imported


def test_transactions_imports_no_clock_prng_or_filesystem_module() -> None:
    imported = _imported_dotted_names(STATS_TRANSACTIONS)
    banned = {"datetime", "time", "os", "pathlib", "random", "secrets"}
    hits = {name for name in imported if name.split(".")[0] in banned}
    assert not hits, hits


def test_transactions_output_carries_no_player_display_name_field() -> None:
    for cls in (Move, TradeSide, Trade, MarketNote, TransactionsDesk):
        assert "name" not in cls.model_fields
        assert "display_name" not in cls.model_fields
        assert "manager" not in cls.model_fields


def test_transactions_names_are_reachable_via_the_package_re_export() -> None:
    import commishdesk.stats as stats

    assert stats.compute_transactions_desk is compute_transactions_desk
    for name in (
        "RECENT_TRADE_WEEKS",
        "MarketNote",
        "Move",
        "Trade",
        "TradeSide",
        "TransactionsDesk",
        "compute_transactions_desk",
    ):
        assert name in stats.__all__



# --------------------------------------------------------------------------- #
# Review patches: ordering the first pass left unpinned
# --------------------------------------------------------------------------- #


def test_trade_sides_players_and_picks_are_ordered_numerically_not_by_input() -> None:
    week = _week(
        5,
        transactions=(
            _txn(
                "t_trade",
                type="trade",
                roster_ids=("10", "2"),
                adds={"p9": "10", "p10": "10", "p3": "2"},
                picks=(
                    TradedPick(season="2027", round=1, roster_id="4", owner_id="10"),
                    TradedPick(season="2026", round=3, roster_id="5", owner_id="10"),
                    TradedPick(season="2026", round=2, roster_id="9", owner_id="10"),
                ),
            ),
        ),
        past={1: [], 2: [], 3: [], 4: []},
    )
    (trade,) = compute_transactions_desk(week).recent_trades

    assert [side.roster_id for side in trade.sides] == ["2", "10"]
    ten = trade.sides[1]
    assert [(pick.season, pick.round) for pick in ten.picks] == [
        ("2026", 2),
        ("2026", 3),
        ("2027", 1),
    ]


def test_two_trades_in_one_week_are_ordered_by_transaction_id() -> None:
    week = _week(
        5,
        transactions=(
            _txn("b_trade", type="trade", roster_ids=("1", "2")),
            _txn("a_trade", type="trade", roster_ids=("1", "2")),
        ),
        past={1: [], 2: [], 3: [], 4: [_txn("z_prior", type="trade", roster_ids=("1", "2"))]},
    )
    desk = compute_transactions_desk(week)

    assert [(trade.week, trade.transaction_id) for trade in desk.recent_trades] == [
        (5, "a_trade"),
        (5, "b_trade"),
        (4, "z_prior"),
    ]
