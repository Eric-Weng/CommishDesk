"""``commishdesk/ingest/byes.py`` — committed NFL bye-week data (Story 5.3b).

Nothing in the engine otherwise knows which NFL teams are on bye in a given
week without a live network lookup every time -- which would silently rewrite
history when a past week is regenerated after the fact (FR-5). Byes are
static per season, so -- exactly like ``narrate/safety_lists.toml`` -- the
data is committed, version-controlled, and loaded once via
:mod:`importlib.resources` + :mod:`tomllib`, mirroring
``narrate/safety.py``'s ``_load_lists`` / ``_lists_toml_text`` pattern.

:func:`load_byes` is the loader: it returns one season's validated
``{team_abbrev: bye_week}`` table, or ``None`` if that season is not in the
packaged file (never a guess -- see the module docstring's design note in the
spec). :func:`bye_teams` is the convenience accessor most callers want: the
frozenset of team abbreviations on bye in one season's given week.

A malformed packaged file (bad key set, an out-of-range or non-int week
value) raises a chained :class:`~commishdesk.errors.IngestError` the first
time it is loaded. Both a successful parse *and* a failure are cached at
module level -- ``functools.lru_cache`` alone is not enough here, since it
never caches a raised exception, and this story's own contract is "raised at
first load, cached failure not retried per call": a malformed file must
never be re-read or re-parsed on a later call in the same process, and the
same exception is re-raised each time. A season simply missing from an
otherwise-valid file is not malformed -- it degrades to ``None`` and one
warning naming the missing season, logged here so no caller of this module
has to remember to.
"""

from __future__ import annotations

import logging
import tomllib
from importlib import resources

from commishdesk.errors import IngestError

__all__ = ["bye_teams", "load_byes"]

_BYES_FILENAME = "nfl_byes.toml"

_logger = logging.getLogger("commishdesk.ingest.byes")

#: The 32 standard NFL team abbreviations. No canonical list of these existed
#: anywhere else in the repo before this story.
_KNOWN_NFL_TEAMS: frozenset[str] = frozenset(
    {
        "ARI",
        "ATL",
        "BAL",
        "BUF",
        "CAR",
        "CHI",
        "CIN",
        "CLE",
        "DAL",
        "DEN",
        "DET",
        "GB",
        "HOU",
        "IND",
        "JAX",
        "KC",
        "LAC",
        "LAR",
        "LV",
        "MIA",
        "MIN",
        "NE",
        "NO",
        "NYG",
        "NYJ",
        "PHI",
        "PIT",
        "SF",
        "SEA",
        "TB",
        "TEN",
        "WAS",
    }
)

_MIN_WEEK = 1
_MAX_WEEK = 18


def _byes_toml_text() -> str:
    return resources.files("commishdesk.ingest").joinpath(_BYES_FILENAME).read_text(encoding="utf-8")


def _validate_season_table(season: str, table: object) -> dict[str, int]:
    """One ``[<season>]`` TOML table, structurally validated: its keys must be
    exactly the 32 known NFL abbreviations, and every value an ``int`` in
    ``1..18``. Raises :class:`IngestError` (not chained -- no underlying
    exception exists at this point) naming the season and the problem."""
    if not isinstance(table, dict):
        raise IngestError(f"{_BYES_FILENAME}: season {season!r} must be a table")

    keys = set(table)
    if keys != _KNOWN_NFL_TEAMS:
        missing = sorted(_KNOWN_NFL_TEAMS - keys)
        extra = sorted(keys - _KNOWN_NFL_TEAMS)
        raise IngestError(
            f"{_BYES_FILENAME}: season {season!r} keys are not exactly the 32 known "
            f"NFL team abbreviations (missing={missing}, extra={extra})"
        )

    weeks: dict[str, int] = {}
    for team, week in table.items():
        if isinstance(week, bool) or not isinstance(week, int) or not (_MIN_WEEK <= week <= _MAX_WEEK):
            raise IngestError(
                f"{_BYES_FILENAME}: season {season!r} team {team!r} has an invalid bye "
                f"week ({week!r}); expected an int in {_MIN_WEEK}..{_MAX_WEEK}"
            )
        weeks[team] = week
    return weeks


def _parse_byes(text: str) -> dict[str, dict[str, int]]:
    """Parse + structurally validate the raw packaged TOML. Any failure ->
    :class:`IngestError`, chained where an underlying exception exists."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise IngestError(f"{_BYES_FILENAME} is not valid TOML ({exc})") from exc
    if not isinstance(data, dict):
        raise IngestError(f"{_BYES_FILENAME} does not parse to a table of seasons")
    return {season: _validate_season_table(season, table) for season, table in data.items()}


#: Module-level cache for :func:`_load_and_validate`, split into a success
#: slot and a failure slot: a bare ``functools.lru_cache`` never caches a
#: raised exception, and "cached failure not retried per call" is part of
#: this story's own contract, not an optimization.
_cache: dict[str, dict[str, int]] | None = None
_cache_error: IngestError | None = None


def _clear_cache() -> None:
    """Test-only hook: reset the module-level load cache (success or cached
    failure) so a test can monkeypatch :func:`_byes_toml_text` and observe a
    fresh load. Production code never calls this -- the packaged file never
    changes within a running process."""
    global _cache, _cache_error
    _cache = None
    _cache_error = None


def _load_and_validate() -> dict[str, dict[str, int]]:
    """Load, parse, and structurally validate the packaged bye file exactly
    once per process. A read or parse failure raises a chained
    :class:`IngestError` and caches *that same exception*, so a later call
    raises it again immediately without re-reading or re-parsing the file."""
    global _cache, _cache_error
    if _cache_error is not None:
        raise _cache_error
    if _cache is not None:
        return _cache

    try:
        text = _byes_toml_text()
    except (OSError, UnicodeDecodeError, ModuleNotFoundError) as exc:
        error = IngestError(f"{_BYES_FILENAME} could not be read ({type(exc).__name__})")
        error.__cause__ = exc
        _cache_error = error
        raise error from exc

    try:
        result = _parse_byes(text)
    except IngestError as exc:
        _cache_error = exc
        raise

    _cache = result
    return result


def load_byes(season: int) -> dict[str, int] | None:
    """The validated ``{team_abbrev: bye_week}`` table for *season*, or
    ``None`` if *season* is not in the packaged data -- degrade every bye
    flag to "unavailable" rather than guessing, and never raise for a merely
    *missing* season (only a malformed *file* raises). Logs one warning
    naming the missing season each time this happens.

    Raises :class:`~commishdesk.errors.IngestError` (chained) if the packaged
    ``nfl_byes.toml`` itself cannot be read, parsed, or structurally
    validated."""
    table = _load_and_validate().get(str(season))
    if table is None:
        _logger.warning("no NFL bye data for season %s; bye flags unavailable", season)
        return None
    return dict(table)


def bye_teams(season: int, week: int) -> frozenset[str] | None:
    """The frozenset of team abbreviations on bye in *season*'s *week*, or
    ``None`` if *season* is not in the packaged data (see :func:`load_byes`
    for the exact "missing season" contract -- this delegates to it, so the
    same warning is logged). No network call, ever: this is a pure lookup
    over committed, version-controlled data."""
    table = load_byes(season)
    if table is None:
        return None
    return frozenset(team for team, bye_week in table.items() if bye_week == week)
