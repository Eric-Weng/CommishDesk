"""Story 2.2: the Sleeper adapter, the zone's one reference implementation.

One test per I/O & Edge-Case Matrix row, plus the protocol-isolation test and the
``User-Agent`` / id-normalization / offline-guarantee assertions. Every test here runs
against ``httpx.MockTransport`` fed from JSON sliced from ``tests/fixtures/rookie-draft.json``
(``tests/eval/adapters/*.json``) -- zero real network calls, ever.
"""

from __future__ import annotations

import copy
import json
import socket
from collections.abc import Callable, Mapping
from typing import Any

import httpx
import pytest

from commishdesk import __version__
from commishdesk.adapters import Adapter
from commishdesk.adapters.sleeper import SleeperAdapter
from commishdesk.errors import AdapterError, IngestError
from commishdesk.ingest import PlayerSnapshot, build_league_model, build_player_snapshot, build_week_model
from tests.conftest import REPO_ROOT

EVAL_DIR = REPO_ROOT / "tests" / "eval" / "adapters"


def _load(name: str) -> Any:
    return json.loads((EVAL_DIR / name).read_text(encoding="utf-8"))


LEAGUE: dict[str, Any] = _load("league.json")
DRAFT: dict[str, Any] = _load("draft.json")
DRAFT_PICKS: list[dict[str, Any]] = _load("draft_picks.json")
ROSTERS: list[dict[str, Any]] = _load("rosters.json")
USERS: list[dict[str, Any]] = _load("users.json")
HISTORY: dict[str, dict[str, dict[str, Any]]] = _load("history.json")

# Story 5.3a: sliced from the same private league as the above (league_id
# id_rq867j4w7p, playoff_week_start 15) -- see tests/fixtures/week17-playoffs.json,
# whose league/rosters sections are byte-identical to LEAGUE/ROSTERS.
MATCHUPS_BY_WEEK: dict[str, list[dict[str, Any]]] = _load("matchups_by_week.json")
TRANSACTIONS_BY_WEEK: dict[str, list[dict[str, Any]]] = _load("transactions_by_week.json")
WINNERS_BRACKET: list[dict[str, Any]] = _load("winners_bracket.json")
LOSERS_BRACKET: list[dict[str, Any]] = _load("losers_bracket.json")
PLAYOFF_WEEK_START = LEAGUE["settings"]["playoff_week_start"]

# Story 5.3b: a small, hand-curated slice of the Sleeper `/players/nfl` shape --
# four ids that really are referenced by ROSTERS / MATCHUPS_BY_WEEK (one on IR,
# one on taxi, two week-1 starters) plus one id ("88888") that is referenced
# nowhere, to prove `_fetch_players` filters it back out.
PLAYERS: dict[str, dict[str, Any]] = _load("players.json")
UNREFERENCED_PLAYER_ID = "88888"

_BUNDLE_KEYS = {"league", "draft", "draft_picks", "rosters", "users", "previous_league_ids"}
_WEEK_BUNDLE_KEYS = {
    "league",
    "rosters",
    "matchups",
    "next_matchups",
    "transactions",
    "winners_bracket",
    "losers_bracket",
    "players",
}


def _league_with_previous(previous_league_id: str | None) -> dict[str, Any]:
    """A deep copy of the sliced league fixture with ``previous_league_id``
    overridden -- so each test controls its own history-chain starting point
    without mutating the shared module-level fixture."""
    league = copy.deepcopy(LEAGUE)
    league["previous_league_id"] = previous_league_id
    return league


def _build_transport(
    *,
    league: dict[str, Any],
    draft: dict[str, Any],
    draft_picks: list[dict[str, Any]],
    rosters: list[dict[str, Any]],
    users: list[dict[str, Any]],
    history: dict[str, dict[str, Any]] | None = None,
    matchups_by_week: dict[str, list[dict[str, Any]]] | None = None,
    transactions_by_week: dict[str, list[dict[str, Any]]] | None = None,
    winners_bracket: list[dict[str, Any]] | None = None,
    losers_bracket: list[dict[str, Any]] | None = None,
    players: dict[str, dict[str, Any]] | None = None,
    requests: list[httpx.Request] | None = None,
    override: dict[str, Callable[[httpx.Request], httpx.Response]] | None = None,
) -> httpx.MockTransport:
    """A router over the five base Sleeper endpoints, the five Story 5.3a/5.3b
    weekly endpoints (matchups/transactions/winners_bracket/losers_bracket/
    players), plus any ``history`` hop leagues -- backed entirely by in-memory
    fixtures, no real network. Every request is appended to *requests* (if
    given) before routing, so a test can assert on call count, path, headers,
    or timeout. *override* lets a test replace one path's normal 200 response
    with a failure."""
    history = history or {}
    matchups_by_week = matchups_by_week or {}
    transactions_by_week = transactions_by_week or {}
    players = players or {}
    override = override or {}
    league_id = league["league_id"]
    draft_id = draft["draft_id"]

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        path = request.url.path
        if path in override:
            return override[path](request)
        if path == f"/v1/league/{league_id}":
            return httpx.Response(200, json=league)
        if path == f"/v1/draft/{draft_id}":
            return httpx.Response(200, json=draft)
        if path == f"/v1/draft/{draft_id}/picks":
            return httpx.Response(200, json=draft_picks)
        if path == f"/v1/league/{league_id}/rosters":
            return httpx.Response(200, json=rosters)
        if path == f"/v1/league/{league_id}/users":
            return httpx.Response(200, json=users)
        if path == f"/v1/league/{league_id}/winners_bracket":
            return httpx.Response(200, json=winners_bracket if winners_bracket is not None else [])
        if path == f"/v1/league/{league_id}/losers_bracket":
            return httpx.Response(200, json=losers_bracket if losers_bracket is not None else [])
        if path == "/v1/players/nfl":
            return httpx.Response(200, json=players)
        matchup_prefix = f"/v1/league/{league_id}/matchups/"
        if path.startswith(matchup_prefix):
            week = path[len(matchup_prefix) :]
            if week in matchups_by_week:
                return httpx.Response(200, json=matchups_by_week[week])
        txn_prefix = f"/v1/league/{league_id}/transactions/"
        if path.startswith(txn_prefix):
            week = path[len(txn_prefix) :]
            if week in transactions_by_week:
                return httpx.Response(200, json=transactions_by_week[week])
        for hop_id, hop_doc in history.items():
            if path == f"/v1/league/{hop_id}":
                return httpx.Response(200, json=hop_doc)
        return httpx.Response(404, json={"error": f"unmapped path: {path}"})

    return httpx.MockTransport(handler)


def _adapter_for(transport: httpx.MockTransport, **kwargs: Any) -> SleeperAdapter:
    return SleeperAdapter(client=httpx.Client(transport=transport), **kwargs)


# --------------------------------------------------------------------------- #
# Row: Happy path
# --------------------------------------------------------------------------- #


def test_happy_path_completed_draft_two_hop_chain() -> None:
    league = _league_with_previous("id_histhop0001")
    requests: list[httpx.Request] = []
    transport = _build_transport(
        league=league,
        draft=DRAFT,
        draft_picks=DRAFT_PICKS,
        rosters=ROSTERS,
        users=USERS,
        history=HISTORY["two_hop"],
        requests=requests,
    )
    adapter = _adapter_for(transport)

    bundle = adapter.fetch(league["league_id"])

    assert set(bundle) == _BUNDLE_KEYS
    assert bundle["previous_league_ids"] == ["id_histhop0001", "id_histhop0002"]

    # ids normalized to str throughout, even though the source fixture carries
    # roster_id as an int.
    assert isinstance(bundle["league"]["league_id"], str)
    assert isinstance(bundle["draft"]["draft_id"], str)
    assert bundle["draft_picks"], "fixture has no picks"
    for pick in bundle["draft_picks"]:
        assert isinstance(pick["roster_id"], str)
    assert bundle["rosters"], "fixture has no rosters"
    for roster in bundle["rosters"]:
        assert isinstance(roster["roster_id"], str)
    assert bundle["users"], "fixture has no users"
    for user in bundle["users"]:
        assert isinstance(user["user_id"], str)
    slot_to_roster_id = bundle["draft"]["slot_to_roster_id"]
    assert slot_to_roster_id, "fixture has no slot_to_roster_id"
    for roster_id in slot_to_roster_id.values():
        assert isinstance(roster_id, str)

    # acceptance: every pick carries pick number, round, slot, player
    # (id, name, position, NFL team), and picked_by -- raw shape.
    pick = bundle["draft_picks"][0]
    assert pick["pick_no"] == 1
    assert pick["round"] == 1
    assert "draft_slot" in pick
    assert pick["player_id"]
    assert pick["metadata"]["first_name"] and pick["metadata"]["last_name"]
    assert pick["metadata"]["position"]
    assert pick["metadata"]["team"]
    assert pick["picked_by"]

    # exactly 5 base calls + 2 history hops = 7
    assert len(requests) == 7


# --------------------------------------------------------------------------- #
# Row: Long chain
# --------------------------------------------------------------------------- #


def test_history_depth_cap_is_pinned_to_ten_for_fr40() -> None:
    """FR-40 (Story 3.6): the draft-history depth cap is a hard 10, defined once
    in ``_MAX_HISTORY_HOPS``. A crafted ``previous_league_id`` chain cannot
    balloon one league's fetch past this. Changing the value is a deliberate
    FR-40 renegotiation, not a tweak — this guard makes that explicit."""
    from commishdesk.adapters.sleeper import _MAX_HISTORY_HOPS

    assert _MAX_HISTORY_HOPS == 10


def test_long_chain_of_fifteen_hops_stops_at_ten() -> None:
    from commishdesk.adapters.sleeper import _MAX_HISTORY_HOPS  # FR-40 depth cap

    long_chain = HISTORY["long_chain"]
    first_hop = next(iter(sorted(long_chain)))
    league = _league_with_previous(first_hop)
    requests: list[httpx.Request] = []
    transport = _build_transport(
        league=league,
        draft=DRAFT,
        draft_picks=DRAFT_PICKS,
        rosters=ROSTERS,
        users=USERS,
        history=long_chain,
        requests=requests,
    )
    adapter = _adapter_for(transport)

    bundle = adapter.fetch(league["league_id"])

    assert len(long_chain) == 15  # sanity: the fixture really is a 15-hop chain
    assert len(bundle["previous_league_ids"]) == _MAX_HISTORY_HOPS == 10
    assert all(isinstance(pid, str) for pid in bundle["previous_league_ids"])

    history_calls = [
        r for r in requests if r.url.path.startswith("/v1/league/id_histchain")
    ]
    assert len(history_calls) == _MAX_HISTORY_HOPS  # exactly 10 extra GET /league/* calls


# --------------------------------------------------------------------------- #
# Row: Cyclic history (retro finding C3)
# --------------------------------------------------------------------------- #


def test_two_node_cycle_stops_on_the_repeat_not_the_hop_cap() -> None:
    """``previous_league_id`` A -> B -> A terminates the moment the repeat is
    seen, with no duplicate id in the result and none of the ``_MAX_HISTORY_HOPS``
    - 2 redundant requests a naive cap-only loop would issue."""
    cycle = {
        "id_cycle_a": {"previous_league_id": "id_cycle_b"},
        "id_cycle_b": {"previous_league_id": "id_cycle_a"},
    }
    league = _league_with_previous("id_cycle_a")
    requests: list[httpx.Request] = []
    transport = _build_transport(
        league=league,
        draft=DRAFT,
        draft_picks=DRAFT_PICKS,
        rosters=ROSTERS,
        users=USERS,
        history=cycle,
        requests=requests,
    )
    adapter = _adapter_for(transport)

    bundle = adapter.fetch(league["league_id"])

    assert bundle["previous_league_ids"] == ["id_cycle_a", "id_cycle_b"]
    history_calls = [
        r for r in requests if r.url.path.startswith("/v1/league/id_cycle")
    ]
    assert len(history_calls) == 2  # not 10 -- stopped on the repeat, not the cap


def test_self_referencing_history_stops_after_one_hop() -> None:
    """A league whose own ``previous_league_id`` points back at itself is a
    one-node cycle: the second sighting is caught before any request for it."""
    cycle = {"id_cycle_self": {"previous_league_id": "id_cycle_self"}}
    league = _league_with_previous("id_cycle_self")
    requests: list[httpx.Request] = []
    transport = _build_transport(
        league=league,
        draft=DRAFT,
        draft_picks=DRAFT_PICKS,
        rosters=ROSTERS,
        users=USERS,
        history=cycle,
        requests=requests,
    )
    adapter = _adapter_for(transport)

    bundle = adapter.fetch(league["league_id"])

    assert bundle["previous_league_ids"] == ["id_cycle_self"]
    history_calls = [
        r for r in requests if r.url.path.startswith("/v1/league/id_cycle_self")
    ]
    assert len(history_calls) == 1


# --------------------------------------------------------------------------- #
# Row: No history
# --------------------------------------------------------------------------- #


def test_no_history_previous_league_id_null_makes_no_extra_call() -> None:
    league = _league_with_previous(None)
    requests: list[httpx.Request] = []
    transport = _build_transport(
        league=league,
        draft=DRAFT,
        draft_picks=DRAFT_PICKS,
        rosters=ROSTERS,
        users=USERS,
        requests=requests,
    )
    adapter = _adapter_for(transport)

    bundle = adapter.fetch(league["league_id"])

    assert bundle["previous_league_ids"] == []
    assert len(requests) == 5  # exactly the 5 base calls, no history hop


# --------------------------------------------------------------------------- #
# Row: Platform 404/5xx
# --------------------------------------------------------------------------- #


def test_platform_error_status_raises_adapter_error_chained_from_http_status_error() -> (
    None
):
    league = _league_with_previous(None)
    rosters_path = f"/v1/league/{league['league_id']}/rosters"
    transport = _build_transport(
        league=league,
        draft=DRAFT,
        draft_picks=DRAFT_PICKS,
        rosters=ROSTERS,
        users=USERS,
        override={rosters_path: lambda req: httpx.Response(503, json={"error": "down"})},
    )
    adapter = _adapter_for(transport)

    with pytest.raises(AdapterError) as exc_info:
        adapter.fetch(league["league_id"])

    assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)


# --------------------------------------------------------------------------- #
# Row: Transport failure
# --------------------------------------------------------------------------- #


def _raise_connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def test_transport_failure_raises_adapter_error_chained_from_transport_error() -> None:
    league = _league_with_previous(None)
    rosters_path = f"/v1/league/{league['league_id']}/rosters"
    transport = _build_transport(
        league=league,
        draft=DRAFT,
        draft_picks=DRAFT_PICKS,
        rosters=ROSTERS,
        users=USERS,
        override={rosters_path: _raise_connect_error},
    )
    adapter = _adapter_for(transport)

    with pytest.raises(AdapterError) as exc_info:
        adapter.fetch(league["league_id"])

    assert isinstance(exc_info.value.__cause__, httpx.ConnectError)


# --------------------------------------------------------------------------- #
# Additional failure-mode coverage: parsing, shape, and lifecycle faults must
# also become `AdapterError`, never a bare KeyError/TypeError/AttributeError/
# ValueError/RuntimeError escaping to the caller (CLAUDE.md AD-9).
# --------------------------------------------------------------------------- #


def test_non_json_response_body_raises_adapter_error_chained_from_value_error() -> None:
    league = _league_with_previous(None)
    rosters_path = f"/v1/league/{league['league_id']}/rosters"
    transport = _build_transport(
        league=league,
        draft=DRAFT,
        draft_picks=DRAFT_PICKS,
        rosters=ROSTERS,
        users=USERS,
        override={rosters_path: lambda req: httpx.Response(200, text="not json")},
    )
    adapter = _adapter_for(transport)

    with pytest.raises(AdapterError) as exc_info:
        adapter.fetch(league["league_id"])

    assert isinstance(exc_info.value.__cause__, ValueError)


def test_missing_draft_id_raises_adapter_error() -> None:
    league = _league_with_previous(None)
    del league["draft_id"]
    transport = _build_transport(
        league=league, draft=DRAFT, draft_picks=DRAFT_PICKS, rosters=ROSTERS, users=USERS
    )
    adapter = _adapter_for(transport)

    with pytest.raises(AdapterError) as exc_info:
        adapter.fetch(league["league_id"])

    assert isinstance(exc_info.value.__cause__, KeyError)


def test_null_draft_id_raises_adapter_error() -> None:
    league = _league_with_previous(None)
    league["draft_id"] = None
    transport = _build_transport(
        league=league, draft=DRAFT, draft_picks=DRAFT_PICKS, rosters=ROSTERS, users=USERS
    )
    adapter = _adapter_for(transport)

    with pytest.raises(AdapterError):
        adapter.fetch(league["league_id"])


def test_non_mapping_history_hop_response_raises_adapter_error() -> None:
    league = _league_with_previous("id_histhop0001")
    hop_path = "/v1/league/id_histhop0001"
    transport = _build_transport(
        league=league,
        draft=DRAFT,
        draft_picks=DRAFT_PICKS,
        rosters=ROSTERS,
        users=USERS,
        history=HISTORY["two_hop"],
        override={hop_path: lambda req: httpx.Response(200, json=["not", "an", "object"])},
    )
    adapter = _adapter_for(transport)

    with pytest.raises(AdapterError):
        adapter.fetch(league["league_id"])


def test_fetch_after_close_on_owned_client_raises_adapter_error() -> None:
    league = _league_with_previous(None)
    transport = _build_transport(
        league=league, draft=DRAFT, draft_picks=DRAFT_PICKS, rosters=ROSTERS, users=USERS
    )
    # An adapter that owns its client (client=None at construction), but with
    # that client swapped for a mock transport so we can close it and still
    # observe what happens on the next `fetch()` -- no real network either way.
    adapter = SleeperAdapter()
    adapter._client = httpx.Client(transport=transport)  # type: ignore[attr-defined]
    adapter.close()

    with pytest.raises(AdapterError) as exc_info:
        adapter.fetch(league["league_id"])

    assert isinstance(exc_info.value.__cause__, RuntimeError)


# --------------------------------------------------------------------------- #
# Lifecycle: close()
# --------------------------------------------------------------------------- #


def test_default_construction_close_closes_its_own_client() -> None:
    adapter = SleeperAdapter()
    assert adapter._client.is_closed is False  # type: ignore[attr-defined]

    adapter.close()

    assert adapter._client.is_closed is True  # type: ignore[attr-defined]


def test_close_does_not_close_an_injected_client() -> None:
    injected = httpx.Client()
    adapter = SleeperAdapter(client=injected)

    adapter.close()

    assert injected.is_closed is False
    injected.close()


# --------------------------------------------------------------------------- #
# Protocol isolation
# --------------------------------------------------------------------------- #


class _FakeAdapter:
    """A hand-written fake satisfying ``Adapter`` structurally. Imports nothing
    from ``commishdesk.adapters.sleeper`` -- only stdlib typing. ``Adapter`` now
    has two members (Story 5.3a's ``fetch_week``), so a structural fake must
    implement both for ``isinstance(fake, Adapter)`` to hold."""

    def __init__(
        self, payload: Mapping[str, Any], week_payload: Mapping[str, Any] | None = None
    ) -> None:
        self._payload = payload
        self._week_payload = payload if week_payload is None else week_payload

    def fetch(self, league_id: str) -> Mapping[str, Any]:
        return self._payload

    def fetch_week(self, league_id: str, week: int) -> Mapping[str, Any]:
        return self._week_payload


def _run_through_adapter(adapter: Adapter, league_id: str) -> Mapping[str, Any]:
    """Typed only as ``Adapter`` -- works identically for ``SleeperAdapter`` and
    any hand-written fake."""
    return adapter.fetch(league_id)


def test_protocol_isolation_sleeper_and_a_hand_written_fake_both_satisfy_adapter() -> (
    None
):
    league = _league_with_previous(None)
    transport = _build_transport(
        league=league, draft=DRAFT, draft_picks=DRAFT_PICKS, rosters=ROSTERS, users=USERS
    )
    real_adapter: Adapter = _adapter_for(transport)
    fake_payload = {
        "league": {"league_id": "fake-league"},
        "draft": {},
        "draft_picks": [],
        "rosters": [],
        "users": [],
        "previous_league_ids": [],
    }
    fake_adapter: Adapter = _FakeAdapter(fake_payload)

    assert isinstance(real_adapter, Adapter)
    assert isinstance(fake_adapter, Adapter)

    real_result = _run_through_adapter(real_adapter, league["league_id"])
    fake_result = _run_through_adapter(fake_adapter, "fake-league")

    assert set(real_result) == set(fake_result) == _BUNDLE_KEYS
    assert fake_result is fake_payload


def test_a_week_runs_through_the_engine_against_a_hand_written_fake_adapter_with_no_network() -> (
    None
):
    """AC: a week runs through the engine against a hand-written fake adapter
    with no network. ``_FakeAdapter.fetch_week`` returns an in-memory bundle
    built entirely by hand -- no fixture file, no ``httpx`` transport --
    proving ``build_week_model`` needs only the ``Adapter`` protocol's shape,
    never ``SleeperAdapter`` or a real Sleeper response."""
    week_bundle: dict[str, Any] = {
        "league": {"league_id": "fake-league", "settings": {"playoff_week_start": 15}},
        "rosters": [
            {
                "roster_id": 1,
                "reserve": ["100"],
                "taxi": [],
                "settings": {"wins": 5, "losses": 3, "ties": 0, "fpts": 600, "fpts_decimal": 5},
            },
            {
                "roster_id": 2,
                "reserve": None,
                "taxi": ["200"],
                "settings": {"wins": 3, "losses": 5, "ties": 0, "fpts": 500, "fpts_decimal": 0},
            },
        ],
        "matchups": {
            "1": [
                {
                    "roster_id": 1,
                    "matchup_id": 1,
                    "points": 105.5,
                    "starters": ["10", "11"],
                    "starters_points": [55.0, 50.5],
                    "players": ["10", "11", "12"],
                    "players_points": {"10": 55.0, "11": 50.5, "12": 0.0},
                },
                {
                    "roster_id": 2,
                    "matchup_id": 1,
                    "points": 98.0,
                    "starters": ["20", "21"],
                    "starters_points": [48.0, 50.0],
                    "players": ["20", "21"],
                    "players_points": {"20": 48.0, "21": 50.0},
                },
            ]
        },
        "transactions": {
            "1": [
                {
                    "transaction_id": "txn1",
                    "type": "waiver",
                    "status": "complete",
                    "roster_ids": [2],
                    "adds": {"30": 2},
                    "drops": {"12": 2},
                    "settings": {"waiver_bid": 5},
                },
                {
                    "transaction_id": "txn2",
                    "type": "waiver",
                    "status": "failed",
                    "roster_ids": [1],
                },
            ]
        },
        "winners_bracket": [],
        "losers_bracket": [],
        # Story 5.3b: the fake adapter supplies "players" through the same
        # protocol member (no third Adapter method) -- keyed by every id the
        # bundle above actually references, plus one unreferenced id.
        "players": {
            "10": {"position": "QB", "team": "KC"},
            "11": {"position": "WR", "team": "KC"},
            "12": {"position": "RB", "team": "KC"},
            "20": {"position": "QB", "team": "BUF"},
            "21": {"position": "WR", "team": "BUF"},
            "999": {"position": "K", "team": "MIA"},  # never rostered/started
        },
    }
    fake_adapter: Adapter = _FakeAdapter({}, week_payload=week_bundle)
    assert isinstance(fake_adapter, Adapter)

    bundle = fake_adapter.fetch_week("fake-league", 1)
    week_model = build_week_model(bundle)

    assert week_model.week == 1
    rosters_by_id = {r.roster_id: r for r in week_model.rosters}
    assert set(rosters_by_id) == {"1", "2"}
    assert rosters_by_id["1"].fpts == 600.05
    assert rosters_by_id["1"].ir == ["100"]
    assert rosters_by_id["2"].taxi == ["200"]
    assert rosters_by_id["2"].ir == []  # a null `reserve` never raises

    matchups_by_roster = {m.roster_id: m for m in week_model.matchups}
    assert set(matchups_by_roster) == {"1", "2"}
    assert matchups_by_roster["1"].opponent_roster_id == "2"
    assert matchups_by_roster["2"].opponent_roster_id == "1"
    assert matchups_by_roster["1"].bench == ["12"]  # the one player not a starter

    assert len(week_model.transactions) == 1  # the "failed" one is dropped
    txn = week_model.transactions[0]
    assert txn.transaction_id == "txn1"
    assert txn.adds == {"30": "2"}
    assert txn.drops == {"12": "2"}
    assert txn.waiver_bid == 5

    # Story 5.3b: the same fake bundle's "players" key builds a PlayerSnapshot
    # map through the ingest seam -- no SleeperAdapter, no network.
    snapshot = build_player_snapshot(bundle)
    assert snapshot["10"] == PlayerSnapshot(player_id="10", position="QB", nfl_team="KC")
    assert snapshot["999"] == PlayerSnapshot(player_id="999", position="K", nfl_team="MIA")


# --------------------------------------------------------------------------- #
# User-Agent
# --------------------------------------------------------------------------- #


def test_default_contact_url_matches_prereq_1() -> None:
    league = _league_with_previous(None)
    requests: list[httpx.Request] = []
    transport = _build_transport(
        league=league,
        draft=DRAFT,
        draft_picks=DRAFT_PICKS,
        rosters=ROSTERS,
        users=USERS,
        requests=requests,
    )
    adapter = _adapter_for(transport)  # default contact_url

    adapter.fetch(league["league_id"])

    expected = f"CommishDesk/{__version__} (+https://github.com/Eric-Weng/CommishDesk)"
    assert requests, "no requests captured"
    assert all(r.headers.get("user-agent") == expected for r in requests)


def test_custom_contact_url_is_sent_on_every_request() -> None:
    league = _league_with_previous("id_histhop0001")
    requests: list[httpx.Request] = []
    transport = _build_transport(
        league=league,
        draft=DRAFT,
        draft_picks=DRAFT_PICKS,
        rosters=ROSTERS,
        users=USERS,
        history=HISTORY["two_hop"],
        requests=requests,
    )
    adapter = _adapter_for(transport, contact_url="https://example.com/contact")

    adapter.fetch(league["league_id"])

    expected = f"CommishDesk/{__version__} (+https://example.com/contact)"
    assert requests
    assert all(r.headers.get("user-agent") == expected for r in requests)


def test_explicit_timeout_is_applied_to_every_request() -> None:
    league = _league_with_previous(None)
    requests: list[httpx.Request] = []
    transport = _build_transport(
        league=league,
        draft=DRAFT,
        draft_picks=DRAFT_PICKS,
        rosters=ROSTERS,
        users=USERS,
        requests=requests,
    )
    adapter = _adapter_for(transport, timeout=3.5)

    adapter.fetch(league["league_id"])

    assert requests
    for r in requests:
        timeout_ext = r.extensions.get("timeout", {})
        assert timeout_ext.get("connect") == 3.5
        assert timeout_ext.get("read") == 3.5


# --------------------------------------------------------------------------- #
# Offline guarantee
# --------------------------------------------------------------------------- #


def test_fetch_makes_zero_real_network_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - must never run
        raise AssertionError("real network access attempted")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)

    league = _league_with_previous("id_histhop0001")
    transport = _build_transport(
        league=league,
        draft=DRAFT,
        draft_picks=DRAFT_PICKS,
        rosters=ROSTERS,
        users=USERS,
        history=HISTORY["two_hop"],
    )
    adapter = _adapter_for(transport)

    bundle = adapter.fetch(league["league_id"])

    assert bundle["previous_league_ids"] == ["id_histhop0001", "id_histhop0002"]


# --------------------------------------------------------------------------- #
# Adapter -> ingest seam (retro finding C4)
# --------------------------------------------------------------------------- #


def test_a_null_picks_response_becomes_a_typed_ingest_error_naming_the_section() -> (
    None
):
    """``SleeperAdapter.fetch`` validates ``league`` and each history hop's
    shape but passes ``draft_picks`` (and the other three sub-bundles) through
    unvalidated. Sleeper genuinely returns HTTP 200 with a JSON ``null`` body
    for a deleted draft's picks endpoint. Each side was previously tested only
    in isolation -- the 2.2 adapter tests all abort before the picks fetch,
    and the 2.3 ingest tests use hand-built bundles, never real adapter
    output. This drives a real ``fetch()`` result straight into
    ``build_league_model`` and confirms the seam is sound: ``ingest/build.py``'s
    ``_section`` catches it as a typed ``IngestError`` naming the section, not
    an uncaught ``TypeError`` deeper in league-model construction."""
    league = _league_with_previous(None)
    picks_path = f"/v1/draft/{DRAFT['draft_id']}/picks"
    transport = _build_transport(
        league=league,
        draft=DRAFT,
        draft_picks=DRAFT_PICKS,
        rosters=ROSTERS,
        users=USERS,
        override={picks_path: lambda req: httpx.Response(200, content=b"null")},
    )
    adapter = _adapter_for(transport)

    bundle = adapter.fetch(league["league_id"])
    assert bundle["draft_picks"] is None  # the seam: fetch() passed it through

    with pytest.raises(IngestError) as exc_info:
        build_league_model(bundle)
    assert "draft_picks" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# Story 5.3a: fetch_week
# --------------------------------------------------------------------------- #


def _week_transport(
    *,
    week: int | None = None,
    league: dict[str, Any] | None = None,
    requests: list[httpx.Request] | None = None,
    override: dict[str, Callable[[httpx.Request], httpx.Response]] | None = None,
) -> httpx.MockTransport:
    """A transport pre-loaded with every base + weekly Sleeper fixture, keyed
    off the shared ``MATCHUPS_BY_WEEK`` / ``TRANSACTIONS_BY_WEEK`` /
    ``*_BRACKET`` eval slices (sliced from ``tests/fixtures/week17-playoffs.json``,
    the same private league as ``LEAGUE`` / ``ROSTERS``)."""
    return _build_transport(
        league=league if league is not None else LEAGUE,
        draft=DRAFT,
        draft_picks=DRAFT_PICKS,
        rosters=ROSTERS,
        users=USERS,
        matchups_by_week=MATCHUPS_BY_WEEK,
        transactions_by_week=TRANSACTIONS_BY_WEEK,
        winners_bracket=WINNERS_BRACKET,
        losers_bracket=LOSERS_BRACKET,
        players=PLAYERS,
        requests=requests,
        override=override,
    )


# Row: Regular-season week
def test_fetch_week_regular_season_matchups_cumulative_transactions_single_week_no_brackets() -> (
    None
):
    week = 10
    assert week < PLAYOFF_WEEK_START  # sanity: this fixture's regular season
    requests: list[httpx.Request] = []
    transport = _week_transport(requests=requests)
    adapter = _adapter_for(transport)

    bundle = adapter.fetch_week(LEAGUE["league_id"], week)

    assert set(bundle) == _WEEK_BUNDLE_KEYS
    assert set(bundle["matchups"]) == {str(w) for w in range(1, week + 1)}
    assert set(bundle["transactions"]) == {str(w) for w in range(1, week + 1)}
    assert bundle["next_matchups"]
    assert all(set(row) == {"roster_id", "matchup_id"} for row in bundle["next_matchups"])
    assert bundle["winners_bracket"] == []
    assert bundle["losers_bracket"] == []
    assert bundle["rosters"], "fixture has no rosters"
    for roster in bundle["rosters"]:
        assert isinstance(roster["roster_id"], str)
    for row in bundle["matchups"][str(week)]:
        assert isinstance(row["roster_id"], str)
    # Story 5.3b: "players" is filtered to referenced ids only.
    assert bundle["players"]
    assert UNREFERENCED_PLAYER_ID not in bundle["players"]
    assert set(bundle["players"]) <= set(PLAYERS)

    # exactly: league + rosters + `week` matchup calls + `week` transactions
    # calls + 1 next-week pairings call + 1 players call
    assert len(requests) == 2 + week + week + 1 + 1


# Row: Playoff week
def test_fetch_week_playoff_week_populates_both_brackets() -> None:
    week = 17
    assert week >= PLAYOFF_WEEK_START  # sanity: this fixture's championship round
    requests: list[httpx.Request] = []
    transport = _week_transport(requests=requests)
    adapter = _adapter_for(transport)

    bundle = adapter.fetch_week(LEAGUE["league_id"], week)

    assert bundle["winners_bracket"] == WINNERS_BRACKET
    assert bundle["losers_bracket"] == LOSERS_BRACKET
    assert bundle["winners_bracket"] and bundle["losers_bracket"]

    assert bundle["next_matchups"] == []

    # exactly: league + rosters + `week` matchup calls + `week` transactions
    # calls + winners_bracket + losers_bracket + 1 players call (no next-week
    # pairings call: week + 1 is not regular season)
    assert len(requests) == 2 + week + week + 2 + 1


# Row: Story 5.7 -- the next-week pairings cutoff
def test_fetch_week_final_regular_week_skips_the_next_week_request() -> None:
    week = PLAYOFF_WEEK_START - 1  # week + 1 == playoff_week_start: not regular season
    requests: list[httpx.Request] = []
    adapter = _adapter_for(_week_transport(requests=requests))

    bundle = adapter.fetch_week(LEAGUE["league_id"], week)

    assert bundle["next_matchups"] == []
    league_id = LEAGUE["league_id"]
    assert not any(
        request.url.path == f"/v1/league/{league_id}/matchups/{week + 1}" for request in requests
    )


def test_fetch_week_last_pre_cutoff_week_requests_the_next_week() -> None:
    week = PLAYOFF_WEEK_START - 2  # week + 1 == playoff_week_start - 1: still regular season
    requests: list[httpx.Request] = []
    adapter = _adapter_for(_week_transport(requests=requests))

    bundle = adapter.fetch_week(LEAGUE["league_id"], week)

    assert bundle["next_matchups"]
    assert all(set(row) == {"roster_id", "matchup_id"} for row in bundle["next_matchups"])
    league_id = LEAGUE["league_id"]
    assert (
        sum(request.url.path == f"/v1/league/{league_id}/matchups/{week + 1}" for request in requests)
        == 1
    )


def test_fetch_week_league_without_a_playoff_week_start_has_no_next_matchups() -> None:
    league = {**LEAGUE, "settings": {k: v for k, v in LEAGUE["settings"].items() if k != "playoff_week_start"}}
    requests: list[httpx.Request] = []
    adapter = _adapter_for(_week_transport(league=league, requests=requests))

    bundle = adapter.fetch_week(LEAGUE["league_id"], 10)

    assert bundle["next_matchups"] == []
    league_id = LEAGUE["league_id"]
    assert not any(request.url.path == f"/v1/league/{league_id}/matchups/11" for request in requests)


# Row: Story 5.7 -- a malformed next-week response degrades, it never leaks or raises
@pytest.mark.parametrize(
    "body, expected",
    [
        pytest.param(None, [], id="null_body"),
        pytest.param({"not": "a list"}, [], id="object_body"),
        pytest.param(
            [
                {"roster_id": 1, "matchup_id": 4, "points": 88.8, "starters": ["p1"]},
                "not-a-row",
                None,
                {"roster_id": 2, "matchup_id": 4, "players_points": {"p1": 1.0}},
            ],
            [{"roster_id": "1", "matchup_id": 4}, {"roster_id": "2", "matchup_id": 4}],
            id="non_object_rows_skipped_and_fields_projected",
        ),
    ],
)
def test_fetch_week_next_week_response_is_projected_and_never_leaks_a_score(
    body: Any, expected: list[dict[str, Any]]
) -> None:
    week = 10
    league_id = LEAGUE["league_id"]
    override = {
        # ``json=None`` would send an empty body; a literal ``null`` is what a
        # platform "no data" answer looks like.
        f"/v1/league/{league_id}/matchups/{week + 1}": lambda request: httpx.Response(
            200, content=json.dumps(body).encode(), headers={"content-type": "application/json"}
        )
    }
    adapter = _adapter_for(_week_transport(override=override))

    bundle = adapter.fetch_week(league_id, week)

    assert bundle["next_matchups"] == expected


# Row: Story 5.3b -- the "players" bundle key
def test_fetch_week_players_key_is_filtered_to_ids_this_weeks_bundle_references() -> None:
    """`_fetch_players` keeps only ids seen in this week's rosters
    (`players`/`reserve`/`taxi`) or any fetched week's matchup rows
    (`starters`/`players`) -- an id present in the raw `/players/nfl` response
    but never referenced (`players.json`'s "88888") is dropped."""
    adapter = _adapter_for(_week_transport())

    bundle = adapter.fetch_week(LEAGUE["league_id"], 1)

    # "11576" (roster 1's reserve/IR) and "6904"/"9226" (week-1 starters) are
    # all referenced; "88888" is in the raw players fixture but never
    # rostered/started anywhere, so it must not survive the filter.
    assert "11576" in bundle["players"]
    assert "6904" in bundle["players"]
    assert "9226" in bundle["players"]
    assert UNREFERENCED_PLAYER_ID not in bundle["players"]
    assert bundle["players"]["6904"]["position"] == "QB"
    assert bundle["players"]["6904"]["team"] == "SF"


def test_fetch_week_players_id_from_an_earlier_fetched_week_survives_the_final_weeks_filter() -> None:
    """`_rostered_player_ids` unions ids across **every** fetched week
    (`matchups.values()`, weeks 1..week), not just the final requested week.
    "10235" (`players.json`) is on roster 7's week-1 bench (`players`, not
    `starters`) only -- it is absent from week 10's matchup rows and from
    every current roster's `players`/`reserve`/`taxi` -- so it would be
    dropped by a "final-week-only" (or "current rosters only") regression but
    must still survive a `fetch_week(..., 10)` call, which fetches weeks
    1..10 inclusive."""
    week = 10
    week1_ids = {
        str(pid)
        for row in MATCHUPS_BY_WEEK["1"]
        for pid in (row.get("starters") or []) + (row.get("players") or [])
    }
    week10_ids = {
        str(pid)
        for row in MATCHUPS_BY_WEEK[str(week)]
        for pid in (row.get("starters") or []) + (row.get("players") or [])
    }
    current_roster_ids = {
        str(pid)
        for roster in ROSTERS
        for key in ("players", "reserve", "taxi")
        for pid in (roster.get(key) or [])
    }
    assert "10235" in week1_ids
    assert "10235" not in week10_ids
    assert "10235" not in current_roster_ids  # sanity: only an earlier week's row carries it

    adapter = _adapter_for(_week_transport())

    bundle = adapter.fetch_week(LEAGUE["league_id"], week)

    assert "10235" in bundle["players"]


def test_fetch_week_players_response_not_an_object_raises_adapter_error() -> None:
    players_path = "/v1/players/nfl"
    transport = _week_transport(
        override={players_path: lambda req: httpx.Response(200, json=["not", "an", "object"])}
    )
    adapter = _adapter_for(transport)

    with pytest.raises(AdapterError):
        adapter.fetch_week(LEAGUE["league_id"], 1)


@pytest.mark.parametrize(
    "malformed_league",
    [
        pytest.param({"league_id": "id_missing_settings"}, id="missing_settings"),
        pytest.param({"league_id": "id_missing_pws", "settings": {}}, id="missing_playoff_week_start"),
        pytest.param(
            {"league_id": "id_bool_pws", "settings": {"playoff_week_start": True}},
            id="bool_playoff_week_start",
        ),
    ],
)
def test_fetch_week_malformed_playoff_week_start_shape_degrades_to_non_playoff(
    malformed_league: dict[str, Any],
) -> None:
    """``_in_playoff_period`` must fail soft, not raise, on a league response
    with no ``settings`` at all, a ``settings`` missing
    ``playoff_week_start``, or a ``bool`` where an ``int`` week number is
    expected (``bool`` is an ``int`` subclass in Python, so this needs its own
    guard). Each degrades to non-playoff: empty brackets, no exception."""
    transport = _week_transport(league=malformed_league)
    adapter = _adapter_for(transport)

    bundle = adapter.fetch_week(malformed_league["league_id"], 1)

    assert bundle["winners_bracket"] == []
    assert bundle["losers_bracket"] == []
    assert bundle["next_matchups"] == []


# Row: Eliminated roster (fixture already carries the drop -- confirms the
# adapter passes it through unmodified, no raise).
def test_fetch_week_eliminated_rosters_are_simply_absent_from_that_weeks_matchups() -> (
    None
):
    adapter = _adapter_for(_week_transport())

    bundle = adapter.fetch_week(LEAGUE["league_id"], 17)

    present = {row["roster_id"] for row in bundle["matchups"]["17"]}
    all_rosters = {str(r["roster_id"]) for r in ROSTERS}
    assert all_rosters - present == {"3", "6", "7", "9"}


# Row: Failed/non-final transaction
def test_fetch_week_drops_non_complete_transactions() -> None:
    week = 1
    txn_path = f"/v1/league/{LEAGUE['league_id']}/transactions/{week}"
    live_payload = [
        {**TRANSACTIONS_BY_WEEK["1"][0], "status": "complete"},
        {**TRANSACTIONS_BY_WEEK["1"][0], "transaction_id": "id_pending999", "status": "pending"},
    ]
    transport = _week_transport(
        override={txn_path: lambda req: httpx.Response(200, json=live_payload)}
    )
    adapter = _adapter_for(transport)

    bundle = adapter.fetch_week(LEAGUE["league_id"], week)

    statuses = {txn["status"] for txn in bundle["transactions"][str(week)]}
    assert statuses == {"complete"}
    assert len(bundle["transactions"][str(week)]) == 1


# Row: Regular-season week -- request/parse failure
def test_fetch_week_platform_error_status_raises_adapter_error() -> None:
    week = 3
    matchup_path = f"/v1/league/{LEAGUE['league_id']}/matchups/{week}"
    transport = _week_transport(
        override={matchup_path: lambda req: httpx.Response(503, json={"error": "down"})}
    )
    adapter = _adapter_for(transport)

    with pytest.raises(AdapterError) as exc_info:
        adapter.fetch_week(LEAGUE["league_id"], week)
    assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)


def test_fetch_week_non_int_week_raises_adapter_error_chained() -> None:
    adapter = _adapter_for(_week_transport())

    with pytest.raises(AdapterError) as exc_info:
        adapter.fetch_week(LEAGUE["league_id"], "not-a-week")  # type: ignore[arg-type]
    assert isinstance(exc_info.value.__cause__, ValueError)


def test_fetch_week_transport_failure_raises_adapter_error() -> None:
    txn_path = f"/v1/league/{LEAGUE['league_id']}/transactions/1"
    transport = _week_transport(override={txn_path: _raise_connect_error})
    adapter = _adapter_for(transport)

    with pytest.raises(AdapterError) as exc_info:
        adapter.fetch_week(LEAGUE["league_id"], 1)
    assert isinstance(exc_info.value.__cause__, httpx.ConnectError)


def test_fetch_week_carries_the_identifying_user_agent_on_every_request() -> None:
    requests: list[httpx.Request] = []
    transport = _week_transport(requests=requests)
    adapter = _adapter_for(transport)

    adapter.fetch_week(LEAGUE["league_id"], 17)

    expected = f"CommishDesk/{__version__} (+https://github.com/Eric-Weng/CommishDesk)"
    assert requests, "no requests captured"
    assert all(r.headers.get("user-agent") == expected for r in requests)


def test_fetch_week_makes_zero_real_network_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - must never run
        raise AssertionError("real network access attempted")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)

    adapter = _adapter_for(_week_transport())

    bundle = adapter.fetch_week(LEAGUE["league_id"], 17)

    assert bundle["winners_bracket"]


def test_fetch_week_result_builds_a_week_model_through_the_ingest_seam() -> None:
    """Drives a real ``fetch_week()`` result straight into ``build_week_model``
    -- the same seam-soundness check ``fetch()`` gets against
    ``build_league_model`` above."""
    adapter = _adapter_for(_week_transport())

    bundle = adapter.fetch_week(LEAGUE["league_id"], 17)
    week_model = build_week_model(bundle)

    assert week_model.week == 17
    assert len(week_model.rosters) == len(ROSTERS)
    assert any(m.week == 1 for m in week_model.matchups)
    assert any(m.week == 17 for m in week_model.matchups)
    assert all(txn.status == "complete" for txn in week_model.transactions)
