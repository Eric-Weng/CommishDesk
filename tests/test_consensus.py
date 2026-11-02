"""Story 2.4b / 9.9: ``build_consensus_rank`` -- the draft-recap board ranked by League Value.

One test per row of the 9.9 I/O & Edge-Case Matrix, driven by ``httpx.MockTransport`` (zero real network,
ever). Two sets of recorded payloads: ``tests/fixtures/consensus/`` (a Sleeper players file over the 72-pick
rookie board) and ``tests/fixtures/league_value/`` (synthetic nflverse stats and a veteran league, Story 9.6a).
"""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from commishdesk import consensus as consensus_mod
from commishdesk.consensus import ConsensusRank, build_consensus_rank
from commishdesk.errors import ConsensusError, StoreError
from commishdesk.facts import build_draft_recap_facts
from commishdesk.ingest import (
    Draft,
    LeagueFormat,
    LeagueModel,
    Pick,
    Player,
    Team,
    build_league_model,
)
from commishdesk.league_value import snapshot_cache_key
from commishdesk.narrate import recap_to_text, render_draft_recap
from commishdesk.render import render_email, render_web
from commishdesk.stats import compute_board_metrics, compute_consensus_metrics, compute_draft_grades
from commishdesk.store import FileStore
from tests.conftest import REPO_ROOT

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
CONSENSUS_DIR = FIXTURE_DIR / "consensus"
LV_DIR = FIXTURE_DIR / "league_value"

SLEEPER_PLAYERS: dict[str, dict[str, Any]] = json.loads(
    (CONSENSUS_DIR / "sleeper-players-nfl.json").read_text(encoding="utf-8")
)
LV: dict[str, Any] = json.loads((LV_DIR / "league.json").read_text(encoding="utf-8"))
SEASON = 2026
STAMP = "2026-09-03T00:00:00.000000Z"
NFLVERSE_CREDIT = "stats: nflverse, CC BY 4.0"


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
        "players": raw.get("players", {}),
        "previous_league_ids": [],
    }


def _rookie_league() -> LeagueModel:
    return build_league_model(_bundle("rookie-draft.json"))


def _drafted_ids(league: LeagueModel) -> set[str]:
    return {p.player.sleeper_id for p in league.picks if p.player.sleeper_id}


def _vet_league(drafted: list[str], *, season: int = SEASON) -> LeagueModel:
    """A 3-team league (the League Value fixture) that drafted ``drafted`` in that order."""
    fmt = LeagueFormat(
        team_count=3,
        roster_slots=["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF"],
        flex_eligibility={"FLEX": ["RB", "WR", "TE"]},
        scoring_label="PPR",
        is_superflex_or_2qb=False,
        te_premium=False,
    )
    picks = [
        Pick(
            pick_no=i + 1,
            round=1,
            slot=i + 1,
            board_label=f"{i // 3 + 1}.{i % 3 + 1:02d}",
            roster_id=str(i % 3 + 1),
            manager=None,
            player=Player(sleeper_id=pid, name=f"Player {pid}", position=LV["players"][pid]["position"]),
        )
        for i, pid in enumerate(drafted)
    ]
    return LeagueModel(
        league_id="9001",
        name="Veterans",
        season=season,
        format=fmt,
        teams=[Team(roster_id=r, manager=f"m{r}") for r in ("1", "2", "3")],
        picks=picks,
        draft=Draft(id="d1"),
    )


def _mini_league(drafted: list[tuple[str, str]]) -> LeagueModel:
    """A 12-team league that drafted ``(sleeper_id, roster_id)`` pairs in order (all RB, no stats)."""
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
        league_id="L1", name="Mini", season=2025, format=fmt, teams=teams, picks=picks, draft=Draft(id="d1")
    )


def _dynasty_settings() -> dict[str, Any]:
    league = json.loads(json.dumps(LV["league"]))
    league["settings"]["type"] = 2
    return league


class _Router:
    """A MockTransport router over the Sleeper players file and the nflverse release assets. Every
    request is recorded. ``sleeper`` / ``nflverse_status`` break the respective source."""

    def __init__(
        self,
        *,
        sleeper: Any = SLEEPER_PLAYERS,
        nflverse_status: int = 200,
        missing: tuple[str, ...] = (),
    ) -> None:
        self.sleeper = sleeper
        self.nflverse_status = nflverse_status
        self.missing = missing
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        if host == "api.sleeper.app":
            if callable(self.sleeper):
                return self.sleeper(request)
            return httpx.Response(200, json=self.sleeper)
        if host == "github.com":
            name = request.url.path.rsplit("/", 1)[1]
            if self.nflverse_status != 200 or any(name.startswith(m) for m in self.missing):
                return httpx.Response(self.nflverse_status if self.nflverse_status != 200 else 404)
            raw = (LV_DIR / name.removesuffix(".gz")).read_bytes()
            return httpx.Response(200, content=gzip.compress(raw))
        return httpx.Response(404, json={"error": f"unmapped {request.url}"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]


def _connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def _server_error(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, json={"error": "down"})


VET_DRAFT = ["100", "101", "102", "200", "201", "300", "301", "400", "700", "900"]


def _vet_rank(
    tmp_path: Path,
    router: _Router | None = None,
    *,
    drafted: list[str] | None = None,
    settings: dict[str, Any] | None = None,
    source: str | None = "nflverse",
    store: FileStore | None = None,
) -> ConsensusRank:
    router = router or _Router(sleeper=LV["players"])
    return build_consensus_rank(
        _vet_league(drafted or VET_DRAFT),
        store or FileStore(tmp_path),
        league_settings=settings or LV["league"],
        client=router.client(),
        league_value_source=source,
    )


def _key() -> str:
    return snapshot_cache_key("9001", SEASON, 0)


def _snapshot(store: FileStore) -> dict[str, Any]:
    snap = store.read_cache("league_value", _key())
    assert snap is not None
    return snap


# --------------------------------------------------------------------------- #
# Row: startup draft, nflverse up -> League Value board
# --------------------------------------------------------------------------- #


def test_startup_draft_ranks_by_league_value_with_the_nflverse_credit(tmp_path: Path) -> None:
    store = FileStore(tmp_path)
    router = _Router(sleeper=LV["players"])
    rank = _vet_rank(tmp_path, router, store=store)

    assert rank.source == "league_value"
    assert rank.nflverse is True
    snap = _snapshot(store)
    assert snap["week"] == 0 and snap["source"] == "nflverse"
    values = snap["players"]

    ranked = sorted(rank.slots, key=rank.slots.get)  # type: ignore[arg-type]
    assert sorted(rank.slots.values()) == list(range(1, len(rank.slots) + 1))  # dense
    # Ordered by value descending, ties by Sleeper search_rank then id.
    expected = sorted(
        rank.slots,
        key=lambda pid: (-values[pid]["value"], LV["players"][pid]["search_rank"], pid),
    )
    assert ranked == expected
    assert values["100"]["est"] is False and values["900"]["est"] is True  # a veteran and a rookie
    assert "900" in rank.slots  # an est. rookie still ranks, by his positional-rank fill


def test_league_value_is_the_last_seasons_points_scored_in_the_leagues_settings(tmp_path: Path) -> None:
    """Independent oracle: at week 0 a veteran's ppg is last season's mean under the league's scoring."""
    import csv

    store = FileStore(tmp_path)
    _vet_rank(tmp_path, store=store)
    snap = _snapshot(store)
    with (LV_DIR / "stats_player_week_2025.csv").open(encoding="utf-8", newline="") as handle:
        rows = [r for r in csv.DictReader(handle) if r["player_id"] == "00-00200"]
    assert len(rows) == 17
    points = []
    for row in rows:
        f = {k: float(v or 0) for k, v in row.items() if k in {
            "rushing_yards", "rushing_tds", "receptions", "receiving_yards", "receiving_tds",
            "rushing_fumbles_lost"}}
        pts = 0.1 * f["rushing_yards"] + 6 * f["rushing_tds"] + f["receptions"] + 0.1 * f["receiving_yards"]
        pts += 6 * f["receiving_tds"] - 2 * f["rushing_fumbles_lost"]
        if f["rushing_yards"] >= 100:
            pts += 2
        points.append(pts)
    assert snap["players"]["200"]["ppg"] == pytest.approx(sum(points) / len(points), abs=1e-3)
    assert snap["players"]["200"]["games"] == 0


def test_dynasty_ranks_by_future_value_where_a_player_has_one(tmp_path: Path) -> None:
    store = FileStore(tmp_path)
    rank = _vet_rank(tmp_path, settings=_dynasty_settings(), store=store)
    values = _snapshot(store)["players"]
    assert rank.source == "league_value"
    assert any("future" in values[pid] for pid in rank.slots)

    def key(pid: str) -> tuple[float, float, str]:
        info = values[pid]
        return (-info.get("future", info["value"]), LV["players"][pid]["search_rank"], pid)

    assert sorted(rank.slots, key=rank.slots.get) == sorted(rank.slots, key=key)  # type: ignore[arg-type]


def test_a_player_league_value_does_not_value_is_no_consensus_never_zero(tmp_path: Path) -> None:
    """A kicker with no ``players_points`` at draft time has no snapshot row: not a slot key."""
    rank = _vet_rank(tmp_path)
    assert "700" not in rank.slots
    league = _vet_league(VET_DRAFT)
    metrics = compute_consensus_metrics(league, rank.slots)
    kicker = next(p for p in metrics.picks if p.player == "Player 700")
    assert kicker.consensus_slot is None and kicker.delta is None and kicker.flags == ["no_consensus"]


def test_veterans_and_est_rookies_mix_on_one_board(tmp_path: Path) -> None:
    """Mixed set: the est. rookie ranks by his League Value (positional-rank fill), source league_value."""
    store = FileStore(tmp_path)
    rank = _vet_rank(tmp_path, drafted=["200", "201", "900", "901"], store=store)
    values = _snapshot(store)["players"]
    assert rank.source == "league_value"
    assert values["900"]["est"] is True and values["901"]["est"] is True
    assert set(rank.slots) == {"200", "201", "900", "901"}


# --------------------------------------------------------------------------- #
# Row: rookie draft -> Sleeper search_rank, said plainly
# --------------------------------------------------------------------------- #


def test_rookie_draft_ranks_by_sleeper_search_rank_directly(tmp_path: Path) -> None:
    league = _rookie_league()
    router = _Router()
    rank = build_consensus_rank(
        league, FileStore(tmp_path), league_settings=_bundle("rookie-draft.json")["league"], client=router.client()
    )
    assert rank.source == "sleeper_search_rank"
    assert rank.nflverse is False
    ranked = {pid: rec["search_rank"] for pid, rec in SLEEPER_PLAYERS.items() if rec["search_rank"] is not None}
    expected = sorted((pid for pid in ranked if pid in _drafted_ids(league)), key=lambda p: (ranked[p], p))
    assert sorted(rank.slots, key=rank.slots.get) == expected  # type: ignore[arg-type]
    assert len(rank.slots) == len(expected) < len(_drafted_ids(league))  # null search_rank stays out


def test_every_pick_est_or_absent_is_a_rookie_draft(tmp_path: Path) -> None:
    rank = _vet_rank(tmp_path, drafted=["900", "901", "902", "700"])
    assert rank.source == "sleeper_search_rank"
    # The kicker has no League Value but does have a Sleeper rank, so he ranks on the Sleeper board.
    assert sorted(rank.slots, key=rank.slots.get) == ["900", "901", "902", "700"]  # type: ignore[arg-type]


def test_rookie_draft_facts_and_recap_say_sleeper_rank_not_a_stats_board(tmp_path: Path) -> None:
    league = _rookie_league()
    rank = build_consensus_rank(
        league, FileStore(tmp_path), league_settings=_bundle("rookie-draft.json")["league"], client=_Router().client()
    )
    facts = _facts_for(league, rank)
    assert facts.consensus_source.name == "sleeper_search_rank"
    assert facts.consensus_source.attribution is None
    assert facts.narration.board_source == "sleeper_search_rank"
    assert "search_rank" in facts.consensus_source.engine_note and "League Value" in facts.consensus_source.engine_note

    recap = recap_to_text(render_draft_recap(facts.narration))
    assert "against Sleeper rank (est.), not a stats-based board" in recap
    assert "Sleeper rank (est.) had it at" in recap
    assert "consensus" not in recap.lower()

    page = render_web(facts, recap=render_draft_recap(facts.narration), output_id="x", generated_at=STAMP)
    assert "against Sleeper rank (est.), not a stats-based board" in page
    assert NFLVERSE_CREDIT not in page
    mail = render_email(facts, recap=render_draft_recap(facts.narration), generated_at=STAMP)
    assert "against Sleeper rank (est.), not a stats-based board" in mail.html
    assert NFLVERSE_CREDIT not in mail.html


def _facts_for(league: LeagueModel, rank: ConsensusRank) -> Any:
    consensus = compute_consensus_metrics(league, rank.slots)
    return build_draft_recap_facts(
        league,
        compute_board_metrics(league),
        consensus,
        compute_draft_grades(league, consensus),
        generated_at=STAMP,
        consensus_source_name=rank.source,
        consensus_as_of=rank.as_of,
        consensus_nflverse=rank.nflverse,
    )


def test_league_value_board_facts_recap_and_footer_credit_nflverse(tmp_path: Path) -> None:
    league = _vet_league(VET_DRAFT)
    rank = _vet_rank(tmp_path)
    facts = _facts_for(league, rank)
    assert facts.consensus_source.name == "league_value"
    assert facts.consensus_source.attribution == NFLVERSE_CREDIT

    narration_recap = render_draft_recap(facts.narration)
    text = recap_to_text(narration_recap)
    assert "The League Value board had it at" in text
    assert "stats-based board" not in text
    assert "consensus" not in text.lower()

    page = render_web(facts, recap=narration_recap, output_id="x", generated_at=STAMP)
    assert "against the League Value board." in page
    assert NFLVERSE_CREDIT in page
    mail = render_email(facts, recap=narration_recap, generated_at=STAMP)
    assert NFLVERSE_CREDIT in mail.html and "against the League Value board." in mail.html


def test_footer_carries_no_credit_when_nflverse_was_not_used(tmp_path: Path) -> None:
    """nflverse unreachable: a labelled sleeper_only snapshot, the rank is Sleeper rank, no credit."""
    store = FileStore(tmp_path)
    router = _Router(sleeper=LV["players"], nflverse_status=503)
    rank = _vet_rank(tmp_path, router, store=store)
    snap = _snapshot(store)
    assert snap["source"] == "sleeper_only" and snap["fallback"] == "nflverse_unavailable"
    assert rank.nflverse is False
    facts = _facts_for(_vet_league(VET_DRAFT), rank)
    assert facts.consensus_source.attribution is None
    page = render_web(facts, recap=render_draft_recap(facts.narration), output_id="x", generated_at=STAMP)
    assert NFLVERSE_CREDIT not in page


# --------------------------------------------------------------------------- #
# Row: new-season draft before week 1
# --------------------------------------------------------------------------- #


def test_week_zero_never_fetches_the_unpublished_current_season_stats(tmp_path: Path) -> None:
    router = _Router(sleeper=LV["players"])
    _vet_rank(tmp_path, router)
    nflverse = [u for u in router.urls() if "github.com" in u]
    assert any("stats_player_week_2025" in u for u in nflverse)
    assert not any("stats_player_week_2026" in u for u in nflverse)


def test_week_zero_snapshot_with_only_last_season_stats_stays_nflverse(tmp_path: Path) -> None:
    """``stats_player_week_{season}`` is not published yet (404): no fallback to sleeper_only."""
    store = FileStore(tmp_path)
    router = _Router(sleeper=LV["players"], missing=("stats_player_week_2026",))
    rank = _vet_rank(tmp_path, router, store=store)
    snap = _snapshot(store)
    assert snap["source"] == "nflverse" and snap["fallback"] is None
    assert rank.source == "league_value" and rank.nflverse is True
    assert snap["players"]["200"]["est"] is False  # still valued on last season's points


# --------------------------------------------------------------------------- #
# Row: no snapshot / no usable source
# --------------------------------------------------------------------------- #


def test_no_snapshot_ranks_by_sleeper_search_rank(tmp_path: Path) -> None:
    rank = _vet_rank(tmp_path, source="off")
    assert rank.source == "sleeper_search_rank" and rank.nflverse is False
    ranks = {p: LV["players"][p]["search_rank"] for p in VET_DRAFT if LV["players"][p].get("search_rank")}
    expected = sorted(ranks, key=ranks.__getitem__)
    assert sorted(rank.slots, key=rank.slots.get) == expected  # type: ignore[arg-type]


def test_without_league_settings_there_is_no_snapshot(tmp_path: Path) -> None:
    router = _Router()
    rank = build_consensus_rank(_rookie_league(), FileStore(tmp_path), client=router.client())
    assert rank.source == "sleeper_search_rank"
    assert not any("github.com" in u for u in router.urls())


def test_no_snapshot_and_no_search_rank_raises_consensus_error(tmp_path: Path) -> None:
    with pytest.raises(ConsensusError):
        build_consensus_rank(_rookie_league(), FileStore(tmp_path), client=_Router(sleeper={}).client())


def test_players_file_unreachable_and_no_snapshot_raises_consensus_error(tmp_path: Path) -> None:
    with pytest.raises(ConsensusError):
        build_consensus_rank(_rookie_league(), FileStore(tmp_path), client=_Router(sleeper=_connect_error).client())


@pytest.mark.parametrize("failure", [_connect_error, _server_error])
def test_a_failed_players_fetch_still_ranks_from_a_cached_snapshot(tmp_path: Path, failure: Callable[..., Any]) -> None:
    store = FileStore(tmp_path)
    first = _vet_rank(tmp_path, store=store)
    # A fresh Store dir would miss; here the Sleeper players cache is warm too, so wipe only that entry.
    (tmp_path / "cache" / "consensus" / "sleeper-players-nfl.json").unlink()
    router = _Router(sleeper=failure)
    again = _vet_rank(tmp_path, router, store=store)
    assert again.source == "league_value" and again.slots.keys() == first.slots.keys()


def test_a_supplied_players_file_replaces_the_sleeper_fetch(tmp_path: Path) -> None:
    router = _Router()
    store = FileStore(tmp_path)
    rank = build_consensus_rank(_rookie_league(), store, client=router.client(), players=SLEEPER_PLAYERS)
    assert rank.source == "sleeper_search_rank"
    assert not any("sleeper.app" in u for u in router.urls())
    assert store.read_cache("consensus", "sleeper-players-nfl") is None  # nothing written


def test_a_supplied_players_file_that_ranks_nothing_names_its_label(tmp_path: Path) -> None:
    with pytest.raises(ConsensusError, match="my-players.json"):
        build_consensus_rank(
            _rookie_league(),
            FileStore(tmp_path),
            client=_Router().client(),
            players={},
            players_label="my-players.json",
        )


# --------------------------------------------------------------------------- #
# Cache + determinism
# --------------------------------------------------------------------------- #


def test_warm_cache_issues_zero_requests_and_is_deterministic(tmp_path: Path) -> None:
    store = FileStore(tmp_path)
    cold = _Router(sleeper=LV["players"])
    first = _vet_rank(tmp_path, cold, store=store)
    assert cold.requests  # players file + nflverse assets

    warm = _Router(sleeper=_connect_error, nflverse_status=503)
    second = _vet_rank(tmp_path, warm, store=store)
    assert warm.requests == []
    assert first.model_dump() == second.model_dump()


def test_two_runs_with_one_cached_snapshot_are_byte_identical(tmp_path: Path) -> None:
    store = FileStore(tmp_path)
    _vet_rank(tmp_path, store=store)
    path = tmp_path / "cache" / "league_value" / f"{_key()}.json"
    before = path.read_bytes()
    a = _vet_rank(tmp_path, store=store)
    b = _vet_rank(tmp_path, store=store)
    assert path.read_bytes() == before
    assert a.model_dump_json() == b.model_dump_json()


def test_corrupt_players_cache_entry_is_treated_as_a_miss(tmp_path: Path) -> None:
    path = tmp_path / "cache" / "consensus" / "sleeper-players-nfl.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ not json at all", encoding="utf-8")
    router = _Router()
    rank = build_consensus_rank(_rookie_league(), FileStore(tmp_path), client=router.client())
    assert rank.source == "sleeper_search_rank"
    assert len(router.requests) == 1  # the corrupt entry forced a refetch
    assert FileStore(tmp_path).read_cache("consensus", "sleeper-players-nfl") is not None  # and repaired it


def test_cache_write_failure_still_returns_the_rank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = FileStore(tmp_path)

    def boom(*args: Any, **kwargs: Any) -> None:
        raise StoreError("disk full")

    monkeypatch.setattr(store, "write_cache", boom)
    rank = build_consensus_rank(_rookie_league(), store, client=_Router().client())
    assert rank.source == "sleeper_search_rank"
    assert set(rank.slots.values()) == set(range(1, len(rank.slots) + 1))


def test_build_consensus_rank_makes_zero_real_network_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def no_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a real network call was attempted")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    build_consensus_rank(_rookie_league(), FileStore(tmp_path), client=_Router().client())
    _vet_rank(tmp_path / "vet")


def test_self_built_client_is_closed_on_the_raise_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[httpx.Client] = []
    real_client = httpx.Client

    def fake_client(*args: Any, **kwargs: Any) -> httpx.Client:
        client = real_client(transport=httpx.MockTransport(_connect_error))
        built.append(client)
        return client

    monkeypatch.setattr("commishdesk.consensus.httpx.Client", fake_client)
    with pytest.raises(ConsensusError):
        build_consensus_rank(_rookie_league(), FileStore(tmp_path))  # client=None
    assert built and built[0].is_closed is True


# --------------------------------------------------------------------------- #
# Ranking rules (unit)
# --------------------------------------------------------------------------- #


def _snap(players: dict[str, dict[str, Any]], *, dynasty: bool = False, source: str = "nflverse") -> dict[str, Any]:
    return {"players": players, "dynasty": dynasty, "source": source}


def _v(value: Any, *, est: bool = False, future: float | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"pos": "RB", "ppg": 1.0, "games": 0, "value": value, "est": est}
    if future is not None:
        out["future"] = future
    return out


def test_ties_break_on_search_rank_then_id() -> None:
    snap = _snap({"a": _v(5.0), "b": _v(5.0), "c": _v(5.0), "d": _v(5.0), "top": _v(9.0)})
    players = {"a": {"search_rank": 20}, "b": {"search_rank": 10}, "c": {"search_rank": 20}, "top": {"search_rank": 99}}
    rank = consensus_mod._rank_from_league_value(snap, players, {"a", "b", "c", "d", "top"})
    assert rank is not None
    # top by value; b before a/c on search_rank; a before c on id; d (no search_rank) last of the tie
    assert sorted(rank.slots, key=rank.slots.get) == ["top", "b", "a", "c", "d"]  # type: ignore[arg-type]


def test_est_only_snapshot_hands_the_draft_to_sleeper_rank() -> None:
    snap = _snap({"a": _v(0.0, est=True), "b": _v(3.0, est=True)})
    assert consensus_mod._rank_from_league_value(snap, {}, {"a", "b"}) is None


def test_unvalued_and_malformed_rows_are_left_out_never_zero() -> None:
    snap = _snap({"a": _v(4.0), "b": {"pos": "K", "value": None, "est": False}, "c": _v(True)})
    rank = consensus_mod._rank_from_league_value(snap, {}, {"a", "b", "c", "missing"})
    assert rank is not None and rank.slots == {"a": 1}


def test_a_sleeper_only_source_is_not_credited_to_nflverse() -> None:
    snap = _snap({"a": _v(4.0)}, source="sleeper_only")
    rank = consensus_mod._rank_from_league_value(snap, {}, {"a"})
    assert rank is not None and rank.nflverse is False


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


# --------------------------------------------------------------------------- #
# AR-71: the market-value source is gone from the engine and its tests
# --------------------------------------------------------------------------- #

_REMOVED_SOURCE = "fantasy" + "calc"


def test_no_removed_market_source_string_anywhere_in_the_engine_or_its_tests() -> None:
    offenders: list[str] = []
    for root in (REPO_ROOT / "commishdesk", REPO_ROOT / "tests"):
        for path in root.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".gz", ".png"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if re.search(_REMOVED_SOURCE, text, re.IGNORECASE) or _REMOVED_SOURCE in path.name.lower():
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []
