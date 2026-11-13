"""Story 9B.3: history callbacks (``stats/history_callbacks.py``).

One test per I/O & Edge-Case Matrix row. The full-chain rows run on the 9B.2 goldens in
``tests/fixtures/history/`` (six rosters; regular weeks 1-3 pair 1v2 3v4 5v6 / 1v3 2v5 4v6 /
1v4 2v6 3v5; the higher roster id always wins, ``100 + 10 * roster_id``; 2022 has playoffs from
week 4, 2023 median ON, 2024 playoffs off with roster 4 changing owner u4 -> u7 and roster 6
unowned). This season is hand-built on the same shape: roster 6 is owned again by u6.
Rows the goldens cannot reach use a tiny synthetic ``History``.
"""

from __future__ import annotations

import json
from typing import Any

from commishdesk.ingest import (
    Draft,
    History,
    HistoryGame,
    LeagueFormat,
    LeagueModel,
    Matchup,
    PlayoffFormat,
    Roster,
    SeasonHistory,
    Team,
    WeekModel,
)
from commishdesk.stats.history_callbacks import (
    MAX_LEAGUE_LINES,
    HistoryCallbacks,
    compute_history_callbacks,
)
from commishdesk.stats.standings import compute_standings
from tests.test_history_ingest import _adapter, _history

PAIRS = {
    1: [(1, 2), (3, 4), (5, 6)],
    2: [(1, 3), (2, 5), (4, 6)],
    3: [(1, 4), (2, 6), (3, 5)],
    4: [(1, 5), (2, 3), (4, 6)],
    5: [(1, 6), (2, 4), (3, 5)],
}
OWNERS: dict[str, str | None] = {"1": "u1", "2": "u2", "3": "u3", "4": "u7", "5": "u5", "6": "u6"}


def _high_id_wins(week: int, roster: str) -> float:
    return 100.0 + 10 * int(roster)


def _season(
    n: int, score: Any = _high_id_wins, owners: dict[str, str | None] = OWNERS, start: int | None = None
) -> tuple[WeekModel, LeagueModel]:
    league = LeagueModel(
        league_id="h2025",
        name="L",
        season=2025,
        format=LeagueFormat(
            team_count=6,
            roster_slots=["QB"],
            flex_eligibility={},
            scoring_label="PPR",
            is_superflex_or_2qb=False,
            te_premium=False,
            playoff=PlayoffFormat(bracket_teams=4),
        ),
        teams=[Team(roster_id=r, owner_id=o) for r, o in owners.items()],
        picks=[],
        draft=Draft(id="d"),
    )
    matchups = [
        Matchup(week=w, roster_id=str(x), matchup_id=i + 1, opponent_roster_id=str(y), points=score(w, str(x)))
        for w in range(1, n + 1)
        for i, (a, b) in enumerate(PAIRS[w])
        for x, y in ((a, b), (b, a))
    ]
    nxt = [
        Matchup(week=n + 1, roster_id=str(x), matchup_id=i + 1) for i, (a, b) in enumerate(PAIRS[n + 1]) for x in (a, b)
    ]
    week = WeekModel(
        week=n,
        rosters=[Roster(roster_id=r) for r in owners],
        matchups=matchups,
        transactions=[],
        playoff_week_start=start,
        next_matchups=nxt,
    )
    return week, league


def _run(week: WeekModel, league: LeagueModel, history: History | None) -> HistoryCallbacks | None:
    return compute_history_callbacks(week, league, history, standings=compute_standings(week, league))


def _full(n: int = 3, **kw: Any) -> HistoryCallbacks:
    week, league = _season(n, **kw)
    out = _run(week, league, _history(_adapter()))
    assert out is not None
    return out


def _pair(out: HistoryCallbacks, when: str, a: str, b: str) -> Any:
    return next(m for m in out.matchups if (m.when, m.a_roster_id, m.b_roster_id) == (when, a, b))


def _game(
    season: int, week: int, ra: str, rb: str, ma: str | None, mb: str | None, pa: float, pb: float
) -> HistoryGame:
    return HistoryGame(
        season=season,
        week=week,
        kind="regular",
        roster_a=ra,
        roster_b=rb,
        manager_a=ma,
        manager_b=mb,
        points_a=pa,
        points_b=pb,
        winner=ma if pa > pb else mb if pb > pa else None,
    )


def _synthetic(*seasons: tuple[int, list[HistoryGame]]) -> History:
    return History(
        seasons=[SeasonHistory(season=y, league_id=f"x{y}", games=g) for y, g in seasons],
        seasons_covered=[y for y, _ in seasons],
    )


def _one_beats_two(w: int, r: str) -> float:
    return {"1": 130.0, "2": 90.0}.get(r, 100.0 + int(r))


# --- full chain ----------------------------------------------------------------


def test_full_chain_series_last_meeting_and_streak_per_matchup() -> None:
    out = _full()
    assert out.all_time is True and out.wording == "all_time" and out.partial is False
    assert out.seasons_covered == [2022, 2023, 2024, 2025]
    m = _pair(out, "this_week", "3", "5")  # u3 v u5, week 3 of every season: u5 always wins
    h = m.h2h
    assert (h.a_wins, h.b_wins, h.ties, h.meetings) == (0, 4, 0, 4)
    assert h.seasons == [2022, 2023, 2024, 2025]
    assert (h.last_meeting.season, h.last_meeting.week) == (2024, 3)  # the meeting before this week's game
    assert (h.last_meeting.a_points, h.last_meeting.b_points) == (130.0, 150.0)
    assert (h.streak.roster_id, h.streak.count) == ("5", 4)
    assert [(c.season, c.weeks) for c in h.sources] == [(2022, [3]), (2023, [3]), (2024, [3]), (2025, [3])]
    assert m.callback is not None and m.callback.kind == "h2h_streak" and m.callback.value == 4.0
    nxt = _pair(out, "next_week", "2", "3")
    assert nxt.week == 4 and nxt.h2h.last_meeting.season == 2024  # next week: the latest meeting on record
    assert nxt.a_points is None and nxt.winner_roster_id is None


def test_records_and_every_callback_cites_seasons_and_weeks() -> None:
    out = _full()
    by = {r.metric: r for r in out.records}
    assert (by["highest_score"].value, by["highest_score"].season, by["highest_score"].week) == (160.0, 2022, 1)
    assert by["lowest_score"].value == 110.0 and by["biggest_margin"].value == 40.0
    assert by["win_streak"].value == 9.0 and by["win_streak"].manager_id == "u6"  # 2022 + 2023 + this season
    assert {c.season for c in by["win_streak"].sources} == {2022, 2023, 2025}
    callbacks = [*out.league_lines, *(m.callback for m in out.matchups if m.callback)]
    assert callbacks
    for cb in callbacks:
        assert cb.sources and all(c.weeks for c in cb.sources), cb.kind


def test_first_win_since_and_first_win_ever() -> None:
    # 2022: u1 beats u2; 2023 and 2024: u2 beats u1; this week u1 beats u2 -> first win since 2022.
    hist = _synthetic(
        (2022, [_game(2022, 1, "1", "2", "u1", "u2", 300, 20)]),
        (2023, [_game(2023, 1, "1", "2", "u1", "u2", 20, 300)]),
        (2024, [_game(2024, 1, "1", "2", "u1", "u2", 20, 300)]),
    )
    out = _run(*_season(1, score=_one_beats_two), hist)
    assert out is not None
    cb = _pair(out, "this_week", "1", "2").callback
    assert cb.kind == "first_win_since" and cb.since_season == 2022 and cb.roster_id == "1"
    assert {(c.season, tuple(c.weeks)) for c in cb.sources} == {(2022, (1,)), (2025, (1,))}
    # never beaten them before: first win ever
    hist = _synthetic((2024, [_game(2024, 1, "1", "2", "u1", "u2", 20, 300)]))
    out = _run(*_season(1, score=_one_beats_two), hist)
    assert out is not None
    assert _pair(out, "this_week", "1", "2").callback.kind == "first_win_ever"


def test_first_time_on_top_since_a_season_and_ever() -> None:
    # u1 finished first in 2022 only (u2 in 2023 and 2024); roster 1 tops the table after week 2, roster 6 after week 1.
    from commishdesk.ingest import HistoryTeam

    def season(year: int, top_owner: str) -> SeasonHistory:
        teams = [
            HistoryTeam(
                roster_id="1",
                owner_id="u1",
                wins=9 if top_owner == "u1" else 1,
                losses=1 if top_owner == "u1" else 9,
                points_for=500,
            ),
            HistoryTeam(
                roster_id="3",
                owner_id="u3",
                wins=9 if top_owner == "u3" else 1,
                losses=1 if top_owner == "u3" else 9,
                points_for=400,
            ),
            HistoryTeam(roster_id="2", owner_id="u2", wins=5, losses=5, points_for=450),
        ]
        return SeasonHistory(
            season=year,
            league_id=f"x{year}",
            teams=teams,
            games=[_game(year, 1, "1", "2", "u1", "u2", *((500, 0) if year == 2022 else (0, 500)))],
        )

    hist = History(
        seasons=[season(2022, "u1"), season(2023, "u2"), season(2024, "u2")], seasons_covered=[2022, 2023, 2024]
    )

    def score(w: int, r: str) -> float:  # week 1: roster 6 leads the table; week 2: roster 1 goes 2-0 and tops it
        if w == 1:
            return {"1": 130.0, "2": 100.0, "3": 100.0, "4": 140.0, "5": 100.0, "6": 190.0}[r]
        return {"1": 150.0}.get(r, 90.0)

    out = _run(*_season(2, score=score), hist)
    assert out is not None
    top = [c for c in out.league_lines if c.kind.startswith("first_top")]
    assert top and top[0].kind == "first_top_since" and top[0].since_season == 2022 and top[0].roster_id == "1"
    assert {c.season for c in top[0].sources} == {2022, 2025}
    # one season on record: never emitted
    out = _run(*_season(2, score=score), History())
    assert out is not None and not [c for c in out.league_lines if c.kind.startswith("first_top")]


def test_first_time_on_top_ever_needs_an_earlier_season_in_the_league() -> None:
    from commishdesk.ingest import HistoryTeam

    def season(year: int, owner: str) -> SeasonHistory:
        teams = [
            HistoryTeam(roster_id="1", owner_id="u1", wins=1, losses=9, points_for=500),
            HistoryTeam(roster_id="2", owner_id=owner, wins=9, losses=1, points_for=400),
        ]
        return SeasonHistory(
            season=year, league_id=f"x{year}", teams=teams, games=[_game(year, 1, "1", "2", "u1", "u2", 500, 0)]
        )

    def score(w: int, r: str) -> float:
        if w == 1:
            return {"1": 130.0, "2": 100.0, "3": 100.0, "4": 140.0, "5": 100.0, "6": 190.0}[r]
        return {"1": 150.0}.get(r, 90.0)

    hist = History(seasons=[season(2023, "u2"), season(2024, "u2")], seasons_covered=[2023, 2024])
    out = _run(*_season(2, score=score), hist)
    assert out is not None
    top = [c for c in out.league_lines if c.kind.startswith("first_top")]
    assert top and top[0].kind == "first_top_ever" and top[0].since_season is None and top[0].roster_id == "1"
    # a manager new to the league this season has no "ever" to speak of
    hist = History(seasons=[season(2023, "u2")], seasons_covered=[2023])
    owners: dict[str, str | None] = {**OWNERS, "1": "u99"}
    out = _run(*_season(2, score=score, owners=owners), hist)
    assert out is not None and not [c for c in out.league_lines if c.kind.startswith("first_top")]


# --- one season / partial / owner change / playoffs ----------------------------------


def test_one_season_has_season_callbacks_only_and_never_says_all_time() -> None:
    week, league = _season(3)
    out = _run(week, league, History())  # enabled, nothing earlier
    assert out is not None
    assert out.all_time is False and out.wording == "this_season" and out.seasons_on_record == 1
    assert out.seasons_covered == [2025]
    text = json.dumps(out.model_dump()).lower()
    assert "all-time" not in text and '"all_time": true' not in text and '"wording": "all_time"' not in text
    for m in out.matchups:
        if m.h2h is not None:
            assert m.h2h.seasons == [2025]
        if m.callback is not None:
            assert m.callback.all_time is False
            assert not m.callback.kind.startswith("first_")
    assert not any(c.kind.startswith("first_") for c in out.league_lines)
    # a pairing that has not met this season has no series (never "0-0")
    assert _pair(out, "next_week", "1", "5").h2h is None


def test_partial_chain_is_worded_in_n_seasons_on_record() -> None:
    week, league = _season(3)
    hist = _history(_adapter()).model_copy(update={"partial": True, "reason": "failed to load league h2021"})
    out = _run(week, league, hist)
    assert out is not None
    assert out.partial is True and out.partial_reason == "failed to load league h2021"
    assert out.wording == "seasons_on_record" and out.all_time is True and out.seasons_on_record == 4
    assert all(m.callback.wording == "seasons_on_record" for m in out.matchups if m.callback)


def test_owner_change_never_credits_the_new_owner_with_the_old_owners_games() -> None:
    out = _full()
    # Roster 4 was u4 in 2022-2023 and u7 in 2024. u7's series starts in 2024.
    h = _pair(out, "this_week", "1", "4").h2h
    assert (h.a_manager_id, h.b_manager_id) == ("u1", "u7")
    assert h.seasons == [2024, 2025]
    assert (h.a_wins, h.b_wins) == (0, 3)  # not 2022 / 2023 (those were u4's)
    other = _pair(out, "next_week", "4", "6").h2h  # u7 v u6: they only meet this season
    assert other.seasons == [2025] and other.meetings == 1


def test_unowned_rosters_count_for_no_one() -> None:
    owners: dict[str, str | None] = {**OWNERS, "6": None}
    week, league = _season(3, owners=owners)
    out = _run(week, league, _history(_adapter()))
    assert out is not None
    assert _pair(out, "this_week", "2", "6").h2h is None
    assert all(r.roster_id != "6" for r in out.records if r.metric == "highest_score")


def test_playoff_meetings_count_in_playoff_h2h_only() -> None:
    out = _full()
    h = _pair(out, "next_week", "2", "3").h2h  # 2022 and 2023 playoff week 4, 2024 regular week 4
    assert (h.a_wins, h.b_wins, h.ties) == (0, 1, 0)
    assert (h.playoff_a_wins, h.playoff_b_wins, h.playoff_ties) == (0, 2, 0)
    assert h.meetings == 3


def test_a_game_outside_the_bracket_this_season_is_never_counted() -> None:
    week, league = _season(4, start=4)  # week 4 is a playoff week and no bracket is given
    out = _run(week, league, History())
    assert out is not None
    sources = [c for m in out.matchups if m.h2h for c in m.h2h.sources]
    assert sources and all(4 not in c.weeks for c in sources)


# --- records -----------------------------------------------------------------------


def test_a_score_equal_to_the_record_is_tied_and_cites_the_record() -> None:
    out = _full()
    tied = [c for c in out.league_lines if c.kind == "record_tied"]
    assert tied and tied[0].metric == "lowest_score" and tied[0].value == 110.0
    assert (tied[0].record.season, tied[0].record.week) == (2022, 1)
    assert {(c.season, tuple(c.weeks)) for c in tied[0].sources} == {(2022, (1,)), (2025, (3,))}


def test_a_score_beating_every_earlier_game_is_set() -> None:
    week, league = _season(3, score=lambda w, r: 999.0 if (w, r) == (3, "2") else 100.0 + 10 * int(r))
    out = _run(week, league, _history(_adapter()))
    assert out is not None
    cb = _pair(out, "this_week", "2", "6").callback
    assert cb.kind == "record_set" and cb.tier == 0 and cb.record is not None
    assert cb.record.season == 2022 and cb.sources[-1].season == 2025
    assert {r.metric: r.value for r in out.records}["highest_score"] == 999.0


def test_a_streak_one_short_of_the_record_is_within_reach() -> None:
    # u1 won weeks 1-4 of 2024 (a 4-game record); u3 has won 3 straight this season.
    prior = [_game(2024, w, "1", "9", "u1", "u9", 900, 10) for w in (1, 2, 3, 4)]
    hist = _synthetic((2024, prior))

    def score(w: int, r: str) -> float:
        return {"3": 200.0, "1": 50.0}.get(r, 100.0 + int(r))

    week, league = _season(3, score=score)
    out = _run(week, league, hist)
    assert out is not None
    reach = [c for c in out.league_lines if c.kind == "record_within_reach" and c.metric == "win_streak"]
    assert reach and reach[0].roster_id == "3" and reach[0].value == 3.0
    assert reach[0].record.value == 4.0 and reach[0].record.manager_id == "u1" and reach[0].record.season == 2024


def test_a_bracket_game_this_season_counts_as_a_playoff_meeting_only() -> None:
    from commishdesk.ingest import BracketMatch

    week, league = _season(4, start=4)
    week = week.model_copy(update={"winners_bracket": [BracketMatch(round=1, roster_ids=["2", "3"])]})
    out = _run(week, league, History())
    assert out is not None
    h = _pair(out, "this_week", "2", "3").h2h
    assert h is not None and h.meetings >= 1
    assert h.playoff_a_wins + h.playoff_b_wins == 1
    assert (h.a_wins, h.b_wins) != (0, 0) or h.meetings == 1
    assert any(4 in c.weeks for c in h.sources)


def _streak_events(out: HistoryCallbacks, roster: str, metric: str) -> list[Any]:
    return [
        c
        for c in out.league_lines
        if c.kind in ("record_set", "record_tied") and c.metric == metric and c.roster_id == roster
    ]



def test_a_win_streak_beating_or_equalling_the_record_is_set_or_tied() -> None:
    hist = _synthetic(*[(2024, [_game(2024, w, "1", "9", "u1", "u9", 900, 10) for w in (1, 2, 3)])])

    def score(w: int, r: str) -> float:
        return {"3": 500.0, "1": 50.0}.get(r, 100.0 + int(r))

    week, league = _season(3, score=score)
    tied = _streak_events(_run(week, league, hist), "3", "win_streak")  # type: ignore[arg-type]
    assert tied and tied[0].kind == "record_tied" and tied[0].value == 3.0 and tied[0].record.value == 3.0
    week, league = _season(4, score=score)
    won = _streak_events(_run(week, league, hist), "3", "win_streak")  # type: ignore[arg-type]
    assert won and won[0].kind == "record_set" and won[0].value == 4.0


def test_a_loss_streak_beating_or_equalling_the_record_is_set_or_tied() -> None:
    hist = _synthetic(
        (
            2024,
            [
                *[_game(2024, w, "9", "1", "u9", "u1", 300, 10) for w in (1, 2, 3)],
                _game(2024, 4, "9", "1", "u9", "u1", 10, 300),
            ],
        )
    )

    def score(w: int, r: str) -> float:
        return {"1": 50.0}.get(r, 100.0 + int(r))

    week, league = _season(3, score=score)
    tied = _streak_events(_run(week, league, hist), "1", "loss_streak")  # type: ignore[arg-type]
    assert tied and tied[0].kind == "record_tied" and tied[0].value == 3.0
    week, league = _season(4, score=score)
    lost = _streak_events(_run(week, league, hist), "1", "loss_streak")  # type: ignore[arg-type]
    assert lost and lost[0].kind == "record_set" and lost[0].value == 4.0


def test_a_tie_breaks_a_streak_so_it_is_never_within_reach() -> None:
    prior = [_game(2024, w, "1", "9", "u1", "u9", 900, 10) for w in (1, 2, 3, 4)]
    hist = _synthetic((2024, prior))

    def score(w: int, r: str) -> float:
        if r in ("3", "2") and w == 4:
            return 200.0  # roster 3 ties roster 2 in week 4, ending its 3-game run
        return {"3": 200.0, "1": 50.0}.get(r, 100.0 + int(r))

    week, league = _season(4, score=score)
    out = _run(week, league, hist)
    assert out is not None
    assert not [c for c in out.league_lines if c.kind == "record_within_reach" and c.roster_id == "3"]


def test_no_record_is_set_with_nothing_earlier_on_record() -> None:
    week, league = _season(1)
    out = _run(week, league, History())
    assert out is not None
    assert not [c for c in out.league_lines if c.kind in ("record_set", "record_tied")]


# --- ranking, determinism, absence ---------------------------------------------------


def test_many_candidates_keep_one_per_matchup_and_three_league_lines_ranked() -> None:
    out = _full()
    assert len(out.league_lines) <= MAX_LEAGUE_LINES == 3
    tiers = [c.tier for c in out.league_lines]
    assert tiers == sorted(tiers)
    assert len([m for m in out.matchups if m.when == "this_week"]) == 3
    # the matchup callback is the best-ranked candidate: a record set beats the h2h streak
    assert _pair(out, "this_week", "1", "4").callback.tier == 0
    assert len({(m.when, m.a_roster_id, m.b_roster_id) for m in out.matchups}) == len(out.matchups)


def test_same_input_gives_the_same_output_and_inputs_are_not_mutated() -> None:
    week, league = _season(3)
    hist = _history(_adapter())
    before = (week.model_dump(), league.model_dump(), hist.model_dump())
    first = _run(week, league, hist)
    second = _run(week, league, hist)
    assert first is not None and second is not None
    assert first == second and first.model_dump_json() == second.model_dump_json()
    assert (week.model_dump(), league.model_dump(), hist.model_dump()) == before


def test_no_history_or_a_disabled_one_gives_no_block() -> None:
    week, league = _season(3)
    assert _run(week, league, None) is None
    assert _run(week, league, History(enabled=False)) is None
