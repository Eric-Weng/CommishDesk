"""Story 8B.1 -- the engine accepts a supplied ``players/nfl.json``.

One test per row of the story's I/O & Edge-Case Matrix. Every test runs against
``httpx.MockTransport`` or stubs: zero real network, no keys.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from commishdesk.adapters.sleeper import SleeperAdapter, load_players_file
from commishdesk.cli import app
from commishdesk.consensus import build_consensus_rank
from commishdesk.errors import AdapterError, CommishDeskError, ConsensusError
from commishdesk.store import FileStore
from commishdesk.weekly import build_weekly_issue
from tests.test_consensus import _mini_league, _Router, _server_error
from tests.test_sleeper_adapter import LEAGUE, PLAYERS, UNREFERENCED_PLAYER_ID, _week_transport

runner = CliRunner()


def _write(path: Path, payload: Any) -> Path:
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def _players_requests(requests: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in requests if r.url.path == "/v1/players/nfl"]


# --------------------------------------------------------------------------- #
# Weekly, file given / no file
# --------------------------------------------------------------------------- #


def test_weekly_with_file_filters_to_rostered_ids_and_makes_zero_players_requests(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    path = _write(tmp_path / "nfl.json", PLAYERS)
    adapter = SleeperAdapter(client=httpx.Client(transport=_week_transport(requests=requests)), players_file=path)

    bundle = adapter.fetch_week(LEAGUE["league_id"], 1)

    assert requests, "the other endpoints are still fetched"
    assert _players_requests(requests) == []
    assert "6904" in bundle["players"] and UNREFERENCED_PLAYER_ID not in bundle["players"]


def test_weekly_with_file_is_identical_to_the_live_path(tmp_path: Path) -> None:
    live = SleeperAdapter(client=httpx.Client(transport=_week_transport())).fetch_week(LEAGUE["league_id"], 3)
    path = _write(tmp_path / "nfl.json", PLAYERS)
    supplied = SleeperAdapter(
        client=httpx.Client(transport=_week_transport()), players_file=path
    ).fetch_week(LEAGUE["league_id"], 3)
    assert supplied == live


def test_weekly_without_a_file_still_makes_one_players_request() -> None:
    requests: list[httpx.Request] = []
    SleeperAdapter(client=httpx.Client(transport=_week_transport(requests=requests))).fetch_week(
        LEAGUE["league_id"], 1
    )
    assert len(_players_requests(requests)) == 1


def test_a_missing_individual_id_is_tolerated(tmp_path: Path) -> None:
    partial = {"6904": PLAYERS["6904"]}
    path = _write(tmp_path / "nfl.json", partial)
    bundle = SleeperAdapter(client=httpx.Client(transport=_week_transport()), players_file=path).fetch_week(
        LEAGUE["league_id"], 1
    )
    assert set(bundle["players"]) == {"6904"}


# --------------------------------------------------------------------------- #
# Bad files: no requests, a typed error naming the path
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "content",
    [None, "{not json", "[1, 2, 3]", '"a string"', "{}", ""],
    ids=["missing", "malformed", "array", "string", "empty-object", "empty-file"],
)
def test_unusable_file_raises_adapter_error_naming_the_path_with_no_requests(
    tmp_path: Path, content: str | None
) -> None:
    path = tmp_path / "nope.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    requests: list[httpx.Request] = []
    with pytest.raises(AdapterError, match="nope.json"):
        SleeperAdapter(client=httpx.Client(transport=_week_transport(requests=requests)), players_file=path)
    assert requests == []
    with pytest.raises(AdapterError, match="nope.json"):
        load_players_file(path)


def test_non_utf8_file_is_a_typed_error(tmp_path: Path) -> None:
    path = tmp_path / "bin.json"
    path.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(AdapterError, match="bin.json"):
        load_players_file(path)


def test_stale_file_raises_naming_the_path_and_never_falls_back(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    path = _write(tmp_path / "old.json", {"1": {"position": "QB"}, "2": {"position": "RB"}})
    adapter = SleeperAdapter(client=httpx.Client(transport=_week_transport(requests=requests)), players_file=path)
    with pytest.raises(AdapterError, match=r"stale.*old\.json|old\.json.*stale"):
        adapter.fetch_week(LEAGUE["league_id"], 1)
    assert _players_requests(requests) == []


# --------------------------------------------------------------------------- #
# build_weekly_issue
# --------------------------------------------------------------------------- #


def test_build_weekly_issue_rejects_adapter_plus_players_file(tmp_path: Path) -> None:
    class _Spy:
        calls: list[str] = []

        def fetch(self, league_id: str) -> Any:
            self.calls.append("fetch")

        def fetch_week(self, league_id: str, week: int) -> Any:
            self.calls.append("fetch_week")

        def close(self) -> None:
            self.calls.append("close")

    spy = _Spy()
    path = _write(tmp_path / "nfl.json", PLAYERS)
    with pytest.raises(CommishDeskError, match="players_file"):
        build_weekly_issue(FileStore(tmp_path / "s"), "L1", 17, adapter=spy, players_file=path, suppressions=set())
    assert spy.calls == []


def test_build_weekly_issue_with_a_bad_file_fails_before_any_fetch(tmp_path: Path) -> None:
    with pytest.raises(AdapterError, match="missing.json"):
        build_weekly_issue(
            FileStore(tmp_path / "s"), "L1", 17, players_file=tmp_path / "missing.json", suppressions=set()
        )


# --------------------------------------------------------------------------- #
# Draft recap consensus
# --------------------------------------------------------------------------- #

_PLAYERS_RANKS = {"500": {"search_rank": 5}, "900": {"search_rank": 9}}


def test_consensus_uses_the_supplied_players_when_fantasycalc_fails(tmp_path: Path) -> None:
    league = _mini_league([("500", "1"), ("900", "2")])
    router = _Router(fantasycalc=_server_error)

    rank = build_consensus_rank(
        league, FileStore(tmp_path), client=router.client(), players=_PLAYERS_RANKS, players_label="nfl.json"
    )

    assert rank.source == "sleeper_search_rank"
    assert rank.slots == {"500": 1, "900": 2}
    assert [r.url.host for r in router.requests if "sleeper.app" in r.url.host] == []
    assert FileStore(tmp_path).read_cache("consensus", "sleeper-players-nfl") is None


def test_consensus_prefers_fantasycalc_and_leaves_the_file_unused(tmp_path: Path) -> None:
    league = _mini_league([("500", "1"), ("900", "2")])
    router = _Router(
        fantasycalc=[
            {"player": {"sleeperId": "500"}, "value": 10},
            {"player": {"sleeperId": "900"}, "value": 20},
        ]
    )

    class _Boom(dict):  # type: ignore[type-arg]
        def items(self) -> Any:
            raise AssertionError("the players file must not be read when FantasyCalc works")

    rank = build_consensus_rank(league, FileStore(tmp_path), client=router.client(), players=_Boom(_PLAYERS_RANKS))
    assert rank.source == "fantasycalc"
    assert rank.slots == {"900": 1, "500": 2}


def test_consensus_file_that_ranks_none_raises_naming_the_file_with_no_sleeper_fetch(tmp_path: Path) -> None:
    league = _mini_league([("500", "1"), ("900", "2")])
    router = _Router(fantasycalc=_server_error)

    with pytest.raises(ConsensusError, match="elsewhere.json"):
        build_consensus_rank(
            league,
            FileStore(tmp_path),
            client=router.client(),
            players={"777": {"search_rank": 1}},
            players_label="elsewhere.json",
        )
    assert [r for r in router.requests if "sleeper.app" in r.url.host] == []


def test_consensus_ignores_a_warm_sleeper_cache_when_a_file_is_supplied(tmp_path: Path) -> None:
    league = _mini_league([("500", "1"), ("900", "2")])
    store = FileStore(tmp_path)
    store.write_cache("consensus", "sleeper-players-nfl", {"500": {"search_rank": 9}, "900": {"search_rank": 1}})
    router = _Router(fantasycalc=_server_error)

    rank = build_consensus_rank(league, store, client=router.client(), players=_PLAYERS_RANKS)
    assert rank.slots == {"500": 1, "900": 2}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", [["--week", "5"], ["--draft-recap"]])
def test_cli_missing_file_exits_1_with_one_line_and_no_traceback(tmp_path: Path, mode: list[str]) -> None:
    result = runner.invoke(
        app, ["--league", "77", *mode, "--players-file", str(tmp_path / "nope.json"), "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "nope.json" in result.output
    assert "Traceback" not in result.output
    assert len([line for line in result.output.splitlines() if line.strip()]) == 1


def test_cli_players_file_requires_a_mode(tmp_path: Path) -> None:
    path = _write(tmp_path / "nfl.json", PLAYERS)
    result = runner.invoke(app, ["--league", "77", "--players-file", str(path)])
    assert result.exit_code != 0
    assert "--players-file" in result.output


def test_cli_weekly_passes_the_file_to_the_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_cli_weekly import _stub_adapter

    _stub_adapter(monkeypatch)
    seen: list[dict[str, object]] = []
    stub_cls = __import__("commishdesk.adapters.sleeper", fromlist=["SleeperAdapter"]).SleeperAdapter

    class _Recording(stub_cls):  # type: ignore[misc, valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            seen.append(dict(kwargs))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("commishdesk.adapters.sleeper.SleeperAdapter", _Recording)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    path = _write(tmp_path / "nfl.json", PLAYERS)

    result = runner.invoke(
        app, ["--league", "77", "--week", "17", "--players-file", str(path), "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert seen == [{"players_file": path}]


def test_cli_without_a_file_builds_the_adapter_as_before(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_cli_weekly import _stub_adapter

    _stub_adapter(monkeypatch)
    seen: list[tuple[tuple[object, ...], dict[str, object]]] = []
    stub_cls = __import__("commishdesk.adapters.sleeper", fromlist=["SleeperAdapter"]).SleeperAdapter

    class _Recording(stub_cls):  # type: ignore[misc, valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            seen.append((args, dict(kwargs)))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("commishdesk.adapters.sleeper.SleeperAdapter", _Recording)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(app, ["--league", "77", "--week", "17", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert seen == [((), {})]


def test_cli_draft_recap_passes_the_players_to_the_consensus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from commishdesk import consensus, demo
    from commishdesk.consensus import ConsensusRank

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("COMMISHDESK_LLM_VERIFIER", "off")
    bundle = demo.load_demo_bundle()

    class _FakeAdapter:
        def __init__(self, *a: object, **k: object) -> None: ...

        def fetch(self, league_id: str) -> dict[str, Any]:
            return bundle

        def close(self) -> None: ...

    captured: dict[str, object] = {}

    def _fake(*args: object, **kwargs: object) -> ConsensusRank:
        captured.update(kwargs)
        return ConsensusRank(source="fantasycalc", as_of="2099-07", slots=demo.demo_consensus_slots())

    monkeypatch.setattr("commishdesk.adapters.sleeper.SleeperAdapter", _FakeAdapter)
    monkeypatch.setattr(consensus, "build_consensus_rank", _fake)
    path = _write(tmp_path / "nfl.json", PLAYERS)

    result = runner.invoke(
        app, ["--league", "999", "--draft-recap", "--players-file", str(path), "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert captured["players"] == PLAYERS
    assert captured["players_label"] == str(path)
