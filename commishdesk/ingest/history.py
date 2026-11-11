"""Load a league's multi-season history, caching each finished season (Story 9B.2).

:func:`load_history` walks the ids the league bundle already carries
(``previous_league_ids``, newest first), takes each finished season from the
``Store`` blob cache (namespace :data:`HISTORY_NAMESPACE`, key = that season's own
league id) or fetches and builds it through the caller's *fetch_season* callable,
then returns one :class:`~commishdesk.ingest.model.History`. A cached entry carries
:data:`~commishdesk.ingest.model.HISTORY_MODEL_VERSION`; another version is refetched.
Only a season Sleeper reports ``status == "complete"`` is cached, and the current
season is never part of history, so it can never be cached as finished. A second run
over fully cached seasons therefore makes zero Sleeper requests.

History is optional input: this function never raises. Any failure (adapter, ingest,
store or validation) ends the walk, marks the history partial with the seasons covered
and a one-line reason, and leaves nothing guessed. It imports no adapter: the caller
passes the fetch callable (``SleeperAdapter.fetch_history_season``), keeping AD-1.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from commishdesk.errors import CommishDeskError

from .build import build_history, build_season_history
from .model import HISTORY_MODEL_VERSION, History, HistorySkip, SeasonHistory

if TYPE_CHECKING:
    from commishdesk.store import Store

__all__ = ["HISTORY_NAMESPACE", "load_history"]

#: The ``Store`` blob-cache namespace holding finished seasons (recorded in CLAUDE.md, AD-5).
HISTORY_NAMESPACE = "history"

_log = logging.getLogger("commishdesk.ingest.history")


def _read_cached(store: Store | None, league_id: str) -> SeasonHistory | None:
    if store is None:
        return None
    try:
        entry = store.read_cache(HISTORY_NAMESPACE, league_id)
        if entry is None or entry.get("history_model_version") != HISTORY_MODEL_VERSION:
            return None
        season = SeasonHistory.model_validate(entry.get("season"))
    except (CommishDeskError, ValidationError, AttributeError):
        return None  # unreadable / malformed / stale shape: refetch
    return season if season.league_id == league_id else None


def _write_cached(store: Store | None, season: SeasonHistory) -> None:
    if store is None or season.status != "complete":
        return  # an unfinished season is never cached as finished
    try:
        store.write_cache(
            HISTORY_NAMESPACE,
            season.league_id,
            {
                "history_model_version": HISTORY_MODEL_VERSION,
                "league_id": season.league_id,
                "season": season.model_dump(mode="json"),
            },
        )
    except CommishDeskError:
        _log.warning("history cache write failed for league %s", season.league_id)


def _failure_reason(league_id: str, exc: Exception) -> str:
    return f"season league {league_id} could not be loaded ({type(exc).__name__})"


def load_history(
    league_bundle: Mapping[str, Any],
    fetch_season: Callable[[str], Mapping[str, Any]],
    store: Store | None = None,
    *,
    max_seasons: int | None = None,
    enabled: bool = True,
) -> History:
    """The league's prior seasons, oldest first. *max_seasons* keeps only the newest N
    (the rest are named in ``skipped`` and the history is marked partial);
    ``enabled=False`` is the off switch (an empty, disabled history, no request).
    Never raises."""
    if not enabled:
        return History(enabled=False)
    try:
        chain = [str(i) for i in (league_bundle.get("previous_league_ids") or []) if i is not None]
    except (AttributeError, TypeError):
        return History(partial=True, reason="league bundle has no readable history chain")
    wanted = chain if max_seasons is None else chain[: max(max_seasons, 0)]
    skipped = [HistorySkip(league_id=i, reason=f"capped at {max_seasons} seasons") for i in chain[len(wanted) :]]

    seasons: list[SeasonHistory] = []
    reason: str | None = None
    for position, league_id in enumerate(wanted):
        season = _read_cached(store, league_id)
        if season is None:
            try:
                season = build_season_history(fetch_season(league_id))
            except Exception as exc:  # noqa: BLE001 -- history is optional input; nothing here may fail the run
                reason = _failure_reason(league_id, exc)
                _log.warning("history stopped: %s", reason)
                unloaded = [HistorySkip(league_id=i, reason="not loaded after a failure") for i in wanted[position:]]
                skipped = unloaded + skipped
                break
            _write_cached(store, season)
        seasons.append(season)

    if reason is None and skipped:
        reason = f"capped at {max_seasons} seasons"
    if reason is None and seasons:
        oldest = seasons[-1].previous_league_id
        if oldest is not None and oldest not in chain:
            reason = f"the chain continues past league {oldest} but it could not be followed"
    return build_history(
        seasons, current_bundle=league_bundle, partial=reason is not None, reason=reason, skipped=skipped
    )
