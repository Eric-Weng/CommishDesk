"""``SleeperAdapter`` — the ``adapters/`` zone's one reference implementation.

Pulls one league's raw Sleeper draft-board data behind the ``Adapter`` protocol:
the league, the draft, every draft pick, the rosters, the users, and (capped at
10 seasons) the ``previous_league_id`` history chain. Five sequential requests
per league plus at most 10 history hops — never concurrent, trivially under
Sleeper's rate ceiling, no throttling logic needed.

``fetch_week`` (Story 5.3a) pulls one league-week instead: the league (read
only for ``settings.playoff_week_start``), the current rosters, every week
``1..week``'s matchups, every week ``1..week``'s transactions (filtered to
``status == "complete"``), and — only once ``week`` reaches the playoff
period — the winners/losers brackets (``[]``/``[]`` otherwise, mirroring
``tools/assemble_bundle.py``'s Story 5.2 convention). Also sequential, same
``_get``/``AdapterError``/id-normalization conventions as ``fetch``.

Story 5.3b adds one more call inside ``fetch_week``: a second
``GET /players/nfl``, filtered down to every player id this week's bundle
actually references (every roster's ``players``/``reserve``/``taxi``, plus
every matchup row's ``starters``/``players``), and returned under the
bundle's new ``"players"`` key. ``ingest/build.py::build_player_snapshot``
turns that into the shape-agnostic ``PlayerSnapshot`` map. This extends the
``Adapter`` protocol's *bundle*, not its member surface — ``fetch_week``'s
signature and return type hint are unchanged, so
``test_extension_zones.py``'s pinned ``get_type_hints`` assertion still
holds; a third ``Adapter`` protocol member was deliberately not added
(Story 5.3b's own frozen boundary).

Story 5.7 adds two more, both inside the same unchanged ``fetch_week``
signature. First, transactions are fetched for **every** week ``1..week``
(previously only the target week's), so a weekly Issue can read the league's
trade history. Second, a ``"next_matchups"`` key is filled from a
``GET /matchups/{week + 1}`` projected to ``roster_id`` / ``matchup_id`` only
— never a point, a lineup or a player list, so a fixture cannot leak a future
outcome — and only while ``week + 1`` is still regular season
(``settings.playoff_week_start`` an int and ``week + 1 < playoff_week_start``);
past that cutoff it is ``[]``, with no request made.

Story 5.11a adds one more, again inside the unchanged ``fetch_week``
signature: a ``GET /state/nfl`` projected to ``{"week", "season_type"}`` under
a new ``"nfl_state"`` key. That is Sleeper's own "which week is it" signal, and
it is what lets the weekly CLI refuse a ``--week`` whose games are not final
yet — a bundle *key*, not a third ``Adapter`` protocol member.

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
# FR-40 / Story 3.6: the single source of truth for the draft-history depth cap.
# At most this many prior seasons are followed (the origin league is not counted
# as a hop), so a crafted `previous_league_id` chain cannot balloon one league's
# fetch. `_walk_history` enforces it (plus a repeated-id stop, retro finding C3);
# pinned by a guard test in `tests/test_sleeper_adapter.py`.
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


def _completed_only(value: Any) -> Any:
    """Drop every transaction whose ``status`` isn't ``"complete"``. Anything
    other than a list (e.g. a null response body) passes through unmodified —
    shape validation is ``ingest/build.py``'s job, not the adapter's (mirrors
    ``fetch()`` passing ``draft_picks`` etc. through unvalidated)."""
    if isinstance(value, list):
        return [txn for txn in value if isinstance(txn, Mapping) and txn.get("status") == "complete"]
    return value


def _project_pairings(value: Any) -> list[dict[str, Any]]:
    """Story 5.7: week ``n+1``'s matchup rows projected to ``{roster_id,
    matchup_id}`` only. Every other field Sleeper returns on a matchup row — a
    point total, a lineup, a per-player score — is dropped, so a fixture can
    never leak a future outcome. Anything other than a list (a null body) and
    any non-object row is simply skipped."""
    if not isinstance(value, list):
        return []
    return [
        {"roster_id": row.get("roster_id"), "matchup_id": row.get("matchup_id")}
        for row in value
        if isinstance(row, Mapping)
    ]


def _nfl_state(state: Any) -> dict[str, Any]:
    """Story 5.11a: project Sleeper's ``GET /state/nfl`` response to
    ``{"week": int | None, "season_type": str | None}`` — the two fields the
    weekly CLI's week-finality check reads.

    A non-object response, or a non-int ``week`` / non-str ``season_type``
    (``bool`` is an ``int`` subclass and is rejected) projects to ``None`` on
    that field: an unreadable state means "finality unknown", never an
    ``AdapterError`` of its own (mirrors :func:`_playoff_week_start`)."""
    if not isinstance(state, Mapping):
        return {"week": None, "season_type": None}
    week = state.get("week")
    week = week if isinstance(week, int) and not isinstance(week, bool) else None
    season_type = state.get("season_type")
    season_type = season_type if isinstance(season_type, str) else None
    return {"week": week, "season_type": season_type}


def _rostered_player_ids(rosters: Any, matchups: Mapping[str, Any]) -> set[str]:
    """Every player id referenced by this week-bundle's rosters (``players`` --
    the roster's full current player list, which is how a mid-week waiver add
    not yet reflected in any matchup snapshot and not on IR/taxi still gets
    counted -- plus ``reserve`` / ``taxi``) or any week's matchup rows
    (``starters`` / ``players`` -- the latter is the full roster for that
    matchup, starters included, so it already covers the bench). The universe
    :meth:`SleeperAdapter._fetch_players` filters Sleeper's full player table
    down to. Tolerant of a missing or malformed shape at any level -- never
    raises; shape validation belongs to ``ingest/build.py``, not here (mirrors
    this module's existing convention of passing sub-bundles through largely
    unvalidated)."""
    ids: set[str] = set()
    for roster in rosters if isinstance(rosters, list) else []:
        if not isinstance(roster, Mapping):
            continue
        for key in ("players", "reserve", "taxi"):
            value = roster.get(key)
            for pid in value if isinstance(value, list) else []:
                if pid is not None:
                    ids.add(str(pid))
    for week_rows in matchups.values():
        for row in week_rows if isinstance(week_rows, list) else []:
            if not isinstance(row, Mapping):
                continue
            for key in ("starters", "players"):
                value = row.get(key)
                for pid in value if isinstance(value, list) else []:
                    if pid is not None:
                        ids.add(str(pid))
    return ids


def _playoff_week_start(league: Any) -> int | None:
    """``league["settings"]["playoff_week_start"]`` as an ``int``, or ``None``
    when the league response carries no usable value (no ``settings``, no key,
    a non-int, or a ``bool`` -- ``bool`` is an ``int`` subclass in Python, so it
    needs its own guard). Shared by :func:`_in_playoff_period` and Story 5.7's
    schedule projection, which both gate on it -- a missing value simply means
    "no playoff period known", never an ``AdapterError`` of its own."""
    if not isinstance(league, Mapping):
        return None
    settings = league.get("settings")
    if not isinstance(settings, Mapping):
        return None
    start = settings.get("playoff_week_start")
    if isinstance(start, bool) or not isinstance(start, int):
        return None
    return start


def _in_playoff_period(league: Any, week: int) -> bool:
    """``True`` once ``week`` reaches ``league["settings"]["playoff_week_start"]``.
    Any missing or malformed shape defaults to ``False`` — a bracket fetch is a
    nice-to-have gated on an optional field, never worth an ``AdapterError`` of
    its own."""
    start = _playoff_week_start(league)
    return start is not None and week >= start


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
        ``GET /league/{id}`` per hop, capped at ``_MAX_HISTORY_HOPS`` prior
        seasons (the FR-40 depth cap — Story 2.2, pinned to FR-40 in Story 3.6;
        the origin league is not counted as a hop). Returns the visited ids in
        traversal order (``[]`` if there is no history). Raises ``AdapterError``
        if a hop response isn't an object.

        Stops on a repeated id (retro finding C3) as well as on the hop cap: a
        self-referencing or cyclic ``previous_league_id`` chain would otherwise
        re-request the same league up to ``_MAX_HISTORY_HOPS`` times and return
        that many duplicate ids instead of terminating immediately."""
        visited: list[str] = []
        current = previous_league_id
        while current and len(visited) < _MAX_HISTORY_HOPS:
            current_id = str(current)
            if current_id in visited:
                break
            hop_league = self._get(f"/league/{current_id}")
            if not isinstance(hop_league, Mapping):
                raise AdapterError(
                    f"Sleeper history hop {current_id!r} returned a non-object response"
                )
            visited.append(current_id)
            current = hop_league.get("previous_league_id")
        return visited

    def fetch_week(self, league_id: str, week: int) -> Mapping[str, Any]:
        """Sequentially pull one league-week's raw Sleeper data: the league
        (read only for ``settings.playoff_week_start``), the current rosters,
        Sleeper's own NFL state (``GET /state/nfl``, projected to
        ``{"week", "season_type"}`` under ``"nfl_state"`` — Story 5.11a),
        every week ``1..week``'s matchups, every week ``1..week``'s
        transactions (each filtered to ``status == "complete"``), — only once
        ``week`` reaches the playoff period — the winners/losers brackets
        (``[]``/``[]`` otherwise), — Story 5.7 — week ``week + 1``'s pairings
        projected to ``roster_id`` / ``matchup_id`` only (``[]`` at or past the
        playoff cutoff, with no request), and (Story 5.3b) a second
        ``GET /players/nfl`` filtered to this week's rostered players, under
        the bundle's ``"players"`` key. Returns the platform's own shape in one
        ``Mapping``. Top-level ``league_id`` / ``roster_id`` / ``user_id``
        fields are normalized to ``str`` by the same ``_stringify_ids`` pass
        ``fetch()`` uses; it does not reach a *plural* key like
        ``transactions[].roster_ids`` or the ids inside ``adds``/``drops`` --
        ``ingest/build.py`` normalizes those independently. Raises
        ``AdapterError`` (never a partial bundle) on any request, parsing, or
        shape failure."""
        league_id = str(league_id)
        try:
            week = int(week)
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"Sleeper fetch_week: {week!r} is not a valid week") from exc

        league = self._get(f"/league/{league_id}")
        rosters = self._get(f"/league/{league_id}/rosters")
        # Story 5.11a: Sleeper's own "which week is it" signal, for the weekly
        # CLI's week-finality check. Projected down to the two fields the check
        # reads, so the raw response's dozens of other keys never reach a
        # bundle.
        nfl_state = _nfl_state(self._get("/state/nfl"))

        matchups: dict[str, Any] = {
            str(wk): self._get(f"/league/{league_id}/matchups/{wk}") for wk in range(1, week + 1)
        }
        transactions = {
            str(wk): _completed_only(self._get(f"/league/{league_id}/transactions/{wk}"))
            for wk in range(1, week + 1)
        }

        # Story 5.7: week n+1's pairings, while n+1 is still regular season.
        start = _playoff_week_start(league)
        if start is None or week + 1 >= start:
            next_matchups: list[dict[str, Any]] = []
        else:
            next_matchups = _project_pairings(self._get(f"/league/{league_id}/matchups/{week + 1}"))

        if _in_playoff_period(league, week):
            winners_bracket = self._get(f"/league/{league_id}/winners_bracket")
            losers_bracket = self._get(f"/league/{league_id}/losers_bracket")
        else:
            # A fixture/response reaching only the regular season must never
            # carry bracket data — mirrors tools/assemble_bundle.py's Story 5.2
            # convention (the raw bracket endpoints hold the *completed*
            # season's results, which would otherwise leak future outcomes).
            winners_bracket = []
            losers_bracket = []

        players = self._fetch_players(_rostered_player_ids(rosters, matchups))

        bundle: dict[str, Any] = {
            "league": league,
            "rosters": rosters,
            "nfl_state": nfl_state,
            "matchups": matchups,
            "transactions": transactions,
            "next_matchups": next_matchups,
            "winners_bracket": winners_bracket,
            "losers_bracket": losers_bracket,
            "players": players,
        }
        return _stringify_ids(bundle)

    def _fetch_players(self, rostered_ids: set[str]) -> dict[str, Any]:
        """``GET /players/nfl`` — Sleeper's full player table (several
        megabytes) — filtered down to *rostered_ids*, the player ids this
        week's bundle actually references. Story 5.3b's way of getting a
        player's current NFL team/position into the bundle: a second call on
        the same ``Adapter`` protocol member, not a third protocol member, so
        any future non-Sleeper ``Adapter`` is still guaranteed to supply
        player data through ``fetch_week``'s one return value. Raises
        ``AdapterError`` if the response isn't a JSON object."""
        raw = self._get("/players/nfl")
        if not isinstance(raw, Mapping):
            raise AdapterError("Sleeper /players/nfl response is not a JSON object")
        return {str(pid): record for pid, record in raw.items() if str(pid) in rostered_ids}

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
