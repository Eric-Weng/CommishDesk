"""``SleeperAdapter`` — the ``adapters/`` zone's one reference implementation.

Pulls one league's raw Sleeper draft-board data behind the ``Adapter`` protocol:
the league, the draft, every draft pick, the rosters, the users, and (capped at
10 seasons) the ``previous_league_id`` history chain. Five sequential requests
per league plus at most 10 history hops — never concurrent, trivially under
Sleeper's rate ceiling, no throttling logic needed.

Everything comes back in the platform's own shape, unmodified, except that
``league_id`` / ``draft_id`` / ``roster_id`` / ``user_id`` are normalized to
``str`` wherever they appear as a field (Sleeper returns ``roster_id`` as an
int), and ``draft.slot_to_roster_id``'s *values* (also roster ids) get the same
treatment. This is id-type normalization, not sanitization — there is no
string-content cleanup here. That happens once, later, at the ``ingest/``
boundary (AD-24, Story 2.3). A player's position and NFL team come only from
the pick's own ``metadata`` — the snapshot at the moment it was made — never
from a live ``/players/nfl`` lookup, so regenerating a recap months later never
changes the facts.

Any failure this adapter can hit — a non-2xx response, a transport error, a
malformed/non-JSON body, an already-closed client, a league response with no
usable ``draft_id``, or a malformed history-hop response — is caught narrowly
and re-raised as an ``AdapterError`` chained from the original exception
(CLAUDE.md AD-9: a fault skips one league, never the batch; no bare
``except``). No partial bundle is ever returned.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from commishdesk import __version__
from commishdesk.errors import AdapterError

__all__ = ["SleeperAdapter"]

_DEFAULT_CONTACT_URL = "https://github.com/Eric-Weng/CommishDesk"
_DEFAULT_TIMEOUT = 10.0
_MAX_HISTORY_HOPS = 10
_API_BASE = "https://api.sleeper.app/v1"

# Keys normalized to `str` wherever they appear as a field in the returned
# bundle, however deeply nested — Sleeper returns most ids as strings already,
# but `roster_id` comes back as an int in `draft_picks` and `rosters`.
_ID_KEYS = frozenset({"league_id", "draft_id", "roster_id", "user_id"})

# Fields whose *value* is itself a mapping of roster ids (not the field itself
# an id) — `draft.slot_to_roster_id` maps draft slot -> roster_id (int).
_ID_VALUE_MAP_KEYS = frozenset({"slot_to_roster_id"})


def _stringify_ids(value: Any) -> Any:
    """Recursively normalize any of ``_ID_KEYS`` found as a mapping key to
    ``str`` (leaving ``None`` alone); for ``_ID_VALUE_MAP_KEYS``, normalize the
    *values* of that sub-mapping instead. Everything else — every other key,
    every list, every other value — passes through untouched."""
    if isinstance(value, Mapping):
        result: dict[Any, Any] = {}
        for key, val in value.items():
            if key in _ID_KEYS:
                result[key] = str(val) if val is not None else val
            elif key in _ID_VALUE_MAP_KEYS and isinstance(val, Mapping):
                result[key] = {
                    slot: (str(roster_id) if roster_id is not None else roster_id)
                    for slot, roster_id in val.items()
                }
            else:
                result[key] = _stringify_ids(val)
        return result
    if isinstance(value, list):
        return [_stringify_ids(item) for item in value]
    return value


class SleeperAdapter:
    """Reference ``Adapter`` implementation for the Sleeper platform.

    Parameters
    ----------
    contact_url:
        Included in every request's ``User-Agent`` header per Sleeper's
        politeness convention. Defaults to the project's repository.
    client:
        An optional pre-built ``httpx.Client`` — tests inject one backed by
        ``httpx.MockTransport``. When omitted, the adapter builds its own (and
        ``close()`` will close it; an injected client's lifecycle stays the
        caller's responsibility).
    timeout:
        Explicit per-request timeout (seconds), applied to every request this
        adapter issues.
    """

    def __init__(
        self,
        contact_url: str = _DEFAULT_CONTACT_URL,
        client: httpx.Client | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._timeout = timeout
        self._headers = {"User-Agent": f"CommishDesk/{__version__} (+{contact_url})"}
        self._client = client if client is not None else httpx.Client()
        self._owns_client = client is None

    def close(self) -> None:
        """Close the underlying ``httpx.Client``, but only if this adapter
        built it. An injected client is the caller's to close."""
        if self._owns_client:
            self._client.close()

    def fetch(self, league_id: str) -> Mapping[str, Any]:
        """Sequentially pull one league's raw Sleeper data: league, draft,
        draft picks, rosters, users, plus the capped ``previous_league_id``
        history chain. Returns the platform's own shape in one ``Mapping``,
        with id fields normalized to ``str``. Raises ``AdapterError`` (never a
        partial bundle) on any request, parsing, or shape failure."""
        league_id = str(league_id)
        league = self._get(f"/league/{league_id}")

        try:
            draft_id_raw = league["draft_id"]
        except (KeyError, TypeError) as exc:
            raise AdapterError(
                f"Sleeper league {league_id!r} response has no 'draft_id'"
            ) from exc
        if draft_id_raw is None:
            raise AdapterError(f"Sleeper league {league_id!r} response has a null 'draft_id'")
        draft_id = str(draft_id_raw)

        draft = self._get(f"/draft/{draft_id}")
        draft_picks = self._get(f"/draft/{draft_id}/picks")
        rosters = self._get(f"/league/{league_id}/rosters")
        users = self._get(f"/league/{league_id}/users")
        previous_league_ids = self._walk_history(league.get("previous_league_id"))

        bundle: dict[str, Any] = {
            "league": league,
            "draft": draft,
            "draft_picks": draft_picks,
            "rosters": rosters,
            "users": users,
            "previous_league_ids": previous_league_ids,
        }
        return _stringify_ids(bundle)

    def _walk_history(self, previous_league_id: Any) -> list[str]:
        """Follow ``previous_league_id`` one hop at a time via a real
        ``GET /league/{id}`` per hop, capped at ``_MAX_HISTORY_HOPS``. Returns
        the visited ids in traversal order (``[]`` if there is no history).
        Raises ``AdapterError`` if a hop response isn't an object."""
        visited: list[str] = []
        current = previous_league_id
        while current and len(visited) < _MAX_HISTORY_HOPS:
            current_id = str(current)
            hop_league = self._get(f"/league/{current_id}")
            if not isinstance(hop_league, Mapping):
                raise AdapterError(
                    f"Sleeper history hop {current_id!r} returned a non-object response"
                )
            visited.append(current_id)
            current = hop_league.get("previous_league_id")
        return visited

    def _get(self, path: str) -> Any:
        """Issue one ``GET`` with the identifying ``User-Agent`` and explicit
        timeout, raise for a non-2xx status, and return the parsed JSON body.
        Every failure mode becomes an ``AdapterError`` chained from the
        original exception — never a bare ``except``:

        * ``httpx.HTTPError`` covers both a non-2xx status and a transport
          failure (connect/read/timeout) — it is the common base of both.
        * ``RuntimeError`` covers calling ``.get()`` on an already-closed
          ``httpx.Client`` (a caller-lifecycle misuse, not just a platform
          fault) — still narrow, since it can only originate from this one
          ``self._client.get(...)`` call.
        * ``ValueError`` (its subclass ``json.JSONDecodeError``) covers a 200
          response whose body isn't valid JSON.
        """
        url = f"{_API_BASE}{path}"
        try:
            response = self._client.get(url, headers=self._headers, timeout=self._timeout)
            response.raise_for_status()
        except (httpx.HTTPError, RuntimeError) as exc:
            raise AdapterError(f"Sleeper request failed: GET {path}") from exc
        try:
            return response.json()
        except ValueError as exc:
            raise AdapterError(f"Sleeper response was not valid JSON: GET {path}") from exc
