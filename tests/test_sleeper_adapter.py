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
from pathlib import Path
from typing import Any

import httpx
import pytest

from commishdesk import __version__
from commishdesk.adapters import Adapter
from commishdesk.adapters.sleeper import SleeperAdapter
from commishdesk.errors import AdapterError, IngestError
from commishdesk.ingest import build_league_model

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = REPO_ROOT / "tests" / "eval" / "adapters"


def _load(name: str) -> Any:
    return json.loads((EVAL_DIR / name).read_text(encoding="utf-8"))


LEAGUE: dict[str, Any] = _load("league.json")
DRAFT: dict[str, Any] = _load("draft.json")
DRAFT_PICKS: list[dict[str, Any]] = _load("draft_picks.json")
ROSTERS: list[dict[str, Any]] = _load("rosters.json")
USERS: list[dict[str, Any]] = _load("users.json")
HISTORY: dict[str, dict[str, dict[str, Any]]] = _load("history.json")

_BUNDLE_KEYS = {"league", "draft", "draft_picks", "rosters", "users", "previous_league_ids"}


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
    requests: list[httpx.Request] | None = None,
    override: dict[str, Callable[[httpx.Request], httpx.Response]] | None = None,
) -> httpx.MockTransport:
    """A router over the five base Sleeper endpoints plus any ``history`` hop
    leagues, backed entirely by in-memory fixtures -- no real network. Every
    request is appended to *requests* (if given) before routing, so a test can
    assert on call count, path, headers, or timeout. *override* lets a test
    replace one path's normal 200 response with a failure."""
    history = history or {}
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


def test_long_chain_of_fifteen_hops_stops_at_ten() -> None:
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
    assert len(bundle["previous_league_ids"]) == 10
    assert all(isinstance(pid, str) for pid in bundle["previous_league_ids"])

    history_calls = [
        r for r in requests if r.url.path.startswith("/v1/league/id_histchain")
    ]
    assert len(history_calls) == 10  # exactly 10 extra GET /league/* calls


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
    from ``commishdesk.adapters.sleeper`` -- only stdlib typing."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self._payload = payload

    def fetch(self, league_id: str) -> Mapping[str, Any]:
        return self._payload


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
