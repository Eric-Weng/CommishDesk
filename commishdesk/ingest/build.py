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

Story 5.3a adds :func:`build_week_model`, mirroring this exact
validation/error-wrap shape for a weekly ``Adapter.fetch_week`` bundle (keys
``rosters`` / ``matchups`` / ``transactions``). No ids/enums/numbers it
carries need :func:`sanitize` -- see ``ingest/sanitize.py``'s module
docstring.

Story 5.4 extends :func:`build_week_model` to also read the bundle's
``league`` and the two bracket sections -- ``league.settings.playoff_week_start``
and ``winners_bracket``/``losers_bracket`` -- into :class:`WeekModel`'s three
new fields, so playoff/consolation classification (``stats/weekly.py``) has
the bracket shape it needs. All three are optional: a bundle missing
``league`` (or one predating this story) simply yields
``playoff_week_start=None`` and empty bracket lists, never a raise.

Story 5.3b adds :func:`build_player_snapshot` -- a pure ``bundle ->
{player_id: PlayerSnapshot}`` transform reading the weekly bundle's
``"players"`` key -- and :func:`get_player_snapshot`, the one function in this
module that is not a pure transform: it takes a :class:`~commishdesk.store.Store`
and implements "reuse the persisted snapshot, never re-derive it live"
(FR-5). Every other function here has no I/O; this one exists because that
reuse decision *is* the story's AC3, and no CLI entry point exists yet
(Story 5.11a) to host it instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from commishdesk.errors import IngestError

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
    Roster,
    Team,
    TradedPick,
    Transaction,
    WeekModel,
)
from .sanitize import sanitize

if TYPE_CHECKING:
    # Type-checking only: a runtime import here would cycle back through
    # commishdesk.store -> commishdesk.ingest.model -> commishdesk.ingest
    # (this package's own __init__, which imports this module). Nothing in
    # get_player_snapshot needs the Store *class* at runtime -- it only calls
    # duck-typed methods on the instance it is handed.
    from commishdesk.store import Store

__all__ = ["build_league_model", "build_player_snapshot", "build_week_model", "get_player_snapshot"]

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
    # Optional: an older bundle may predate the players blob. Absent it, every
    # player simply carries college=None and the narrator says nothing about it.
    raw_players = bundle.get("players")
    players_by_id: dict[str, Mapping[str, Any]] = (
        {str(k): v for k, v in raw_players.items() if isinstance(v, Mapping)}
        if isinstance(raw_players, Mapping)
        else {}
    )

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
            (_build_pick(_object(item, "draft_picks"), users_by_id, players_by_id) for item in draft_picks),
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
        raise IngestError(f"could not build a league model from the bundle ({type(exc).__name__})") from exc


def build_week_model(bundle: Mapping[str, Any]) -> WeekModel:
    """Build a validated :class:`WeekModel` from a raw weekly ``Adapter``
    bundle (``rosters`` / ``matchups`` / ``transactions``, plus the optional
    ``league`` / ``winners_bracket`` / ``losers_bracket`` sections Story 5.4
    reads for playoff classification). The target week is the highest week
    key present in ``matchups`` (``fetch_week(league_id, n)`` always carries
    matchups for every week ``1..n``, so its max key is ``n``). Raises
    :class:`~commishdesk.errors.IngestError` (chained where possible, never a
    partial model) on any structural failure."""
    if not isinstance(bundle, Mapping):
        raise IngestError("bundle is not a JSON object")

    rosters_raw = _section(bundle, "rosters", _LIST)
    matchups_raw = _section(bundle, "matchups", _MAPPING)
    transactions_raw = _section(bundle, "transactions", _MAPPING)

    if not rosters_raw:
        raise IngestError("bundle has no rosters")

    try:
        week = _target_week(matchups_raw)

        rosters = sorted(
            (_build_roster(_object(item, "rosters")) for item in rosters_raw),
            key=lambda roster: _sort_key(roster.roster_id),
        )

        matchups: list[Matchup] = []
        for wk in range(1, week + 1):
            week_rows = matchups_raw.get(str(wk))
            week_rows = week_rows if isinstance(week_rows, list) else []
            opponents = _opponents_for_week(week_rows)
            for row in week_rows:
                matchups.append(_build_matchup(_object(row, f"matchups[{wk}]"), wk, opponents))
        matchups.sort(key=lambda m: (m.week, _sort_key(m.roster_id)))

        week_transactions_raw = transactions_raw.get(str(week))
        week_transactions_raw = week_transactions_raw if isinstance(week_transactions_raw, list) else []
        transactions = sorted(
            (
                _build_transaction(_object(item, "transactions"))
                for item in week_transactions_raw
                if isinstance(item, Mapping) and item.get("status") == "complete"
            ),
            key=lambda txn: txn.transaction_id,
        )

        league_settings = _mapping(_mapping(bundle.get("league")).get("settings"))
        playoff_week_start = _as_int(league_settings.get("playoff_week_start"))
        winners_bracket = _build_bracket(bundle.get("winners_bracket"))
        losers_bracket = _build_bracket(bundle.get("losers_bracket"))

        return WeekModel(
            week=week,
            rosters=rosters,
            matchups=matchups,
            transactions=transactions,
            playoff_week_start=playoff_week_start,
            winners_bracket=winners_bracket,
            losers_bracket=losers_bracket,
        )
    except _CAUGHT as exc:
        raise IngestError(f"could not build a week model from the bundle ({type(exc).__name__})") from exc


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
        raise IngestError(f"{key!r} section is {type(value).__name__}, expected a JSON {kind}")
    return value


def _object(item: Any, section: str) -> Mapping[str, Any]:
    """Assert a list item is a JSON object; a non-object item is a structural
    failure, not something to skip over."""
    if not isinstance(item, Mapping):
        raise IngestError(f"{section!r} contains a non-object item ({type(item).__name__})")
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

    team_count = _as_int(settings.get("num_teams")) or _as_int(league.get("total_rosters")) or len(rosters)
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


def _scoring_label(scoring: Mapping[str, Any], roster_slots: list[str], settings: Mapping[str, Any]) -> str:
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


def _divisions(league: Mapping[str, Any], rosters: list[Any], settings: Mapping[str, Any]) -> list[Division]:
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


def _build_team(roster: Mapping[str, Any], users_by_id: dict[str, Mapping[str, Any]]) -> Team:
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


def _team_name(roster: Mapping[str, Any], user: Mapping[str, Any] | None) -> str | None:
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
    pick: Mapping[str, Any],
    users_by_id: dict[str, Mapping[str, Any]],
    players_by_id: dict[str, Mapping[str, Any]] | None = None,
) -> Pick:
    round_no = int(pick["round"])
    slot = int(pick["draft_slot"])

    picked_by = pick.get("picked_by")
    manager = _display_name(users_by_id.get(str(picked_by))) if picked_by is not None else None

    return Pick(
        pick_no=int(pick["pick_no"]),
        round=round_no,
        slot=slot,
        board_label=f"{round_no}.{slot:02d}",
        roster_id=str(pick["roster_id"]),
        manager=manager,
        player=_build_player(pick, players_by_id),
    )


def _build_player(pick: Mapping[str, Any], players_by_id: dict[str, Mapping[str, Any]] | None = None) -> Player:
    metadata = _mapping(pick.get("metadata"))
    first = _text(metadata.get("first_name")).strip()
    last = _text(metadata.get("last_name")).strip()
    sleeper_id = str(metadata.get("player_id") or pick.get("player_id") or "")
    record = (players_by_id or {}).get(sleeper_id) or {}
    return Player(
        sleeper_id=sleeper_id,
        name=f"{first} {last}".strip(),
        position=(metadata.get("position") or None),
        nfl_team=(metadata.get("team") or None),
        college=(_text(record.get("college")).strip() or None),
        # Sleeper writes "" for a healthy player, not null.
        injury_status=(_text(metadata.get("injury_status")).strip() or None),
        years_exp=_opt_int(metadata.get("years_exp")),
    )


def _opt_int(value: Any) -> int | None:
    """``"0"`` -> ``0``; blank / unparseable -> ``None``."""
    text = _text(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


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
# Story 5.3a: weekly ingest (rosters / matchups / transactions)
# --------------------------------------------------------------------------- #


def _target_week(matchups: Mapping[str, Any]) -> int:
    """The highest week key present in ``matchups`` -- ``fetch_week(league_id,
    n)`` always carries matchups for every week ``1..n``, so its max key is
    the target week. Raises :class:`~commishdesk.errors.IngestError` if
    ``matchups`` has no weeks at all, or if its keys don't form a contiguous
    ``1..week`` range -- a gap week must fail loudly, not silently vanish from
    the model (no partial model escapes)."""
    weeks = {int(key) for key in matchups}
    if not weeks:
        raise IngestError("bundle 'matchups' section has no weeks")
    week = max(weeks)
    missing = sorted(set(range(1, week + 1)) - weeks)
    if missing:
        raise IngestError(
            f"bundle 'matchups' section is missing week(s) {missing} "
            f"(expected a contiguous 1..{week} range)"
        )
    return week


def _build_roster(roster: Mapping[str, Any]) -> Roster:
    settings = _mapping(roster.get("settings"))
    return Roster(
        roster_id=str(roster["roster_id"]),
        wins=_as_int(settings.get("wins")) or 0,
        losses=_as_int(settings.get("losses")) or 0,
        ties=_as_int(settings.get("ties")) or 0,
        fpts=_combine_points(settings.get("fpts"), settings.get("fpts_decimal")),
        fpts_against=_combine_points(settings.get("fpts_against"), settings.get("fpts_against_decimal")),
        ppts=_combine_points(settings.get("ppts"), settings.get("ppts_decimal")),
        ir=[str(pid) for pid in (roster.get("reserve") or [])],
        taxi=[str(pid) for pid in (roster.get("taxi") or [])],
    )


def _combine_points(whole: Any, decimal: Any) -> float:
    """Sleeper splits a points total into an integer ``whole`` plus a
    ``decimal`` (hundredths) sibling field, e.g. ``fpts=2766``,
    ``fpts_decimal=12`` -> ``2766.12``. Either half missing or unparseable
    contributes ``0``."""
    return (_as_float(whole) or 0.0) + (_as_int(decimal) or 0) / 100


def _opponents_for_week(rows: list[Any]) -> dict[Any, Any]:
    """Map each row's raw ``roster_id`` to its opponent's raw ``roster_id`` for
    one week, by grouping rows on ``matchup_id``. A group of exactly two
    distinct rosters pairs them; a lone roster (a bye) or a null
    ``matchup_id`` is simply absent from the result (``.get`` then yields
    ``None`` -- no raise)."""
    by_matchup: dict[Any, list[Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        matchup_id = row.get("matchup_id")
        if matchup_id is None:
            continue
        by_matchup.setdefault(matchup_id, []).append(row.get("roster_id"))

    opponents: dict[Any, Any] = {}
    for roster_ids in by_matchup.values():
        if len(roster_ids) == 2 and roster_ids[0] != roster_ids[1]:
            a, b = roster_ids
            opponents[a] = b
            opponents[b] = a
    return opponents


def _build_matchup(row: Mapping[str, Any], week: int, opponents: dict[Any, Any]) -> Matchup:
    roster_id_raw = row["roster_id"]
    starters = [str(pid) for pid in (row.get("starters") or [])]
    players = [str(pid) for pid in (row.get("players") or [])]
    starters_set = set(starters)
    bench = [pid for pid in players if pid not in starters_set]

    raw_points = row.get("players_points")
    players_points = (
        {str(pid): (_as_float(pts) or 0.0) for pid, pts in raw_points.items()}
        if isinstance(raw_points, Mapping)
        else {}
    )

    opponent = opponents.get(roster_id_raw)
    return Matchup(
        week=week,
        roster_id=str(roster_id_raw),
        matchup_id=_as_int(row.get("matchup_id")),
        opponent_roster_id=str(opponent) if opponent is not None else None,
        points=_as_float(row.get("points")) or 0.0,
        starters=starters,
        starters_points=[_as_float(pts) or 0.0 for pts in (row.get("starters_points") or [])],
        bench=bench,
        players_points=players_points,
    )


def _build_bracket(raw: Any) -> list[BracketMatch]:
    """Build a :class:`BracketMatch` list from a raw ``winners_bracket`` /
    ``losers_bracket`` section (each row shaped ``{"r": round, "t1": id,
    "t2": id, ...}`` -- ``w``/``l``/``p``/``t*_from`` are Sleeper's own
    progression bookkeeping, not needed for round-membership classification
    and not carried). Absent, non-list, a row missing ``r``/``t1``/``t2``, or
    a degenerate self-paired row (``t1 == t2``) is skipped -- a bye,
    not-yet-decided slot, or malformed entry in an incomplete bracket --
    never a raise."""
    if not isinstance(raw, list):
        return []
    matches: list[BracketMatch] = []
    for row in raw:
        if not isinstance(row, Mapping):
            continue
        round_no = _as_int(row.get("r"))
        t1 = row.get("t1")
        t2 = row.get("t2")
        if round_no is None or t1 is None or t2 is None or str(t1) == str(t2):
            continue
        matches.append(BracketMatch(round=round_no, roster_ids=[str(t1), str(t2)]))
    return matches


def _build_transaction(item: Mapping[str, Any]) -> Transaction:
    settings = _mapping(item.get("settings"))
    txn_type = item.get("type")
    status = item.get("status")
    return Transaction(
        transaction_id=str(item["transaction_id"]),
        type=str(txn_type) if txn_type is not None else "",
        status=str(status) if status is not None else "",
        roster_ids=[str(rid) for rid in (item.get("roster_ids") or [])],
        adds=_id_map(item.get("adds")),
        drops=_id_map(item.get("drops")),
        draft_picks=[
            _build_traded_pick(_object(p, "transactions.draft_picks"))
            for p in (item.get("draft_picks") or [])
        ],
        faab=[
            _build_faab(_object(f, "transactions.waiver_budget"))
            for f in (item.get("waiver_budget") or [])
        ],
        waiver_bid=_as_int(settings.get("waiver_bid")),
    )


def _id_map(raw: Any) -> dict[str, str]:
    """Normalize a ``{player_id: roster_id}`` mapping (``adds`` / ``drops``) to
    ``str`` keys and values; anything other than a mapping becomes ``{}``."""
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): str(v) for k, v in raw.items() if v is not None}


def _build_traded_pick(pick: Mapping[str, Any]) -> TradedPick:
    owner_id = pick.get("owner_id")
    previous_owner_id = pick.get("previous_owner_id")
    return TradedPick(
        season=_text(pick.get("season")),
        round=_as_int(pick.get("round")) or 0,
        roster_id=str(pick["roster_id"]),
        owner_id=str(owner_id) if owner_id is not None else None,
        previous_owner_id=str(previous_owner_id) if previous_owner_id is not None else None,
    )


def _build_faab(transfer: Mapping[str, Any]) -> FaabTransfer:
    return FaabTransfer(
        sender=str(transfer["sender"]),
        receiver=str(transfer["receiver"]),
        amount=_as_int(transfer.get("amount")) or 0,
    )


# --------------------------------------------------------------------------- #
# Story 5.3b: NFL bye data and the player snapshot
# --------------------------------------------------------------------------- #


def build_player_snapshot(bundle: Mapping[str, Any]) -> dict[str, PlayerSnapshot]:
    """Build a pure ``player_id -> PlayerSnapshot`` map from a weekly
    ``Adapter.fetch_week`` bundle's ``"players"`` key (``SleeperAdapter``'s
    second ``/players/nfl`` call, filtered to that week's rostered players --
    see ``adapters/sleeper.py::_fetch_players``).

    ``"players"`` is optional in the bundle -- an older bundle, or one from a
    hand-written fake ``Adapter`` that predates this story, simply has no
    player snapshot data; this returns ``{}`` rather than raising. When
    present it must be a JSON object mapping player id -> player record (the
    same raw Sleeper ``/players/nfl`` shape ``build_league_model`` already
    reads ``college`` from); a malformed shape raises a chained
    :class:`~commishdesk.errors.IngestError`, never a partial map."""
    if not isinstance(bundle, Mapping):
        raise IngestError("bundle is not a JSON object")

    raw_players = bundle.get("players")
    if raw_players is None:
        return {}
    if not isinstance(raw_players, Mapping):
        raise IngestError("bundle 'players' section is not a JSON object")

    try:
        snapshot: dict[str, PlayerSnapshot] = {}
        for player_id, record in raw_players.items():
            if not isinstance(record, Mapping):
                raise IngestError(f"'players' contains a non-object item ({type(record).__name__})")
            pid = str(player_id)
            position = record.get("position")
            nfl_team = record.get("team")
            snapshot[pid] = PlayerSnapshot(
                player_id=pid,
                position=str(position) if position is not None else None,
                nfl_team=str(nfl_team) if nfl_team is not None else None,
            )
        return snapshot
    except _CAUGHT as exc:
        raise IngestError(f"could not build a player snapshot from the bundle ({type(exc).__name__})") from exc


def get_player_snapshot(
    store: Store, league_id: str, week: int, bundle: Mapping[str, Any]
) -> dict[str, PlayerSnapshot]:
    """Persisted-or-build: the one function in this module with I/O.

    Reads ``store.read_player_snapshot(league_id, week)`` first. If a
    snapshot was already persisted for this league-week -- meaning it was
    generated once before -- that persisted snapshot is returned **verbatim**,
    never re-derived from *bundle* even though *bundle* may reflect a fresher
    live lookup (a trade since the first generation). This is what FR-5 and
    this story's AC3 require: a past week's facts must not silently change
    when it is regenerated.

    Only on the *first* generation (``read_player_snapshot`` returns
    ``None``) is a fresh snapshot built via :func:`build_player_snapshot` and
    persisted via ``store.write_player_snapshot`` before being returned."""
    existing = store.read_player_snapshot(league_id, week)
    if existing is not None:
        return existing
    snapshot = build_player_snapshot(bundle)
    store.write_player_snapshot(league_id, week, snapshot)
    return snapshot


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
