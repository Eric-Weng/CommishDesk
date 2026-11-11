"""Story 9B.2: Sleeper-true multi-season history in the engine.

One test per I/O & Edge-Case Matrix row, driven by the anonymized goldens in
``tests/fixtures/history/`` through ``httpx.MockTransport`` (zero real network).

Fixture facts the assertions lean on: six rosters, regular weeks 1-3 pair
(1v2, 3v4, 5v6) / (1v3, 2v5, 4v6) / (1v4, 2v6, 3v5) and the higher roster id always
scores more (``100 + 10 * roster_id``), so the higher id wins every game. 2022: median off,
playoffs from week 4 (bracket of 1-4, 5v6 consolation). 2023: median ON, roster 2 co-owned.
2024: median off, playoffs off, roster 4 changes owner u4 -> u7, roster 6 unowned.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from commishdesk.adapters.sleeper import SleeperAdapter
from commishdesk.errors import AdapterError
from commishdesk.ingest import (
    HISTORY_MODEL_VERSION,
    HISTORY_NAMESPACE,
    History,
    build_history,
    build_season_history,
    load_history,
)
from commishdesk.store import FileStore
from commishdesk.weekly import load_weekly_history
from tests.conftest import REPO_ROOT

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "history"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


SEASONS = {"h2022": _load("season-2022"), "h2023": _load("season-2023"), "h2024": _load("season-2024")}
CURRENT = _load("current")


def _adapter(
    requests: list[httpx.Request] | None = None, *, fail: dict[str, int] | None = None
) -> SleeperAdapter:
    """An adapter over the three season fixtures. *fail* maps a league id to a status
    code returned for every request about that league."""
    fail = fail or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        parts = request.url.path.split("/")  # "", "v1", "league", id, ...
        assert parts[2] == "league", request.url.path
        lid = parts[3]
        if lid in fail:
            return httpx.Response(fail[lid], json={})
        season = SEASONS[lid]
        tail = parts[4:]
        if not tail:
            return httpx.Response(200, json=season["league"])
        if tail == ["users"]:
            return httpx.Response(200, json=season["users"])
        if tail == ["rosters"]:
            return httpx.Response(200, json=season["rosters"])
        if tail == ["winners_bracket"]:
            return httpx.Response(200, json=season["winners_bracket"])
        assert tail[0] == "matchups", request.url.path
        return httpx.Response(200, json=season["matchups"].get(tail[1], []))

    return SleeperAdapter(client=httpx.Client(transport=httpx.MockTransport(handler)))


def _history(adapter: SleeperAdapter, store: FileStore | None = None, **kwargs: Any) -> History:
    return load_history(CURRENT, adapter.fetch_history_season, store, **kwargs)


def _season(history: History, year: int) -> Any:
    return next(s for s in history.seasons if s.season == year)


def _team(history: History, year: int, roster: str) -> Any:
    return next(t for t in _season(history, year).teams if t.roster_id == roster)


# --- full chain ---------------------------------------------------------------


def test_full_chain_builds_three_complete_seasons() -> None:
    history = _history(_adapter())
    assert history.partial is False and history.reason is None and history.skipped == []
    assert history.seasons_covered == [2022, 2023, 2024]
    assert [s.league_id for s in history.seasons] == ["h2022", "h2023", "h2024"]  # renewal: new id per season
    assert history.model_version == HISTORY_MODEL_VERSION
    assert history.enabled is True


def test_season_game_tagging_and_standings() -> None:
    history = _history(_adapter())
    s22 = _season(history, 2022)
    kinds = [g.kind for g in s22.games]
    assert kinds.count("regular") == 9 and kinds.count("playoff") == 4 and kinds.count("consolation") == 2
    assert {(g.roster_a, g.roster_b) for g in s22.games if g.kind == "consolation"} == {("5", "6")}
    assert s22.champion_roster_id == "4"
    assert all(g.kind == "regular" for g in s22.games if g.week <= 3)
    assert (_team(history, 2022, "6").wins, _team(history, 2022, "6").losses) == (3, 0)
    assert (_team(history, 2022, "1").wins, _team(history, 2022, "1").losses) == (0, 3)
    game = next(g for g in s22.games if g.week == 1 and g.roster_a == "1")
    assert (game.manager_a, game.manager_b, game.winner) == ("u1", "u2", "u2")


# --- owner change -------------------------------------------------------------


def test_owner_change_starts_a_new_manager_record() -> None:
    history = _history(_adapter())
    managers = {m.user_id: m for m in history.managers}
    assert managers["u4"].seasons == [2022, 2023]  # A keeps <= 2023
    assert managers["u7"].seasons == [2024]  # B starts 2024
    assert managers["u4"].current is False and managers["u7"].current is True
    assert _team(history, 2024, "4").owner_id == "u7"
    # games credit the owner of the season, never the roster id
    g24 = next(g for g in _season(history, 2024).games if g.week == 1 and g.roster_a == "3")
    assert (g24.manager_a, g24.manager_b, g24.winner) == ("u3", "u7", "u7")
    g22 = next(g for g in _season(history, 2022).games if g.week == 1 and g.roster_a == "3")
    assert (g22.manager_b, g22.winner) == ("u4", "u4")


def test_current_name_for_current_manager_and_last_seen_for_departed() -> None:
    history = _history(_adapter())
    names = {m.user_id: m.display_name for m in history.managers}
    assert names["u1"] == "Alice Prime"  # current display name
    assert names["u4"] == "Dana"  # departed: last seen
    assert names["u6"] == "Finn"


def test_names_pass_sanitize() -> None:
    history = _history(_adapter())
    assert _season(history, 2022).user_names["u2"] == "Bob"  # the URL is stripped
    assert all("evil" not in (m.display_name or "") for m in history.managers)
    assert _team(history, 2022, "2").team_name == "Team Bob"


# --- co-owned -----------------------------------------------------------------


def test_co_owned_roster_credits_owner_and_lists_co_owners() -> None:
    history = _history(_adapter())
    team = _team(history, 2023, "2")
    assert team.owner_id == "u2" and team.co_owners == ["u8"]
    games = [g for g in _season(history, 2023).games if "2" in (g.roster_a, g.roster_b)]
    credited = {m for g in games for m in (g.manager_a, g.manager_b, g.winner) if m is not None}
    assert "u8" not in credited
    assert all(m.user_id != "u8" for m in history.managers)
    assert _team(history, 2022, "2").co_owners == []  # null co_owners normalizes to []


# --- median rule change -------------------------------------------------------


def test_median_rule_is_per_season_and_standings_only() -> None:
    history = _history(_adapter())
    assert _season(history, 2023).median_rule is True
    assert _season(history, 2024).median_rule is False
    assert _season(history, 2022).median_rule is False
    # 2023: median 135 each regular week -> rosters 4-6 beat it, 1-3 do not
    assert _team(history, 2023, "6").median_wins == 3 and _team(history, 2023, "6").median_losses == 0
    assert _team(history, 2023, "1").median_losses == 3 and _team(history, 2023, "1").median_wins == 0
    for year in (2022, 2024):
        assert all(t.median_wins == 0 and t.median_losses == 0 for t in _season(history, year).teams)
    # head-to-head is untouched by the median: same games and same W-L in 2022 and 2023
    for roster in "123456":
        a, b = _team(history, 2022, roster), _team(history, 2023, roster)
        assert (a.wins, a.losses, a.ties) == (b.wins, b.losses, b.ties)
    assert len(_season(history, 2023).games) == len(_season(history, 2022).games)


# --- playoffs off -------------------------------------------------------------


def test_playoffs_off_season_has_no_playoff_games_or_bracket() -> None:
    history = _history(_adapter())
    s24 = _season(history, 2024)
    assert s24.playoffs is False and s24.playoff_week_start is None and s24.champion_roster_id is None
    assert {g.kind for g in s24.games} == {"regular"}
    assert len(s24.games) == 12  # weeks 1-4, all regular


def test_playoffs_off_season_never_asks_for_a_bracket() -> None:
    requests: list[httpx.Request] = []
    _adapter(requests).fetch_history_season("h2024")
    assert not any(r.url.path.endswith("winners_bracket") for r in requests)


# --- broken chain -------------------------------------------------------------


def test_broken_chain_is_partial_with_seasons_covered_and_reason() -> None:
    history = _history(_adapter(fail={"h2023": 404}))
    assert history.partial is True
    assert history.seasons_covered == [2024]
    assert history.reason is not None and "h2023" in history.reason
    assert [s.league_id for s in history.skipped] == ["h2023", "h2022"]  # nothing guessed past the hole


def test_fetch_error_mid_chain_is_partial_not_raised() -> None:
    history = _history(_adapter(fail={"h2022": 500}))
    assert history.partial is True and history.seasons_covered == [2023, 2024]
    assert "h2022" in (history.reason or "")


def test_chain_cut_by_the_hop_cap_is_partial() -> None:
    bundle = {**CURRENT, "previous_league_ids": ["h2024", "h2023"]}  # h2023 points on to h2022
    history = load_history(bundle, _adapter().fetch_history_season)
    assert history.partial is True and "h2022" in (history.reason or "")
    assert history.seasons_covered == [2023, 2024]


def test_history_failure_never_fails_the_run(tmp_path: Path) -> None:
    class Boom:
        def fetch_history_season(self, league_id: str) -> Any:
            raise AdapterError("boom")

    history = load_weekly_history(Boom(), CURRENT, FileStore(tmp_path))
    assert history.partial is True and history.seasons == []

    class NoMethod:
        pass

    assert load_weekly_history(NoMethod(), CURRENT, FileStore(tmp_path)).enabled is False


def test_malformed_season_body_is_partial() -> None:
    class Bad:
        def fetch_history_season(self, league_id: str) -> Any:
            return {"league": {"league_id": league_id}}  # no users / rosters

    history = load_history(CURRENT, Bad().fetch_history_season)
    assert history.partial is True and history.seasons == []


# --- cache --------------------------------------------------------------------


def test_second_run_makes_zero_history_requests(tmp_path: Path) -> None:
    store = FileStore(tmp_path)
    first_requests: list[httpx.Request] = []
    first = _history(_adapter(first_requests), store)
    assert len(first_requests) == 28  # 3 seasons: 10 + 10 + 8 (see docs/EXTENDING.md)
    assert store.read_cache(HISTORY_NAMESPACE, "h2023") is not None

    second_requests: list[httpx.Request] = []
    adapter = _adapter(second_requests)
    second = _history(adapter, store)
    assert second_requests == [] and adapter.history_calls == 0
    assert second == first


def test_model_version_mismatch_refetches(tmp_path: Path) -> None:
    store = FileStore(tmp_path)
    _history(_adapter(), store)
    entry = store.read_cache(HISTORY_NAMESPACE, "h2023")
    assert entry is not None
    store.write_cache(HISTORY_NAMESPACE, "h2023", {**entry, "history_model_version": HISTORY_MODEL_VERSION + 1})
    requests: list[httpx.Request] = []
    history = _history(_adapter(requests), store)
    assert {r.url.path.split("/")[3] for r in requests} == {"h2023"}  # only the stale season
    assert history.seasons_covered == [2022, 2023, 2024]
    refreshed = store.read_cache(HISTORY_NAMESPACE, "h2023")
    assert refreshed is not None and refreshed["history_model_version"] == HISTORY_MODEL_VERSION


def test_corrupt_cache_entry_is_refetched(tmp_path: Path) -> None:
    store = FileStore(tmp_path)
    store.write_cache(HISTORY_NAMESPACE, "h2024", {"history_model_version": HISTORY_MODEL_VERSION, "season": {"x": 1}})
    history = _history(_adapter(), store)
    assert history.seasons_covered == [2022, 2023, 2024]


def test_unfinished_season_is_never_cached_as_finished(tmp_path: Path) -> None:
    store = FileStore(tmp_path)
    season = json.loads(json.dumps(SEASONS["h2024"]))
    season["league"]["status"] = "in_season"
    bundle = {**CURRENT, "previous_league_ids": ["h2024"]}
    load_history(bundle, lambda _lid: season, store)
    assert store.read_cache(HISTORY_NAMESPACE, "h2024") is None
    assert store.read_cache(HISTORY_NAMESPACE, CURRENT["league"]["league_id"]) is None  # the current season, never


# --- cap / off ----------------------------------------------------------------


def test_season_cap_keeps_the_newest_and_names_the_skipped() -> None:
    requests: list[httpx.Request] = []
    history = _history(_adapter(requests), max_seasons=2)
    assert history.seasons_covered == [2023, 2024]
    assert [(s.league_id, s.reason) for s in history.skipped] == [("h2022", "capped at 2 seasons")]
    assert history.partial is True and history.reason == "capped at 2 seasons"
    assert "h2022" not in {r.url.path.split("/")[3] for r in requests}


def test_history_off_makes_no_request(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    history = _history(_adapter(requests), enabled=False)
    assert requests == [] and history.enabled is False and history.seasons == [] and history.partial is False
    weekly = load_weekly_history(_adapter(requests), CURRENT, FileStore(tmp_path), enabled=False)
    assert requests == [] and weekly.enabled is False


def test_one_season_league_has_empty_complete_history() -> None:
    history = load_history({**CURRENT, "previous_league_ids": []}, _adapter().fetch_history_season)
    assert history.seasons == [] and history.partial is False


# --- unowned roster -----------------------------------------------------------


def test_unowned_roster_is_recorded_and_counts_for_nobody() -> None:
    history = _history(_adapter())
    team = _team(history, 2024, "6")
    assert team.owner_id is None
    assert sorted(m.user_id for m in history.managers) == ["u1", "u2", "u3", "u4", "u5", "u6", "u7"]  # never None
    games = [g for g in _season(history, 2024).games if "6" in (g.roster_a, g.roster_b)]
    assert games and all((g.manager_b if g.roster_b == "6" else g.manager_a) is None for g in games)
    assert all(g.winner is None for g in games if max(g.points_a, g.points_b) == 160.0)  # unowned winner: nobody
    assert _team(history, 2024, "6").wins == 4  # four regular weeks; the roster's own line is kept, owner-less


# --- adapter ------------------------------------------------------------------


def test_adapter_counts_calls_and_reports_a_hole() -> None:
    adapter = _adapter()
    adapter.fetch_history_season("h2022")
    assert adapter.history_calls == 10  # league, users, rosters, weeks 1-6 (6 is the empty probe), bracket
    with pytest.raises(AdapterError):
        _adapter(fail={"h2022": 404}).fetch_history_season("h2022")


def test_build_history_is_pure_over_built_seasons() -> None:
    seasons = [build_season_history(SEASONS[i]) for i in ("h2023", "h2022")]
    history = build_history(seasons, current_bundle=CURRENT, partial=True, reason="why")
    assert history.seasons_covered == [2022, 2023] and history.partial and history.reason == "why"


def test_history_logs_skipped_seasons(caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    adapter = _adapter()
    with caplog.at_level(logging.INFO):
        history = load_weekly_history(adapter, CURRENT, FileStore(tmp_path), max_seasons=1)
    assert history.seasons_covered == [2024]
    assert sum("skipped league" in r.getMessage() for r in caplog.records) == 2
