"""History callbacks: the angles a league talks about, from its own earlier seasons (Story 9B.3).

:func:`compute_history_callbacks` folds a :class:`~commishdesk.ingest.History` (the prior
Sleeper seasons, 9B.2) and this season's games through week ``n`` (from the
:class:`~commishdesk.ingest.WeekModel`) into :class:`HistoryCallbacks`: head-to-head
series, league records and first-time events, every one citing the seasons and weeks it
comes from. It is pure: no file, clock, PRNG or network, and it never mutates its inputs.
The doc is ``docs/concepts/history-callbacks.md``.

**Managers.** Everything is keyed by Sleeper user id (a roster's ``owner_id``). A history game
carries the user id that owned each roster *that season*; this season's owner comes from
``league.teams``. An owner change therefore starts a new record for the new owner and
nothing of the old owner's seasons is ever counted for them. An unowned roster counts for no
one: it is never half of a head-to-head series and never holds a record. Median wins are not
games and never count.

**Seasons on record.** The prior seasons ``History`` carries plus this one. With two or more
the callbacks are ``all_time`` (a partial chain is worded "in N seasons on record"). With
one, only this-season callbacks exist (season head-to-head, season highs): ``all_time`` is
``False`` and no first-time event is emitted.

**Records.** Over regular-season games only: highest and lowest score, biggest margin, longest
win streak and longest loss streak (a manager's streak runs across seasons; a bye neither
extends nor breaks it, a tie breaks it). A record is *set* when this week strictly beats every
earlier game on record, *tied* when it equals the best, and a streak is *within reach* when
the current run is exactly one short of the record. Nothing is "set" with no earlier game on
record, and a streak record needs :data:`MIN_STREAK_RECORD` games to count at all.

**Ranking.** At most one callback per matchup and :data:`MAX_LEAGUE_LINES` league-level lines.
Candidates are ordered by tier (:data:`TIER`: records set or tied, then first-time events,
then a head-to-head streak of at least :data:`MIN_H2H_STREAK`, then a series lead; a league
line ranks within reach after the first-time events), then most recent cited season first,
then roster id (numeric where it parses), then kind and metric. No randomness, no clock.

This module stays inside the ``stats/`` fence: stdlib, pydantic, ``commishdesk.errors``,
``commishdesk.ingest`` and sibling ``stats/`` modules only.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from commishdesk.errors import CommishDeskError
from commishdesk.ingest import History, LeagueModel, Matchup, WeekModel

from .standings import Standings, compute_standings
from .weekly import _sort_key

__all__ = [
    "MAX_LEAGUE_LINES",
    "MIN_H2H_STREAK",
    "MIN_STREAK_RECORD",
    "TIER",
    "HistoryCallback",
    "HistoryCallbacks",
    "HistoryCite",
    "HistoryH2H",
    "HistoryMatchup",
    "HistoryMeeting",
    "HistoryRecord",
    "HistorySeriesStreak",
    "compute_history_callbacks",
]

#: League-level lines kept per Issue.
MAX_LEAGUE_LINES = 3
#: A streak record (or a within-reach line) needs at least this many games.
MIN_STREAK_RECORD = 3
#: A head-to-head streak is a callback from this many straight meetings.
MIN_H2H_STREAK = 3

#: Ranking tier of each callback kind (lower ranks first).
TIER: dict[str, int] = {
    "record_set": 0,
    "record_tied": 0,
    "first_win_ever": 1,
    "first_win_since": 1,
    "first_top_ever": 1,
    "first_top_since": 1,
    "record_within_reach": 2,
    "h2h_streak": 2,
    "series_lead": 3,
}

Metric = Literal["highest_score", "lowest_score", "biggest_margin", "win_streak", "loss_streak"]
_STREAKS: tuple[tuple[str, Metric], ...] = (("W", "win_streak"), ("L", "loss_streak"))
Wording = Literal["all_time", "seasons_on_record", "this_season"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class HistoryCite(_Frozen):
    """One season and the weeks of it a callback draws on."""

    season: int
    weeks: list[int]


class HistoryMeeting(_Frozen):
    """One meeting of a series, points oriented to the matchup's ``a`` / ``b`` rosters."""

    season: int
    week: int
    kind: Literal["regular", "playoff"]
    a_points: float
    b_points: float


class HistorySeriesStreak(_Frozen):
    """``roster_id`` (user ``manager_id``) won the last ``count`` meetings in a row."""

    roster_id: str
    manager_id: str
    count: int


class HistoryH2H(_Frozen):
    """The head-to-head series of one pairing through the issue week. Regular season and
    playoffs are counted apart; ``meetings`` is both together. ``last_meeting`` is the meeting
    before the game in question (never this week's own game)."""

    a_manager_id: str
    b_manager_id: str
    a_wins: int
    b_wins: int
    ties: int
    playoff_a_wins: int
    playoff_b_wins: int
    playoff_ties: int
    meetings: int
    seasons: list[int]
    last_meeting: HistoryMeeting | None
    streak: HistorySeriesStreak | None
    sources: list[HistoryCite]


class HistoryRecord(_Frozen):
    """A league record as it stands through the issue week, and who holds it (the earliest
    holder when tied). ``roster_id`` is the holder's roster this season, ``None`` when the
    holder is not in the league now."""

    metric: Metric
    value: float
    manager_id: str
    roster_id: str | None
    season: int
    week: int
    sources: list[HistoryCite]


class HistoryCallback(_Frozen):
    """One callback: a kind, who it is about, its value and where it comes from. ``record`` is the
    record set, tied or held. ``since_season`` is the season of the last time for a "first
    since". ``tier`` is its ranking tier."""

    kind: str
    tier: int
    all_time: bool
    wording: Wording
    roster_id: str | None = None
    manager_id: str | None = None
    opponent_roster_id: str | None = None
    metric: Metric | None = None
    value: float | None = None
    since_season: int | None = None
    record: HistoryRecord | None = None
    sources: list[HistoryCite]


class HistoryMatchup(_Frozen):
    """One pairing (``a`` is the lower roster id) with its series and its one kept callback.
    ``this_week`` pairings carry the result; ``next_week`` pairings are the week ``n+1`` cards."""

    when: Literal["this_week", "next_week"]
    week: int
    matchup_id: int | None = None
    a_roster_id: str
    b_roster_id: str
    a_points: float | None = None
    b_points: float | None = None
    winner_roster_id: str | None = None
    h2h: HistoryH2H | None = None
    callback: HistoryCallback | None = None


class HistoryCallbacks(_Frozen):
    """The whole block. ``seasons_covered`` includes this season. ``all_time`` is ``False`` with a
    single season on record (``wording`` is then ``this_season``); a partial chain is worded
    ``seasons_on_record``."""

    all_time: bool
    wording: Wording
    seasons_covered: list[int]
    seasons_on_record: int
    partial: bool
    partial_reason: str | None = None
    records: list[HistoryRecord]
    matchups: list[HistoryMatchup]
    league_lines: list[HistoryCallback]


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #

Pos = tuple[int, int]  # (season, week)
_Make = Callable[..., "HistoryCallback"]


@dataclass(frozen=True)
class _Game:
    season: int
    week: int
    kind: Literal["regular", "playoff"]
    ra: str
    rb: str
    ma: str | None
    mb: str | None
    pa: float
    pb: float

    @property
    def pos(self) -> Pos:
        return (self.season, self.week)

    def winner_manager(self) -> str | None:
        if self.pa > self.pb:
            return self.ma
        if self.pb > self.pa:
            return self.mb
        return None

    def is_tie(self) -> bool:
        return self.pa == self.pb


@dataclass(frozen=True)
class _Run:
    """A manager's streak as of one game: ``count`` straight ``kind`` ("W"/"L") ending at ``pos``."""

    manager: str
    kind: str
    count: int
    pos: Pos
    positions: tuple[Pos, ...]


@dataclass
class _Cand:
    callback: HistoryCallback
    rosters: frozenset[str] = field(default_factory=frozenset)


def _cites(positions: Iterable[Pos]) -> list[HistoryCite]:
    by_season: dict[int, set[int]] = {}
    for season, week in positions:
        by_season.setdefault(season, set()).add(week)
    return [HistoryCite(season=s, weeks=sorted(w)) for s, w in sorted(by_season.items())]


def _merge(*groups: Iterable[Pos]) -> list[HistoryCite]:
    return _cites(p for g in groups for p in g)


def _history_games(history: History, season: int) -> list[_Game]:
    out: list[_Game] = []
    for s in history.seasons:
        if s.season is None or s.season == season:
            continue
        for g in s.games:
            if g.kind not in ("regular", "playoff"):
                continue
            out.append(
                _Game(
                    s.season, g.week, g.kind, g.roster_a, g.roster_b, g.manager_a, g.manager_b, g.points_a, g.points_b
                )
            )
    return out


def _current_games(week: WeekModel, league: LeagueModel) -> list[_Game]:
    owner = {t.roster_id: t.owner_id for t in league.teams}
    rows = {(m.week, m.roster_id): m for m in week.matchups}
    start = week.playoff_week_start
    bracket = {frozenset(m.roster_ids) for m in week.winners_bracket}
    out: list[_Game] = []
    for m in week.matchups:
        opp = m.opponent_roster_id
        if m.week > week.week or opp is None or _sort_key(m.roster_id) >= _sort_key(opp):
            continue
        other = rows.get((m.week, opp))
        if other is None:
            continue
        kind: Literal["regular", "playoff"]
        if start is None or m.week < start:
            kind = "regular"
        elif frozenset((m.roster_id, opp)) in bracket:
            kind = "playoff"
        else:
            continue  # a consolation game is never counted
        out.append(
            _Game(
                league.season,
                m.week,
                kind,
                m.roster_id,
                opp,
                owner.get(m.roster_id),
                owner.get(opp),
                m.points,
                other.points,
            )
        )
    return out


def _runs(games: list[_Game]) -> dict[str, list[_Run]]:
    """Every manager's streak after each regular-season game, chronological."""
    per: dict[str, list[tuple[Pos, str]]] = {}
    for g in sorted((g for g in games if g.kind == "regular"), key=lambda g: (g.pos, _sort_key(g.ra))):
        for mgr, mine, theirs in ((g.ma, g.pa, g.pb), (g.mb, g.pb, g.pa)):
            if mgr is not None:
                per.setdefault(mgr, []).append((g.pos, "W" if mine > theirs else "L" if mine < theirs else "T"))
    out: dict[str, list[_Run]] = {}
    for mgr, results in per.items():
        runs: list[_Run] = []
        kind: str = ""
        positions: tuple[Pos, ...] = ()
        for pos, res in results:
            if res == "T":
                kind, positions = "", ()
                runs.append(_Run(mgr, "T", 0, pos, ()))  # a tie ends the streak; never a current run
                continue
            if res == kind:
                positions = (*positions, pos)
            else:
                kind, positions = res, (pos,)
            runs.append(_Run(mgr, kind, len(positions), pos, positions))
        out[mgr] = runs
    return out


def _best_run(runs: Mapping[str, list[_Run]], kind: str, *, before: Pos | None = None) -> _Run | None:
    """The longest ``kind`` streak on record (strictly before *before* when given); the earliest
    to reach that length wins a tie."""
    pool = [r for rs in runs.values() for r in rs if r.kind == kind and (before is None or r.pos < before)]
    if not pool:
        return None
    top = max(r.count for r in pool)
    return min((r for r in pool if r.count == top), key=lambda r: (r.pos, r.manager))


@dataclass(frozen=True)
class _Side:
    points: float
    pos: Pos
    manager: str
    roster: str


def _sides(games: list[_Game]) -> list[_Side]:
    out: list[_Side] = []
    for g in games:
        if g.kind != "regular":
            continue
        if g.ma is not None:
            out.append(_Side(g.pa, g.pos, g.ma, g.ra))
        if g.mb is not None:
            out.append(_Side(g.pb, g.pos, g.mb, g.rb))
    return out


def _margins(games: list[_Game]) -> list[_Side]:
    """The winner's margin of each regular game (points field holds the margin)."""
    out: list[_Side] = []
    for g in games:
        if g.kind != "regular" or g.is_tie():
            continue
        win = g.ma if g.pa > g.pb else g.mb
        if win is not None:
            out.append(_Side(round(abs(g.pa - g.pb), 2), g.pos, win, g.ra if g.pa > g.pb else g.rb))
    return out


def _earliest(pool: list[_Side], value: float) -> _Side:
    return min((s for s in pool if round(s.points, 2) == value), key=lambda s: (s.pos, _sort_key(s.roster)))


def compute_history_callbacks(
    week: WeekModel,
    league: LeagueModel,
    history: History | None,
    *,
    standings: Standings | None = None,
) -> HistoryCallbacks | None:
    """The callbacks for week ``week.week``. ``None`` when *history* is ``None`` or switched off;
    an enabled but empty history yields the one-season shape. *standings* (the issue week's) feeds
    the first-time-on-top line; without it that line is skipped."""
    if history is None or not history.enabled:
        return None
    n = week.week
    season = league.season
    now: Pos = (season, n)
    prior_seasons = sorted({s.season for s in history.seasons if s.season is not None and s.season != season})
    covered = [*prior_seasons, season]
    all_time = len(covered) >= 2
    wording: Wording = "this_season" if not all_time else ("seasons_on_record" if history.partial else "all_time")
    games = sorted(
        _history_games(history, season) + _current_games(week, league), key=lambda g: (g.pos, _sort_key(g.ra))
    )
    through = [g for g in games if g.pos <= now]
    roster_of = _first_roster(league)

    def callback(kind: str, sources: list[HistoryCite], **kw: Any) -> HistoryCallback:
        return HistoryCallback(kind=kind, tier=TIER[kind], all_time=all_time, wording=wording, sources=sources, **kw)

    runs = _runs(through)
    records, events = _records(through, runs, now, roster_of, callback)
    reach = _within_reach(runs, roster_of, callback)

    pairs = _week_pairs(week, league)
    matchups: list[HistoryMatchup] = []
    both: tuple[tuple[Literal["this_week", "next_week"], list[tuple[int | None, str, str]]], ...] = (
        ("this_week", pairs[0]),
        ("next_week", pairs[1]),
    )
    for when, rows in both:
        for matchup_id, a, b in rows:
            matchups.append(
                _matchup(
                    when,
                    n if when == "this_week" else n + 1,
                    matchup_id,
                    a,
                    b,
                    week,
                    league,
                    through,
                    now,
                    events,
                    callback,
                    all_time,
                )
            )

    lines = [*events, *reach, *_first_top(week, league, history, standings, covered, now, all_time, callback)]
    league_lines = sorted((c.callback for c in lines), key=_rank_key)[:MAX_LEAGUE_LINES]

    return HistoryCallbacks(
        all_time=all_time,
        wording=wording,
        seasons_covered=covered,
        seasons_on_record=len(covered),
        partial=history.partial,
        partial_reason=history.reason if history.partial else None,
        records=records,
        matchups=matchups,
        league_lines=league_lines,
    )


def _first_roster(league: LeagueModel) -> dict[str, str]:
    out: dict[str, str] = {}
    for t in sorted(league.teams, key=lambda t: _sort_key(t.roster_id)):
        if t.owner_id and t.owner_id not in out:
            out[t.owner_id] = t.roster_id
    return out


def _rank_key(c: HistoryCallback) -> tuple[object, ...]:
    newest = max((s.season for s in c.sources), default=0)
    return (c.tier, -newest, _sort_key(c.roster_id or ""), c.kind, c.metric or "")


def _week_pairs(
    week: WeekModel, league: LeagueModel
) -> tuple[list[tuple[int | None, str, str]], list[tuple[int | None, str, str]]]:
    def pairs(rows: Iterable[Matchup], at: int | None) -> list[tuple[int | None, str, str]]:
        seen: set[frozenset[str]] = set()
        out: list[tuple[int | None, str, str]] = []
        by_id: dict[int, list[str]] = {}
        for m in rows:
            if at is not None and m.week != at:
                continue
            if m.matchup_id is not None:
                by_id.setdefault(m.matchup_id, []).append(m.roster_id)
            elif m.opponent_roster_id is not None:
                key = frozenset((m.roster_id, m.opponent_roster_id))
                if key not in seen and len(key) == 2:
                    seen.add(key)
                    a, b = sorted(key, key=_sort_key)
                    out.append((None, a, b))
        for mid, ids in by_id.items():
            if len(ids) == 2:
                a, b = sorted(ids, key=_sort_key)
                out.append((mid, a, b))
        return sorted(out, key=lambda p: (_sort_key(p[1]), _sort_key(p[2])))

    return pairs(week.matchups, week.week), pairs(week.next_matchups, None)


def _record_ref(
    metric: Metric,
    value: float,
    mgr: str,
    season: int,
    week: int,
    roster_of: Mapping[str, str],
    sources: list[HistoryCite],
) -> HistoryRecord:
    return HistoryRecord(
        metric=metric,
        value=value,
        manager_id=mgr,
        roster_id=roster_of.get(mgr),
        season=season,
        week=week,
        sources=sources,
    )


def _records(
    through: list[_Game], runs: Mapping[str, list[_Run]], now: Pos, roster_of: Mapping[str, str], callback: _Make
) -> tuple[list[HistoryRecord], list[_Cand]]:
    """The records through *now*, and the "set or tied this week" candidates."""
    records: list[HistoryRecord] = []
    events: list[_Cand] = []
    make = callback

    def ref_side(metric: Metric, side: _Side) -> HistoryRecord:
        return _record_ref(
            metric, round(side.points, 2), side.manager, side.pos[0], side.pos[1], roster_of, _cites([side.pos])
        )

    pools: dict[Metric, list[_Side]] = {
        "highest_score": _sides(through),
        "lowest_score": _sides(through),
        "biggest_margin": _margins(through),
    }
    for metric, pool in pools.items():
        if not pool:
            continue
        pick = min if metric == "lowest_score" else max
        best = pick(round(s.points, 2) for s in pool)
        records.append(ref_side(metric, _earliest(pool, best)))
        before = [s for s in pool if s.pos < now]
        if not before:
            continue
        prior = pick(round(s.points, 2) for s in before)
        prior_ref = ref_side(metric, _earliest(before, prior))
        for s in sorted((s for s in pool if s.pos == now), key=lambda s: _sort_key(s.roster)):
            value = round(s.points, 2)
            beats = value < prior if metric == "lowest_score" else value > prior
            if (beats and value == best) or value == prior:  # only the week's best can be the new record
                events.append(
                    _Cand(
                        make(
                            "record_set" if beats else "record_tied",
                            _merge([s.pos], [(prior_ref.season, prior_ref.week)]),
                            roster_id=s.roster,
                            manager_id=s.manager,
                            metric=metric,
                            value=value,
                            record=prior_ref,
                        ),
                        frozenset({s.roster}),
                    )
                )
    for kind, metric_k in _STREAKS:
        best_run = _best_run(runs, kind)
        if best_run is not None:
            records.append(
                _record_ref(
                    metric_k,
                    float(best_run.count),
                    best_run.manager,
                    best_run.pos[0],
                    best_run.pos[1],
                    roster_of,
                    _cites(best_run.positions),
                )
            )
        prior_run = _best_run(runs, kind, before=now)
        if prior_run is None:
            continue
        prior_ref = _record_ref(
            metric_k,
            float(prior_run.count),
            prior_run.manager,
            prior_run.pos[0],
            prior_run.pos[1],
            roster_of,
            _cites(prior_run.positions),
        )
        now_best = max((r.count for rs in runs.values() for r in rs if r.pos == now and r.kind == kind), default=0)
        for mgr in sorted(runs, key=_sort_key):
            cur = next((r for r in runs[mgr] if r.pos == now and r.kind == kind), None)
            if cur is None or cur.count < MIN_STREAK_RECORD or cur.count < prior_run.count or cur.count < now_best:
                continue
            beats = cur.count > prior_run.count
            events.append(
                _Cand(
                    make(
                        "record_set" if beats else "record_tied",
                        _merge(cur.positions, prior_run.positions),
                        roster_id=roster_of.get(mgr),
                        manager_id=mgr,
                        metric=metric_k,
                        value=float(cur.count),
                        record=prior_ref,
                    ),
                    frozenset({roster_of[mgr]} if mgr in roster_of else set()),
                )
            )
    return records, events


def _within_reach(runs: Mapping[str, list[_Run]], roster_of: Mapping[str, str], callback: _Make) -> list[_Cand]:
    out: list[_Cand] = []
    for kind, metric in _STREAKS:
        top = _best_run(runs, kind)
        if top is None or top.count < MIN_STREAK_RECORD:
            continue
        record = _record_ref(
            metric, float(top.count), top.manager, top.pos[0], top.pos[1], roster_of, _cites(top.positions)
        )
        for mgr in sorted(runs, key=_sort_key):
            if mgr not in roster_of or not runs[mgr]:
                continue
            last = runs[mgr][-1]
            if last.kind == kind and last.count == top.count - 1:
                out.append(
                    _Cand(
                        callback(
                            "record_within_reach",
                            _merge(last.positions, top.positions),
                            roster_id=roster_of[mgr],
                            manager_id=mgr,
                            metric=metric,
                            value=float(last.count),
                            record=record,
                        ),
                        frozenset({roster_of[mgr]}),
                    )
                )
    return out


def _h2h(series: list[_Game], a: str, b: str, ma: str, mb: str, now_last: Pos | None) -> HistoryH2H | None:
    """The series of managers ``ma`` (roster *a*) and ``mb`` (roster *b*); ``None`` with no meeting."""
    if not series:
        return None
    mine = {"regular": [0, 0, 0], "playoff": [0, 0, 0]}
    for g in series:
        a_pts, b_pts = (g.pa, g.pb) if g.ma == ma else (g.pb, g.pa)
        slot = 0 if a_pts > b_pts else 1 if b_pts > a_pts else 2
        mine[g.kind][slot] += 1
    run_mgr, count = None, 0
    for g in reversed(series):
        w = g.winner_manager()
        if w is None or (run_mgr is not None and w != run_mgr):
            break
        run_mgr, count = w, count + 1
    streak = (
        HistorySeriesStreak(roster_id=a if run_mgr == ma else b, manager_id=run_mgr, count=count) if run_mgr else None
    )
    earlier = [g for g in series if now_last is None or g.pos < now_last]
    last = earlier[-1] if earlier else None
    meeting = None
    if last is not None:
        a_pts, b_pts = (last.pa, last.pb) if last.ma == ma else (last.pb, last.pa)
        meeting = HistoryMeeting(season=last.season, week=last.week, kind=last.kind, a_points=a_pts, b_points=b_pts)
    return HistoryH2H(
        a_manager_id=ma,
        b_manager_id=mb,
        a_wins=mine["regular"][0],
        b_wins=mine["regular"][1],
        ties=mine["regular"][2],
        playoff_a_wins=mine["playoff"][0],
        playoff_b_wins=mine["playoff"][1],
        playoff_ties=mine["playoff"][2],
        meetings=len(series),
        seasons=sorted({g.season for g in series}),
        last_meeting=meeting,
        streak=streak,
        sources=_cites(g.pos for g in series),
    )


def _matchup(
    when: Literal["this_week", "next_week"],
    wk: int,
    matchup_id: int | None,
    a: str,
    b: str,
    week: WeekModel,
    league: LeagueModel,
    through: list[_Game],
    now: Pos,
    events: list[_Cand],
    callback: _Make,
    all_time: bool,
) -> HistoryMatchup:
    make = callback
    owner = {t.roster_id: t.owner_id for t in league.teams}
    ma, mb = owner.get(a), owner.get(b)
    a_pts = b_pts = None
    winner = None
    if when == "this_week":
        rows = {m.roster_id: m.points for m in week.matchups if m.week == week.week}
        a_pts, b_pts = rows.get(a), rows.get(b)
        if a_pts is not None and b_pts is not None and a_pts != b_pts:
            winner = a if a_pts > b_pts else b
    h2h = None
    cands: list[HistoryCallback] = []
    if ma is not None and mb is not None and ma != mb:
        series = [g for g in through if {g.ma, g.mb} == {ma, mb}]
        h2h = _h2h(series, a, b, ma, mb, now if when == "this_week" else None)
        this_game = next((g for g in series if g.pos == now), None) if when == "this_week" else None
        if h2h is not None:
            if h2h.streak is not None and h2h.streak.count >= MIN_H2H_STREAK:
                cands.append(
                    make(
                        "h2h_streak",
                        h2h.sources,
                        roster_id=h2h.streak.roster_id,
                        manager_id=h2h.streak.manager_id,
                        opponent_roster_id=b if h2h.streak.roster_id == a else a,
                        value=float(h2h.streak.count),
                    )
                )
            reg = h2h.a_wins + h2h.b_wins + h2h.ties
            if reg >= 2 and h2h.a_wins != h2h.b_wins:
                lead_a = h2h.a_wins > h2h.b_wins
                cands.append(
                    make(
                        "series_lead",
                        h2h.sources,
                        roster_id=a if lead_a else b,
                        manager_id=ma if lead_a else mb,
                        opponent_roster_id=b if lead_a else a,
                        value=float(abs(h2h.a_wins - h2h.b_wins)),
                    )
                )
        if all_time and this_game is not None and not this_game.is_tie():
            win_m = this_game.winner_manager()
            before = [g for g in series if g.pos < now]
            if win_m is not None and before:
                win_r, lose_r = (a, b) if win_m == ma else (b, a)
                wins = [g for g in before if g.winner_manager() == win_m]
                if not wins:
                    cands.append(
                        make(
                            "first_win_ever",
                            _merge(g.pos for g in series),
                            roster_id=win_r,
                            manager_id=win_m,
                            opponent_roster_id=lose_r,
                        )
                    )
                elif wins[-1].season < now[0] and before[-1].winner_manager() != win_m:
                    cands.append(
                        make(
                            "first_win_since",
                            _merge([wins[-1].pos], [now]),
                            roster_id=win_r,
                            manager_id=win_m,
                            opponent_roster_id=lose_r,
                            since_season=wins[-1].season,
                        )
                    )
    cands += [e.callback for e in events if e.rosters & {a, b}] if when == "this_week" else []
    best = min(cands, key=_rank_key) if cands else None
    return HistoryMatchup(
        when=when,
        week=wk,
        matchup_id=matchup_id,
        a_roster_id=a,
        b_roster_id=b,
        a_points=a_pts,
        b_points=b_pts,
        winner_roster_id=winner,
        h2h=h2h,
        callback=best,
    )


def _first_top(
    week: WeekModel,
    league: LeagueModel,
    history: History,
    standings: Standings | None,
    covered: list[int],
    now: Pos,
    all_time: bool,
    callback: _Make,
) -> list[_Cand]:
    """The manager newly first in the standings, when that is a first since an earlier season (or ever)."""
    if not all_time or standings is None or week.week < 2:
        return []
    leader = next((t.roster_id for t in standings.teams if t.rank == 1), None)
    if leader is None:
        return []
    try:
        before = compute_standings(week.model_copy(update={"week": week.week - 1}), league)
    except CommishDeskError:
        return []
    if next((t.roster_id for t in before.teams if t.rank == 1), None) == leader:
        return []
    owner = {t.roster_id: t.owner_id for t in league.teams}.get(leader)
    if owner is None:
        return []
    tops: dict[int, Pos] = {}
    played = False
    for s in sorted(history.seasons, key=lambda s: s.season or 0):
        if s.season is None or s.season == now[0] or not s.teams:
            continue
        if any(t.owner_id == owner for t in s.teams):
            played = True
        ranked = sorted(
            s.teams,
            key=lambda t: (
                -(t.wins + 0.5 * t.ties) / max(1, t.wins + t.losses + t.ties),
                -t.points_for,
                _sort_key(t.roster_id),
            ),
        )
        last_week = max((g.week for g in s.games if g.kind == "regular"), default=0)
        if ranked[0].owner_id == owner:
            tops[s.season] = (s.season, last_week)
    if not played:
        return []
    cites = _merge([now], tops.values())
    if not tops:
        return [_Cand(callback("first_top_ever", cites, roster_id=leader, manager_id=owner), frozenset({leader}))]
    since = max(tops)
    if since == max(s for s in covered if s != now[0]):
        return []
    return [
        _Cand(
            callback("first_top_since", cites, roster_id=leader, manager_id=owner, since_season=since),
            frozenset({leader}),
        )
    ]
