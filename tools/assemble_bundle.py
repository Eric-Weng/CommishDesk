#!/usr/bin/env python3
"""Assemble one raw Sleeper *bundle* from the per-endpoint export files.

``tools/anonymize.py`` consumes a single JSON object in the "bundle shape" (see
``tests/fixtures/README.md``). Sleeper's API, and the private Phase-0 export in
``../brief/phase-0/raw/``, is a *directory* of per-endpoint files instead. This
module is the bridge — the manual pre-step that used to live in an uncommitted
script:

* map the per-endpoint files onto the bundle sections;
* truncate ``matchups`` / ``transactions`` to the scenario's week window;
* drop non-settled transactions (``status != "complete"``);
* for the synthetic superflex case, change the second ``QB`` roster slot to
  ``SUPER_FLEX`` (a roster-slot property; scoring is untouched);
* for a case whose ``target_week`` falls in the playoff period
  (``league.settings.playoff_week_start``), populate ``winners_bracket`` /
  ``losers_bracket`` from the raw export, and drop any curated set of roster
  rows from that week's matchups (an eliminated team that does not play);
* for a case with a curated ``custom_points`` override map, apply it to that
  week's matchups (a synthetic median-scoring week);
* trim ``players`` to the ids the scenario actually references (size budget);
* attach the ``meta`` block.

Like ``anonymize.py`` this ships in ``tools/`` (no ``__init__.py``), depends only
on the standard library plus ``pydantic`` v2, and is never packaged. It does
**not** anonymize — pipe its output through ``anonymize.py``:

    uv run python tools/assemble_bundle.py ../brief/phase-0/raw week10-blowout \\
        | uv run python tools/anonymize.py - --seed 0 > tests/fixtures/week10-blowout.json

The output is a valid :class:`anonymize.Bundle`.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

# --------------------------------------------------------------------------- #
# Scenario table
# --------------------------------------------------------------------------- #
# ``weeks`` is the inclusive game-week window (empty = pre-week-1). ``exercises``
# is copied verbatim into ``meta`` and must match what each fixture documents.


class Case:
    __slots__ = (
        "name",
        "weeks",
        "target_week",
        "superflex",
        "exercises",
        "drop_rosters_at_target",
        "custom_points_override",
    )

    def __init__(
        self,
        name: str,
        weeks: tuple[int, ...],
        target_week: int | None,
        superflex: bool,
        exercises: str,
        drop_rosters_at_target: frozenset[int] = frozenset(),
        custom_points_override: dict[int, float] | None = None,
    ) -> None:
        self.name = name
        self.weeks = weeks
        self.target_week = target_week
        self.superflex = superflex
        self.exercises = exercises
        # Playoff case: roster ids whose row is dropped from
        # ``matchups[str(target_week)]`` — a bracket-eliminated team that does
        # not play that week. Empty for every other case.
        self.drop_rosters_at_target = drop_rosters_at_target
        # Median case: {roster_id: custom_points} applied to
        # ``matchups[str(target_week)]`` to curate an exact-median week. ``None``
        # for every other case.
        self.custom_points_override = custom_points_override


CASES: dict[str, Case] = {
    c.name: c
    for c in (
        Case(
            "rookie-draft",
            (),
            None,
            False,
            "post-draft rosters, keeper/dynasty rookie draft, traded picks",
        ),
        Case(
            "week02-nailbiter",
            (1, 2),
            2,
            False,
            "1-point nailbiter; two sub-1 and sub-7 margins same week; heavy FAAB",
        ),
        Case(
            "week05-trade",
            (1, 2, 3, 4, 5),
            5,
            False,
            "lopsided trade with 2026/2027 pick swaps; season-high 263.99; "
            "84-pt blowout",
        ),
        Case(
            "week10-blowout",
            tuple(range(1, 11)),
            10,
            False,
            "3 blowouts (loser < 65% of winner); reference-newsletter week",
        ),
        Case(
            "week10-superflex",
            tuple(range(1, 11)),
            10,
            True,
            "synthetic superflex: roster_positions QB,QB -> QB,SUPER_FLEX; "
            "scoring unchanged",
        ),
        Case(
            "week01-openers",
            (1,),
            1,
            False,
            "week 1 season opener; no prior-week history",
        ),
        Case(
            "week17-playoffs",
            tuple(range(1, 18)),
            17,
            False,
            "championship round (week 17, playoff_week_start 15); non-empty "
            "winners/losers brackets; rosters 3, 6, 7, and 9 were eliminated in "
            "an earlier round and do not play this week",
            drop_rosters_at_target=frozenset({3, 6, 7, 9}),
        ),
        Case(
            "week08-median",
            tuple(range(1, 9)),
            8,
            False,
            "synthetic median-scoring week: curated custom_points so rosters 6 "
            "and 7 tie exactly at the week-8 league median, five rosters strictly "
            "below, five strictly above",
            custom_points_override={
                1: 160.0,
                2: 150.0,
                3: 140.0,
                4: 135.0,
                5: 130.0,
                6: 120.0,
                7: 120.0,
                8: 110.0,
                9: 105.0,
                10: 100.0,
                11: 95.0,
                12: 90.0,
            },
        ),
    )
}

# raw filename -> bundle section, for the sections that pass straight through.
_RAW_FILES = {
    "league": "league.json",
    "users": "users.json",
    "rosters": "rosters.json",
    "draft": "draft.json",
    "draft_picks": "draft_picks.json",
    "traded_picks": "traded_picks.json",
    "players": "players_filtered.json",
    "matchups": "matchups_by_week.json",
    "transactions": "transactions_by_week.json",
    "winners_bracket": "winners_bracket.json",
    "losers_bracket": "losers_bracket.json",
}

# Bracket files are the *completed* season's results — real for every raw
# export, but only meaningful (and only required) for a case whose
# ``target_week`` actually reaches the playoff period. A raw export missing
# them is fine for every other case; ``assemble()`` raises only when a case
# that needs them can't find them.
_OPTIONAL_RAW_KEYS = frozenset({"winners_bracket", "losers_bracket"})


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def _load_raw(raw_dir: Path) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    for key, fname in _RAW_FILES.items():
        path = raw_dir / fname
        try:
            raw[key] = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            if key in _OPTIONAL_RAW_KEYS:
                # Sentinel for "not present in this raw export" — assemble()
                # decides whether that's an error, based on the case.
                raw[key] = None
                continue
            raise ValueError(f"cannot read raw file {fname}: {exc}") from None
        except json.JSONDecodeError as exc:
            raise ValueError(f"raw file {fname} is not valid JSON: {exc}") from None
    if not isinstance(raw["rosters"], list):
        raise ValueError("raw rosters.json must be a JSON array")
    for section in ("matchups", "transactions"):
        if not isinstance(raw[section], dict):
            raise ValueError(f"raw {section}_by_week.json must be a JSON object")
    for key in _OPTIONAL_RAW_KEYS:
        if raw[key] is not None and not isinstance(raw[key], list):
            raise ValueError(f"raw {_RAW_FILES[key]} must be a JSON array")
    return raw


def _is_settled(txn: Any) -> bool:
    """A transaction that actually went through — the only kind a fixture keeps."""
    return isinstance(txn, dict) and txn.get("status") == "complete"


def _string_ids(values: Any) -> set[str]:
    if not isinstance(values, (list, tuple)):
        return set()
    return {str(v) for v in values if v is not None}


def _referenced_player_ids(raw: dict[str, Any], weeks: tuple[int, ...]) -> set[str]:
    """The player ids a scenario actually needs — matches the current fixtures."""
    ids: set[str] = set()

    for roster in raw["rosters"]:
        if not isinstance(roster, dict):
            continue
        for field in ("players", "starters", "reserve", "taxi", "keepers"):
            ids |= _string_ids(roster.get(field))

    for pick in raw["draft_picks"]:
        if not isinstance(pick, dict):
            continue
        if pick.get("player_id") is not None:
            ids.add(str(pick["player_id"]))
        md = pick.get("metadata")
        if isinstance(md, dict) and md.get("player_id") is not None:
            ids.add(str(md["player_id"]))

    for week in weeks:
        for row in raw["matchups"].get(str(week)) or []:
            if not isinstance(row, dict):
                continue
            ids |= _string_ids(row.get("players"))
            ids |= _string_ids(row.get("starters"))
        for txn in raw["transactions"].get(str(week)) or []:
            if not _is_settled(txn):
                continue
            for field in ("adds", "drops"):
                value = txn.get(field)
                if isinstance(value, dict):
                    ids |= {str(k) for k in value}

    return ids


def _superflex_roster_positions(positions: Any) -> list[Any]:
    """Change the second ``QB`` slot to ``SUPER_FLEX``; leave everything else."""
    if not isinstance(positions, list):
        raise ValueError("league.roster_positions is not a list — cannot mutate")
    out = list(positions)
    seen_qb = 0
    for i, slot in enumerate(out):
        if slot == "QB":
            seen_qb += 1
            if seen_qb == 2:
                out[i] = "SUPER_FLEX"
                return out
    raise ValueError("expected a second QB roster slot to mutate, found none")


def assemble(raw_dir: str | Path, case_name: str) -> dict[str, Any]:
    """Build the bundle for ``case_name`` from the raw export in ``raw_dir``."""
    try:
        case = CASES[case_name]
    except KeyError:
        raise ValueError(
            f"unknown case {case_name!r}; choose one of {', '.join(sorted(CASES))}"
        ) from None

    raw = _load_raw(Path(raw_dir))
    weeks = case.weeks

    league = dict(raw["league"])
    if case.superflex:
        league["roster_positions"] = _superflex_roster_positions(
            league.get("roster_positions")
        )

    missing_weeks = [
        w for w in weeks if str(w) not in raw["matchups"] or str(w) not in raw["transactions"]
    ]
    if missing_weeks:
        raise ValueError(
            f"case {case_name!r} needs weeks {list(weeks)} but the raw export is "
            f"missing {missing_weeks} from matchups/transactions"
        )

    matchups = {str(w): raw["matchups"][str(w)] or [] for w in weeks}
    transactions = {
        str(w): [txn for txn in (raw["transactions"][str(w)] or []) if _is_settled(txn)]
        for w in weeks
    }

    playoff_week_start = (raw["league"].get("settings") or {}).get("playoff_week_start")
    in_playoff_period = (
        case.target_week is not None
        and isinstance(playoff_week_start, int)
        and case.target_week >= playoff_week_start
    )
    if in_playoff_period:
        if raw["winners_bracket"] is None or raw["losers_bracket"] is None:
            raise ValueError(
                f"case {case_name!r} reaches target_week {case.target_week} "
                f"(playoff_week_start {playoff_week_start}) but the raw export is "
                f"missing winners_bracket.json / losers_bracket.json"
            )
        winners_bracket = raw["winners_bracket"]
        losers_bracket = raw["losers_bracket"]
    else:
        # A fixture ending mid-regular-season must emit empty brackets — the raw
        # bracket files hold the *completed* season's results, which would
        # otherwise leak future outcomes into an earlier week's fixture.
        winners_bracket = []
        losers_bracket = []

    if case.drop_rosters_at_target and case.target_week is not None:
        tw = str(case.target_week)
        if tw in matchups:
            matchups[tw] = [
                row
                for row in matchups[tw]
                if not (
                    isinstance(row, dict)
                    and row.get("roster_id") in case.drop_rosters_at_target
                )
            ]

    if case.custom_points_override and case.target_week is not None:
        tw = str(case.target_week)
        overrides = case.custom_points_override
        if tw in matchups:
            matchups[tw] = [
                {**row, "custom_points": overrides[row["roster_id"]]}
                if isinstance(row, dict) and row.get("roster_id") in overrides
                else row
                for row in matchups[tw]
            ]

    # ``players_filtered.json`` is already a curated subset of Sleeper's full
    # player table — deep-bench rookies and players touched only by minor
    # transactions are intentionally absent, and Sleeper's ``"0"`` placeholder
    # id appears in some adds/drops. Keep only ids that resolve to a record; the
    # dropped-count is surfaced on stderr for the person regenerating.
    wanted = _referenced_player_ids(raw, weeks)
    all_players = raw["players"]
    players = {pid: all_players[pid] for pid in sorted(wanted) if pid in all_players}
    dropped = len(wanted) - len(players)
    if dropped:
        print(
            f"assemble_bundle: {dropped} referenced player id(s) not in "
            f"players_filtered.json — omitted from the fixture",
            file=sys.stderr,
        )

    return {
        "meta": {
            "case": case.name,
            "target_week": case.target_week,
            "exercises": case.exercises,
        },
        "league": league,
        "users": raw["users"],
        "rosters": raw["rosters"],
        "matchups": matchups,
        "transactions": transactions,
        "draft": raw["draft"],
        "draft_picks": raw["draft_picks"],
        "traded_picks": raw["traded_picks"],
        "winners_bracket": winners_bracket,
        "losers_bracket": losers_bracket,
        "players": players,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _load_anonymize() -> Any:
    """Import the sibling ``anonymize`` module for defensive bundle validation."""
    path = Path(__file__).resolve().parent / "anonymize.py"
    spec = importlib.util.spec_from_file_location("anonymize", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="assemble_bundle.py",
        description=(
            "Assemble one raw Sleeper bundle from a per-endpoint export "
            "directory. Writes bundle JSON to stdout; pipe it through "
            "anonymize.py."
        ),
    )
    parser.add_argument(
        "raw_dir", help="directory of per-endpoint raw JSON files"
    )
    parser.add_argument(
        "case", choices=sorted(CASES), help="which fixture scenario to build"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        bundle = assemble(args.raw_dir, args.case)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"assemble_bundle: {exc}", file=sys.stderr)
        return 1

    # Fail loudly here rather than letting anonymize.py reject a malformed bundle.
    # Never interpolate the raw ValidationError — like anonymize.py's own CLI it
    # embeds un-anonymized `input_value=` fragments of the raw league.
    anonymize = _load_anonymize()
    try:
        anonymize.load_bundle(bundle)
    except ValidationError as exc:
        print(
            f"assemble_bundle: assembled bundle failed validation "
            f"{anonymize._terse_validation_error(exc)}",
            file=sys.stderr,
        )
        return 1
    except (ValueError, TypeError) as exc:  # never a traceback
        print(f"assemble_bundle: assembled bundle is not valid: {exc}", file=sys.stderr)
        return 1

    json.dump(bundle, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
