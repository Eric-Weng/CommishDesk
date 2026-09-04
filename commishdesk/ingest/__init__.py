"""Stage 1 — pull a league through a platform-agnostic Adapter port and sanitize
every league-supplied string at the boundary.

Public surface: :func:`sanitize` (the one league-string scrubber, AD-24),
:func:`build_league_model` (raw platform bundle -> shape-agnostic
:class:`LeagueModel`), and the frozen Pydantic models the model is built from.
This package imports nothing from ``commishdesk.adapters`` or any later stage
(AD-1).
"""

from __future__ import annotations

from .build import build_league_model
from .model import Division, Draft, LeagueFormat, LeagueModel, Pick, Player, Team
from .sanitize import MAX_NAME_LENGTH, sanitize

__all__ = [
    "MAX_NAME_LENGTH",
    "Division",
    "Draft",
    "LeagueFormat",
    "LeagueModel",
    "Pick",
    "Player",
    "Team",
    "build_league_model",
    "sanitize",
]
