"""Fetch, cache, and dense-rank an external pre-draft consensus board (Story 2.4b).

:func:`build_consensus_rank` turns a stage-1
:class:`~commishdesk.ingest.LeagueModel` plus a :class:`~commishdesk.store.Store`
into a frozen :class:`ConsensusRank`: the drafted players, ranked ``1..N`` against
an external source, with the source that was used recorded on the result.

Sources, in order of preference:

1. **FantasyCalc** ``GET https://api.fantasycalc.com/values/current`` -- players
   keyed by Sleeper id, ordered by trade ``value`` descending. The four query
   params are a deterministic function of ``league.format``:

   ===========  ======================================================================
   ``isDynasty``  ``true`` -- a draft recap is a rookie/dynasty artifact by default
                  (spec Design Notes).
   ``numQbs``     ``2`` if ``format.is_superflex_or_2qb`` else ``1``.
   ``numTeams``   ``format.team_count``.
   ``ppr``        ``0.5`` if ``"0.5"`` / ``"half"`` in ``scoring_label`` (case-
                  insensitive); ``0`` if ``"standard"`` / ``"non-ppr"``; else ``1``.
   ===========  ======================================================================

   Leagues of the same shape share one cached fetch -- the cache key is a
   filesystem-safe string of those four values.

2. **Sleeper** ``GET https://api.sleeper.app/v1/players/nfl`` -- last resort when
   FantasyCalc is unreachable, unparseable, or ranks none of the drafted set.
   Ordered by ``search_rank`` ascending, ``null`` ignored. Read **only** for
   ``search_rank``; position / NFL team stay the snapshot from the pick record
   (Story 2.2).

Resolution order per call: a warm FantasyCalc cache short-circuits everything; on
a miss the LIVE FantasyCalc fetch is attempted before any Sleeper path, so a
stale Sleeper fallback never pre-empts a working primary source. Only when the
live FantasyCalc attempt fails (or yields zero ranked drafted players) does a
warm Sleeper cache -- then a live players-file fetch -- come into play.

The raw upstream payload is cached to the ``Store`` on a successful fetch, so a
warm cache issues **zero** network requests and two :func:`build_consensus_rank`
calls return an equal ``model_dump()``. At most two requests are ever made
(FantasyCalc, then the players file). The cache is best-effort: a ``StoreError``
on read is a miss, a ``StoreError`` on write is swallowed and the computed rank
still returned. Both sources failing to fetch, parse, or rank anything raises
:class:`~commishdesk.errors.ConsensusError` -- the caller decides whether to
degrade.

This module may import ``httpx`` at top level (as ``adapters/sleeper.py`` does);
it is **not** part of the ``stats/`` pipeline fence.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeGuard

import httpx
from pydantic import BaseModel, ConfigDict

from commishdesk import __version__
from commishdesk.errors import ConsensusError, StoreError
from commishdesk.ingest import LeagueFormat, LeagueModel
from commishdesk.store import Store

__all__ = ["ConsensusRank", "build_consensus_rank"]

_NAMESPACE = "consensus"
_SLEEPER_KEY = "sleeper-players-nfl"

_FANTASYCALC_URL = "https://api.fantasycalc.com/values/current"
_SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
_CONTACT_URL = "https://github.com/Eric-Weng/CommishDesk"
_TIMEOUT = 15.0
_HEADERS = {"User-Agent": f"CommishDesk/{__version__} (+{_CONTACT_URL})"}

# A fetch that could not JSON-decode or returned the wrong top-level type raises
# ``ValueError``; transport / status failures raise ``httpx.HTTPError``; an
# already-closed injected client raises ``RuntimeError``.
_FETCH_FAULTS = (httpx.HTTPError, RuntimeError, ValueError)

_BOTH_FAILED = (
    "no consensus source is usable (FantasyCalc and the Sleeper players file "
    "both failed to fetch or parse)"
)


class ConsensusRank(BaseModel):
    """A drafted board ranked ``1..N`` against one external source.

    ``slots`` maps ``sleeper_id`` -> dense rank (``1`` = the source's top player
    among the drafted set). A drafted player absent from the source is simply not
    a key -- :func:`~commishdesk.stats.compute_consensus_metrics` turns that into
    a ``no_consensus`` pick.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["fantasycalc", "sleeper_search_rank"]
    as_of: str | None
    slots: dict[str, int]


def build_consensus_rank(
    league: LeagueModel, store: Store, *, client: httpx.Client | None = None
) -> ConsensusRank:
    """Return a :class:`ConsensusRank` over ``league``'s drafted players.

    Warm FantasyCalc cache -> live FantasyCalc -> warm Sleeper cache -> live
    Sleeper players file. Raises :class:`~commishdesk.errors.ConsensusError` only
    when every path fails to fetch, parse, or rank a single drafted player.
    """
    params = _fantasycalc_params(league.format)
    fc_key = _fantasycalc_cache_key(params)
    drafted = {
        pick.player.sleeper_id for pick in league.picks if pick.player.sleeper_id
    }

    cached = _read_cache(store, fc_key)
    if cached is not None:
        rank = _rank_from_fantasycalc(cached, drafted)
        if rank is not None:
            return rank

    owns_client = client is None
    client = client or httpx.Client()
    try:
        try:
            fc_payload: dict[str, Any] | None = _fetch_fantasycalc(client, params)
        except _FETCH_FAULTS:
            fc_payload = None
        if fc_payload is not None:
            rank = _rank_from_fantasycalc(fc_payload, drafted)
            if rank is not None:
                _write_cache(store, fc_key, fc_payload)
                return rank

        cached = _read_cache(store, _SLEEPER_KEY)
        if cached is not None:
            rank = _rank_from_sleeper(cached, drafted)
            if rank is not None:
                return rank

        try:
            sleeper_payload = _fetch_sleeper_players(client)
        except _FETCH_FAULTS as exc:
            raise ConsensusError(_BOTH_FAILED) from exc
        rank = _rank_from_sleeper(sleeper_payload, drafted)
        if rank is None:
            raise ConsensusError(_BOTH_FAILED)
        _write_cache(store, _SLEEPER_KEY, sleeper_payload)
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
# FantasyCalc query params (deterministic function of league.format)
# --------------------------------------------------------------------------- #


def _fantasycalc_params(fmt: LeagueFormat) -> dict[str, str]:
    label = (fmt.scoring_label or "").lower()
    # Spec Design Notes: a draft recap is a rookie/dynasty artifact by default.
    is_dynasty = True
    num_qbs = 2 if fmt.is_superflex_or_2qb else 1
    if "0.5" in label or "half" in label:
        ppr = "0.5"
    elif "standard" in label or "non-ppr" in label:
        ppr = "0"
    else:
        ppr = "1"
    return {
        "isDynasty": "true" if is_dynasty else "false",
        "numQbs": str(num_qbs),
        "numTeams": str(fmt.team_count),
        "ppr": ppr,
    }


def _fantasycalc_cache_key(params: Mapping[str, str]) -> str:
    dyn = "1" if params["isDynasty"] == "true" else "0"
    ppr = params["ppr"].replace(".", "_")
    return f"fc-dyn{dyn}-qb{params['numQbs']}-tm{params['numTeams']}-ppr{ppr}"


# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #


def _fetch_fantasycalc(
    client: httpx.Client, params: Mapping[str, str]
) -> dict[str, Any]:
    """Fetch the FantasyCalc board. The raw JSON array is wrapped as
    ``{"values": [...]}`` so it fits the ``Store`` blob cache's object contract;
    the array's contents are preserved (per-object key order is normalized on
    disk and is not significant)."""
    response = client.get(
        _FANTASYCALC_URL, params=dict(params), headers=_HEADERS, timeout=_TIMEOUT
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError("FantasyCalc /values/current did not return a JSON array")
    return {"values": data}


def _fetch_sleeper_players(client: httpx.Client) -> dict[str, Any]:
    """Fetch the Sleeper players file (a JSON object keyed by player id)."""
    response = client.get(
        _SLEEPER_PLAYERS_URL, headers=_HEADERS, timeout=_TIMEOUT
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Sleeper /players/nfl did not return a JSON object")
    return data


# --------------------------------------------------------------------------- #
# Payload -> ConsensusRank | None  (filter to drafted, then dense re-rank)
#
# ``None`` == "this source ranked none of the drafted players" -> the caller
# treats it exactly like an unreachable source and falls through.
# --------------------------------------------------------------------------- #


def _dense_rank(ordered_ids: list[str]) -> dict[str, int]:
    return {sleeper_id: slot for slot, sleeper_id in enumerate(ordered_ids, start=1)}


def _is_number(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _rank_from_fantasycalc(
    payload: Mapping[str, Any], drafted: set[str]
) -> ConsensusRank | None:
    values = payload.get("values") if isinstance(payload, Mapping) else None
    value_by_id: dict[str, float] = {}
    for entry in values or []:
        if not isinstance(entry, Mapping):
            continue
        player = entry.get("player")
        if not isinstance(player, Mapping):
            continue
        raw_id = player.get("sleeperId")
        if raw_id is None or str(raw_id) not in drafted:
            continue
        trade_value = entry.get("value")
        if not _is_number(trade_value):
            continue
        value_by_id.setdefault(str(raw_id), float(trade_value))
    if not value_by_id:
        return None
    ordered = sorted(value_by_id, key=lambda sid: (-value_by_id[sid], sid))
    return ConsensusRank(source="fantasycalc", as_of=None, slots=_dense_rank(ordered))


def _rank_from_sleeper(
    payload: Mapping[str, Any], drafted: set[str]
) -> ConsensusRank | None:
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
    return ConsensusRank(
        source="sleeper_search_rank", as_of=None, slots=_dense_rank(ordered)
    )
