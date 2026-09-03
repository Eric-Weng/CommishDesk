"""Story 2.4b: ``build_consensus_rank`` — fetch, cache, and dense re-rank.

One test per HTTP / cache row of the spec's I/O & Edge-Case Matrix, driven by
``httpx.MockTransport`` (zero real network, ever) over the synthetic
``tests/fixtures/consensus/`` payloads.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest

from commishdesk.consensus import ConsensusRank, build_consensus_rank
from commishdesk.errors import CommishDeskError, ConsensusError, StoreError
from commishdesk.ingest import (
    Draft,
    LeagueFormat,
    LeagueModel,
    Pick,
    Player,
    Team,
    build_league_model,
)
from commishdesk.stats import compute_consensus_metrics
from commishdesk.store import FileStore

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
CONSENSUS_DIR = FIXTURE_DIR / "consensus"

FANTASYCALC_BOARD: list[dict[str, Any]] = json.loads(
    (CONSENSUS_DIR / "fantasycalc-values.json").read_text(encoding="utf-8")
)
SLEEPER_PLAYERS: dict[str, dict[str, Any]] = json.loads(
    (CONSENSUS_DIR / "sleeper-players-nfl.json").read_text(encoding="utf-8")
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _bundle(name: str) -> dict[str, Any]:
    raw = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    return {
        "league": raw["league"],
        "draft": raw["draft"],
        "draft_picks": raw["draft_picks"],
        "rosters": raw["rosters"],
        "users": raw["users"],
        "previous_league_ids": [],
    }


def _rookie_league() -> LeagueModel:
    return build_league_model(_bundle("rookie-draft.json"))


def _superflex_league() -> LeagueModel:
    return build_league_model(_bundle("week10-superflex.json"))


def _drafted_ids(league: LeagueModel) -> set[str]:
    return {p.player.sleeper_id for p in league.picks if p.player.sleeper_id}


class _Router:
    """A MockTransport router over the two consensus endpoints. Each endpoint is
    either a canned 200 body or a callable that fails. Every request is recorded."""

    def __init__(
        self,
        *,
        fantasycalc: Any = FANTASYCALC_BOARD,
        sleeper: Any = SLEEPER_PLAYERS,
    ) -> None:
        self.fantasycalc = fantasycalc
        self.sleeper = sleeper
        self.requests: list[httpx.Request] = []

    def _one(self, kind: str, body: Any, request: httpx.Request) -> httpx.Response:
        if callable(body):
            return body(request)
        return httpx.Response(200, json=body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        if "fantasycalc.com" in host:
            return self._one("fantasycalc", self.fantasycalc, request)
        if "sleeper.app" in host:
            return self._one("sleeper", self.sleeper, request)
        return httpx.Response(404, json={"error": f"unmapped {request.url}"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def _connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def _server_error(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, json={"error": "down"})


# --------------------------------------------------------------------------- #
# Row: FantasyCalc OK, cold cache
# --------------------------------------------------------------------------- #


def test_fantasycalc_cold_cache_writes_raw_payload_and_ranks(tmp_path: Path) -> None:
    league = _rookie_league()
    store = FileStore(tmp_path)
    router = _Router()

    rank = build_consensus_rank(league, store, client=router.client())

    assert isinstance(rank, ConsensusRank)
    assert rank.source == "fantasycalc"
    # dense 1..N over the drafted players present in the source
    assert set(rank.slots.values()) == set(range(1, len(rank.slots) + 1))
    assert rank.slots.keys() <= _drafted_ids(league)
    # exactly one FantasyCalc request, no Sleeper request
    assert len(router.requests) == 1
    assert "fantasycalc.com" in router.requests[0].url.host
    # raw payload cached under the "consensus" namespace, verbatim array inside
    cached = store.read_cache("consensus", "fc-dyn1-qb2-tm12-ppr0_5")
    assert cached is not None
    assert cached["values"] == FANTASYCALC_BOARD


# --------------------------------------------------------------------------- #
# Row: Warm cache — zero HTTP requests, identical rank
# --------------------------------------------------------------------------- #


def test_warm_cache_issues_zero_requests_and_is_deterministic(tmp_path: Path) -> None:
    league = _rookie_league()
    store = FileStore(tmp_path)

    first = build_consensus_rank(league, store, client=_Router().client())

    def boom(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(f"warm cache must not hit the network: {request.url}")

    warm_client = httpx.Client(transport=httpx.MockTransport(boom))
    second = build_consensus_rank(league, store, client=warm_client)

    assert first.model_dump() == second.model_dump()
    assert first.source == "fantasycalc"


# --------------------------------------------------------------------------- #
# Row: FantasyCalc down, Sleeper OK
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("failure", [_connect_error, _server_error])
def test_fantasycalc_down_falls_back_to_sleeper_search_rank(
    tmp_path: Path, failure: Any
) -> None:
    league = _rookie_league()
    store = FileStore(tmp_path)
    router = _Router(fantasycalc=failure)

    rank = build_consensus_rank(league, store, client=router.client())

    assert rank.source == "sleeper_search_rank"
    assert set(rank.slots.values()) == set(range(1, len(rank.slots) + 1))
    # the Sleeper players payload is what got cached, not FantasyCalc
    assert store.read_cache("consensus", "sleeper-players-nfl") is not None
    assert store.read_cache("consensus", "fc-dyn1-qb2-tm12-ppr0_5") is None
    hosts = [r.url.host for r in router.requests]
    assert any("fantasycalc.com" in h for h in hosts)
    assert any("sleeper.app" in h for h in hosts)


# --------------------------------------------------------------------------- #
# Row: Both sources down, cold cache
# --------------------------------------------------------------------------- #


def test_both_sources_down_raises_consensus_error(tmp_path: Path) -> None:
    league = _rookie_league()
    store = FileStore(tmp_path)
    router = _Router(fantasycalc=_connect_error, sleeper=_connect_error)

    with pytest.raises(ConsensusError) as excinfo:
        build_consensus_rank(league, store, client=router.client())

    assert isinstance(excinfo.value, CommishDeskError)
    assert isinstance(excinfo.value.__cause__, httpx.HTTPError)
    # nothing partial was cached
    assert store.read_cache("consensus", "sleeper-players-nfl") is None
    assert store.read_cache("consensus", "fc-dyn1-qb2-tm12-ppr0_5") is None


def test_both_sources_down_then_warm_cache_recovers(tmp_path: Path) -> None:
    """A cold-cache total failure must not poison a later successful run."""
    league = _rookie_league()
    store = FileStore(tmp_path)
    with pytest.raises(ConsensusError):
        build_consensus_rank(
            league, store, client=_Router(fantasycalc=_connect_error, sleeper=_connect_error).client()
        )
    rank = build_consensus_rank(league, store, client=_Router().client())
    assert rank.source == "fantasycalc"


# --------------------------------------------------------------------------- #
# Row: FantasyCalc query params are a function of league.format
# --------------------------------------------------------------------------- #


def test_fantasycalc_params_map_from_the_committed_fixtures(tmp_path: Path) -> None:
    # Both committed fixtures are 2-QB (rookie: two QB slots; superflex:
    # SUPER_FLEX) half-PPR dynasty -> identical FantasyCalc params.
    for name in ("rookie-draft.json", "week10-superflex.json"):
        league = build_league_model(_bundle(name))
        router = _Router()
        build_consensus_rank(
            league, FileStore(tmp_path / name), client=router.client()
        )
        params = router.requests[0].url.params
        assert params["numTeams"] == "12"
        assert params["numQbs"] == "2"
        assert params["ppr"] == "0.5"
        assert params["isDynasty"] == "true"


def test_fantasycalc_params_and_key_track_a_one_qb_standard_league(
    tmp_path: Path,
) -> None:
    fmt = LeagueFormat(
        team_count=10,
        roster_slots=["QB", "RB", "WR", "TE"],
        flex_eligibility={},
        scoring_label="Standard",
        is_superflex_or_2qb=False,
        te_premium=False,
    )
    league = LeagueModel(
        league_id="L1",
        name="Std",
        season=2025,
        format=fmt,
        teams=[Team(roster_id="1", manager="a")],
        picks=[
            Pick(
                pick_no=1,
                round=1,
                slot=1,
                board_label="1.01",
                roster_id="1",
                manager=None,
                player=Player(sleeper_id="500", name="P", position="RB"),
            )
        ],
        draft=Draft(id="d1"),
    )
    router = _Router(fantasycalc=[{"player": {"sleeperId": "500"}, "value": 1}])
    store = FileStore(tmp_path)
    build_consensus_rank(league, store, client=router.client())

    params = router.requests[0].url.params
    assert params["numQbs"] == "1"
    assert params["numTeams"] == "10"
    assert params["ppr"] == "0"
    # cache landed under the shape-specific key
    assert store.read_cache("consensus", "fc-dyn1-qb1-tm10-ppr0") is not None


# --------------------------------------------------------------------------- #
# Row: dense re-rank + player in neither source
# --------------------------------------------------------------------------- #


def _mini_league(drafted: list[tuple[str, str]]) -> LeagueModel:
    fmt = LeagueFormat(
        team_count=12,
        roster_slots=["QB", "RB", "WR", "TE"],
        flex_eligibility={},
        scoring_label="PPR",
        is_superflex_or_2qb=False,
        te_premium=False,
    )
    picks = [
        Pick(
            pick_no=i + 1,
            round=1,
            slot=i + 1,
            board_label=f"1.{i + 1:02d}",
            roster_id=roster_id,
            manager=None,
            player=Player(sleeper_id=sid, name=f"Player {sid}", position="RB"),
        )
        for i, (sid, roster_id) in enumerate(drafted)
    ]
    teams = [Team(roster_id=r, manager=f"m{r}") for r in sorted({r for _, r in drafted})]
    return LeagueModel(
        league_id="L1",
        name="Mini",
        season=2025,
        format=fmt,
        teams=teams,
        picks=picks,
        draft=Draft(id="d1"),
    )


def test_dense_re_rank_collapses_source_gaps(tmp_path: Path) -> None:
    league = _mini_league([("500", "1"), ("900", "2"), ("200", "3")])
    board = [
        {"player": {"sleeperId": "900"}, "value": 300},
        {"player": {"sleeperId": "500"}, "value": 200},
        {"player": {"sleeperId": "200"}, "value": 100},
        {"player": {"sleeperId": "777"}, "value": 999},  # not drafted -> ignored
    ]
    rank = build_consensus_rank(
        league, FileStore(tmp_path), client=_Router(fantasycalc=board).client()
    )
    assert rank.slots == {"900": 1, "500": 2, "200": 3}


def test_player_absent_from_source_is_not_a_slot_key(tmp_path: Path) -> None:
    league = _mini_league([("500", "1"), ("900", "2")])
    board = [{"player": {"sleeperId": "500"}, "value": 10}]  # 900 missing
    rank = build_consensus_rank(
        league, FileStore(tmp_path), client=_Router(fantasycalc=board).client()
    )
    assert rank.slots == {"500": 1}


def test_sleeper_fallback_ignores_null_search_rank(tmp_path: Path) -> None:
    league = _mini_league([("500", "1"), ("900", "2"), ("200", "3")])
    players = {
        "500": {"search_rank": 30},
        "900": {"search_rank": None},  # ignored
        "200": {"search_rank": 10},
    }
    rank = build_consensus_rank(
        league,
        FileStore(tmp_path),
        client=_Router(fantasycalc=_connect_error, sleeper=players).client(),
    )
    assert rank.source == "sleeper_search_rank"
    assert rank.slots == {"200": 1, "500": 2}


# --------------------------------------------------------------------------- #
# Malformed upstream bodies -> ConsensusError, never a bare fault
# --------------------------------------------------------------------------- #


def test_non_json_fantasycalc_body_falls_back(tmp_path: Path) -> None:
    league = _rookie_league()
    router = _Router(fantasycalc=lambda req: httpx.Response(200, text="not json"))
    rank = build_consensus_rank(league, FileStore(tmp_path), client=router.client())
    assert rank.source == "sleeper_search_rank"


def test_both_bodies_malformed_raises_consensus_error(tmp_path: Path) -> None:
    league = _rookie_league()
    router = _Router(
        fantasycalc=lambda req: httpx.Response(200, json={"not": "an array"}),
        sleeper=lambda req: httpx.Response(200, json=["not", "an", "object"]),
    )
    with pytest.raises(ConsensusError) as excinfo:
        build_consensus_rank(league, FileStore(tmp_path), client=router.client())
    assert isinstance(excinfo.value.__cause__, ValueError)


# --------------------------------------------------------------------------- #
# User-Agent + offline guarantee
# --------------------------------------------------------------------------- #


def test_requests_carry_the_identifying_user_agent(tmp_path: Path) -> None:
    router = _Router(fantasycalc=_connect_error)
    build_consensus_rank(_rookie_league(), FileStore(tmp_path), client=router.client())
    for request in router.requests:
        ua = request.headers.get("user-agent", "")
        assert ua.startswith("CommishDesk/")
        assert "github.com/Eric-Weng/CommishDesk" in ua


def test_build_consensus_rank_makes_zero_real_network_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - must never run
        raise AssertionError("real network access attempted")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)

    rank = build_consensus_rank(
        _rookie_league(), FileStore(tmp_path), client=_Router().client()
    )
    assert rank.source == "fantasycalc"


def test_unsafe_would_be_key_is_a_store_error_not_silent(tmp_path: Path) -> None:
    """Guard rail: the format-derived key stays a safe single segment. A league
    format can't smuggle a path separator through ``team_count`` etc., but if the
    key derivation ever regressed, the Store must reject it."""
    store = FileStore(tmp_path)
    with pytest.raises(StoreError):
        store.write_cache("consensus", "../escape", {"x": 1})


# --------------------------------------------------------------------------- #
# Row: a source that ranks NONE of the drafted set == a source failure
# --------------------------------------------------------------------------- #


def test_empty_fantasycalc_board_falls_back_to_sleeper(tmp_path: Path) -> None:
    league = _mini_league([("500", "1"), ("900", "2")])
    router = _Router(
        fantasycalc=[],
        sleeper={"500": {"search_rank": 5}, "900": {"search_rank": 9}},
    )
    rank = build_consensus_rank(league, FileStore(tmp_path), client=router.client())

    assert rank.source == "sleeper_search_rank"
    assert rank.slots == {"500": 1, "900": 2}


def test_fantasycalc_ranks_no_drafted_player_falls_back(tmp_path: Path) -> None:
    league = _mini_league([("500", "1"), ("900", "2")])
    router = _Router(
        fantasycalc=[{"player": {"sleeperId": "999999"}, "value": 100}],  # undrafted
        sleeper={"500": {"search_rank": 1}, "900": {"search_rank": 2}},
    )
    rank = build_consensus_rank(league, FileStore(tmp_path), client=router.client())

    assert rank.source == "sleeper_search_rank"
    assert set(rank.slots) == {"500", "900"}


def test_both_sources_empty_raises_consensus_error(tmp_path: Path) -> None:
    league = _mini_league([("500", "1"), ("900", "2")])
    router = _Router(fantasycalc=[], sleeper={})
    with pytest.raises(ConsensusError):
        build_consensus_rank(league, FileStore(tmp_path), client=router.client())


def test_partial_fantasycalc_rank_is_kept_not_discarded(tmp_path: Path) -> None:
    """A source that ranks *some* drafted players is a valid partial rank; the
    unranked ones just aren't keys. This must NOT trigger the fallback."""
    league = _mini_league([("500", "1"), ("900", "2"), ("200", "3")])
    router = _Router(fantasycalc=[{"player": {"sleeperId": "500"}, "value": 10}])
    rank = build_consensus_rank(league, FileStore(tmp_path), client=router.client())

    assert rank.source == "fantasycalc"
    assert rank.slots == {"500": 1}
    assert not any("sleeper.app" in r.url.host for r in router.requests)


def test_fantasycalc_value_that_is_bool_is_ignored(tmp_path: Path) -> None:
    league = _mini_league([("500", "1"), ("900", "2")])
    router = _Router(
        fantasycalc=[
            {"player": {"sleeperId": "500"}, "value": True},  # bool -> not a number
            {"player": {"sleeperId": "900"}, "value": 42},
        ]
    )
    rank = build_consensus_rank(league, FileStore(tmp_path), client=router.client())
    assert rank.slots == {"900": 1}


# --------------------------------------------------------------------------- #
# Row: FantasyCalc 4xx (not just 5xx) also triggers the fallback
# --------------------------------------------------------------------------- #


def test_fantasycalc_4xx_falls_back_to_sleeper(tmp_path: Path) -> None:
    league = _rookie_league()
    router = _Router(fantasycalc=lambda req: httpx.Response(429, json={"error": "slow down"}))
    rank = build_consensus_rank(league, FileStore(tmp_path), client=router.client())

    assert rank.source == "sleeper_search_rank"
    assert any("sleeper.app" in r.url.host for r in router.requests)


# --------------------------------------------------------------------------- #
# Row: the cache is best-effort, never fatal
# --------------------------------------------------------------------------- #


def test_corrupt_cache_entry_is_treated_as_a_miss(tmp_path: Path) -> None:
    store = FileStore(tmp_path)
    path = tmp_path / "cache" / "consensus" / "fc-dyn1-qb2-tm12-ppr0_5.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ not json at all", encoding="utf-8")
    router = _Router()

    rank = build_consensus_rank(_rookie_league(), store, client=router.client())

    assert rank.source == "fantasycalc"
    assert len(router.requests) == 1  # the corrupt entry forced a refetch
    # the refetch also repaired the cache
    assert store.read_cache("consensus", "fc-dyn1-qb2-tm12-ppr0_5") is not None


def test_cache_write_failure_still_returns_the_rank(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileStore(tmp_path)

    def boom(*args: Any, **kwargs: Any) -> None:
        raise StoreError("disk full")

    monkeypatch.setattr(store, "write_cache", boom)

    rank = build_consensus_rank(_rookie_league(), store, client=_Router().client())

    assert rank.source == "fantasycalc"
    assert set(rank.slots.values()) == set(range(1, len(rank.slots) + 1))


# --------------------------------------------------------------------------- #
# Row: a self-built client is always closed, including on the raise path
# --------------------------------------------------------------------------- #


def test_self_built_client_is_closed_on_the_raise_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[httpx.Client] = []
    real_get = httpx.Client

    def fake_client(*args: Any, **kwargs: Any) -> httpx.Client:
        client = real_get(transport=httpx.MockTransport(_connect_error))
        built.append(client)
        return client

    monkeypatch.setattr("commishdesk.consensus.httpx.Client", fake_client)

    with pytest.raises(ConsensusError):
        build_consensus_rank(_rookie_league(), FileStore(tmp_path))  # client=None

    assert built and built[0].is_closed is True


# --------------------------------------------------------------------------- #
# End-to-end: rank -> compute_consensus_metrics is coherent
# --------------------------------------------------------------------------- #


def test_rank_feeds_compute_consensus_metrics_coherently(tmp_path: Path) -> None:
    league = _rookie_league()
    rank = build_consensus_rank(league, FileStore(tmp_path), client=_Router().client())

    metrics = compute_consensus_metrics(league, rank.slots)

    ranked = [p for p in metrics.picks if p.consensus_slot is not None]
    assert len(ranked) == len(rank.slots)
    assert {p.consensus_slot for p in ranked} == set(range(1, len(ranked) + 1))
    for pick in metrics.picks:
        if pick.consensus_slot is None:
            assert pick.delta is None and pick.flags == ["no_consensus"]
        else:
            assert pick.delta == pick.pick_no - pick.consensus_slot
            rnd, col = pick.consensus_label.split(".")
            assert int(col) == (pick.consensus_slot - 1) % 12 + 1
            assert int(rnd) == (pick.consensus_slot - 1) // 12 + 1


def test_committed_sleeper_slice_yields_a_well_formed_dense_rank(tmp_path: Path) -> None:
    league = _rookie_league()
    router = _Router(fantasycalc=_connect_error)

    rank = build_consensus_rank(league, FileStore(tmp_path), client=router.client())

    assert rank.source == "sleeper_search_rank"
    assert rank.slots.keys() <= _drafted_ids(league)
    assert len(rank.slots) > 0
    assert sorted(rank.slots.values()) == list(range(1, len(rank.slots) + 1))


def test_warm_sleeper_cache_short_circuits_the_players_file_fetch(tmp_path: Path) -> None:
    league = _rookie_league()
    store = FileStore(tmp_path)
    # first run: FantasyCalc down -> live Sleeper fetch -> Sleeper payload cached
    build_consensus_rank(store=store, league=league, client=_Router(fantasycalc=_connect_error).client())
    assert store.read_cache("consensus", "sleeper-players-nfl") is not None

    # second run: FantasyCalc still down, but the warm Sleeper cache means no
    # players-file request goes out.
    router = _Router(fantasycalc=_connect_error)
    rank = build_consensus_rank(league, store, client=router.client())

    assert rank.source == "sleeper_search_rank"
    assert not any("sleeper.app" in r.url.host for r in router.requests)
