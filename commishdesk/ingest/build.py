"""``build_league_model`` -- raw platform bundle -> validated :class:`LeagueModel`.

Consumes a ``Mapping[str, Any]`` in the Story 2.2 adapter-bundle shape (keys
``league`` / ``draft`` / ``draft_picks`` / ``rosters`` / ``users`` /
``previous_league_ids``) and produces the shape-agnostic stage-1 model. It
imports nothing from ``commishdesk.adapters`` or any later stage (AD-1); the
bundle shape is the whole input contract.

Pipeline:

* validate the bundle is an object with the required sections, present and
  correctly typed, and at least one roster;
* index users by id;
* derive :class:`LeagueFormat` from ``league.roster_positions`` /
  ``scoring_settings`` / ``settings`` -- team count, ordered starting slots, flex
  eligibility, a best-effort scoring label, divisions -- all as data;
* join each roster to its owning user into a :class:`Team` (co-owned and orphan
  rosters tolerated, never a raise), ordered by ``roster_id``;
* snapshot each ``draft_picks`` entry into a :class:`Pick` whose player position
  and NFL team come only from that pick's own ``metadata`` -- never a live
  ``/players/nfl`` lookup -- ordered by ``pick_no``;
* carry draft-level metadata into :class:`Draft`.

Every league-supplied string (league name, team name, display name, division
name) passes through :func:`~commishdesk.ingest.sanitize.sanitize` exactly once,
here, before it reaches a model field (AD-24). A structural failure -- a missing
or mistyped section, a non-object list item, a field the model rejects -- raises
:class:`~commishdesk.errors.IngestError` chained (where an underlying exception
exists) from the original ``KeyError`` / ``TypeError`` / ``ValueError`` /
``OverflowError`` / ``ValidationError``; no partial model escapes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from commishdesk.errors import IngestError

from .model import Division, Draft, LeagueFormat, LeagueModel, Pick, Player, Team
from .sanitize import sanitize

__all__ = ["build_league_model"]

# Flex slot -> the positions it will accept. Only slots that actually appear in
# `roster_positions` land in the built `flex_eligibility`.
_FLEX_ELIGIBILITY: dict[str, tuple[str, ...]] = {
    "FLEX": ("RB", "WR", "TE"),
    "SUPER_FLEX": ("QB", "RB", "WR", "TE"),
    "REC_FLEX": ("WR", "TE"),
    "WRRB_FLEX": ("RB", "WR"),
}
_NON_STARTING_SLOTS = frozenset({"BN", "IR", "TAXI"})

# A hostile `settings.divisions` (say 1e9) would blow up `range()`; above this we
# ignore the declared count and keep only the ids individual rosters reference.
_MAX_DECLARED_DIVISIONS = 32

_CAUGHT = (KeyError, TypeError, ValueError, OverflowError, ValidationError)


def build_league_model(bundle: Mapping[str, Any]) -> LeagueModel:
    """Build a validated :class:`LeagueModel` from a raw platform bundle. Raises
    :class:`~commishdesk.errors.IngestError` (chained where possible, never a
    partial model) on any structural failure."""
    if not isinstance(bundle, Mapping):
        raise IngestError("bundle is not a JSON object")

    league = _section(bundle, "league", _MAPPING)
    rosters = _section(bundle, "rosters", _LIST)
    users = _section(bundle, "users", _LIST)
    draft = _section(bundle, "draft", _MAPPING)
    draft_picks = _section(bundle, "draft_picks", _LIST)

    if not rosters:
        raise IngestError("bundle has no rosters")

    try:
        users_by_id = _index_users(users)
        league_format = _build_format(league, rosters)
        teams = sorted(
            (_build_team(_object(item, "rosters"), users_by_id) for item in rosters),
            key=lambda team: _sort_key(team.roster_id),
        )
        picks = sorted(
            (_build_pick(_object(item, "draft_picks"), users_by_id) for item in draft_picks),
            key=lambda pick: pick.pick_no,
        )
        return LeagueModel(
            league_id=str(league["league_id"]),
            name=sanitize(_text(league.get("name"))),
            season=int(league["season"]),
            format=league_format,
            teams=teams,
            picks=picks,
            draft=_build_draft(draft),
        )
    except _CAUGHT as exc:
        raise IngestError(
            f"could not build a league model from the bundle ({type(exc).__name__})"
        ) from exc


# --------------------------------------------------------------------------- #
# Section / item validation
# --------------------------------------------------------------------------- #

_MAPPING = "object"
_LIST = "array"


def _section(bundle: Mapping[str, Any], key: str, kind: str) -> Any:
    """Return ``bundle[key]``, raising an :class:`IngestError` that names the
    section if it is missing (chained from the ``KeyError``) or the wrong JSON
    type."""
    try:
        value = bundle[key]
    except (KeyError, TypeError) as exc:
        raise IngestError(f"bundle is missing the required {key!r} section") from exc
    ok = isinstance(value, Mapping) if kind == _MAPPING else isinstance(value, list)
    if not ok:
        raise IngestError(
            f"{key!r} section is {type(value).__name__}, expected a JSON {kind}"
        )
    return value


def _object(item: Any, section: str) -> Mapping[str, Any]:
    """Assert a list item is a JSON object; a non-object item is a structural
    failure, not something to skip over."""
    if not isinstance(item, Mapping):
        raise IngestError(
            f"{section!r} contains a non-object item ({type(item).__name__})"
        )
    return item


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #


def _index_users(users: list[Any]) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    for item in users:
        user = _object(item, "users")
        user_id = user.get("user_id")
        if user_id is not None:
            index[str(user_id)] = user
    return index


def _display_name(user: Mapping[str, Any] | None) -> str | None:
    """The user's sanitized display name, or ``None`` when there is no user or
    the platform gave no name (``""`` after sanitizing collapses to ``None``)."""
    if user is None:
        return None
    raw = user.get("display_name")
    if raw is None:
        return None
    return sanitize(_text(raw)) or None


# --------------------------------------------------------------------------- #
# League format (shape-as-data)
# --------------------------------------------------------------------------- #


def _build_format(league: Mapping[str, Any], rosters: list[Any]) -> LeagueFormat:
    settings = _mapping(league.get("settings"))
    scoring = _mapping(league.get("scoring_settings"))

    raw_positions = league.get("roster_positions")
    if not isinstance(raw_positions, list):
        raise IngestError("league.roster_positions is missing or not an array")
    roster_slots = [str(slot) for slot in raw_positions if str(slot) not in _NON_STARTING_SLOTS]

    flex_eligibility: dict[str, list[str]] = {}
    for slot in dict.fromkeys(roster_slots):
        if slot in _FLEX_ELIGIBILITY:
            flex_eligibility[slot] = list(_FLEX_ELIGIBILITY[slot])
        elif "FLEX" in slot:
            # An unrecognized flex slot (e.g. IDP_FLEX): keep it in roster_slots
            # but record eligibility unknown so a downstream lookup never KeyErrors.
            flex_eligibility[slot] = []

    team_count = (
        _as_int(settings.get("num_teams"))
        or _as_int(league.get("total_rosters"))
        or len(rosters)
    )
    is_superflex_or_2qb = roster_slots.count("QB") >= 2 or "SUPER_FLEX" in roster_slots

    return LeagueFormat(
        team_count=team_count,
        roster_slots=roster_slots,
        flex_eligibility=flex_eligibility,
        scoring_label=_scoring_label(scoring, roster_slots, settings),
        is_superflex_or_2qb=is_superflex_or_2qb,
        te_premium=_te_premium(scoring),
        divisions=_divisions(league, rosters, settings),
    )


def _te_premium(scoring: Mapping[str, Any]) -> bool:
    """True when the league scores TE receptions above every other position's."""
    rec_te = _as_float(scoring.get("rec_te"))
    if rec_te is None:
        return False
    return rec_te > (_as_float(scoring.get("rec")) or 0.0)


def _scoring_label(
    scoring: Mapping[str, Any], roster_slots: list[str], settings: Mapping[str, Any]
) -> str:
    """Best-effort, deterministic scoring label. Wording is not an acceptance
    oracle (Story 2.5 reconciles the built Facts JSON and may refine it)."""
    tokens: list[str] = []

    rec = _as_float(scoring.get("rec"))
    if rec == 1.0:
        tokens.append("PPR")
    elif rec == 0.5:
        tokens.append("Half PPR")
    else:
        tokens.append("Standard")

    if roster_slots.count("QB") >= 2:
        tokens.append("2QB")
    elif "SUPER_FLEX" in roster_slots:
        tokens.append("Superflex")

    if _te_premium(scoring):
        tokens.append("TE premium")

    league_type = _as_int(settings.get("type"))
    if league_type == 1:
        tokens.append("keeper")
    elif league_type == 2:
        tokens.append("dynasty")

    return " · ".join(tokens)


def _divisions(
    league: Mapping[str, Any], rosters: list[Any], settings: Mapping[str, Any]
) -> list[Division]:
    ids: set[int] = set()
    count = _as_int(settings.get("divisions"))
    if count and 0 < count <= _MAX_DECLARED_DIVISIONS:
        ids.update(range(1, count + 1))
    for item in rosters:
        if isinstance(item, Mapping):
            division = _as_int(_mapping(item.get("settings")).get("division"))
            if division is not None:
                ids.add(division)
    if not ids:
        return []

    metadata = _mapping(league.get("metadata"))
    divisions: list[Division] = []
    for division_id in sorted(ids):
        raw_name = metadata.get(f"division_{division_id}")
        name = sanitize(_text(raw_name)) if _text(raw_name) else None
        divisions.append(Division(id=division_id, name=name or None))
    return divisions


# --------------------------------------------------------------------------- #
# Teams
# --------------------------------------------------------------------------- #


def _build_team(
    roster: Mapping[str, Any], users_by_id: dict[str, Mapping[str, Any]]
) -> Team:
    owner_id_raw = roster.get("owner_id")
    owner_id = str(owner_id_raw) if owner_id_raw is not None else None
    user = users_by_id.get(owner_id) if owner_id is not None else None

    return Team(
        roster_id=str(roster["roster_id"]),
        owner_id=owner_id,
        manager=_display_name(user),
        team_name=_team_name(roster, user),
        co_owners=_co_owners(roster.get("co_owners")),
        division_id=_as_int(_mapping(roster.get("settings")).get("division")),
    )


def _team_name(
    roster: Mapping[str, Any], user: Mapping[str, Any] | None
) -> str | None:
    """The team's display name, preferring the per-league roster metadata over
    the user's own. Sanitized; ``None`` when neither source has a usable value
    (an orphan roster has none)."""
    for source in (roster, user):
        if source is None:
            continue
        raw = _mapping(source.get("metadata")).get("team_name")
        cleaned = sanitize(_text(raw)) if _text(raw) else ""
        if cleaned:
            return cleaned
    return None


def _co_owners(raw: Any) -> list[str]:
    """Normalize ``co_owners`` -- which may be ``null``, a list, or (defensively)
    a bare string -- to a list of ``str`` user ids, dropping ``null`` entries."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw if item is not None]
    return []


# --------------------------------------------------------------------------- #
# Picks
# --------------------------------------------------------------------------- #


def _build_pick(
    pick: Mapping[str, Any], users_by_id: dict[str, Mapping[str, Any]]
) -> Pick:
    round_no = int(pick["round"])
    slot = int(pick["draft_slot"])

    picked_by = pick.get("picked_by")
    manager = (
        _display_name(users_by_id.get(str(picked_by))) if picked_by is not None else None
    )

    return Pick(
        pick_no=int(pick["pick_no"]),
        round=round_no,
        slot=slot,
        board_label=f"{round_no}.{slot:02d}",
        roster_id=str(pick["roster_id"]),
        manager=manager,
        player=_build_player(pick),
    )


def _build_player(pick: Mapping[str, Any]) -> Player:
    metadata = _mapping(pick.get("metadata"))
    first = _text(metadata.get("first_name")).strip()
    last = _text(metadata.get("last_name")).strip()
    return Player(
        sleeper_id=str(metadata.get("player_id") or pick.get("player_id") or ""),
        name=f"{first} {last}".strip(),
        position=(metadata.get("position") or None),
        nfl_team=(metadata.get("team") or None),
    )


# --------------------------------------------------------------------------- #
# Draft
# --------------------------------------------------------------------------- #


def _build_draft(draft: Mapping[str, Any]) -> Draft:
    draft_type = draft.get("type")
    return Draft(
        id=str(draft["draft_id"]),
        type=str(draft_type) if draft_type is not None else None,
        rounds=_as_int(_mapping(draft.get("settings")).get("rounds")),
        started_at_ms=_as_int(draft.get("start_time")),
        completed_at_ms=_as_int(draft.get("last_picked")),
    )


# --------------------------------------------------------------------------- #
# Small coercions
# --------------------------------------------------------------------------- #


def _sort_key(value: str) -> tuple[int, Any]:
    """Order ids numerically when they parse as ints, lexically otherwise. The
    leading tag keeps int and str keys from ever being compared to each other."""
    try:
        return (0, int(value))
    except (TypeError, ValueError):
        return (1, value)


def _mapping(value: Any) -> Mapping[str, Any]:
    """A mapping is returned as-is; anything else becomes an empty mapping, so a
    missing or mistyped optional sub-object never crashes a ``.get`` chain."""
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    """Coerce an optional scalar to ``str`` (``None`` -> ``""``) for sanitizing
    or name-joining."""
    return "" if value is None else str(value)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None
