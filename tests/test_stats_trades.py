"""Story 9.6: ``compute_trade_grades`` -- instant trade grades and revisit numbers.

One test per row of the spec's I/O & Edge-Case Matrix on a small hand-built league, the scale
boundaries (0.05 / 0.10 / 0.15 / 0.30), mirroring, no ``F``, a byte-identical rebuild, the
no-snapshot path, the import fence, the facts hook, and the committed worked example the concept
doc quotes (``tests/fixtures/trade_grades/worked-example.json``).

World: a 4-team league (slots RB, WR, FLEX) at week 8 with a 2-team playoff bracket, so rosters 1
and 2 are alive and rosters 3 and 4 are eliminated. Every number below is hand-checked in the
comments.
"""

from __future__ import annotations

import ast
import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from commishdesk.ingest import (
    Draft,
    LeagueFormat,
    LeagueModel,
    Matchup,
    PlayoffFormat,
    Roster,
    Team,
    TradedPick,
    Transaction,
    WeekModel,
)
from commishdesk.stats.standings import compute_standings
from commishdesk.stats.trades import (
    FAIR_BAND,
    FAIR_BAND_SLEEPER_ONLY,
    SCALE,
    TradeGrade,
    TradeGrades,
    compute_trade_grades,
)
from commishdesk.stats.trades import _verdict as verdict
from tests.conftest import REPO_ROOT

STATS_TRADES = REPO_ROOT / "commishdesk" / "stats" / "trades.py"
WORKED_EXAMPLE = REPO_ROOT / "tests" / "fixtures" / "trade_grades" / "worked-example.json"
WEEK = 8

#: ``pid: (position, value, future, est)``. Values are points per week over replacement.
_PLAYERS: dict[str, tuple[str, float, float | None, bool]] = {
    "rbA": ("RB", 10.0, 12.0, False),
    "rbB": ("RB", 4.0, 3.0, False),
    "rb2": ("RB", 6.0, 5.0, False),
    "wr1": ("WR", 8.0, 7.0, False),
    "wr2": ("WR", 2.0, 2.0, False),
    "wr3": ("WR", 5.0, 5.0, False),
    "wr4": ("WR", 1.0, 1.0, False),
    "rbR": ("RB", 7.0, None, False),
    "rook": ("RB", 10.0, 9.0, True),
    "rbC": ("RB", 6.0, 6.0, False),
    "rbD": ("RB", 5.0, 5.0, False),
}

#: Who holds whom in the week-8 matchups (the post-trade rosters).
_ROSTERS: dict[str, list[str]] = {
    "1": ["rbA", "rb2", "wr1", "wr2", "rbR"],
    "2": ["rbB", "wr3", "wr4"],
    "3": ["rbC", "rook", "k1"],
    "4": ["rbD"],
}


def _snapshot(
    *, source: str = "nflverse", dynasty: bool = False, with_picks: bool = True
) -> dict[str, Any]:
    picks = None
    if dynasty and with_picks:
        picks = {
            "next_season": 2027,
            "rounds": 2,
            "est": True,
            "by_tier": {
                "early": {"1": 8.0, "2": 4.0},
                "mid": {"1": 7.0, "2": 3.5},
                "late": {"1": 6.0, "2": 3.0},
            },
            "later": {"1": 6.0, "2": 3.0},
            "tier_by_roster": {"1": "late", "2": "mid", "3": "early", "4": "early"},
        }
    return {
        "method_version": 1,
        "source": source,
        "dynasty": dynasty,
        "constants": {"pick_later_discount": 0.8},
        "replacement": {"RB": 8.0, "WR": 7.0},
        "players": {
            pid: {"pos": pos, "value": value, **({"future": future} if future is not None else {}), "est": est}
            for pid, (pos, value, future, est) in _PLAYERS.items()
        },
        "picks": picks,
    }


def _league() -> LeagueModel:
    return LeagueModel(
        league_id="id_league",
        name="Trade League",
        season=2026,
        platform="sleeper",
        format=LeagueFormat(
            team_count=4,
            roster_slots=["RB", "WR", "FLEX"],
            flex_eligibility={"FLEX": ["RB", "WR"]},
            scoring_label="PPR",
            is_superflex_or_2qb=False,
            te_premium=False,
            playoff=PlayoffFormat(bracket_teams=2),
        ),
        teams=[Team(roster_id=r, manager=f"m{r}") for r in "1234"],
        picks=[],
        draft=Draft(id="id_draft"),
    )


def _trade_txn(
    transaction_id: str,
    gets: Mapping[str, list[str]],
    *,
    picks: tuple[TradedPick, ...] = (),
    faab: int = 0,
    drops: Mapping[str, str] | None = None,
) -> Transaction:
    """A trade where ``gets[roster]`` are the player ids that roster receives."""
    from commishdesk.ingest import FaabTransfer

    adds = {pid: roster for roster, pids in gets.items() for pid in pids}
    receiver = next(iter(gets))
    sender = next(r for r in gets if r != receiver)
    return Transaction(
        transaction_id=transaction_id,
        type="trade",
        status="complete",
        roster_ids=sorted(gets),
        adds=adds,
        drops=dict(drops or {}),
        draft_picks=list(picks),
        faab=[FaabTransfer(sender=sender, receiver=receiver, amount=faab)] if faab else [],
    )


def _week(
    this_week: tuple[Transaction, ...] = (),
    past: dict[int, list[Transaction]] | None = None,
    *,
    rosters: dict[str, list[str]] | None = None,
    points: Mapping[str, float] | None = None,
) -> WeekModel:
    """Weeks 1..8 of the 4-team world. Roster 1 beats 4 and 2 beats 3 every week, so 3 and 4 are
    eliminated at week 8 (cur 0, max 2, two rosters at 8 wins). ``points`` overrides a player's
    weekly points; every other player scores 1.0."""
    held = rosters or _ROSTERS
    matchups: list[Matchup] = []
    for wk in range(1, WEEK + 1):
        for roster_id, opponent, score in (("1", "4", 100.0), ("4", "1", 70.0), ("2", "3", 90.0), ("3", "2", 80.0)):
            matchups.append(
                Matchup(
                    week=wk,
                    roster_id=roster_id,
                    matchup_id=1 if roster_id in "14" else 2,
                    opponent_roster_id=opponent,
                    points=score,
                    starters=[],
                    starters_points=[],
                    bench=list(held[roster_id]),
                    players_points={pid: float((points or {}).get(f"{pid}@{wk}", (points or {}).get(pid, 1.0)))
                                    for pid in held[roster_id]},
                )
            )
    return WeekModel(
        week=WEEK,
        rosters=[Roster(roster_id=r) for r in "1234"],
        matchups=matchups,
        transactions=list(this_week),
        playoff_week_start=11,
        past_transactions=dict(past or {}),
    )


def _grade(
    this_week: tuple[Transaction, ...],
    snapshot: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> TradeGrades:
    week = _week(this_week, **kwargs)
    league = _league()
    chosen = snapshot if snapshot is not None else _snapshot()
    return compute_trade_grades(week, league, chosen, compute_standings(week, league))


def _only(result: TradeGrades) -> TradeGrade:
    assert len(result.grades) == 1
    return result.grades[0]


def _side(grade: TradeGrade, roster_id: str) -> Any:
    return next(side for side in grade.sides if side.roster_id == roster_id)


# --------------------------------------------------------------------------- #
# Row: 2-team redraft
# --------------------------------------------------------------------------- #


def test_two_team_redraft_gives_one_mirrored_this_season_verdict_each() -> None:
    txn = _trade_txn("t1", {"1": ["rbA"], "2": ["rbB"]})
    grade = _only(_grade((txn,)))

    assert grade.status == "graded" and grade.reason is None and grade.dynasty is False
    one, two = _side(grade, "1"), _side(grade, "2")
    assert [v.horizon for v in one.verdicts] == [v.horizon for v in two.verdicts] == ["this_season"]
    a, b = one.verdicts[0], two.verdicts[0]
    # 10 vs 4: share 6/14 = 0.4286 -> Lopsided, mirrored A / D.
    assert (a.total, a.counter_total, a.share) == (10.0, 4.0, 0.4286)
    assert (a.tier, a.letter, a.word, a.ahead) == ("lopsided", "A", "Lopsided", True)
    assert (b.tier, b.letter, b.word, b.ahead) == ("lopsided", "D", "Far behind", False)
    assert (b.total, b.counter_total, b.share) == (a.counter_total, a.total, a.share)
    assert one.future_total is None and two.future_total is None
    assert (one.this_season_total, two.this_season_total) == (10.0, 4.0)
    assert grade.source == "nflverse" and grade.fair_band == FAIR_BAND


def test_lineup_impact_is_the_change_in_the_best_starting_lineup() -> None:
    txn = _trade_txn("t1", {"1": ["rbA"], "2": ["rbB"]}, drops={"rbA": "2", "rbB": "1"})
    grade = _only(_grade((txn,)))
    # Roster 1 holds rbA 10, rb2 6, rbR 7, wr1 8, wr2 2. After: RB rbA 10, WR wr1 8, FLEX rbR 7 = 25.
    # Before (minus rbA, plus rbB 4): RB rbR 7, WR wr1 8, FLEX rb2 6 = 21.
    assert _side(grade, "1").lineup_impact == 4.0
    # Roster 2 after: rbB 4, wr3 5, FLEX wr4 1 = 10. Before (minus rbB, plus rbA): 10 + 5 + 1 = 16.
    assert _side(grade, "2").lineup_impact == -6.0


def test_lineup_impact_falls_back_to_the_other_sides_receipts_without_drops() -> None:
    with_drops = _only(_grade((_trade_txn("t1", {"1": ["rbA"], "2": ["rbB"]}, drops={"rbA": "2", "rbB": "1"}),)))
    without = _only(_grade((_trade_txn("t1", {"1": ["rbA"], "2": ["rbB"]}),)))
    assert [s.lineup_impact for s in without.sides] == [s.lineup_impact for s in with_drops.sides]


def test_a_two_for_one_applies_no_roster_spot_multiplier() -> None:
    # Roster 2 sends rbA (10) for wr1 (8) + wr2 (2): values add to 10 vs 10 -> Even, no adjustment;
    # the lineup impact is what carries the consolidation.
    txn = _trade_txn("t1", {"1": ["rbA"], "2": ["wr1", "wr2"]})
    grade = _only(_grade((txn,)))
    one, two = _side(grade, "1").verdicts[0], _side(grade, "2").verdicts[0]
    assert (one.total, one.counter_total) == (10.0, 10.0)
    assert (one.letter, two.letter, one.word, two.word) == ("B", "B", "Even", "Even")
    assert one.ahead is None and two.ahead is None


# --------------------------------------------------------------------------- #
# The scale (pinned)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("a", "b", "band", "tier", "lead", "trail"),
    [
        (105, 95, FAIR_BAND, "even", "B", "B"),  # share exactly 0.05 is inside the band
        (1051, 949, FAIR_BAND, "slight", "B+", "C+"),  # 0.051 is outside it
        (110, 90, FAIR_BAND_SLEEPER_ONLY, "even", "B", "B"),  # sleeper_only widens the band to 0.10
        (111, 89, FAIR_BAND_SLEEPER_ONLY, "slight", "B+", "C+"),
        (110, 90, FAIR_BAND, "slight", "B+", "C+"),  # 0.10 is outside the normal band
        (115, 85, FAIR_BAND, "slight", "B+", "C+"),  # exactly 0.15 is still Slight
        (116, 84, FAIR_BAND, "clear", "A-", "C-"),
        (130, 70, FAIR_BAND, "clear", "A-", "C-"),  # exactly 0.30 is still Clear
        (131, 69, FAIR_BAND, "lopsided", "A", "D"),
        (5, 0, FAIR_BAND, "lopsided", "A", "D"),
    ],
)
def test_scale_boundaries(a: float, b: float, band: float, tier: str, lead: str, trail: str) -> None:
    ahead, behind = verdict("this_season", a, b, band, False), verdict("this_season", b, a, band, False)
    assert (ahead.tier, behind.tier) == (tier, tier)
    assert (ahead.letter, behind.letter) == (lead, trail)
    assert ahead.share == behind.share
    assert (ahead.counter_total, behind.counter_total) == (behind.total, ahead.total)  # one mirrors the other


def test_the_scale_table_is_the_documented_one() -> None:
    assert FAIR_BAND == 0.05 and FAIR_BAND_SLEEPER_ONLY == 0.10
    assert SCALE == (
        (0.15, "slight", "B+", "Slight edge", "C+", "Slightly behind"),
        (0.30, "clear", "A-", "Clear edge", "C-", "Clearly behind"),
        (float("inf"), "lopsided", "A", "Lopsided", "D", "Far behind"),
    )


def test_no_f_is_ever_emitted_and_d_is_the_floor() -> None:
    letters = set()
    for a in range(0, 101, 5):
        for band in (FAIR_BAND, FAIR_BAND_SLEEPER_ONLY):
            letters.add(verdict("future", float(a), float(100 - a), band, False).letter)
    assert letters == {"A", "A-", "B+", "B", "C+", "C-", "D"}
    assert "F" not in "".join(letters)


def test_sleeper_only_snapshot_uses_the_wider_band_end_to_end() -> None:
    # rbC (6) against rbD (5): share 1/11 = 0.0909, outside 0.05 but inside the sleeper_only 0.10 band.
    txn = _trade_txn("t1", {"3": ["rbC"], "4": ["rbD"]})
    normal = _only(_grade((txn,), _snapshot(source="nflverse")))
    wide = _only(_grade((txn,), _snapshot(source="sleeper_only")))
    assert _side(normal, "3").verdicts[0].tier == "slight"
    assert _side(wide, "3").verdicts[0].tier == "even"
    assert wide.fair_band == FAIR_BAND_SLEEPER_ONLY and wide.source == "sleeper_only"


def test_a_verdict_is_est_when_more_than_half_of_its_value_is_estimated() -> None:
    # The rookie (est., 10) against rbC (6, not est.): est. share 10 / 16 > half.
    grade = _only(_grade((_trade_txn("t1", {"3": ["rook"], "4": ["rbC"]}),)))
    assert all(v.est for side in grade.sides for v in side.verdicts)
    assert _side(grade, "3").assets[0].est is True
    # Half exactly is not "more than half": est. 5 against non-est. 5.
    snap = _snapshot()
    snap["players"]["rbD"]["est"] = True  # 5 est vs rbC 6 non-est: 5/11 < half
    half = _only(_grade((_trade_txn("t1", {"3": ["rbC"], "4": ["rbD"]}),), snap))
    assert not any(v.est for side in half.sides for v in side.verdicts)


# --------------------------------------------------------------------------- #
# Row: dynasty
# --------------------------------------------------------------------------- #


def test_dynasty_gets_two_verdicts_and_picks_count_only_toward_future() -> None:
    pick = TradedPick(season="2027", round=1, roster_id="4", owner_id="2", previous_owner_id="4")
    txn = _trade_txn("t1", {"1": ["rbA"], "2": ["rbB"]}, picks=(pick,))
    grade = _only(_grade((txn,), _snapshot(dynasty=True)))

    assert grade.dynasty is True
    one, two = _side(grade, "1"), _side(grade, "2")
    assert [v.horizon for v in one.verdicts] == ["this_season", "future"]
    # This season counts players only: 10 vs 4, Lopsided.
    assert (one.verdicts[0].total, one.verdicts[0].counter_total) == (10.0, 4.0)
    assert (one.verdicts[0].letter, two.verdicts[0].letter) == ("A", "D")
    # Future: 12 vs 3 + the 2027 round-1 pick of roster 4 (tier early = 8.0) = 11; share 1/23 -> Even.
    assert (one.future_total, two.future_total) == (12.0, 11.0)
    assert (one.verdicts[1].tier, two.verdicts[1].tier) == ("even", "even")
    pick_asset = next(a for a in two.assets if a.kind == "pick")
    assert (pick_asset.value, pick_asset.future, pick_asset.est) == (None, 8.0, True)
    # The units differ: This season is never added to Future.
    assert one.this_season_total == 10.0 and one.future_total == 12.0


def test_later_season_picks_are_tierless_and_discounted() -> None:
    pick = TradedPick(season="2028", round=1, roster_id="4", owner_id="2", previous_owner_id="4")
    grade = _only(_grade((_trade_txn("t1", {"1": ["rbA"], "2": ["rbB"]}, picks=(pick,)),), _snapshot(dynasty=True)))
    asset = next(a for a in _side(grade, "2").assets if a.kind == "pick")
    assert asset.future == 4.8  # later[1] 6.0 * 0.8 ** 1


def test_redraft_picks_are_listed_not_counted() -> None:
    pick = TradedPick(season="2027", round=1, roster_id="4", owner_id="2", previous_owner_id="4")
    grade = _only(_grade((_trade_txn("t1", {"1": ["rbA"], "2": ["rbB"]}, picks=(pick,)),)))
    two = _side(grade, "2")
    assert [a.kind for a in two.assets] == ["player"]
    assert [(u.kind, u.reason, u.pick_season, u.pick_round) for u in two.ungraded] == [
        ("pick", "redraft_pick", "2027", 1)
    ]
    assert two.this_season_total == 4.0


def test_a_dynasty_pick_only_trade_grades_future_alone() -> None:
    pick = TradedPick(season="2027", round=1, roster_id="4", owner_id="1", previous_owner_id="2")
    other = TradedPick(season="2027", round=2, roster_id="4", owner_id="2", previous_owner_id="1")
    txn = Transaction(
        transaction_id="t1", type="trade", status="complete", roster_ids=["1", "2"], draft_picks=[pick, other]
    )
    grade = _only(_grade((txn,), _snapshot(dynasty=True)))
    assert grade.status == "graded"
    assert [v.horizon for v in _side(grade, "1").verdicts] == ["future"]
    assert _side(grade, "1").this_season_total is None
    # Round 1 early 8.0 against round 2 early 4.0: 4/12 = 0.333 -> Lopsided, both est. picks.
    assert _side(grade, "1").verdicts[0].letter == "A" and _side(grade, "1").verdicts[0].est is True


# --------------------------------------------------------------------------- #
# Row: 3-team trade
# --------------------------------------------------------------------------- #


def test_three_team_trade_grades_each_side_against_the_rest() -> None:
    txn = _trade_txn("t1", {"1": ["rbA"], "2": ["wr3"], "3": ["rbC"]})
    grade = _only(_grade((txn,)))
    assert [s.roster_id for s in grade.sides] == ["1", "2", "3"]
    v1, v2, v3 = (_side(grade, r).verdicts[0] for r in "123")
    # Totals 10, 5, 6. Roster 1: 10 vs 11 -> share 1/21 = 0.0476 Even. Roster 2: 5 vs 16 -> 11/21 Lopsided.
    # Roster 3: 6 vs 15 -> 9/21 = 0.4286 Lopsided.
    assert (v1.counter_total, v1.tier) == (11.0, "even")
    assert (v2.counter_total, v2.tier, v2.letter) == (16.0, "lopsided", "D")
    assert (v3.counter_total, v3.tier, v3.letter) == (15.0, "lopsided", "D")


def test_three_team_given_players_come_from_the_transaction_drops() -> None:
    txn = _trade_txn(
        "t1", {"1": ["rbA"], "2": ["wr3"], "3": ["rbC"]}, drops={"rbA": "2", "wr3": "3", "rbC": "1"}
    )
    grade = _only(_grade((txn,)))
    # Roster 1 received rbA and gave rbC: after 25 (see above); before swaps rbA for rbC (6): RB 7, WR 8, FLEX 6 = 21.
    assert _side(grade, "1").lineup_impact == 4.0


# --------------------------------------------------------------------------- #
# Row: partly unvalued, nothing valued, no snapshot
# --------------------------------------------------------------------------- #


def test_unvalued_faab_and_unpriced_picks_are_listed_with_a_reason_and_the_rest_is_graded() -> None:
    old_pick = TradedPick(season="2026", round=1, roster_id="4", owner_id="3", previous_owner_id="4")
    odd_round = TradedPick(season="2027", round=9, roster_id="4", owner_id="3", previous_owner_id="4")
    txn = _trade_txn("t1", {"3": ["rbC", "k1"], "4": ["rbD"]}, picks=(old_pick, odd_round), faab=7)
    grade = _only(_grade((txn,), _snapshot(dynasty=True)))
    assert grade.status == "graded"
    three = _side(grade, "3")
    reasons = sorted((u.kind, u.reason) for u in three.ungraded)
    assert reasons == [
        ("faab", "faab"),
        ("pick", "pick_not_priced"),
        ("pick", "pick_not_priced"),
        ("player", "not_valued"),
    ]
    four = _side(grade, "4")
    assert three.this_season_total == 6.0 and four.this_season_total == 5.0


def test_a_trade_with_nothing_valued_is_ungraded_never_a_letter() -> None:
    txn = _trade_txn("t1", {"3": ["k1"], "4": ["k2"]}, faab=3)
    grade = _only(_grade((txn,)))
    assert (grade.status, grade.reason) == ("ungraded", "nothing_valued")
    for side in grade.sides:
        assert side.verdicts == [] and side.assets == [] and side.this_season_total is None
    assert sorted(u.reason for s in grade.sides for u in s.ungraded) == ["faab", "not_valued", "not_valued"]


@pytest.mark.parametrize("snapshot", [None, {}, {"players": {}}])
def test_no_snapshot_leaves_every_trade_ungraded_without_raising(snapshot: Any) -> None:
    week = _week((_trade_txn("t1", {"1": ["rbA"], "2": ["rbB"]}),))
    league = _league()
    result = compute_trade_grades(week, league, snapshot, compute_standings(week, league))
    grade = _only(result)
    assert (grade.status, grade.reason, grade.source, grade.fair_band) == ("ungraded", "no_snapshot", None, None)
    assert all(not s.verdicts and not s.assets for s in grade.sides)
    assert [u.reason for s in grade.sides for u in s.ungraded] == ["no_snapshot", "no_snapshot"]


def test_a_malformed_snapshot_or_lineup_fault_never_fails_the_issue() -> None:
    txn = _trade_txn("t1", {"1": ["rbA"], "2": ["rbB"]})
    broken = _snapshot()
    broken["players"]["rbA"] = "garbage"
    result = _grade((txn,), broken)
    assert _only(result).status == "graded"  # rbB still valued; rbA is listed not_valued
    assert [u.reason for u in _side(_only(result), "1").ungraded] == ["not_valued"]

    week = _week((txn,))
    league = _league().model_copy(update={"format": _league().format.model_copy(update={"roster_slots": []})})
    result = compute_trade_grades(week, league, _snapshot(), compute_standings(week, _league()))
    assert _only(result).status == "graded"
    assert all(side.lineup_impact is None for side in _only(result).sides)  # malformed slots: no lineup number


def test_lineup_impact_reads_started_players_and_skips_benched_ir() -> None:
    txn = _trade_txn("t1", {"1": ["rbA"], "2": ["rbB"]}, drops={"rbA": "2", "rbB": "1"})
    week = _week((txn,))
    moved = [
        m.model_copy(update={"starters": list(m.bench), "bench": []}) if m.roster_id == "1" else m
        for m in week.matchups
    ]
    rosters = [Roster(roster_id=r, ir=["wr2"] if r == "1" else []) for r in "1234"]
    week = week.model_copy(update={"matchups": moved, "rosters": rosters})
    league = _league()
    result = compute_trade_grades(week, league, _snapshot(), compute_standings(week, league))
    assert _side(_only(result), "1").lineup_impact == 4.0  # starters count; wr2 (IR, not started) is not a candidate

    ir_rosters = [Roster(roster_id=r, ir=["rbR"] if r == "1" else []) for r in "1234"]
    benched_ir = week.model_copy(update={"rosters": ir_rosters})
    ir_moved = [m.model_copy(update={"starters": [p for p in m.starters if p != "rbR"], "bench": ["rbR"]})
                if m.roster_id == "1" else m for m in benched_ir.matchups]
    benched_ir = benched_ir.model_copy(update={"matchups": ir_moved})
    result = compute_trade_grades(benched_ir, league, _snapshot(), compute_standings(benched_ir, league))
    assert _side(_only(result), "1").lineup_impact != 4.0  # a benched IR player is excluded from both lineups


def test_a_fault_in_one_trade_is_ungraded_not_a_failed_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    import commishdesk.stats.trades as trades

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(trades, "_grade_trade", boom)
    monkeypatch.setattr(trades, "_revisit", boom)
    old = _trade_txn("old", {"1": ["rbA"], "2": ["rbB"]})
    result = _grade((_trade_txn("t1", {"1": ["rbA"], "2": ["rbB"]}),), past={3: [old]})
    assert (_only(result).status, _only(result).reason) == ("ungraded", "fault")
    assert result.revisits == []


# --------------------------------------------------------------------------- #
# Row: context flag
# --------------------------------------------------------------------------- #


def test_a_contender_against_an_eliminated_roster_carries_a_context_flag_and_the_same_grade() -> None:
    mixed = _only(_grade((_trade_txn("t1", {"1": ["rbA"], "4": ["rbB"]}),)))
    plain = _only(_grade((_trade_txn("t1", {"1": ["rbA"], "2": ["rbB"]}),)))
    both_out = _only(_grade((_trade_txn("t1", {"3": ["rbA"], "4": ["rbB"]}),)))

    assert (mixed.context, mixed.eliminated_roster_ids) == ("contender_vs_eliminated", ["4"])
    assert plain.context is None and plain.eliminated_roster_ids == []
    assert both_out.context is None
    # The grades are unchanged by the flag.
    assert [(v.letter, v.word, v.share) for s in mixed.sides for v in s.verdicts] == [
        (v.letter, v.word, v.share) for s in plain.sides for v in s.verdicts
    ]
    text = mixed.model_dump_json().lower()
    assert not any(word in text for word in ("veto", "collu", "tank", "suspicious"))


# --------------------------------------------------------------------------- #
# Row: revisit
# --------------------------------------------------------------------------- #


def test_a_trade_three_weeks_old_carries_revisit_numbers() -> None:
    # Week 5 trade (3 weeks old at week 8): roster 1 receives rbR, roster 2 receives wr3.
    # Weeks 6..8 (3 weeks) rbR scores 10, 20, 0 = 30 -> 10.0 / wk; wr3 scores 1.0 a week.
    points = {"rbR@6": 10.0, "rbR@7": 20.0, "rbR@8": 0.0}
    old = _trade_txn("t_old", {"1": ["rbR"], "2": ["wr3"]})
    young = _trade_txn("t_young", {"1": ["rbA"], "2": ["rbB"]})
    result = _grade((), past={5: [old], 6: [young]}, points=points)

    assert result.grades == []  # only the target week's own trades are graded
    assert [r.transaction_id for r in result.revisits] == ["t_old"]  # the week-6 trade is 2 weeks old
    revisit = result.revisits[0]
    assert (revisit.trade_week, revisit.weeks_since, revisit.leader) == (5, 3, "1")
    one, two = revisit.sides
    assert (one.roster_id, one.points_per_week, [p.points_per_week for p in one.players]) == ("1", 10.0, [10.0])
    assert (two.roster_id, two.points_per_week) == ("2", 1.0)


def test_a_revisit_tie_has_no_leader_and_non_trades_are_skipped() -> None:
    waiver = Transaction(transaction_id="w1", type="waiver", status="complete", roster_ids=["1"], adds={"rbA": "1"})
    tie = _trade_txn("t_tie", {"1": ["wr3"], "2": ["wr4"]})  # both score 1.0 a week
    result = _grade((), past={3: [waiver, tie]})
    assert [r.transaction_id for r in result.revisits] == ["t_tie"]
    assert result.revisits[0].leader is None
    assert result.revisits[0].weeks_since == 5


def test_revisit_works_without_a_snapshot() -> None:
    old = _trade_txn("t_old", {"1": ["rbR"], "2": ["wr3"]})
    week = _week((), past={4: [old]})
    league = _league()
    result = compute_trade_grades(week, league, None, compute_standings(week, league))
    assert len(result.revisits) == 1


# --------------------------------------------------------------------------- #
# Determinism, shape, ordering
# --------------------------------------------------------------------------- #


def test_rebuild_is_byte_identical() -> None:
    txns = (
        _trade_txn("t2", {"1": ["rbA"], "2": ["wr1", "wr2"]}),
        _trade_txn("t1", {"3": ["rbC"], "4": ["rbD"]}),
    )
    first, second = _grade(txns, _snapshot(dynasty=True)), _grade(tuple(reversed(txns)), _snapshot(dynasty=True))
    assert first.model_dump_json() == second.model_dump_json()
    assert [g.transaction_id for g in first.grades] == ["t1", "t2"]


def test_grade_facts_carry_only_numbers_and_sleeper_ids() -> None:
    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                assert key not in {"name", "display_name", "manager", "team_name", "text"}, key
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        else:
            assert node is None or isinstance(node, bool | int | float | str)

    pick = TradedPick(season="2027", round=1, roster_id="4", owner_id="2", previous_owner_id="4")
    result = _grade((_trade_txn("t1", {"1": ["rbA"], "2": ["rbB"]}, picks=(pick,)),), _snapshot(dynasty=True))
    walk(result.model_dump())


# --------------------------------------------------------------------------- #
# The AD-1 fence
# --------------------------------------------------------------------------- #


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module)
            elif node.level == 1:
                names.add(f"commishdesk.stats.{node.module}" if node.module else "commishdesk.stats")
    return names


def test_trades_stays_inside_the_stats_fence() -> None:
    imported = _imports(STATS_TRADES)
    forbidden = {"adapters", "store", "facts", "narrate", "render", "deliver", "statmods", "league_value", "consensus"}
    offenders = [n for n in imported if n.startswith("commishdesk.") and n.split(".")[1] in forbidden]
    assert not offenders, offenders
    banned = {"httpx", "requests", "datetime", "time", "os", "pathlib", "random", "secrets"}
    assert not {n.split(".")[0] for n in imported} & banned


def test_the_stats_package_re_exports_the_trade_names() -> None:
    import commishdesk.stats as stats

    assert stats.compute_trade_grades is compute_trade_grades
    for name in ("TradeGrade", "TradeGrades", "TradeRevisit", "Verdict", "best_lineup_total", "roster_eliminated"):
        assert name in stats.__all__


def test_the_public_helpers_are_the_ones_the_spec_names() -> None:
    from commishdesk.stats.lineup import best_lineup_total
    from commishdesk.stats.stakes import roster_eliminated

    league = _league()
    # RB 10, WR 8, FLEX the better of RB 6 / WR 2: 24.
    candidates = [("a", "RB", 10.0), ("b", "RB", 6.0), ("c", "WR", 8.0), ("d", "WR", 2.0), ("k", "K", 99.0)]
    assert best_lineup_total(league, candidates) == 24.0
    assert best_lineup_total(league, []) == 0.0
    week = _week()
    assert roster_eliminated(week, league, compute_standings(week, league)) == {
        "1": False, "2": False, "3": True, "4": True,
    }


# --------------------------------------------------------------------------- #
# The facts hook and the demo season
# --------------------------------------------------------------------------- #


def _demo_facts(tmp_path: Path, week: int, source: str) -> dict[str, Any]:
    from commishdesk import demo_season
    from commishdesk.store import FileStore
    from commishdesk.weekly import compute_weekly_facts

    built = compute_weekly_facts(
        FileStore(tmp_path / f"{source}-{week}"), demo_season.DEMO_LEAGUE_ID, week,
        demo_season.load_week_bundle(week), demo_season.load_league_bundle(),
        seeding=None, hold_on_cross_check=True, logger=logging.getLogger("test"), league_value_source=source,
    )  # fmt: skip
    return built.facts_json["transactions"]  # type: ignore[no-any-return]


@pytest.mark.parametrize("week", [3, 6])
def test_the_demo_season_trades_are_graded_in_the_facts(tmp_path: Path, week: int) -> None:
    transactions = _demo_facts(tmp_path, week, "sleeper_only")
    (grade,) = transactions["trade_grades"]
    assert grade["status"] == "graded" and grade["week"] == week and grade["source"] == "sleeper_only"
    assert grade["fair_band"] == FAIR_BAND_SLEEPER_ONLY
    assert [v["letter"] for s in grade["sides"] for v in s["verdicts"]] != []
    letters = {v["letter"] for s in grade["sides"] for v in s["verdicts"]}
    assert letters <= {"A", "A-", "B+", "B", "C+", "C-", "D"}


def test_the_demo_season_revisits_the_week_three_trade_at_week_six(tmp_path: Path) -> None:
    transactions = _demo_facts(tmp_path, 6, "sleeper_only")
    assert [(r["transaction_id"], r["trade_week"], r["weeks_since"]) for r in transactions["trade_revisits"]] == [
        ("id_cah929mt83", 3, 3)
    ]


def test_the_demo_facts_without_a_snapshot_list_the_trade_ungraded(tmp_path: Path) -> None:
    transactions = _demo_facts(tmp_path, 6, "off")
    (grade,) = transactions["trade_grades"]
    assert (grade["status"], grade["reason"]) == ("ungraded", "no_snapshot")
    assert all(not s["verdicts"] for s in grade["sides"])


def test_a_rebuilt_demo_week_is_byte_identical(tmp_path: Path) -> None:
    from commishdesk import demo_season
    from commishdesk.store import FileStore
    from commishdesk.weekly import compute_weekly_facts

    store = FileStore(tmp_path / "shared")
    runs = [
        compute_weekly_facts(
            store, demo_season.DEMO_LEAGUE_ID, 6, demo_season.load_week_bundle(6), demo_season.load_league_bundle(),
            seeding=None, hold_on_cross_check=True, logger=logging.getLogger("test"),
            league_value_source="sleeper_only",
        ).facts_json["transactions"]
        for _ in range(2)
    ]  # fmt: skip
    assert json.dumps(runs[0], sort_keys=True) == json.dumps(runs[1], sort_keys=True)


# --------------------------------------------------------------------------- #
# The worked example the concept doc quotes
# --------------------------------------------------------------------------- #


def _worked_example() -> dict[str, Any]:
    """The doc's example: a dynasty trade, roster 1 receives rbA and roster 2 receives rbB plus a
    2027 first-round pick that began with roster 4. Roster 2 is alive, roster 1 is alive."""
    pick = TradedPick(season="2027", round=1, roster_id="4", owner_id="2", previous_owner_id="4")
    txn = _trade_txn("t_example", {"1": ["rbA"], "2": ["rbB"]}, picks=(pick,), drops={"rbA": "2", "rbB": "1"})
    grade = _only(_grade((txn,), _snapshot(dynasty=True)))
    return {"transactions": {"trade_grades": [grade.model_dump(mode="json")]}}


def test_the_committed_worked_example_is_current() -> None:
    assert json.loads(WORKED_EXAMPLE.read_text(encoding="utf-8")) == _worked_example()
