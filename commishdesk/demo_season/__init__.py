"""The fictional demo season, shipped as package data with an offline loader (Story 8.6).

Twelve invented teams, twelve final weeks of a 14-week 2026 regular season, in the raw
bundle shapes the Sleeper adapter returns, so ``compute_weekly_facts`` and
``produce_weekly_issue`` take them unchanged. **Fictional, never a League**: every manager,
team and player is invented, no Sleeper id is real, and nothing here is a ``League``, a
``leagues/*.toml`` row or something the weekly batch can address.

The data lives beside this module (``league.json`` and ``weeks/NN.json``), is read with
:mod:`importlib.resources` so it works from an installed wheel, and makes no network call.
``tools/build_demo_season.py`` authors it from a fixed seed; ``tools/check_demo_names.py``
checks the invented names against a real NFL player list.

This module sits outside the ``stats/`` and ``facts/`` fences on purpose (AD-1): it imports
nothing but the standard library and :mod:`commishdesk.errors`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib import resources
from typing import Any

from commishdesk.errors import CommishDeskError

__all__ = ["DEMO_LEAGUE_ID", "WEEKS", "load_league_bundle", "load_week_bundle"]

#: The league id the demo bundles carry. Opaque and not a Sleeper id.
DEMO_LEAGUE_ID = "demo"

#: The weeks the demo has final results for.
WEEKS: tuple[int, ...] = tuple(range(1, 13))


def _read(name: str) -> dict[str, Any]:
    text = resources.files(__package__).joinpath(name).read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise CommishDeskError(f"demo season file {name!r} is not a JSON object")
    return data


def load_league_bundle() -> dict[str, Any]:
    """The league bundle: ``league`` / ``draft`` / ``draft_picks`` / ``rosters`` / ``users`` /
    ``players`` / ``previous_league_ids``, the shape ``SleeperAdapter.fetch`` returns. A fresh
    object on every call."""
    return _read("league.json")


def load_week_bundle(week: int) -> dict[str, Any]:
    """The cumulative week bundle for *week* (1-12), the shape ``SleeperAdapter.fetch_week``
    returns: matchups and settled transactions for weeks ``1..week``, point-in-time rosters,
    the next week's pairings, only the players those reference, and an ``nfl_state`` that
    reads *week* as final. A fresh object on every call; raises
    :class:`~commishdesk.errors.CommishDeskError` outside 1-12."""
    if isinstance(week, bool) or not isinstance(week, int) or week not in WEEKS:
        raise CommishDeskError(f"demo season week {week!r} is outside {WEEKS[0]}-{WEEKS[-1]}")

    matchups: dict[str, Any] = {}
    transactions: dict[str, Any] = {}
    for number in range(1, week + 1):
        part = _read(f"weeks/{number:02d}.json")
        matchups[str(number)] = part["matchups"]
        transactions[str(number)] = part["transactions"]
    current = _read(f"weeks/{week:02d}.json")
    league = load_league_bundle()

    referenced: set[str] = set()
    for roster in current["rosters"]:
        for key in ("players", "reserve", "taxi"):
            referenced.update(roster[key])
    for rows in matchups.values():
        for row in rows:
            referenced.update(row["players"])
    for rows in transactions.values():
        for txn in rows:
            referenced.update(txn["adds"])
            referenced.update(txn["drops"])
    all_players: Mapping[str, Any] = league["players"]

    return {
        "league": league["league"],
        "losers_bracket": [],
        "matchups": matchups,
        "next_matchups": current["next_matchups"],
        "nfl_state": {"season_type": "regular", "week": week + 1},
        "players": {pid: record for pid, record in all_players.items() if pid in referenced},
        "rosters": current["rosters"],
        "transactions": transactions,
        "winners_bracket": [],
    }
