"""The shape-agnostic stage-1 domain model (AD-1, facts-schema design rule 2).

``build.py`` produces one :class:`LeagueModel` from a raw platform bundle. Every
league-shape fact that downstream code might otherwise branch on -- team count,
the ordered starting lineup, which positions each flex slot accepts, the scoring
label, the divisions -- is carried here as *data* on :class:`LeagueFormat`, so no
stage after ingest ever hardcodes "12 teams" or "one QB slot".

All models are ``frozen`` -- attribute reassignment is rejected; note the
collection fields (``roster_slots``, ``flex_eligibility``, ``co_owners``,
``teams``, ``picks``, ``divisions``) are not deep-frozen, so the models are not
hashable. ``extra="forbid"`` guards against a builder mistake and self-documents
the closed field set (the builder passes fixed kwargs, so an unknown *bundle*
key is simply ignored upstream). Ids that Sleeper hands us as strings stay
strings; ``season`` is an ``int`` year. Every string that originates with the
league has already been through ``sanitize()`` by the time it reaches a field
here.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

__all__ = [
    "Division",
    "Draft",
    "LeagueFormat",
    "LeagueModel",
    "Pick",
    "Player",
    "Team",
]


class _Frozen(BaseModel):
    """Shared config: immutable (attribute reassignment rejected; collection
    fields are not deep-frozen) and closed to unknown keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Division(_Frozen):
    """One league division. ``name`` is ``None`` when the platform names none."""

    id: int
    name: str | None = None


class LeagueFormat(_Frozen):
    """The league's shape, as data. No downstream stage branches on a hardcoded
    league shape -- it reads these fields instead."""

    team_count: int
    roster_slots: list[str]
    flex_eligibility: dict[str, list[str]]
    scoring_label: str
    is_superflex_or_2qb: bool
    te_premium: bool
    divisions: list[Division] = []


class Team(_Frozen):
    """One roster, joined to its owning user. Co-owned and orphan rosters are
    tolerated: ``manager`` / ``team_name`` are ``None`` for an orphan (no owner,
    or an owner absent from the users list), and never raise."""

    roster_id: str
    owner_id: str | None = None
    manager: str | None = None
    team_name: str | None = None
    co_owners: list[str] = []
    division_id: int | None = None


class Player(_Frozen):
    """A drafted player, snapshotted from the pick record's own ``metadata`` --
    never a live ``/players/nfl`` lookup, so regenerating the recap months later
    never changes the facts. Player names are NFL data and are not sanitized."""

    sleeper_id: str
    name: str
    position: str | None = None
    nfl_team: str | None = None


class Pick(_Frozen):
    """One draft selection. ``board_label`` is ``f"{round}.{slot:02d}"``;
    ``manager`` is the sanitized display name of whoever made the pick (or
    ``None`` if unknown)."""

    pick_no: int
    round: int
    slot: int
    board_label: str
    roster_id: str
    manager: str | None
    player: Player


class Draft(_Frozen):
    """Draft-level metadata. Epoch-ms timestamps are carried raw (Story 2.2's
    Design Notes deferred ISO 8601 conversion to Story 2.5)."""

    id: str
    type: str | None = None
    rounds: int | None = None
    started_at_ms: int | None = None
    completed_at_ms: int | None = None


class LeagueModel(_Frozen):
    """The whole stage-1 output: the league identity, its format-as-data, one
    :class:`Team` per roster, one :class:`Pick` per selection, and the draft.

    ``teams`` is ordered by ``roster_id`` (numerically where the ids parse as
    ints) and ``picks`` by ascending ``pick_no``, so two builds of one bundle
    produce equal models regardless of the bundle's list order."""

    league_id: str
    name: str
    season: int
    platform: str = "sleeper"
    format: LeagueFormat
    teams: list[Team]
    picks: list[Pick]
    draft: Draft
