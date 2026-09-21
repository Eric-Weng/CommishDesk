"""Stage 1 — pull a league through a platform-agnostic Adapter port and sanitize
every league-supplied string at the boundary.

Public surface: :func:`sanitize` (the one league-string scrubber, AD-24),
:func:`build_league_model` (raw platform bundle -> shape-agnostic
:class:`LeagueModel`), :func:`build_week_model` (raw weekly bundle ->
shape-agnostic :class:`WeekModel`, Story 5.3a), :func:`build_player_snapshot` /
:func:`get_player_snapshot` (Story 5.3b), :func:`load_byes` / :func:`bye_teams`
(the committed NFL bye-week data, Story 5.3b), and the frozen Pydantic models
each is built from. This package imports nothing from ``commishdesk.adapters``
or any later stage (AD-1).
"""

from __future__ import annotations

from .build import build_league_model, build_player_snapshot, build_week_model, get_player_snapshot
from .byes import bye_teams, load_byes
from .model import (
    BracketMatch,
    Division,
    Draft,
    FaabTransfer,
    LeagueFormat,
    LeagueModel,
    Matchup,
    Pick,
    Player,
    PlayerSnapshot,
    PlayoffFormat,
    Roster,
    Team,
    TradedPick,
    Transaction,
    WeekModel,
)
from .sanitize import MAX_NAME_LENGTH, sanitize

__all__ = [
    "MAX_NAME_LENGTH",
    "BracketMatch",
    "Division",
    "Draft",
    "FaabTransfer",
    "LeagueFormat",
    "LeagueModel",
    "Matchup",
    "Pick",
    "Player",
    "PlayerSnapshot",
    "PlayoffFormat",
    "Roster",
    "Team",
    "TradedPick",
    "Transaction",
    "WeekModel",
    "build_league_model",
    "build_player_snapshot",
    "build_week_model",
    "bye_teams",
    "get_player_snapshot",
    "load_byes",
    "sanitize",
]
