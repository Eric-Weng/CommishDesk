"""Rank the drafted set on a draft-recap board: League Value, else Sleeper rank (Story 2.4b, 9.9).

:func:`build_consensus_rank` turns a stage-1 :class:`~commishdesk.ingest.LeagueModel` plus a
:class:`~commishdesk.store.Store` into a frozen :class:`ConsensusRank`: the drafted players,
ranked ``1..N``, with the source that was used recorded on the result. The name ``consensus`` is
kept for the keys and fields (``consensus_slot``, ``delta``); reader-facing copy says "the League
Value board" or "Sleeper rank (est.)".

Sources, in order of preference (AR-71: no market-value source):

1. **League Value** (``commishdesk/league_value.py``) at draft time, week 0: last season's points
   blended per the league's own scoring and lineup, Future value in a dynasty league. Ranked
   descending, ties by Sleeper ``search_rank`` then id. ``source = "league_value"``.
2. **Sleeper** ``GET https://api.sleeper.app/v1/players/nfl``, ordered by ``search_rank`` ascending
   (``null`` ignored), ``source = "sleeper_search_rank"``. Used when there is no snapshot, and for
   a **rookie draft**: when no drafted player has a non-estimated League Value (every pick is a
   rookie or unvalued), the positional-rank fill would reorder across positions, so the set is
   ranked by Sleeper ``search_rank`` directly and the recap says so.

A drafted player League Value does not value (K, DEF, no snapshot row) is simply not a key in
``slots``: :func:`~commishdesk.stats.compute_consensus_metrics` turns that into ``no_consensus``,
never a zero. When no source can rank a single drafted player,
:class:`~commishdesk.errors.ConsensusError` is raised and the caller decides whether to degrade.

The Sleeper players file is cached to the ``Store`` on a successful fetch, and the League Value
snapshot (with its nflverse inputs) under the ``league_value`` namespace, so a warm cache issues
**zero** network requests and two calls return an equal ``model_dump()``. The cache is
best-effort: a ``StoreError`` on read is a miss, on write is swallowed.

This module may import ``httpx`` at top level (as ``adapters/sleeper.py`` does); it is **not**
part of the ``stats/`` pipeline fence.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeGuard

import httpx
from pydantic import BaseModel, ConfigDict

from commishdesk import __version__
from commishdesk.errors import ConsensusError, StoreError
from commishdesk.ingest import LeagueModel
from commishdesk.store import Store

__all__ = ["ConsensusRank", "build_consensus_rank"]

_NAMESPACE = "consensus"
_SLEEPER_KEY = "sleeper-players-nfl"

#: League Value at draft time is the snapshot for week 0 (no games this season yet).
DRAFT_WEEK = 0

_SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
_CONTACT_URL = "https://github.com/Eric-Weng/CommishDesk"
_TIMEOUT = 15.0
_HEADERS = {"User-Agent": f"CommishDesk/{__version__} (+{_CONTACT_URL})"}

# A fetch that could not JSON-decode or returned the wrong top-level type raises
# ``ValueError``; transport / status failures raise ``httpx.HTTPError``; an
# already-closed injected client raises ``RuntimeError``.
_FETCH_FAULTS = (httpx.HTTPError, RuntimeError, ValueError)

_NO_SOURCE = (
    "no board source is usable (no League Value snapshot, and the Sleeper players "
    "file failed to fetch or parse or ranks none of the drafted players)"
)


class ConsensusRank(BaseModel):
    """A drafted board ranked ``1..N`` against one source.

    ``slots`` maps ``sleeper_id`` -> dense rank (``1`` = the source's top player
    among the drafted set). A drafted player absent from the source is simply not
    a key -- :func:`~commishdesk.stats.compute_consensus_metrics` turns that into
    a ``no_consensus`` pick. ``nflverse`` is true when the rank came from a League
    Value snapshot built on nflverse stats (the footer then carries the credit).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["league_value", "sleeper_search_rank"]
    as_of: str | None
    slots: dict[str, int]
    nflverse: bool = False


def build_consensus_rank(
    league: LeagueModel,
    store: Store,
    *,
    league_settings: Mapping[str, Any] | None = None,
    client: httpx.Client | None = None,
    players: Mapping[str, Any] | None = None,
    players_label: str | None = None,
    league_value_source: str | None = None,
) -> ConsensusRank:
    """Return a :class:`ConsensusRank` over ``league``'s drafted players.

    ``league_settings`` is the bundle's raw Sleeper ``league`` object (``scoring_settings``,
    ``settings``): League Value scores in the league's own scoring. Without it there is no
    snapshot and the rank is Sleeper ``search_rank``. ``league_value_source`` overrides the
    snapshot source (``nflverse`` | ``sleeper_only`` | ``off``).

    Raises :class:`~commishdesk.errors.ConsensusError` only when no source can rank a single
    drafted player.

    ``players`` (Story 8B.1) is a supplied ``players/nfl.json`` mapping. When given it replaces
    the Sleeper cache and fetch: no ``/players/nfl`` request is made, and nothing is written to
    the Sleeper cache. A ``ConsensusError`` then names ``players_label`` (the file's path).
    """
    from commishdesk.league_value import league_value_for_week

    drafted = {pick.player.sleeper_id for pick in league.picks if pick.player.sleeper_id}

    owns_client = client is None
    client = client or httpx.Client()
    try:
        sleeper_players: Mapping[str, Any] | None = players
        if sleeper_players is None:
            sleeper_players = _read_cache(store, _SLEEPER_KEY)
            if sleeper_players is None:
                try:
                    fetched: dict[str, Any] | None = _fetch_sleeper_players(client)
                except _FETCH_FAULTS:
                    fetched = None
                if fetched is not None:
                    _write_cache(store, _SLEEPER_KEY, fetched)
                    sleeper_players = fetched

        snapshot = None
        if league_settings is not None:
            snapshot = league_value_for_week(
                store,
                league.league_id,
                DRAFT_WEEK,
                model=league,
                league=league_settings,
                matchups={},
                players=sleeper_players or {},
                source=league_value_source,
                client=client,
            )

        rank = None
        if snapshot is not None:
            rank = _rank_from_league_value(snapshot, sleeper_players or {}, drafted)
        if rank is None and sleeper_players is not None:
            rank = _rank_from_sleeper(sleeper_players, drafted)
        if rank is None:
            if players is not None:
                raise ConsensusError(
                    f"players file {players_label or '(supplied)'!r} ranks none of the drafted "
                    "players, and League Value did not"
                )
            raise ConsensusError(_NO_SOURCE)
        return rank
    finally:
        if owns_client:
            client.close()


# --------------------------------------------------------------------------- #
# Best-effort cache
# --------------------------------------------------------------------------- #


def _read_cache(store: Store, key: str) -> dict[str, Any] | None:
    """A ``StoreError`` (corrupt / unreadable entry) reads as a cache miss."""
    try:
        return store.read_cache(_NAMESPACE, key)
    except StoreError:
        return None


def _write_cache(store: Store, key: str, value: Mapping[str, Any]) -> None:
    """A ``StoreError`` on write must not discard an otherwise-good rank."""
    try:
        store.write_cache(_NAMESPACE, key, value)
    except StoreError:
        pass


# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #


def _fetch_sleeper_players(client: httpx.Client) -> dict[str, Any]:
    """Fetch the Sleeper players file (a JSON object keyed by player id)."""
    response = client.get(_SLEEPER_PLAYERS_URL, headers=_HEADERS, timeout=_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Sleeper /players/nfl did not return a JSON object")
    return data


# --------------------------------------------------------------------------- #
# Snapshot / payload -> ConsensusRank | None  (filter to drafted, then dense re-rank)
#
# ``None`` == "this source ranked none of the drafted players" -> the caller
# falls through to the next source.
# --------------------------------------------------------------------------- #


def _dense_rank(ordered_ids: list[str]) -> dict[str, int]:
    return {sleeper_id: slot for slot, sleeper_id in enumerate(ordered_ids, start=1)}


def _is_number(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _search_rank(players: Mapping[str, Any], sleeper_id: str) -> float | None:
    record = players.get(sleeper_id)
    if isinstance(record, Mapping):
        rank = record.get("search_rank")
        if _is_number(rank):
            return float(rank)
    return None


def _rank_from_league_value(
    snapshot: Mapping[str, Any], players: Mapping[str, Any], drafted: set[str]
) -> ConsensusRank | None:
    """Dense-rank the drafted set by League Value, or ``None`` to fall back to Sleeper rank.

    A drafted player the snapshot does not value is left out (``no_consensus``). Returns ``None``
    when no drafted player has a non-estimated value (a rookie draft): the caller then ranks by
    Sleeper ``search_rank`` directly. Dynasty ranks by Future value where a player has one, else
    by this-season value; ties break on Sleeper ``search_rank`` then id."""
    values = snapshot.get("players")
    if not isinstance(values, Mapping):
        return None
    dynasty = snapshot.get("dynasty") is True
    key_by_id: dict[str, float] = {}
    any_valued = False
    for sleeper_id in drafted:
        info = values.get(sleeper_id)
        if not isinstance(info, Mapping) or not _is_number(info.get("value")):
            continue
        future = info.get("future")
        key_by_id[sleeper_id] = float(future if dynasty and _is_number(future) else info["value"])
        if info.get("est") is False:
            any_valued = True
    if not any_valued:
        return None

    def order(sleeper_id: str) -> tuple[float, float, str]:
        search_rank = _search_rank(players, sleeper_id)
        return (-key_by_id[sleeper_id], search_rank if search_rank is not None else float("inf"), sleeper_id)

    ordered = sorted(key_by_id, key=order)
    return ConsensusRank(
        source="league_value",
        as_of=None,
        slots=_dense_rank(ordered),
        nflverse=snapshot.get("source") == "nflverse",
    )


def _rank_from_sleeper(payload: Mapping[str, Any], drafted: set[str]) -> ConsensusRank | None:
    rank_by_id: dict[str, float] = {}
    for raw_id, record in payload.items():
        if str(raw_id) not in drafted or not isinstance(record, Mapping):
            continue
        search_rank = record.get("search_rank")
        if not _is_number(search_rank):
            continue
        rank_by_id[str(raw_id)] = float(search_rank)
    if not rank_by_id:
        return None
    ordered = sorted(rank_by_id, key=lambda sid: (rank_by_id[sid], sid))
    return ConsensusRank(source="sleeper_search_rank", as_of=None, slots=_dense_rank(ordered))
