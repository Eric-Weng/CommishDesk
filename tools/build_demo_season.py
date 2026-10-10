#!/usr/bin/env python3
"""Author the fictional demo season shipped as ``commishdesk/demo_season/`` package data.

The season is invented end to end: 12 teams, 20-player rosters, a snake draft, twelve
final weeks of a 14-week regular season, waivers, free-agent moves and trades. It is
generated from one fixed seed, so a re-run reproduces the committed files byte for byte
(``--check`` asserts exactly that). Nothing here touches a network, a Sleeper id or a
real league.

Layout written under ``commishdesk/demo_season/``:

* ``league.json`` -- the league bundle (``league`` / ``draft`` / ``draft_picks`` /
  ``rosters`` / ``users`` / ``players`` / ``previous_league_ids``), the shape
  ``SleeperAdapter.fetch`` returns;
* ``weeks/NN.json`` -- what changed in week ``NN``: that week's ``matchups`` and settled
  ``transactions``, the point-in-time ``rosters`` as of the end of the week, and the
  ``next_matchups`` pairings. The loader folds weeks ``1..NN`` into the cumulative
  bundle ``SleeperAdapter.fetch_week`` returns.

The scenario the story needs is forced by construction, then asserted before anything is
written: in week 12 a 9-3 team leads the standings and is the single luckiest team (it
won with middling scores against weak weeks from its opponents), the widest-margin game
is a blowout, one game is decided by under a point, and a trade settles. No game is a tie.

    uv run python tools/build_demo_season.py            # write the data files
    uv run python tools/build_demo_season.py --check    # fail if the committed files differ

Needs ``pydantic`` only because ``tools/anonymize.py`` (whose ``NAME_POOL`` it reuses)
imports it. Never packaged.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import random
import sys
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "commishdesk" / "demo_season"

SEED = 8601
SEASON = 2026
LAST_WEEK = 12
REGULAR_WEEKS = 14
PLAYOFF_WEEK_START = 15
TEAMS = 12
ROUNDS = 20
MAX_ATTEMPTS = 400

LEAGUE_ID = "demo"
SLOTS = ("QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX")
QUOTA = {"QB": 2, "RB": 7, "WR": 8, "TE": 3}
POOL_SIZE = {"QB": 32, "RB": 98, "WR": 112, "TE": 44}
FIRST_FREE_RANK = {"QB": 24.0, "RB": 19.0, "WR": 18.0, "TE": 14.0}
LAST_FREE_RANK = {"QB": 6.0, "RB": 2.0, "WR": 2.0, "TE": 2.0}
NUMBER_RANGE = {"QB": (1, 19), "RB": (20, 49), "WR": (10, 19), "TE": (80, 89)}
TRADE_WEEKS = (3, 6, 9, 12)
FAAB_BUDGET = 100

NFL_TEAMS = (
    "ARI ATL BAL BUF CAR CHI CIN CLE DAL DEN DET GB HOU IND JAX KC LAC LAR LV MIA MIN NE NO NYG NYJ PHI PIT SF SEA TB"
    " TEN WAS"
).split()

FIRST_NAMES = (
    "Aiden Alec Amos Anders Archer Beau Bennett Brandt Brecken Bryce Caden Caleb Callum Cameron Carver Cassius Chandler"
    " Clay Colt Corbin Cormac Dalton Dante Darius Declan Dexter Dominic Easton Elias Emmett Everett Ezra Fenton Finn"
    " Flynn Gage Garrett Gideon Graham Grady Griffin Hale Harlan Hayes Heath Holden Hudson Ivan Jace Jarrett Jasper"
    " Jaxon Jonas Jude Kade Kai Kellan Kendrick Knox Lachlan Landon Lennox Leo Levi Lucian Magnus Marlon Mason Maxwell"
    " Merrick Miles Nash Nico Nolan Oakley Orion Otis Owen Pierce Porter Preston Quentin Quincy Reid Remy Rhett Rowan"
    " Royce Rudy Ryker Sawyer Sebastian Silas Soren Stellan Tanner Tate Theo Titus Tobias Tristan Tucker Ty Ulysses"
    " Vance Wade Warren Wells Weston Wilder Wyatt Xander Yosef Zane Zeke"
).split()

SURNAME_PREFIXES = (
    "Ash Bar Bel Cal Cor Dor Dun Eld Fal Gar Gor Hal Han Isk Ix Jor Kel Kes Lor Lun Mar Mor Nev Nor Orl Pel Pri Quin"
    " Rav Rok Sal Sev Tev Tor Uld Var Wen Yor Zan"
).split()

SURNAME_SUFFIXES = (
    "wick dale mont ridge ford vane crest hollow stead brook mere worth field shaw thorne well gate ton combe holt moor"
    " cott wyn lock den ley mark vik"
).split()


# --------------------------------------------------------------------------- #
# Neighbouring tools, loaded by path (tools/ is not a package)
# --------------------------------------------------------------------------- #


def _load_tool(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "tools" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


def _byes() -> dict[str, int]:
    with (REPO_ROOT / "commishdesk" / "ingest" / "nfl_byes.toml").open("rb") as handle:
        return dict(tomllib.load(handle)[str(SEASON)])


# --------------------------------------------------------------------------- #
# Small helpers (integers for points: cents, so no float drift)
# --------------------------------------------------------------------------- #


def _bump(rng: random.Random, spread: float) -> float:
    """A bell-ish factor around 1.0 from uniforms only (no libm call, so the output is
    bit-stable across platforms)."""
    return 1.0 + (rng.random() + rng.random() + rng.random() + rng.random() - 2.0) * spread


def _curve(x: float) -> float:
    """x ** 1.5 from a correctly rounded square root (no libm pow, so it is bit-stable everywhere)."""
    return x * math.sqrt(x)


def _token(rng: random.Random, used: set[str]) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    while True:
        value = "id_" + "".join(rng.choice(alphabet) for _ in range(10))
        if value not in used:
            used.add(value)
            return value


def _dollars(cents: int) -> float:
    return round(cents / 100, 2)


def _split(cents: int) -> tuple[int, int]:
    return cents // 100, cents % 100


# --------------------------------------------------------------------------- #
# Players, draft
# --------------------------------------------------------------------------- #


def _make_players(rng: random.Random) -> dict[str, dict[str, Any]]:
    surnames = [p + s for p in SURNAME_PREFIXES for s in SURNAME_SUFFIXES]
    total = sum(POOL_SIZE.values())
    chosen = rng.sample(surnames, total)
    players: dict[str, dict[str, Any]] = {}
    index = 0
    for position in ("QB", "RB", "WR", "TE"):
        count = POOL_SIZE[position]
        for rank in range(count):
            index += 1
            pid = f"D{index:04d}"
            span = FIRST_FREE_RANK[position] - LAST_FREE_RANK[position]
            base = LAST_FREE_RANK[position] + span * _curve(1 - rank / count)
            lo, hi = NUMBER_RANGE[position]
            exp = rng.randint(0, 9)
            players[pid] = {
                "age": 21 + exp + rng.randint(0, 2),
                "base": round(base * _bump(rng, 0.08), 2),
                "fantasy_positions": [position],
                "first_name": rng.choice(FIRST_NAMES),
                "injury_status": None,
                "last_name": chosen[index - 1],
                "number": rng.randint(lo, hi),
                "position": position,
                "rookie_year": str(SEASON - exp),
                "status": "Active",
                "team": rng.choice(NFL_TEAMS),
                "years_exp": exp,
            }
    return players


def _snake_draft(
    rng: random.Random, players: dict[str, dict[str, Any]], draft_order: list[int]
) -> tuple[dict[int, list[str]], list[tuple[int, int, str, int]]]:
    """Greedy best-available snake draft under per-position quotas. Returns each roster's
    players and the pick log ``(pick_no, round, player_id, roster_id)``."""
    rosters: dict[int, list[str]] = {rid: [] for rid in draft_order}
    taken: set[str] = set()
    picks: list[tuple[int, int, str, int]] = []
    pick_no = 0
    for round_no in range(1, ROUNDS + 1):
        order = draft_order if round_no % 2 == 1 else list(reversed(draft_order))
        for rid in order:
            held = [players[p]["position"] for p in rosters[rid]]
            open_positions = {pos for pos, quota in QUOTA.items() if held.count(pos) < quota}
            candidates = [
                (players[pid]["base"] * _bump(rng, 0.1), pid)
                for pid in players
                if pid not in taken and players[pid]["position"] in open_positions
            ]
            _, pid = max(candidates)
            taken.add(pid)
            rosters[rid].append(pid)
            pick_no += 1
            picks.append((pick_no, round_no, pid, rid))
    return rosters, picks


# --------------------------------------------------------------------------- #
# Lineups
# --------------------------------------------------------------------------- #


def _choose_lineup(
    rng: random.Random, players: dict[str, dict[str, Any]], roster: list[str], bye_teams: set[str]
) -> list[str]:
    """The lineup a manager starts: best perceived value per slot, a bye-week player only
    when no one else fits or (rarely) when the manager forgets."""
    forgetful = rng.random() < 0.05
    perceived: dict[str, float] = {}
    for pid in sorted(roster):
        on_bye = players[pid]["team"] in bye_teams and not forgetful
        perceived[pid] = (-1000.0 if on_bye else 0.0) + players[pid]["base"] * (0.85 + 0.3 * rng.random())

    def best(position: tuple[str, ...], count: int, exclude: set[str]) -> list[str]:
        pool = [pid for pid in sorted(roster) if players[pid]["position"] in position and pid not in exclude]
        pool.sort(key=lambda pid: -perceived[pid])
        return pool[:count]

    qb = best(("QB",), 1, set())
    rb = best(("RB",), 2, set())
    wr = best(("WR",), 2, set())
    te = best(("TE",), 1, set())
    flex = best(("RB", "WR", "TE"), 2, set(qb + rb + wr + te))
    return qb + rb + wr + te + flex


def _optimal_cents(players: dict[str, dict[str, Any]], points: dict[str, int]) -> int:
    """Best legal lineup total in cents (strict slots first, then the two best leftovers)."""

    def top(position: str, count: int) -> list[str]:
        pool = sorted((pid for pid in points if players[pid]["position"] == position), key=lambda p: -points[p])
        return pool[:count]

    used = top("QB", 1) + top("RB", 2) + top("WR", 2) + top("TE", 1)
    rest = sorted(
        (pid for pid in points if players[pid]["position"] != "QB" and pid not in used), key=lambda p: -points[p]
    )
    return sum(points[pid] for pid in used + rest[:2])


# --------------------------------------------------------------------------- #
# Schedule
# --------------------------------------------------------------------------- #


def _schedule(order: list[int]) -> list[list[tuple[int, int]]]:
    """Circle-method round robin: 11 rounds of 6 pairings."""
    teams = list(order)
    rounds: list[list[tuple[int, int]]] = []
    for _ in range(len(teams) - 1):
        rounds.append([(teams[i], teams[len(teams) - 1 - i]) for i in range(len(teams) // 2)])
        teams = [teams[0], teams[-1], *teams[1:-1]]
    return rounds


def _pairings(rounds: list[list[tuple[int, int]]], week: int) -> list[tuple[int, int]]:
    return rounds[(week - 1) % len(rounds)]


# --------------------------------------------------------------------------- #
# The season
# --------------------------------------------------------------------------- #


class _Rejected(Exception):
    """This attempt's draw cannot satisfy the scenario; try the next one."""


def _simulate(attempt: int, names: list[str]) -> dict[str, Any]:
    rng = random.Random(f"{SEED}:{attempt}")
    byes = _byes()
    used_ids: set[str] = set()

    players = _make_players(rng)
    roster_ids = list(range(1, TEAMS + 1))
    draft_order = list(roster_ids)
    rng.shuffle(draft_order)
    rosters, draft_log = _snake_draft(rng, players, draft_order)
    free_agents = sorted(pid for pid in players if all(pid not in held for held in rosters.values()))
    rounds = _schedule(draft_order)

    user_ids = {rid: _token(rng, used_ids) for rid in roster_ids}
    draft_id = _token(rng, used_ids)

    budget_used = dict.fromkeys(roster_ids, 0)
    moves = dict.fromkeys(roster_ids, 0)
    txn_rank = 0

    week_roster: dict[int, dict[int, list[str]]] = {}
    week_starters: dict[int, dict[int, list[str]]] = {}
    week_points: dict[int, dict[int, dict[str, int]]] = {}
    week_txns: dict[int, list[dict[str, Any]]] = {}

    for week in range(1, LAST_WEEK + 1):
        bye_teams = {team for team, bye in byes.items() if bye == week}
        txns: list[dict[str, Any]] = []

        def lowest_droppable(rid: int, rosters: dict[int, list[str]] = rosters) -> str:
            held = rosters[rid]
            counts = {pos: sum(players[p]["position"] == pos for p in held) for pos in QUOTA}
            floor = {"QB": 2, "RB": 4, "WR": 4, "TE": 2}
            ranked = sorted(held, key=lambda p: players[p]["base"])
            for pid in ranked:
                if counts[players[pid]["position"]] > floor[players[pid]["position"]]:
                    return pid
            return ranked[0]

        # Waivers and free-agent pickups settle first, as they do early in a Sleeper week.
        for index in range(rng.randint(3, 6)):
            rid = rng.choice(roster_ids)
            pool = sorted(free_agents, key=lambda p: -players[p]["base"])
            add = pool[0] if rng.random() < 0.55 else rng.choice(pool[:14])
            drop = lowest_droppable(rid)
            if players[add]["base"] <= players[drop]["base"] - 3:
                continue
            waiver = rng.random() < 0.7
            bid = 0
            if waiver:
                bid = min(rng.choice((0, 1, 2, 3, 5, 8, 12, 18, 25)), FAAB_BUDGET - budget_used[rid])
                budget_used[rid] += bid
            rosters[rid].remove(drop)
            rosters[rid].append(add)
            free_agents.remove(add)
            free_agents.append(drop)
            free_agents.sort()
            moves[rid] += 1
            txns.append(
                {
                    "adds": {add: rid},
                    "consenter_ids": [rid],
                    "creator": user_ids[rid],
                    "draft_picks": [],
                    "drops": {drop: rid},
                    "leg": week,
                    "metadata": {},
                    "roster_ids": [rid],
                    "settings": {"seq": index, "waiver_bid": bid} if waiver else None,
                    "status": "complete",
                    "type": "waiver" if waiver else "free_agent",
                    "waiver_budget": [],
                }
            )

        if week in TRADE_WEEKS:
            a, b = rng.sample(roster_ids, 2)

            def tradeable(rid: int, rosters: dict[int, list[str]] = rosters) -> str:
                ranked = sorted(rosters[rid], key=lambda p: -players[p]["base"])
                return ranked[rng.randint(5, 8)]

            give_a, give_b = tradeable(a), tradeable(b)
            rosters[a].remove(give_a)
            rosters[b].remove(give_b)
            rosters[a].append(give_b)
            rosters[b].append(give_a)
            moves[a] += 1
            moves[b] += 1
            faab = []
            if week in (6, 12):
                amount = rng.choice((5, 7, 10, 15))
                faab = [{"amount": amount, "receiver": b, "sender": a}]
            txns.append(
                {
                    "adds": {give_b: a, give_a: b},
                    "consenter_ids": [a, b],
                    "creator": user_ids[a],
                    "draft_picks": [
                        {
                            "league_id": None,
                            "owner_id": b,
                            "previous_owner_id": a,
                            "roster_id": str(a),
                            "round": rng.randint(2, 5),
                            "season": str(SEASON + 1),
                        }
                    ],
                    "drops": {give_a: a, give_b: b},
                    "leg": week,
                    "metadata": {},
                    "roster_ids": [a, b],
                    "settings": None,
                    "status": "complete",
                    "type": "trade",
                    "waiver_budget": faab,
                }
            )

        for txn in txns:
            txn["transaction_id"] = _token(rng, used_ids)
            txn["created"] = txn_rank
            txn["status_updated"] = txn_rank + 1
            txn_rank += 1
        week_txns[week] = txns
        week_roster[week] = {rid: sorted(held) for rid, held in rosters.items()}

        starters_this_week: dict[int, list[str]] = {}
        points_this_week: dict[int, dict[str, int]] = {}
        for rid in roster_ids:
            held = week_roster[week][rid]
            lineup = _choose_lineup(rng, players, held, bye_teams)
            if len(lineup) != len(SLOTS):
                raise _Rejected("a roster cannot fill its lineup")
            starters_this_week[rid] = lineup
            points: dict[str, int] = {}
            for pid in held:
                if players[pid]["team"] in bye_teams:
                    points[pid] = 0
                    continue
                factor = 0.2 + (rng.random() + rng.random() + rng.random()) * 0.5333
                swing = rng.random()
                if swing < 0.06:
                    factor *= 1.7
                elif swing > 0.94:
                    factor *= 0.25
                points[pid] = round(players[pid]["base"] * factor * 100)
            points_this_week[rid] = points
        week_starters[week] = starters_this_week
        week_points[week] = points_this_week

    # ----- scenario: force the 9-3 lucky leader, then week 12's blowout and one-point game -----
    totals: dict[int, dict[int, int]] = {
        week: {rid: sum(week_points[week][rid][p] for p in week_starters[week][rid]) for rid in roster_ids}
        for week in week_points
    }
    leader = rng.choice(roster_ids)
    win_weeks = set(rng.sample(range(1, LAST_WEEK + 1), 9))
    for week in range(1, LAST_WEEK + 1):
        for left, right in _pairings(rounds, week):
            if leader not in (left, right):
                continue
            opponent = right if left == leader else left
            mine = totals[week][leader]
            gap = rng.randint(250, 1500)
            if week in win_weeks:
                totals[week][opponent] = min(totals[week][opponent], mine - gap)
            else:
                totals[week][opponent] = max(totals[week][opponent], mine + gap)

    games12 = [pair for pair in _pairings(rounds, LAST_WEEK) if leader not in pair]
    games12.sort(key=lambda pair: -max(totals[LAST_WEEK][pair[0]], totals[LAST_WEEK][pair[1]]))
    blow_a, blow_b = games12[0]
    winner, loser = (blow_a, blow_b) if totals[LAST_WEEK][blow_a] >= totals[LAST_WEEK][blow_b] else (blow_b, blow_a)
    totals[LAST_WEEK][winner] = max(totals[LAST_WEEK][winner], 13500)
    totals[LAST_WEEK][loser] = round(totals[LAST_WEEK][winner] * (0.55 + 0.05 * rng.random()))
    close_a, close_b = games12[-1]
    close = (close_a, close_b)
    winner, loser = close if totals[LAST_WEEK][close_a] >= totals[LAST_WEEK][close_b] else close[::-1]
    totals[LAST_WEEK][loser] = totals[LAST_WEEK][winner] - rng.randint(31, 95)

    for week_totals in totals.values():
        if any(not 6500 <= value <= 17500 for value in week_totals.values()):
            raise _Rejected("score out of range")
    for week in range(1, LAST_WEEK + 1):
        for left, right in _pairings(rounds, week):
            if totals[week][left] == totals[week][right]:
                raise _Rejected("tie")

    # ----- scale each lineup to its team total, in cents -----
    for week in range(1, LAST_WEEK + 1):
        for rid in roster_ids:
            starters = week_starters[week][rid]
            points = week_points[week][rid]
            natural = sum(points[p] for p in starters)
            live = [p for p in starters if points[p] > 0]
            if not live:
                raise _Rejected("no live starter")
            if not 0.5 <= totals[week][rid] / natural <= 1.8:
                raise _Rejected("lineup rescaled too hard")
            for pid in live:
                points[pid] = max(1, round(points[pid] * totals[week][rid] / natural))
            drift = totals[week][rid] - sum(points[p] for p in starters)
            top = max(live, key=lambda p: points[p])
            points[top] += drift
            if sum(points[p] for p in starters) != totals[week][rid]:
                raise _Rejected("scaling drift")

    # ----- check the scenario before anything is written -----
    wins = dict.fromkeys(roster_ids, 0)
    expected = dict.fromkeys(roster_ids, 0.0)
    for week in range(1, LAST_WEEK + 1):
        for left, right in _pairings(rounds, week):
            wins[left if totals[week][left] > totals[week][right] else right] += 1
        for rid in roster_ids:
            beaten = sum(totals[week][rid] > totals[week][other] for other in roster_ids if other != rid)
            expected[rid] += beaten / (TEAMS - 1)
    luck = {rid: wins[rid] - expected[rid] for rid in roster_ids}
    if wins[leader] != 9 or any(wins[rid] >= 9 for rid in roster_ids if rid != leader):
        raise _Rejected("leader is not the only 9-win team")
    runner_up = max(luck[rid] for rid in roster_ids if rid != leader)
    if luck[leader] - runner_up < 0.4:
        raise _Rejected("leader is not clearly the luckiest")
    last = totals[LAST_WEEK]
    margins = sorted((abs(last[left] - last[right]), left, right) for left, right in _pairings(rounds, LAST_WEEK))
    if margins[0][0] >= 100 or margins[1][0] <= 100:
        raise _Rejected("week 12 needs exactly one game inside a point")
    widest = margins[-1]
    big, small = sorted((totals[LAST_WEEK][widest[1]], totals[LAST_WEEK][widest[2]]), reverse=True)
    if small > big * 0.65:
        raise _Rejected("widest week-12 game is not a blowout")

    return {
        "names": names,
        "players": players,
        "roster_ids": roster_ids,
        "draft_order": draft_order,
        "draft_log": draft_log,
        "user_ids": user_ids,
        "draft_id": draft_id,
        "rounds": rounds,
        "week_roster": week_roster,
        "week_starters": week_starters,
        "week_points": week_points,
        "week_txns": week_txns,
        "budget_used": budget_used,
        "moves": moves,
        "wins": wins,
        "luck": luck,
        "leader": leader,
        "blowout": widest,
        "attempt": attempt,
        "txn_total": txn_rank,
    }


# --------------------------------------------------------------------------- #
# Bundle assembly
# --------------------------------------------------------------------------- #


def _league_dict(name: str) -> dict[str, Any]:
    return {
        "draft_id": "",
        "league_id": LEAGUE_ID,
        "metadata": {},
        "name": name,
        "previous_league_id": None,
        "roster_positions": [*SLOTS[:6], "FLEX", "FLEX", *["BN"] * (sum(QUOTA.values()) - len(SLOTS))],
        "scoring_settings": {
            "fum_lost": -2,
            "pass_int": -2,
            "pass_td": 4,
            "pass_yd": 0.04,
            "rec": 0.5,
            "rec_td": 6,
            "rec_yd": 0.1,
            "rush_td": 6,
            "rush_yd": 0.1,
        },
        "season": str(SEASON),
        "season_type": "regular",
        "settings": {
            "daily_waivers": 0,
            "divisions": 0,
            "draft_rounds": ROUNDS,
            "leg": LAST_WEEK + 1,
            "league_average_match": 0,
            "num_teams": TEAMS,
            "pick_trading": 1,
            "playoff_teams": 6,
            "playoff_week_start": PLAYOFF_WEEK_START,
            "start_week": 1,
            "trade_deadline": 12,
            "type": 0,
            "waiver_budget": FAAB_BUDGET,
            "waiver_type": 2,
        },
        "sport": "nfl",
        "status": "in_season",
        "total_rosters": TEAMS,
    }


def _public_player(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "base"}


def _build(sim: dict[str, Any]) -> dict[str, Any]:
    """The league bundle and the twelve week files, as plain JSON-ready structures."""
    names: list[str] = sim["names"]
    players: dict[str, dict[str, Any]] = sim["players"]
    roster_ids: list[int] = sim["roster_ids"]
    grid = _load_tool("anonymize")
    point_in_time = _load_tool("point_in_time_rosters")
    league_name = names[0]
    manager_names = names[1 : TEAMS + 1]
    team_names = names[TEAMS + 1 : 2 * TEAMS + 1]

    league = _league_dict(league_name)
    league["draft_id"] = sim["draft_id"]
    users = [
        {
            "display_name": manager_names[rid - 1],
            "is_bot": False,
            "is_owner": rid == 1,
            "league_id": LEAGUE_ID,
            "metadata": {"team_name": team_names[rid - 1]},
            "user_id": sim["user_ids"][rid],
        }
        for rid in roster_ids
    ]
    slot_of = {rid: slot for slot, rid in enumerate(sim["draft_order"], start=1)}
    draft = {
        "creators": [sim["user_ids"][1]],
        "draft_id": sim["draft_id"],
        "draft_order": {sim["user_ids"][rid]: slot_of[rid] for rid in roster_ids},
        "league_id": LEAGUE_ID,
        "metadata": {"name": league_name, "scoring_type": "half_ppr"},
        "season": str(SEASON),
        "season_type": "regular",
        "settings": {"rounds": ROUNDS, "slots_bn": 12, "teams": TEAMS},
        "slot_to_roster_id": {str(slot): str(rid) for rid, slot in slot_of.items()},
        "sport": "nfl",
        "status": "complete",
        "type": "snake",
    }
    draft_picks = []
    for pick_no, round_no, pid, rid in sim["draft_log"]:
        record = players[pid]
        draft_picks.append(
            {
                "draft_id": sim["draft_id"],
                "draft_slot": slot_of[rid],
                "is_keeper": None,
                "metadata": {
                    "first_name": record["first_name"],
                    "injury_status": "",
                    "last_name": record["last_name"],
                    "number": str(record["number"]),
                    "player_id": pid,
                    "position": record["position"],
                    "status": "Active",
                    "team": record["team"],
                    "years_exp": str(record["years_exp"]),
                },
                "pick_no": pick_no,
                "picked_by": sim["user_ids"][rid],
                "player_id": pid,
                "roster_id": str(rid),
                "round": round_no,
            }
        )
    league_rosters = [
        {
            "co_owners": None,
            "league_id": LEAGUE_ID,
            "metadata": {},
            "owner_id": sim["user_ids"][rid],
            "roster_id": str(rid),
            "settings": {},
        }
        for rid in roster_ids
    ]
    league_bundle = {
        "draft": draft,
        "draft_picks": draft_picks,
        "league": league,
        "players": {pid: _public_player(record) for pid, record in sorted(players.items())},
        "previous_league_ids": [],
        "rosters": league_rosters,
        "users": users,
    }

    # Each week's rows. Cumulative matchups feed the shared point-in-time fold.
    matchup_rows: dict[int, list[dict[str, Any]]] = {}
    optimal_total = dict.fromkeys(roster_ids, 0)
    optimal_cumulative: dict[int, dict[int, int]] = {}
    for week in range(1, LAST_WEEK + 1):
        rows = []
        for matchup_id, (left, right) in enumerate(sim["rounds"][(week - 1) % len(sim["rounds"])], start=1):
            for rid in (left, right):
                points = sim["week_points"][week][rid]
                starters = sim["week_starters"][week][rid]
                held = sim["week_roster"][week][rid]
                optimal_total[rid] += _optimal_cents(players, points)
                rows.append(
                    {
                        "custom_points": None,
                        "matchup_id": matchup_id,
                        "players": sorted(held),
                        "players_points": {pid: _dollars(points[pid]) for pid in sorted(held)},
                        "points": _dollars(sum(points[p] for p in starters)),
                        "roster_id": str(rid),
                        "starters": starters,
                        "starters_points": [_dollars(points[p]) for p in starters],
                    }
                )
        matchup_rows[week] = sorted(rows, key=lambda row: int(row["roster_id"]))
        optimal_cumulative[week] = dict(optimal_total)

    weeks: dict[int, dict[str, Any]] = {}
    for week in range(1, LAST_WEEK + 1):
        fold = {
            "matchups": {str(w): matchup_rows[w] for w in range(1, week + 1)},
            "meta": {"target_week": week},
            "rosters": [],
        }
        for rid in roster_ids:
            last_starters = sim["week_starters"][week][rid]
            ppts, ppts_decimal = _split(optimal_cumulative[week][rid])
            fold["rosters"].append(
                {
                    "co_owners": None,
                    "keepers": None,
                    "league_id": LEAGUE_ID,
                    "metadata": {},
                    "owner_id": sim["user_ids"][rid],
                    "player_map": None,
                    "players": sim["week_roster"][week][rid],
                    "reserve": [],
                    "roster_id": str(rid),
                    "settings": {
                        "ppts": ppts,
                        "ppts_decimal": ppts_decimal,
                        "total_moves": sum(
                            1 for w in range(1, week + 1) for t in sim["week_txns"][w] if rid in t["roster_ids"]
                        ),
                        "waiver_budget_used": 0,
                        "waiver_position": (rid * 5 + week) % TEAMS + 1,
                    },
                    "starters": last_starters,
                    "taxi": [],
                }
            )
        used = dict.fromkeys(roster_ids, 0)
        for w in range(1, week + 1):
            for txn in sim["week_txns"][w]:
                if txn["type"] == "waiver":
                    used[txn["roster_ids"][0]] += txn["settings"]["waiver_bid"]
        for roster in fold["rosters"]:
            roster["settings"]["waiver_budget_used"] = used[int(roster["roster_id"])]
        point_in_time.apply(fold)

        transactions = []
        for txn in sim["week_txns"][week]:
            row = dict(txn)
            row["created"] = grid.SYNTH_EPOCH_BASE_MS + txn["created"] * grid.SYNTH_EPOCH_STEP_MS
            row["status_updated"] = grid.SYNTH_EPOCH_BASE_MS + txn["status_updated"] * grid.SYNTH_EPOCH_STEP_MS
            transactions.append(row)
        next_week = week + 1
        weeks[week] = {
            "matchups": matchup_rows[week],
            "next_matchups": (
                []
                if next_week >= PLAYOFF_WEEK_START
                else [
                    {"matchup_id": matchup_id, "roster_id": str(rid)}
                    for matchup_id, pair in enumerate(sim["rounds"][(next_week - 1) % len(sim["rounds"])], start=1)
                    for rid in pair
                ]
            ),
            "rosters": fold["rosters"],
            "transactions": transactions,
            "week": week,
        }
    return {"league": league_bundle, "weeks": weeks}


def build_all() -> tuple[dict[str, str], str]:
    """``({relative path: file text}, scenario summary)`` for the whole data directory."""
    pool = list(_load_tool("anonymize").NAME_POOL)
    last_error = "no attempt ran"
    for attempt in range(MAX_ATTEMPTS):
        name_rng = random.Random(f"{SEED}:names")
        names = name_rng.sample(pool, 2 * TEAMS + 1)
        try:
            sim = _simulate(attempt, names)
        except _Rejected as exc:
            last_error = str(exc)
            continue
        built = _build(sim)
        files = {"league.json": _dump(built["league"])}
        for week, payload in built["weeks"].items():
            files[f"weeks/{week:02d}.json"] = _dump(payload)
        return files, _summary(sim, built)
    raise SystemExit(f"no seed attempt satisfied the scenario ({MAX_ATTEMPTS} tried); last: {last_error}")


def _dump(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"


def _summary(sim: dict[str, Any], built: dict[str, Any]) -> str:
    names: list[str] = sim["names"]
    label = {rid: names[TEAMS + rid] for rid in sim["roster_ids"]}
    leader = sim["leader"]
    _, left, right = sim["blowout"]
    week12 = {row["roster_id"]: row["points"] for row in built["weeks"][LAST_WEEK]["matchups"]}
    pairs = sorted(
        ((abs(week12[str(a)] - week12[str(b)]), a, b) for a, b in _pairings(sim["rounds"], LAST_WEEK)),
    )
    trades = [t for w in sim["week_txns"].values() for t in w if t["type"] == "trade"]
    lines = [
        f"seed {SEED}, attempt {sim['attempt']}",
        f"league: {names[0]}; {TEAMS} teams, weeks 1-{LAST_WEEK} final",
        f"luckiest, and 9-3 leader: roster {leader} ({label[leader]}), luck {sim['luck'][leader]:+.2f}",
        f"blowout (week {LAST_WEEK}): {label[left]} {week12[str(left)]} vs {label[right]} {week12[str(right)]}",
        f"closest (week {LAST_WEEK}): margin {pairs[0][0]:.2f}",
        f"trades: {len(trades)} (weeks {', '.join(str(t['leg']) for t in trades)})",
        f"transactions: {sim['txn_total']}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the committed files differ from a re-run")
    args = parser.parse_args(argv)
    files, summary = build_all()
    print(summary)
    if args.check:
        stale = [
            rel
            for rel, text in files.items()
            if not (DATA_DIR / rel).is_file() or (DATA_DIR / rel).read_bytes() != text.encode("utf-8")
        ]
        on_disk = {p.relative_to(DATA_DIR).as_posix() for p in (DATA_DIR / "weeks").glob("*.json")}
        extra = sorted(on_disk - set(files))
        if stale or extra:
            print(f"committed demo data differs from a re-run: {', '.join(stale + extra)}", file=sys.stderr)
            return 1
        print("committed demo data matches a re-run")
        return 0
    for rel, text in files.items():
        target = DATA_DIR / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {len(files)} files under {DATA_DIR.relative_to(REPO_ROOT).as_posix()}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
