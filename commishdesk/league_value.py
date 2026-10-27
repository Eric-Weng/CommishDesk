"""League Value: every player valued in the league's own scoring and lineup (Story 9.6a, AR-71).

:func:`league_value_for_week` builds -- or reads from the :class:`~commishdesk.store.Store` -- one
frozen *snapshot* per league, season, week and method version. The snapshot is a plain, JSON-safe
mapping, so ``stats/`` can read it without importing this module (this module imports ``httpx``;
``stats/`` and ``facts/`` never may, AD-1). It sits beside ``consensus.py``, outside the pure
facts builder, and is **not** part of the ``stats/`` pipeline fence.

How a value is made (the long form is ``docs/concepts/league-value.md``):

1. **Points per week.** Any week a player was rostered in this league, his Sleeper
   ``players_points`` is the number. Any other week, his nflverse weekly stats are scored with the
   league's own ``scoring_settings``. K, DEF and IDP come from ``players_points`` only; a player
   with none is unvalued (absent from the snapshot), never zero.
2. **Blend.** ``(sum this season + K * last-season ppg) / (games + K)``, ``K = 4``; last season
   counts only with 4 or more games and comes from nflverse only.
3. **Replacement.** A greedy fill of every team's starting slots (FLEX and SUPER_FLEX included,
   the ``stats/lineup.py`` eligibility rules); the last starter at a position is replacement.
   ``value = max(0, ppg - replacement)``, in points per week.
4. **Rookies** (under 2 games, no qualifying prior) take the value of the player at the same
   positional rank by Sleeper ``search_rank`` (``est``).
5. **Future** (dynasty only): this value carried over the next seasons with a per-position age
   curve (``est``). **Picks**: the rookie at the projected overall slot, tiered by the original
   owner's standing for next season, discounted after that (``est``).

External data is numbers and ids only: nflverse names are used for the join and kept only in the
private cache, never in a snapshot.

Fetching is best-effort. An nflverse failure yields a ``sleeper_only`` snapshot, labelled; the
snapshot build never raises into the Issue.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, TypeGuard
from zlib import error as ZlibError

import httpx

from commishdesk import __version__
from commishdesk.errors import CommishDeskError, StoreError
from commishdesk.ingest import LeagueModel
from commishdesk.store import Store

__all__ = [
    "METHOD_VERSION",
    "SOURCE_ENV_VAR",
    "league_value_for_week",
    "pick_tier",
    "pick_value",
    "snapshot_cache_key",
]

#: Bump when any constant or rule below changes the numbers (the snapshot key carries it).
METHOD_VERSION = 1

# --------------------------------------------------------------------------- #
# Named constants (documented in docs/concepts/league-value.md)
# --------------------------------------------------------------------------- #

#: Games of last-season prior blended into this season's average (K).
PRIOR_GAMES_K = 4
#: Last season counts only with at least this many games.
PRIOR_MIN_GAMES = 4
#: A player under this many games this season, with no qualifying prior, is a rookie (rank value).
ROOKIE_GAMES_THRESHOLD = 2
#: Dynasty Future value: how many seasons beyond this one are carried.
FUTURE_HORIZON = 3
#: Dynasty Future value: yearly discount on each carried season.
FUTURE_DISCOUNT = 0.85
#: Picks two or more seasons out lose this share of value per season beyond next season's.
PICK_LATER_DISCOUNT = 0.8
#: A pick slot deeper than the rookie pool shrinks by this factor per slot beyond the pool.
PICK_TAIL_DECAY = 0.9
#: Draft rounds when the league does not declare them.
DEFAULT_DRAFT_ROUNDS = 4
#: Year-over-year production factor by position: ``(age below, factor)``, first match wins.
AGE_CURVES: dict[str, tuple[tuple[int, float], ...]] = {
    "QB": ((24, 1.08), (30, 1.0), (34, 0.95), (99, 0.8)),
    "RB": ((22, 1.05), (24, 1.0), (26, 0.93), (28, 0.82), (99, 0.65)),
    "WR": ((23, 1.07), (26, 1.02), (29, 0.96), (31, 0.88), (99, 0.75)),
    "TE": ((24, 1.06), (28, 1.0), (31, 0.93), (99, 0.8)),
}

#: Positions nflverse stats can value. Everything else (K, DEF, IDP) is ``players_points`` only.
OFFENSE_POSITIONS = ("QB", "RB", "WR", "TE")

#: Environment switch: ``nflverse`` (default), ``sleeper_only`` (the sample container), or ``off``.
SOURCE_ENV_VAR = "COMMISHDESK_LEAGUE_VALUE_SOURCE"
_SOURCES = ("nflverse", "sleeper_only", "off")

_NAMESPACE = "league_value"
_CONTACT_URL = "https://github.com/Eric-Weng/CommishDesk"
_HEADERS = {"User-Agent": f"CommishDesk/{__version__} (+{_CONTACT_URL})"}
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

#: nflverse release assets (CC BY 4.0). The gzip variants keep the download to a few MB.
NFLVERSE_STATS_URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.csv.gz"
NFLVERSE_PLAYERS_URL = "https://github.com/nflverse/nflverse-data/releases/download/players/players.csv.gz"

_FETCH_FAULTS = (httpx.HTTPError, RuntimeError, ValueError, OSError, EOFError, ZlibError, csv.Error, KeyError)

_log = logging.getLogger("commishdesk.league_value")

# --------------------------------------------------------------------------- #
# nflverse columns and the Sleeper scoring keys they can express
# --------------------------------------------------------------------------- #

_COLS = (
    "completions", "attempts", "passing_yards", "passing_tds", "passing_interceptions",
    "sacks_suffered", "sack_fumbles", "sack_fumbles_lost", "passing_2pt_conversions",
    "passing_first_downs", "carries", "rushing_yards", "rushing_tds", "rushing_fumbles",
    "rushing_fumbles_lost", "rushing_first_downs", "rushing_2pt_conversions", "receptions",
    "targets", "receiving_yards", "receiving_tds", "receiving_fumbles", "receiving_fumbles_lost",
    "receiving_first_downs", "receiving_2pt_conversions", "special_teams_tds",
    "punt_return_yards", "kickoff_return_yards",
)  # fmt: skip
_COL_INDEX = {name: i for i, name in enumerate(_COLS)}

#: Sleeper scoring key -> ((nflverse column, multiplier), ...), a plain linear term.
_LINEAR: dict[str, tuple[tuple[str, float], ...]] = {
    "pass_yd": (("passing_yards", 1.0),),
    "pass_td": (("passing_tds", 1.0),),
    "pass_int": (("passing_interceptions", 1.0),),
    "pass_2pt": (("passing_2pt_conversions", 1.0),),
    "pass_cmp": (("completions", 1.0),),
    "pass_att": (("attempts", 1.0),),
    "pass_inc": (("attempts", 1.0), ("completions", -1.0)),
    "pass_sack": (("sacks_suffered", 1.0),),
    "pass_fd": (("passing_first_downs", 1.0),),
    "rush_yd": (("rushing_yards", 1.0),),
    "rush_td": (("rushing_tds", 1.0),),
    "rush_att": (("carries", 1.0),),
    "rush_2pt": (("rushing_2pt_conversions", 1.0),),
    "rush_fd": (("rushing_first_downs", 1.0),),
    "rec": (("receptions", 1.0),),
    "rec_yd": (("receiving_yards", 1.0),),
    "rec_td": (("receiving_tds", 1.0),),
    "rec_2pt": (("receiving_2pt_conversions", 1.0),),
    "rec_tgt": (("targets", 1.0),),
    "rec_fd": (("receiving_first_downs", 1.0),),
    "fum": (("rushing_fumbles", 1.0), ("receiving_fumbles", 1.0), ("sack_fumbles", 1.0)),
    "fum_lost": (("rushing_fumbles_lost", 1.0), ("receiving_fumbles_lost", 1.0), ("sack_fumbles_lost", 1.0)),
    "st_td": (("special_teams_tds", 1.0),),
    "pr_yd": (("punt_return_yards", 1.0),),
    "kr_yd": (("kickoff_return_yards", 1.0),),
}
#: Per-game threshold bonuses: key -> (column, threshold).
_THRESHOLD: dict[str, tuple[str, float]] = {
    "bonus_pass_yd_300": ("passing_yards", 300.0),
    "bonus_pass_yd_400": ("passing_yards", 400.0),
    "bonus_rush_yd_100": ("rushing_yards", 100.0),
    "bonus_rush_yd_200": ("rushing_yards", 200.0),
    "bonus_rec_yd_100": ("receiving_yards", 100.0),
    "bonus_rec_yd_200": ("receiving_yards", 200.0),
    "bonus_pass_cmp_25": ("completions", 25.0),
    "bonus_rush_att_20": ("carries", 20.0),
}
#: Per-reception bonuses by position (TE premium and its RB / WR cousins).
_POSITION_REC_BONUS = {"bonus_rec_te": "TE", "bonus_rec_rb": "RB", "bonus_rec_wr": "WR"}
_EXPRESSIBLE = frozenset(_LINEAR) | frozenset(_THRESHOLD) | frozenset(_POSITION_REC_BONUS)
#: Scoring-key prefixes that belong to an offensive player; a nonzero key here that nflverse cannot
#: express (play-length bonuses and the like) is listed in the snapshot as a documented gap.
_OFFENSE_PREFIXES = ("pass_", "rush_", "rec_", "bonus_pass", "bonus_rush", "bonus_rec", "bonus_fd")


def _gap_keys(scoring: Mapping[str, Any]) -> list[str]:
    gaps = []
    for key, weight in scoring.items():
        if key in _EXPRESSIBLE or not _num(weight) or weight == 0:
            continue
        if key.startswith(_OFFENSE_PREFIXES):
            gaps.append(key)
    return sorted(gaps)


def _num(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _scorer(scoring: Mapping[str, Any]) -> Any:
    """Return ``score(values, pos)`` for one nflverse row (a list in :data:`_COLS` order)."""
    linear: list[tuple[int, float]] = []
    for key, terms in _LINEAR.items():
        weight = scoring.get(key)
        if _num(weight) and weight != 0:
            linear.extend((_COL_INDEX[col], float(weight) * mult) for col, mult in terms)
    thresholds = [
        (_COL_INDEX[col], limit, float(scoring[key]))
        for key, (col, limit) in _THRESHOLD.items()
        if _num(scoring.get(key)) and scoring[key] != 0
    ]
    rec_bonus = {
        pos: float(scoring[key])
        for key, pos in _POSITION_REC_BONUS.items()
        if _num(scoring.get(key)) and scoring[key] != 0
    }
    receptions = _COL_INDEX["receptions"]

    def score(values: Sequence[float], pos: str) -> float:
        total = 0.0
        for index, coef in linear:
            total += coef * values[index]
        for index, limit, weight in thresholds:
            if values[index] >= limit:
                total += weight
        bonus = rec_bonus.get(pos)
        if bonus is not None:
            total += bonus * values[receptions]
        return total

    return score


# --------------------------------------------------------------------------- #
# nflverse fetch + reduce (stdlib csv/gzip)
# --------------------------------------------------------------------------- #


@dataclass
class _Nflverse:
    """Reduced nflverse data: weekly stat rows by gsis id, last season's, and the id table."""

    current: dict[str, dict[int, list[float]]]
    prior: dict[str, dict[int, list[float]]]
    players: list[list[str]]  # [gsis, espn, birth_date, display_name, position]
    fetched: int = 0


def _csv_rows(content: bytes) -> Any:
    with gzip.GzipFile(fileobj=io.BytesIO(content)) as handle:
        text = io.TextIOWrapper(handle, encoding="utf-8", newline="")
        yield from csv.DictReader(text)


def _cell(value: str | None) -> float:
    """A stat cell as a number: blank, ``NA`` and non-finite cells count as 0."""
    try:
        number = float(value or 0)
    except ValueError:
        return 0.0
    return number if math.isfinite(number) else 0.0


def _reduce_stats(content: bytes, through_week: int) -> dict[str, Any]:
    """Regular-season offensive rows up to ``through_week`` as ``{gsis: {week: [cols]}}``."""
    rows: dict[str, dict[str, list[float]]] = {}
    checked = False
    for row in _csv_rows(content):
        if not checked:
            missing = [col for col in (*_COLS, "player_id", "week", "season_type", "position") if col not in row]
            if missing:
                raise ValueError(f"nflverse stats file lacks columns: {missing[:3]}")
            checked = True
        if row["season_type"] != "REG" or row["position"] not in OFFENSE_POSITIONS:
            continue
        week = int(row["week"])
        if week > through_week or not row["player_id"]:
            continue
        rows.setdefault(row["player_id"], {})[str(week)] = [_cell(row[col]) for col in _COLS]
    return {"through_week": through_week, "rows": rows}


def _reduce_players(content: bytes, season: int) -> dict[str, Any]:
    out: list[list[str]] = []
    for row in _csv_rows(content):
        if row.get("position") not in OFFENSE_POSITIONS or not row.get("gsis_id"):
            continue
        last = row.get("last_season") or ""
        if last and int(last) < season - 2:
            continue
        out.append(
            [
                row["gsis_id"],
                row.get("espn_id") or "",
                row.get("birth_date") or "",
                row.get("display_name") or "",
                row["position"],
            ]
        )
    out.sort()
    return {"players": out}


def _unpack_stats(payload: Mapping[str, Any]) -> dict[str, dict[int, list[float]]]:
    return {gsis: {int(wk): list(vals) for wk, vals in weeks.items()} for gsis, weeks in payload["rows"].items()}


def _cache_read(store: Store, key: str) -> dict[str, Any] | None:
    try:
        return store.read_cache(_NAMESPACE, key)
    except StoreError:
        return None


def _cache_write(store: Store, key: str, value: Mapping[str, Any]) -> None:
    try:
        store.write_cache(_NAMESPACE, key, value)
    except StoreError:
        pass


def _download(client: httpx.Client, url: str) -> bytes:
    response = client.get(url, headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
    response.raise_for_status()
    return response.content


def _load_nflverse(store: Store, client: httpx.Client, season: int, week: int) -> _Nflverse:
    """Players table, this season's stats through ``week``, last season's stats. Each is fetched at
    most once and cached (the players table and the current season's stats keyed by season-week, last
    season's by its season); a warm cache issues no request. Raises on any fetch/parse fault."""
    fetched = 0

    def get(key: str, url: str, reduce: Any) -> dict[str, Any]:
        nonlocal fetched
        cached = _cache_read(store, key)
        if cached is not None:
            return cached
        payload: dict[str, Any] = reduce(_download(client, url))
        fetched += 1
        _cache_write(store, key, payload)
        return payload

    players = get(f"players-{season}-w{week}", NFLVERSE_PLAYERS_URL, lambda b: _reduce_players(b, season))
    current_url = NFLVERSE_STATS_URL.format(season=season)
    current = get(f"stats-{season}-w{week}", current_url, lambda b: _reduce_stats(b, week))
    prior_url = NFLVERSE_STATS_URL.format(season=season - 1)
    prior = get(f"stats-{season - 1}-final", prior_url, lambda b: _reduce_stats(b, 30))
    return _Nflverse(_unpack_stats(current), _unpack_stats(prior), players["players"], fetched)


# --------------------------------------------------------------------------- #
# Id join
# --------------------------------------------------------------------------- #

_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b\.?$")


def _norm_name(name: str) -> str:
    text = re.sub(r"[^a-z0-9 ]", "", name.lower().replace(".", "").replace("'", ""))
    text = _SUFFIX.sub("", text.strip()).strip()
    return re.sub(r"\s+", " ", text)


def _sleeper_name(record: Mapping[str, Any]) -> str:
    full = record.get("full_name")
    if isinstance(full, str) and full.strip():
        return full
    return f"{record.get('first_name') or ''} {record.get('last_name') or ''}".strip()


class _Joiner:
    """Sleeper record -> gsis id: gsis_id, else espn_id, else name + birth date, else unique name + position."""

    def __init__(self, nv: _Nflverse) -> None:
        self.known = set(nv.current) | set(nv.prior)
        self.by_espn: dict[str, str] = {}
        by_name_birth: dict[tuple[str, str], set[str]] = {}
        by_name_pos: dict[tuple[str, str], set[str]] = {}
        for gsis, espn, birth, name, pos in nv.players:
            self.known.add(gsis)
            if espn:
                self.by_espn[espn] = gsis
            norm = _norm_name(name)
            if birth:
                by_name_birth.setdefault((norm, birth), set()).add(gsis)
            by_name_pos.setdefault((norm, pos), set()).add(gsis)
        self.by_name_birth = {k: next(iter(v)) for k, v in by_name_birth.items() if len(v) == 1}
        self.by_name_pos = {k: next(iter(v)) for k, v in by_name_pos.items() if len(v) == 1}

    def match(self, record: Mapping[str, Any], pos: str) -> str | None:
        gsis = record.get("gsis_id")
        if isinstance(gsis, str) and gsis in self.known:
            return gsis
        espn = record.get("espn_id")
        if espn not in (None, "") and str(espn) in self.by_espn:
            return self.by_espn[str(espn)]
        norm = _norm_name(_sleeper_name(record))
        birth = record.get("birth_date")
        if norm and isinstance(birth, str) and (norm, birth) in self.by_name_birth:
            return self.by_name_birth[(norm, birth)]
        return self.by_name_pos.get((norm, pos)) if norm else None


# --------------------------------------------------------------------------- #
# Snapshot build (pure: no network, no clock)
# --------------------------------------------------------------------------- #


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _round(value: float) -> float:
    return round(value, 3)


def _player_pos(record: Mapping[str, Any]) -> str | None:
    pos = record.get("position")
    if isinstance(pos, str) and pos:
        return pos
    fantasy = record.get("fantasy_positions")
    if isinstance(fantasy, list) and fantasy and isinstance(fantasy[0], str):
        return fantasy[0]
    return None


def _age(record: Mapping[str, Any], season: int) -> int | None:
    """Whole years on 1 September of ``season`` (deterministic: no clock)."""
    birth = record.get("birth_date")
    if isinstance(birth, str):
        try:
            born = date.fromisoformat(birth[:10])
        except ValueError:
            born = None
        if born is not None:
            ref = date(season, 9, 1)
            return ref.year - born.year - ((ref.month, ref.day) < (born.month, born.day))
    age = record.get("age")
    if isinstance(age, int) and not isinstance(age, bool):
        return age
    return None


def _age_factor(pos: str, age: int) -> float:
    for bound, factor in AGE_CURVES.get(pos, AGE_CURVES["WR"]):
        if age < bound:
            return factor
    return AGE_CURVES.get(pos, AGE_CURVES["WR"])[-1][1]


def _future(value: float, pos: str, age: int) -> float:
    """``value`` carried over the next :data:`FUTURE_HORIZON` seasons on the age curve, discounted."""
    total = 0.0
    carried = value
    for year in range(1, FUTURE_HORIZON + 1):
        carried *= _age_factor(pos, age + year - 1)
        total += carried * FUTURE_DISCOUNT**year
    return total


def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _fill_replacement(
    model: LeagueModel, ppg: Mapping[str, tuple[str, float]]
) -> dict[str, float]:
    """Greedy fill of every team's starting slots; the last starter per position is replacement."""
    from commishdesk.stats.lineup import _slot_eligibility  # reuse the eligibility rules, not the solver

    _slots, eligibility = _slot_eligibility(model)
    groups: dict[tuple[str, ...], int] = {}
    for elig in eligibility:
        groups[tuple(elig)] = groups.get(tuple(elig), 0) + 1
    ordered = sorted(groups.items(), key=lambda item: len(item[0]))  # narrowest slots first; stable
    teams = model.format.team_count
    taken: set[str] = set()
    last: dict[str, float] = {}
    for slot_positions, count in ordered:
        pool = sorted(
            (pid for pid, (pos, _p) in ppg.items() if pos in slot_positions and pid not in taken),
            key=lambda pid: (-ppg[pid][1], pid),
        )
        for pid in pool[: count * teams]:
            taken.add(pid)
            pos, points = ppg[pid]
            last[pos] = min(points, last.get(pos, math.inf))
    return last


def build_snapshot(
    *,
    league_id: str,
    model: LeagueModel,
    league: Mapping[str, Any],
    matchups: Mapping[str, Any],
    players: Mapping[str, Any],
    week: int,
    standings_order: Sequence[str] = (),
    nv: _Nflverse | None,
    fallback: bool = False,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Compute one snapshot. Pure and deterministic: equal inputs give an equal mapping."""
    log = logger or _log
    season = model.season
    scoring: Mapping[str, Any] = _mapping(league.get("scoring_settings"))
    settings: Mapping[str, Any] = _mapping(league.get("settings"))
    dynasty = settings.get("type") == 2
    score = _scorer(scoring)
    joiner = _Joiner(nv) if nv is not None else None

    # Sleeper players_points by week -> player.
    pp: dict[int, dict[str, float]] = {}
    for wk_key, entries in matchups.items():
        try:
            wk = int(wk_key)
        except (TypeError, ValueError):
            continue
        if not 1 <= wk <= week or not isinstance(entries, list):
            continue
        for entry in entries:
            points = entry.get("players_points") if isinstance(entry, Mapping) else None
            if isinstance(points, Mapping):
                for pid, value in points.items():
                    if _num(value):
                        pp.setdefault(wk, {})[str(pid)] = float(value)

    # Per-player points this season, prior-season ppg, rostered-join coverage.
    rows: dict[str, dict[str, Any]] = {}
    rostered_offense = matched = 0
    rostered_ids = {pid for wk_points in pp.values() for pid in wk_points}
    for pid in sorted(players):
        record = players[pid]
        if not isinstance(record, Mapping):
            continue
        pos = _player_pos(record)
        if pos is None:
            continue
        offense = pos in OFFENSE_POSITIONS
        gsis = joiner.match(record, pos) if (joiner is not None and offense) else None
        if offense and pid in rostered_ids:
            rostered_offense += 1
            matched += gsis is not None
        cur_rows = nv.current.get(gsis, {}) if (nv is not None and gsis) else {}
        week_points: list[float] = []
        for wk in range(1, week + 1):
            league_points = pp.get(wk, {}).get(pid)
            nv_row = cur_rows.get(wk) if offense else None
            if league_points is not None:
                if nv_row is not None or league_points != 0:
                    week_points.append(league_points)
            elif nv_row is not None:
                week_points.append(score(nv_row, pos))
        prior_ppg = None
        if nv is not None and gsis and gsis in nv.prior and len(nv.prior[gsis]) >= PRIOR_MIN_GAMES:
            prior_rows = nv.prior[gsis]
            prior_ppg = sum(score(prior_rows[wk], pos) for wk in sorted(prior_rows)) / len(prior_rows)
        search_rank = record.get("search_rank")
        rows[pid] = {
            "pos": pos,
            "games": len(week_points),
            "sum": sum(week_points),
            "prior": prior_ppg,
            "rank": float(search_rank) if _num(search_rank) else None,
            "rookie_year": record.get("years_exp") == 0,
            "age": _age(record, season),
        }

    # Established players: enough games this season, or a qualifying prior to blend in.
    established: dict[str, tuple[str, float]] = {}
    for pid, row in rows.items():
        games, prior = row["games"], row["prior"]
        if prior is not None:
            row["ppg"] = (row["sum"] + PRIOR_GAMES_K * prior) / (games + PRIOR_GAMES_K)
        elif games >= ROOKIE_GAMES_THRESHOLD or (row["pos"] not in OFFENSE_POSITIONS and games >= 1):
            row["ppg"] = row["sum"] / games
        else:
            continue
        established[pid] = (row["pos"], row["ppg"])

    replacement = _fill_replacement(model, established)
    values: dict[str, dict[str, Any]] = {}
    for pid in sorted(established):
        pos, ppg = established[pid]
        if pos not in replacement:
            continue  # no starting slot for this position: unvalued, not zero
        values[pid] = {
            "pos": pos,
            "ppg": _round(ppg),
            "games": rows[pid]["games"],
            "value": _round(max(0.0, ppg - replacement[pos])),
            "est": False,
        }

    # Rookies: the value at the same positional rank by Sleeper search_rank (est.).
    ranked: dict[str, list[tuple[float, str]]] = {}
    for pid, row in rows.items():
        if row["rank"] is not None and row["pos"] in OFFENSE_POSITIONS:
            ranked.setdefault(row["pos"], []).append((row["rank"], pid))
    for pos, entries in ranked.items():
        if pos not in replacement:
            continue
        entries.sort()
        by_value = sorted((info["value"] for info in values.values() if info["pos"] == pos), reverse=True)
        for position, (_rank, pid) in enumerate(entries):
            if pid in values:
                continue
            row = rows[pid]
            fill = by_value[position] if position < len(by_value) else 0.0
            values[pid] = {
                "pos": pos,
                "ppg": None,
                "games": row["games"],
                "value": _round(fill),
                "est": True,
            }

    picks = None
    if dynasty:
        for pid, info in values.items():
            age = rows[pid]["age"]
            if age is not None and info["pos"] in OFFENSE_POSITIONS:
                info["future"] = _round(_future(info["value"], info["pos"], age))
        picks = _pick_table(model, settings, season, values, rows, standings_order)

    snapshot: dict[str, Any] = {
        "method_version": METHOD_VERSION,
        "league_id": league_id,
        "season": season,
        "week": week,
        "as_of_week": week,
        "source": "nflverse" if nv is not None else "sleeper_only",
        "fallback": "nflverse_unavailable" if fallback else None,
        "dynasty": dynasty,
        "constants": {
            "prior_games_k": PRIOR_GAMES_K,
            "prior_min_games": PRIOR_MIN_GAMES,
            "rookie_games_threshold": ROOKIE_GAMES_THRESHOLD,
            "future_horizon": FUTURE_HORIZON,
            "future_discount": FUTURE_DISCOUNT,
            "pick_later_discount": PICK_LATER_DISCOUNT,
        },
        "replacement": {pos: _round(level) for pos, level in sorted(replacement.items())},
        "players": values,
        "picks": picks,
        "join": {
            "rostered": rostered_offense,
            "matched": matched,
            "coverage": _round(matched / rostered_offense) if rostered_offense else None,
        },
        "unexpressed": _gap_keys(scoring) if nv is not None else [],
        "attribution": "stats: nflverse, CC BY 4.0" if nv is not None else None,
    }
    if nv is not None:
        log.info(
            "league value: nflverse join matched %d of %d rostered offensive players (%.1f%%)",
            matched, rostered_offense, 100.0 * matched / rostered_offense if rostered_offense else 0.0,
        )
    return snapshot


def _pick_table(
    model: LeagueModel,
    settings: Mapping[str, Any],
    season: int,
    values: Mapping[str, Mapping[str, Any]],
    rows: Mapping[str, Mapping[str, Any]],
    standings_order: Sequence[str],
) -> dict[str, Any] | None:
    pool = sorted(
        (info["future"] for pid, info in values.items() if rows[pid]["rookie_year"] and "future" in info),
        reverse=True,
    )
    if not pool:
        return None
    teams = model.format.team_count
    rounds = settings.get("draft_rounds")
    rounds = rounds if _is_int(rounds) and 0 < rounds <= 12 else DEFAULT_DRAFT_ROUNDS

    def slot_value(overall: int) -> float:
        if overall <= len(pool):
            return pool[overall - 1]
        return pool[-1] * PICK_TAIL_DECAY ** (overall - len(pool))

    cut_early, cut_mid = (teams + 2) // 3, (2 * teams + 2) // 3
    spans = {"early": (1, cut_early), "mid": (cut_early + 1, cut_mid), "late": (cut_mid + 1, teams)}
    by_tier: dict[str, dict[str, float]] = {tier: {} for tier in spans}
    later: dict[str, float] = {}
    for rnd in range(1, rounds + 1):
        base = (rnd - 1) * teams
        for tier, (lo, hi) in spans.items():
            slots = list(range(lo, hi + 1)) or [teams]
            by_tier[tier][str(rnd)] = _round(sum(slot_value(base + s) for s in slots) / len(slots))
        later[str(rnd)] = _round(sum(slot_value(base + s) for s in range(1, teams + 1)) / teams)
    tier_by_roster = {str(rid): pick_tier(index, len(standings_order)) for index, rid in enumerate(standings_order)}
    return {
        "next_season": season + 1,
        "rounds": rounds,
        "est": True,
        "by_tier": by_tier,
        "later": later,
        "tier_by_roster": tier_by_roster,
    }


def pick_tier(rank_index: int, team_count: int) -> str:
    """The pick tier for the original owner at 0-based standings position ``rank_index`` (0 = best):
    the worst third picks early, the best third late."""
    if team_count <= 0:
        return "mid"
    third = min(2, rank_index * 3 // team_count)
    return ("late", "mid", "early")[third]


def pick_value(
    snapshot: Mapping[str, Any], season: int, rnd: int, original_roster_id: str | None = None
) -> float | None:
    """Future value (est.) of a pick from a snapshot, or ``None`` when the snapshot cannot price it.
    Next season's pick is tiered by its original owner's standing; later seasons are tier-less and
    discounted by :data:`PICK_LATER_DISCOUNT` per season beyond next."""
    picks = snapshot.get("picks")
    if not isinstance(picks, Mapping):
        return None
    ahead = season - int(picks["next_season"])
    if ahead < 0 or str(rnd) not in picks["later"]:
        return None
    if ahead == 0:
        tier = picks["tier_by_roster"].get(str(original_roster_id), "mid")
        return float(picks["by_tier"][tier][str(rnd)])
    return _round(float(picks["later"][str(rnd)]) * PICK_LATER_DISCOUNT**ahead)


# --------------------------------------------------------------------------- #
# The entry: read the cached snapshot or build, cache and return one
# --------------------------------------------------------------------------- #


def snapshot_cache_key(league_id: str, season: int, week: int) -> str:
    """Store cache key: league + season + week + method version (``-`` joins, as the Facts snapshot does)."""
    return f"{league_id}-{season}-w{week}-v{METHOD_VERSION}"


def source_from_env() -> str:
    raw = (os.environ.get(SOURCE_ENV_VAR) or "nflverse").strip().lower()
    return raw if raw in _SOURCES else "nflverse"


def _canonical(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Round-trip through JSON so a first build and a cache re-read are the same mapping."""
    loaded = json.loads(json.dumps(snapshot, sort_keys=True))
    assert isinstance(loaded, dict)
    return loaded


def league_value_for_week(
    store: Store,
    league_id: str,
    week: int,
    *,
    model: LeagueModel,
    league: Mapping[str, Any],
    matchups: Mapping[str, Any],
    players: Mapping[str, Any],
    standings_order: Sequence[str] = (),
    source: str | None = None,
    client: httpx.Client | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, Any] | None:
    """The league's League Value snapshot for ``week``: read from the Store when cached (zero
    network, byte-identical), otherwise built and cached.

    ``source`` is ``nflverse`` (the default, from :data:`SOURCE_ENV_VAR` when ``None``),
    ``sleeper_only`` (no nflverse request, no new egress host: the sample container), or ``off``
    (returns ``None``). An nflverse fault falls back to a labelled ``sleeper_only`` snapshot. Any
    other fault is logged and returns ``None``: this never raises into the Issue."""
    log = logger or _log
    mode = source if source in _SOURCES else source_from_env()
    if mode == "off":
        return None
    try:
        key = snapshot_cache_key(league_id, model.season, week)
        cached = _cache_read(store, key)
        if cached is not None:
            return cached
        nv: _Nflverse | None = None
        fallback = False
        if mode == "nflverse":
            owns_client = client is None
            http = client or httpx.Client()
            try:
                nv = _load_nflverse(store, http, model.season, week)
            except _FETCH_FAULTS as exc:
                log.warning(
                    "league value: nflverse unavailable (%s); building a sleeper_only snapshot", type(exc).__name__
                )
                nv, fallback = None, True
            finally:
                if owns_client:
                    http.close()
        snapshot = _canonical(
            build_snapshot(
                league_id=league_id, model=model, league=league, matchups=matchups, players=players,
                week=week, standings_order=standings_order, nv=nv, fallback=fallback, logger=log,
            )
        )
        _cache_write(store, key, snapshot)
        return snapshot
    except (CommishDeskError, KeyError, TypeError, ValueError, ArithmeticError, AttributeError, IndexError) as exc:
        log.warning("league value: snapshot skipped (%s: %s)", type(exc).__name__, exc)
        return None
