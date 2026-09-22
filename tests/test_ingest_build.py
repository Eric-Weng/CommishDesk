"""Story 5.3a: the shape-agnostic weekly ingest model (``build_week_model``).

One test per I/O & Edge-Case Matrix row (regular-season week, playoff week,
eliminated roster, failed/non-final transaction, malformed bundle), plus the
never-raise guarantees (no opponent, orphan roster, empty starter slot),
determinism, and coverage of the committed ``week17-playoffs`` /
``week08-median`` fixtures per the story's own Tasks list.

Story 5.3b adds: ``build_player_snapshot`` / ``get_player_snapshot`` (the
persisted-vs-fresh branching against a fake ``Store``), and the committed
NFL bye-week data loader (``commishdesk/ingest/byes.py``, ``nfl_byes.toml``)
-- no dedicated test file was carved out for the bye data in this story's own
Code Map, so its I/O & Edge-Case Matrix rows land here instead.

Story 5.8 adds: ``build_player_names`` (the name-join counterpart to
``build_player_snapshot``).
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

import pytest

from commishdesk.errors import CommishDeskError, IngestError
from commishdesk.ingest import (
    BracketMatch,
    FaabTransfer,
    Matchup,
    PlayerSnapshot,
    Roster,
    TradedPick,
    Transaction,
    WeekModel,
    build_player_names,
    build_player_snapshot,
    build_week_model,
    bye_teams,
    get_player_snapshot,
    load_byes,
)
from commishdesk.ingest import byes as byes_module
from tests.conftest import REPO_ROOT

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"


def _load_fixture(name: str) -> dict[str, Any]:
    """The raw fixture bundle. ``build_week_model`` reads ``rosters`` /
    ``matchups`` / ``transactions`` plus (Story 5.4) ``league`` (for
    ``playoff_week_start``) and ``winners_bracket``/``losers_bracket``; every
    other key is simply ignored, exactly like ``build_league_model`` ignores
    an unknown bundle key."""
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _synthetic_bundle() -> dict[str, Any]:
    """A minimal, valid two-roster, two-week bundle -- the base for the
    edge-case rows so each mutation is isolated and obvious."""
    return {
        "rosters": [
            {
                "roster_id": 1,
                "reserve": ["900"],
                "taxi": ["901"],
                "settings": {
                    "wins": 1, "losses": 1, "ties": 0,
                    "fpts": 200, "fpts_decimal": 50,
                    "fpts_against": 190, "fpts_against_decimal": 25,
                    "ppts": 210, "ppts_decimal": 75,
                },
            },
            {
                "roster_id": 2,
                "reserve": None,
                "taxi": None,
                "settings": {"wins": 1, "losses": 1, "ties": 0},
            },
        ],
        "matchups": {
            "1": [
                {
                    "roster_id": 1, "matchup_id": 1, "points": 100.5,
                    "starters": ["10", "11"], "starters_points": [50.0, 50.5],
                    "players": ["10", "11", "12"],
                    "players_points": {"10": 50.0, "11": 50.5, "12": 0.0},
                },
                {
                    "roster_id": 2, "matchup_id": 1, "points": 99.0,
                    "starters": ["20", "21"], "starters_points": [49.0, 50.0],
                    "players": ["20", "21"],
                    "players_points": {"20": 49.0, "21": 50.0},
                },
            ],
            "2": [
                {
                    "roster_id": 1, "matchup_id": 5, "points": 88.0,
                    "starters": ["10", "11"], "starters_points": [40.0, 48.0],
                    "players": ["10", "11"],
                    "players_points": {"10": 40.0, "11": 48.0},
                },
                {
                    "roster_id": 2, "matchup_id": 6, "points": 77.0,
                    "starters": ["20", "21"], "starters_points": [37.0, 40.0],
                    "players": ["20", "21"],
                    "players_points": {"20": 37.0, "21": 40.0},
                },
            ],
        },
        "transactions": {
            "1": [
                {
                    "transaction_id": "txn_w1",
                    "type": "waiver",
                    "status": "complete",
                    "roster_ids": [1],
                    "adds": {"30": 1},
                    "drops": None,
                    "settings": {"waiver_bid": 12},
                },
            ],
            "2": [
                {
                    "transaction_id": "txn_ok",
                    "type": "free_agent",
                    "status": "complete",
                    "roster_ids": [2],
                    "adds": {"40": 2},
                    "drops": {"41": 2},
                },
                {
                    "transaction_id": "txn_pending",
                    "type": "waiver",
                    "status": "pending",
                    "roster_ids": [1],
                    "adds": {"50": 1},
                },
            ],
        },
    }


# --------------------------------------------------------------------------- #
# I/O & Edge-Case Matrix
# --------------------------------------------------------------------------- #


def test_regular_season_week_matchups_cumulative_target_week_only_transactions() -> None:
    """Row: Regular-season week -- ``week08-median.json`` (``target_week`` 8,
    ``playoff_week_start`` 15)."""
    bundle = _load_fixture("week08-median.json")
    model = build_week_model(bundle)

    assert isinstance(model, WeekModel)
    assert model.week == 8
    weeks_present = {m.week for m in model.matchups}
    assert weeks_present == set(range(1, 9))
    assert len(model.rosters) == 12
    assert model.transactions  # week 8 has settled transactions in this fixture
    assert all(isinstance(t, Transaction) for t in model.transactions)


def test_playoff_week_week17_playoffs_fixture() -> None:
    """Row: Playoff week -- ``week17-playoffs.json`` (``target_week`` 17,
    ``playoff_week_start`` 15). Asserts the matchup/roster/transaction
    surface builds cleanly for the championship-round fixture, plus (Story
    5.4) the playoff shape now carried on ``WeekModel``."""
    bundle = _load_fixture("week17-playoffs.json")
    model = build_week_model(bundle)

    assert model.week == 17
    assert {m.week for m in model.matchups} == set(range(1, 18))
    assert len(model.rosters) == 12
    assert model.playoff_week_start == 15
    assert model.winners_bracket
    assert model.losers_bracket
    assert all(isinstance(b, BracketMatch) for b in model.winners_bracket + model.losers_bracket)


# --------------------------------------------------------------------------- #
# Story 5.4: playoff_week_start + BracketMatch
# --------------------------------------------------------------------------- #


def test_playoff_week_start_and_brackets_absent_when_league_key_missing() -> None:
    """A bundle predating this story (no ``league`` key at all, like
    ``_synthetic_bundle``) never raises -- ``playoff_week_start`` is ``None``
    and both bracket lists are empty."""
    model = build_week_model(_synthetic_bundle())
    assert model.playoff_week_start is None
    assert model.winners_bracket == []
    assert model.losers_bracket == []


def test_playoff_week_start_read_from_league_settings_even_pre_playoff() -> None:
    """``playoff_week_start`` is read from ``league.settings`` regardless of
    whether the target week has reached the playoffs -- ``week10-blowout``'s
    target week (10) is pre-playoff, but the league's declared start (15) is
    still carried."""
    model = build_week_model(_load_fixture("week10-blowout.json"))
    assert model.playoff_week_start == 15
    assert model.winners_bracket == []
    assert model.losers_bracket == []


def test_bracket_matches_carry_round_and_both_roster_ids() -> None:
    """``week17-playoffs.json``'s round-3 winners bracket has the
    championship (rosters 1 vs 2) and third-place game (rosters 4 vs 5); the
    round-3 losers bracket has the two consolation placement games (rosters
    11 vs 12, 8 vs 10) -- cross-checked against the fixture's own raw
    ``winners_bracket``/``losers_bracket`` sections."""
    model = build_week_model(_load_fixture("week17-playoffs.json"))

    round3_winners = {frozenset(b.roster_ids) for b in model.winners_bracket if b.round == 3}
    round3_losers = {frozenset(b.roster_ids) for b in model.losers_bracket if b.round == 3}
    assert round3_winners == {frozenset({"1", "2"}), frozenset({"4", "5"})}
    assert round3_losers == {frozenset({"11", "12"}), frozenset({"8", "10"})}


def test_bracket_row_missing_t1_or_t2_is_skipped_never_raises() -> None:
    bundle = _synthetic_bundle()
    bundle["winners_bracket"] = [
        {"r": 1, "t1": 1, "t2": 2},
        {"r": 1, "t1": 3},  # missing t2 -- a bye or undecided slot, skipped
        {"r": "bad", "t1": 4, "t2": 5},  # unparseable round, skipped
        "not-an-object",  # skipped
    ]
    model = build_week_model(bundle)
    assert model.winners_bracket == [BracketMatch(round=1, roster_ids=["1", "2"])]


def test_bracket_row_with_t1_equal_t2_is_skipped_never_raises() -> None:
    """A degenerate self-paired row (``t1 == t2``) is skipped -- it would
    otherwise build a ``BracketMatch`` whose ``roster_ids`` names the same
    roster twice. Covers both a same-type match (``1 == 1``) and a
    cross-type one (``int`` ``1`` vs ``str`` ``"1"``, compared as strings)."""
    bundle = _synthetic_bundle()
    bundle["winners_bracket"] = [
        {"r": 1, "t1": 1, "t2": 1},
        {"r": 1, "t1": "2", "t2": 2},
        {"r": 1, "t1": 3, "t2": 4},
    ]
    model = build_week_model(bundle)
    assert model.winners_bracket == [BracketMatch(round=1, roster_ids=["3", "4"])]


def test_bracket_section_non_list_is_ignored_never_raises() -> None:
    bundle = _synthetic_bundle()
    bundle["losers_bracket"] = {"not": "a list"}
    model = build_week_model(bundle)
    assert model.losers_bracket == []


def test_eliminated_roster_has_no_matchup_row_that_week_but_builds_without_raising() -> None:
    """Row: Eliminated roster -- ``week17-playoffs.json`` fixture: rosters
    3, 6, 7, 9 are absent from ``matchups["17"]`` (eliminated in an earlier
    round). No ``Matchup`` row for that roster/week, no raise -- and those
    same rosters DO have rows in earlier weeks, confirming the row is simply
    missing, not the whole roster dropped from the model."""
    model = build_week_model(_load_fixture("week17-playoffs.json"))

    week17_roster_ids = {m.roster_id for m in model.matchups if m.week == 17}
    assert {"3", "6", "7", "9"} & week17_roster_ids == set()
    week1_roster_ids = {m.roster_id for m in model.matchups if m.week == 1}
    assert {"3", "6", "7", "9"} <= week1_roster_ids
    assert {r.roster_id for r in model.rosters} >= {"3", "6", "7", "9"}


def test_failed_or_non_final_transaction_is_dropped_before_the_model_is_built() -> None:
    """Row: Failed/non-final transaction -- a ``status != "complete"`` entry
    never reaches a :class:`Transaction`."""
    bundle = _synthetic_bundle()
    model = build_week_model(bundle)

    ids = {t.transaction_id for t in model.transactions}
    assert "txn_ok" in ids
    assert "txn_pending" not in ids
    assert all(t.status == "complete" for t in model.transactions)


def test_malformed_bundle_missing_rosters_raises_ingest_error_chained() -> None:
    """Row: Malformed bundle -- ``build_week_model(bad_bundle)`` (missing
    ``rosters``) raises :class:`IngestError`, chained, never a partial model."""
    bundle = _synthetic_bundle()
    del bundle["rosters"]

    with pytest.raises(IngestError) as exc_info:
        build_week_model(bundle)
    assert "rosters" in str(exc_info.value)
    assert exc_info.value.__cause__ is not None
    assert isinstance(exc_info.value, CommishDeskError)


def test_bundle_that_is_not_a_mapping_raises_ingest_error() -> None:
    with pytest.raises(IngestError, match="not a JSON object"):
        build_week_model(None)  # type: ignore[arg-type]


def test_empty_rosters_is_a_structural_failure() -> None:
    bundle = _synthetic_bundle()
    bundle["rosters"] = []
    with pytest.raises(IngestError, match="no rosters"):
        build_week_model(bundle)


def test_missing_matchups_section_raises_ingest_error() -> None:
    bundle = _synthetic_bundle()
    del bundle["matchups"]
    with pytest.raises(IngestError) as exc_info:
        build_week_model(bundle)
    assert "matchups" in str(exc_info.value)


def test_matchups_with_no_weeks_at_all_raises_ingest_error() -> None:
    bundle = _synthetic_bundle()
    bundle["matchups"] = {}
    with pytest.raises(IngestError):
        build_week_model(bundle)


def test_non_object_matchup_row_raises_ingest_error() -> None:
    bundle = _synthetic_bundle()
    bundle["matchups"]["1"].append("not a matchup object")
    with pytest.raises(IngestError):
        build_week_model(bundle)


# --------------------------------------------------------------------------- #
# Never-raise guarantees (AC3): no opponent, orphan roster, empty starter slot
# --------------------------------------------------------------------------- #


def test_no_opponent_bye_builds_without_raising() -> None:
    bundle = _synthetic_bundle()
    # roster 2's week-2 row already carries a distinct matchup_id (6) from
    # roster 1's (5) -- a bye: no shared matchup_id, so no opponent.
    model = build_week_model(bundle)
    week2 = {m.roster_id: m for m in model.matchups if m.week == 2}
    assert week2["1"].opponent_roster_id is None
    assert week2["2"].opponent_roster_id is None


def test_orphan_roster_in_a_matchup_row_builds_without_raising() -> None:
    """A matchup row referencing a ``roster_id`` absent from the ``rosters``
    section (no season-level :class:`Roster` counterpart) still produces a
    :class:`Matchup` row -- the two lists are independent, like ``Team`` /
    ``Pick`` in the league model."""
    bundle = _synthetic_bundle()
    bundle["matchups"]["1"].append(
        {
            "roster_id": 99,
            "matchup_id": 1,
            "points": 12.0,
            "starters": [],
            "starters_points": [],
            "players": [],
            "players_points": {},
        }
    )
    model = build_week_model(bundle)
    assert "99" in {m.roster_id for m in model.matchups if m.week == 1}
    assert "99" not in {r.roster_id for r in model.rosters}


def test_empty_starter_slot_builds_without_raising() -> None:
    bundle = _synthetic_bundle()
    bundle["matchups"]["1"][0]["starters"] = ["10", "", "11"]
    model = build_week_model(bundle)
    row = next(m for m in model.matchups if m.week == 1 and m.roster_id == "1")
    assert row.starters == ["10", "", "11"]


# --------------------------------------------------------------------------- #
# Season totals: fpts/fpts_decimal combining, IR, taxi
# --------------------------------------------------------------------------- #


def test_season_totals_combine_whole_and_decimal_fields() -> None:
    model = build_week_model(_synthetic_bundle())
    roster = next(r for r in model.rosters if r.roster_id == "1")
    assert roster.wins == 1 and roster.losses == 1 and roster.ties == 0
    assert roster.fpts == 200.50
    assert roster.fpts_against == 190.25
    assert roster.ppts == 210.75
    assert roster.ir == ["900"]
    assert roster.taxi == ["901"]


def test_null_reserve_and_taxi_default_to_empty_lists() -> None:
    model = build_week_model(_synthetic_bundle())
    roster = next(r for r in model.rosters if r.roster_id == "2")
    assert roster.ir == []
    assert roster.taxi == []


def test_week17_playoffs_roster_one_season_totals_match_the_raw_fixture() -> None:
    """Cross-check against the raw ``week17-playoffs.json`` values read
    directly (roster 1: wins 10, fpts 2766 + .12, ppts 3051 + .01, reserve
    ["11576"])."""
    model = build_week_model(_load_fixture("week17-playoffs.json"))
    roster = next(r for r in model.rosters if r.roster_id == "1")
    assert roster.wins == 10
    assert roster.losses == 4
    assert roster.ties == 0
    assert roster.fpts == pytest.approx(2766.12)
    assert roster.ppts == pytest.approx(3051.01)
    assert roster.ir == ["11576"]


# --------------------------------------------------------------------------- #
# Transaction assets: adds/drops, traded picks, FAAB
# --------------------------------------------------------------------------- #


def test_week01_openers_trade_carries_traded_picks_and_faab() -> None:
    """``week01-openers.json``'s target week (1) is also the week a real trade
    (``id_gdruqdii4p``) was made, so both a traded pick and a FAAB transfer
    land in the built week's transaction log -- no player-display-name join,
    ids only."""
    model = build_week_model(_load_fixture("week01-openers.json"))
    by_id = {t.transaction_id: t for t in model.transactions}
    first = by_id["id_gdruqdii4p"]
    assert first.type == "trade"
    assert first.roster_ids == ["1", "10"]
    assert first.adds == {"5937": "10"}
    assert first.drops == {"5937": "1"}
    assert first.draft_picks == [
        TradedPick(season="2025", round=3, roster_id="10", owner_id="1", previous_owner_id="10")
    ]
    assert first.faab == [FaabTransfer(sender="1", receiver="10", amount=10)]


def test_only_the_target_weeks_transactions_are_modeled() -> None:
    """``txn_w1`` lives in week 1 of the synthetic bundle, but the bundle's
    target week (the max ``matchups`` key) is 2 -- only week 2's transactions
    are modeled, confirming week selection isn't "every settled transaction
    in the bundle"."""
    model = build_week_model(_synthetic_bundle())
    assert "txn_w1" not in {t.transaction_id for t in model.transactions}
    ok = next(t for t in model.transactions if t.transaction_id == "txn_ok")
    assert ok.waiver_bid is None  # this transaction's settings carry no waiver_bid


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["week08-median.json", "week17-playoffs.json"])
def test_build_is_deterministic_regardless_of_bundle_list_order(name: str) -> None:
    bundle = _load_fixture(name)
    shuffled = copy.deepcopy(bundle)
    shuffled["rosters"] = list(reversed(shuffled["rosters"]))
    for week_rows in shuffled["matchups"].values():
        week_rows.reverse()
    for txn_rows in shuffled["transactions"].values():
        txn_rows.reverse()

    a = build_week_model(bundle)
    b = build_week_model(shuffled)
    assert a.model_dump() == b.model_dump()


# --------------------------------------------------------------------------- #
# Sanity: the model classes themselves
# --------------------------------------------------------------------------- #


def test_week_model_exposes_the_expected_shape() -> None:
    model = build_week_model(_synthetic_bundle())
    assert isinstance(model, WeekModel)
    assert all(isinstance(r, Roster) for r in model.rosters)
    assert all(isinstance(m, Matchup) for m in model.matchups)
    assert all(isinstance(t, Transaction) for t in model.transactions)


# --------------------------------------------------------------------------- #
# Story 5.7: next_matchups and past_transactions
# --------------------------------------------------------------------------- #


def test_a_bundle_without_the_story_5_7_sections_builds_with_both_fields_empty() -> None:
    bundle = _synthetic_bundle()
    assert "next_matchups" not in bundle

    model = build_week_model(bundle)

    assert model.next_matchups == []
    # the synthetic bundle carries week 1's transactions: week 1 < target week 2
    assert set(model.past_transactions) == {1}


def test_next_matchups_are_pairing_only_rows_for_the_week_after_the_target() -> None:
    bundle = _synthetic_bundle()
    bundle["next_matchups"] = [
        {"roster_id": 2, "matchup_id": 7},
        {"roster_id": 1, "matchup_id": 7},
        {"roster_id": 3, "matchup_id": 8},  # lone row: a bye
        {"roster_id": 4, "matchup_id": None},
        "not-a-row",
        {"matchup_id": 9},  # no roster_id
    ]

    model = build_week_model(bundle)

    assert [(m.roster_id, m.opponent_roster_id, m.matchup_id) for m in model.next_matchups] == [
        ("1", "2", 7),
        ("2", "1", 7),
        ("3", None, 8),
        ("4", None, None),
    ]
    assert {m.week for m in model.next_matchups} == {model.week + 1}
    assert all(m.points == 0.0 and m.starters == [] and m.players_points == {} for m in model.next_matchups)


def test_a_non_list_next_matchups_section_is_ignored() -> None:
    bundle = _synthetic_bundle()
    bundle["next_matchups"] = {"1": []}
    assert build_week_model(bundle).next_matchups == []


def test_past_transactions_hold_only_settled_earlier_weeks_keyed_by_week() -> None:
    bundle = _synthetic_bundle()
    week = build_week_model(bundle).week
    settled = {
        "transaction_id": "txn_prior",
        "type": "waiver",
        "status": "complete",
        "roster_ids": [1],
        "adds": {"111": 1},
        "drops": {},
    }
    failed = {**settled, "transaction_id": "txn_failed", "status": "failed"}
    bundle["transactions"]["1"] = [failed, settled]

    model = build_week_model(bundle)

    assert [t.transaction_id for t in model.past_transactions[1]] == ["txn_prior"]
    assert all(wk < week for wk in model.past_transactions)
    # the target week's own moves stay on ``transactions``, not the history
    assert week not in model.past_transactions
    assert "txn_ok" in {t.transaction_id for t in model.transactions}


def test_a_week_with_nothing_settled_still_gets_a_history_key() -> None:
    bundle = _synthetic_bundle()
    bundle["transactions"]["1"] = []
    assert build_week_model(bundle).past_transactions == {1: []}


def test_a_null_earlier_week_body_becomes_an_empty_history_week() -> None:
    bundle = _synthetic_bundle()
    bundle["transactions"]["1"] = None
    assert build_week_model(bundle).past_transactions == {1: []}


def test_a_committed_fixture_builds_the_full_history_and_the_next_weeks_pairings() -> None:
    model = build_week_model(_load_fixture("week10-blowout.json"))

    assert set(model.past_transactions) == set(range(1, 10))
    assert len(model.next_matchups) == 12
    assert {m.week for m in model.next_matchups} == {11}
    assert all(m.opponent_roster_id is not None for m in model.next_matchups)


# --------------------------------------------------------------------------- #
# Story 5.3b: build_player_snapshot
# --------------------------------------------------------------------------- #


def _players_bundle(players: dict[str, Any] | None) -> dict[str, Any]:
    bundle = _synthetic_bundle()
    if players is not None:
        bundle["players"] = players
    return bundle


def test_build_player_snapshot_reads_the_bundles_players_key() -> None:
    bundle = _players_bundle(
        {
            "10": {"position": "RB", "team": "KC", "college": "Alabama"},
            "20": {"position": "WR", "team": None},
        }
    )
    snapshot = build_player_snapshot(bundle)
    assert snapshot == {
        "10": PlayerSnapshot(player_id="10", position="RB", nfl_team="KC"),
        "20": PlayerSnapshot(player_id="20", position="WR", nfl_team=None),
    }


def test_build_player_snapshot_missing_players_key_returns_empty_map() -> None:
    """An older bundle (or one from a hand-written fake ``Adapter`` that
    predates this story) simply has no ``"players"`` key -- never a raise."""
    bundle = _players_bundle(None)
    assert build_player_snapshot(bundle) == {}


def test_build_player_snapshot_non_mapping_players_section_raises_ingest_error() -> None:
    bundle = _players_bundle(["not", "an", "object"])
    with pytest.raises(IngestError, match="players"):
        build_player_snapshot(bundle)


def test_build_player_snapshot_non_object_item_raises_ingest_error_chained() -> None:
    bundle = _players_bundle({"10": "not-an-object"})
    with pytest.raises(IngestError) as exc_info:
        build_player_snapshot(bundle)
    assert isinstance(exc_info.value, CommishDeskError)


def test_build_player_snapshot_bundle_not_a_mapping_raises_ingest_error() -> None:
    with pytest.raises(IngestError, match="not a JSON object"):
        build_player_snapshot(None)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Story 5.8: build_player_names
# --------------------------------------------------------------------------- #


def test_build_player_names_joins_first_and_last_name() -> None:
    bundle = _players_bundle(
        {
            "10": {"first_name": "Bijan", "last_name": "Robinson"},
            "20": {"first_name": "Ja'Marr", "last_name": "Chase"},
        }
    )
    assert build_player_names(bundle) == {"10": "Bijan Robinson", "20": "Ja'Marr Chase"}


def test_build_player_names_sanitizes_each_half() -> None:
    """A league-supplied string never reaches a model field unsanitized; the
    name join is no exception (AD-24)."""
    from commishdesk.ingest import sanitize

    bundle = _players_bundle({"10": {"first_name": "A\u0000B", "last_name": "C"}})
    (name,) = build_player_names(bundle).values()
    assert name == f"{sanitize('A\u0000B').strip()} {sanitize('C').strip()}".strip()


def test_build_player_names_missing_players_key_returns_empty_map() -> None:
    assert build_player_names(_players_bundle(None)) == {}


def test_build_player_names_blank_halves_collapse_to_an_empty_string() -> None:
    bundle = _players_bundle({"10": {"first_name": "", "last_name": ""}})
    assert build_player_names(bundle) == {"10": ""}


def test_build_player_names_missing_last_name_contributes_no_token() -> None:
    bundle = _players_bundle({"10": {"first_name": "Bijan"}})
    assert build_player_names(bundle) == {"10": "Bijan"}


def test_build_player_names_missing_first_name_contributes_no_token() -> None:
    bundle = _players_bundle({"10": {"last_name": "Robinson"}})
    assert build_player_names(bundle) == {"10": "Robinson"}


def test_build_player_names_non_mapping_players_section_raises_ingest_error() -> None:
    bundle = _players_bundle(["not", "an", "object"])
    with pytest.raises(IngestError, match="players"):
        build_player_names(bundle)


def test_build_player_names_non_object_item_raises_ingest_error_chained() -> None:
    bundle = _players_bundle({"10": "not-an-object"})
    with pytest.raises(IngestError) as exc_info:
        build_player_names(bundle)
    assert isinstance(exc_info.value, CommishDeskError)
    assert exc_info.value.__cause__ is not None


def test_build_player_names_bundle_not_a_mapping_raises_ingest_error() -> None:
    with pytest.raises(IngestError, match="not a JSON object"):
        build_player_names(None)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Story 5.3b: get_player_snapshot -- persisted-vs-fresh branching (AC3, FR-5)
# --------------------------------------------------------------------------- #


class _FakeSnapshotStore:
    """A minimal, in-memory duck-typed fake of the two ``Store`` methods
    ``get_player_snapshot`` calls -- it deliberately does not subclass
    ``Store`` (``ingest/build.py`` only ever calls these two methods on the
    object it is handed, never ``isinstance``-checks it)."""

    def __init__(self) -> None:
        self.reads = 0
        self.writes = 0
        self._data: dict[tuple[str, int], dict[str, PlayerSnapshot]] = {}

    def read_player_snapshot(self, league_id: str, week: int) -> dict[str, PlayerSnapshot] | None:
        self.reads += 1
        return self._data.get((league_id, week))

    def write_player_snapshot(self, league_id: str, week: int, snapshot: Any) -> None:
        self.writes += 1
        self._data[(league_id, week)] = dict(snapshot)


def test_get_player_snapshot_first_generation_builds_and_persists() -> None:
    """Row: first generation of week ``n`` -- no persisted snapshot yet, so a
    fresh one is built from the bundle and persisted."""
    store = _FakeSnapshotStore()
    bundle = _players_bundle({"10": {"position": "RB", "team": "KC"}})

    snapshot = get_player_snapshot(store, "league-1", 5, bundle)

    assert snapshot == {"10": PlayerSnapshot(player_id="10", position="RB", nfl_team="KC")}
    assert store.writes == 1
    assert store.read_player_snapshot("league-1", 5) == snapshot


def test_get_player_snapshot_regeneration_reuses_the_persisted_snapshot_verbatim() -> None:
    """Row: regeneration of week ``n`` (a trade happened since) -- the
    persisted snapshot is read back and used **verbatim**, never re-derived
    from a fresher bundle (FR-5). This is the story's central guarantee."""
    store = _FakeSnapshotStore()
    original_bundle = _players_bundle({"10": {"position": "RB", "team": "KC"}})
    first = get_player_snapshot(store, "league-1", 5, original_bundle)
    assert store.writes == 1

    # A trade happened: a regeneration's bundle now reflects the new team.
    traded_bundle = _players_bundle({"10": {"position": "RB", "team": "BUF"}})
    second = get_player_snapshot(store, "league-1", 5, traded_bundle)

    assert second == first  # the pre-trade snapshot, unchanged
    assert second["10"].nfl_team == "KC"  # not "BUF" -- never re-derived live
    assert store.writes == 1  # no second write -- the persisted value was reused


def test_get_player_snapshot_different_weeks_each_build_and_persist_independently() -> None:
    store = _FakeSnapshotStore()
    bundle = _players_bundle({"10": {"position": "RB", "team": "KC"}})

    get_player_snapshot(store, "league-1", 5, bundle)
    get_player_snapshot(store, "league-1", 6, bundle)

    assert store.writes == 2
    assert store.read_player_snapshot("league-1", 5) is not None
    assert store.read_player_snapshot("league-1", 6) is not None


def test_get_player_snapshot_backfill_first_generation_long_after_records_current_team() -> None:
    """Row: backfill -- week ``n`` generated for the first time long after the
    fact. No persisted snapshot exists yet (same as any first generation), so
    ``get_player_snapshot`` builds from whatever the bundle says *today* --
    there is no historical source to prefer. This is the same code path as
    "first generation" (there is no way for the function to tell the two
    apart), which is exactly the documented, accepted limitation in
    docs/EDGE-CASES.md, not a bug: once this first (backfilled) snapshot is
    persisted, it is frozen from then on like any other."""
    store = _FakeSnapshotStore()
    bundle = _players_bundle({"10": {"position": "RB", "team": "BUF"}})  # "today's" team

    snapshot = get_player_snapshot(store, "league-1", 5, bundle)

    assert snapshot["10"].nfl_team == "BUF"
    assert store.writes == 1
    # Frozen from here on, same as the non-backfill case.
    assert store.read_player_snapshot("league-1", 5)["10"].nfl_team == "BUF"


# --------------------------------------------------------------------------- #
# Story 5.3b: committed NFL bye-week data (commishdesk/ingest/byes.py)
# --------------------------------------------------------------------------- #


def test_packaged_bye_file_is_structurally_valid_for_ci() -> None:
    """AC: given CI, the packaged bye file is structurally validated -- every
    season's keys are exactly the 32 known NFL abbreviations, every value an
    int in 1..18. Exercises the real committed ``nfl_byes.toml`` (a 2026
    table, cross-checked against two independent published sources), not a
    monkeypatched stand-in."""
    table = load_byes(2026)
    assert table is not None
    assert set(table) == byes_module._KNOWN_NFL_TEAMS
    assert all(isinstance(week, int) and 1 <= week <= 18 for week in table.values())


def test_bye_teams_known_season_in_range_week_returns_the_on_bye_set() -> None:
    """Row: known season, in-range week -- exercises the real committed
    2026 table."""
    table = load_byes(2026)
    assert table is not None
    team, week = next(iter(table.items()))
    result = bye_teams(2026, week)
    assert result is not None
    assert team in result
    assert result == frozenset(t for t, w in table.items() if w == week)


def test_bye_teams_season_not_in_file_returns_none_and_logs_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Row: season not in the file -- returns None (unavailable); one warning
    names the missing season."""
    caplog.set_level(logging.WARNING, logger="commishdesk.ingest.byes")
    result = bye_teams(2031, 8)
    assert result is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings
    assert all("2031" in r.getMessage() for r in warnings)


def test_load_byes_season_not_in_file_returns_none() -> None:
    assert load_byes(2031) is None


def test_malformed_packaged_bye_file_raises_chained_ingest_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row: malformed bye file -- raises IngestError, chained, at first load."""
    byes_module._clear_cache()
    monkeypatch.setattr(byes_module, "_byes_toml_text", lambda: "not [ valid toml")
    try:
        with pytest.raises(IngestError) as exc_info:
            load_byes(2026)
        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value, CommishDeskError)
    finally:
        byes_module._clear_cache()


def test_malformed_bye_file_failure_is_cached_not_retried_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed packaged file is only ever read once -- the failure is
    cached (`functools.lru_cache`), not re-parsed on every subsequent call."""
    byes_module._clear_cache()
    calls = 0

    def _boom() -> str:
        nonlocal calls
        calls += 1
        return "not [ valid toml"

    monkeypatch.setattr(byes_module, "_byes_toml_text", _boom)
    try:
        for _ in range(3):
            with pytest.raises(IngestError):
                load_byes(2026)
        assert calls == 1
    finally:
        byes_module._clear_cache()


def _toml_document(season: str, table: dict[str, Any]) -> str:
    """A minimal valid-TOML-*syntax* rendering of one ``[<season>]`` table --
    used to feed a structurally-*invalid* (wrong keys / out-of-range values)
    but syntactically-valid document into ``byes.py``'s validator, isolating
    the "bad shape" failure mode from the "bad TOML syntax" one already
    covered by ``test_malformed_packaged_bye_file_raises_chained_ingest_error``."""
    lines = [f"[{season}]"]
    for key, value in table.items():
        rendered = ("true" if value else "false") if isinstance(value, bool) else str(value)
        lines.append(f"{key} = {rendered}")
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    "bad_table",
    [
        pytest.param({team: 1 for team in list(byes_module._KNOWN_NFL_TEAMS)[:-1]}, id="missing_a_team"),
        pytest.param(
            {**{team: 1 for team in byes_module._KNOWN_NFL_TEAMS}, "XYZ": 1}, id="extra_unknown_team"
        ),
        pytest.param(
            {**{team: 1 for team in byes_module._KNOWN_NFL_TEAMS}, "ARI": 19}, id="week_out_of_range_high"
        ),
        pytest.param(
            {**{team: 1 for team in byes_module._KNOWN_NFL_TEAMS}, "ARI": 0}, id="week_out_of_range_low"
        ),
        pytest.param(
            {**{team: 1 for team in byes_module._KNOWN_NFL_TEAMS}, "ARI": True}, id="bool_week_rejected"
        ),
    ],
)
def test_malformed_season_table_shapes_raise_ingest_error(
    monkeypatch: pytest.MonkeyPatch, bad_table: dict[str, Any]
) -> None:
    byes_module._clear_cache()
    monkeypatch.setattr(byes_module, "_byes_toml_text", lambda: _toml_document("2099", bad_table))
    try:
        with pytest.raises(IngestError):
            load_byes(2099)
    finally:
        byes_module._clear_cache()
