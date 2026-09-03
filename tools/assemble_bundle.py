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
    __slots__ = ("name", "weeks", "target_week", "superflex", "exercises")

    def __init__(
        self,
        name: str,
        weeks: tuple[int, ...],
        target_week: int | None,
        superflex: bool,
        exercises: str,
    ) -> None:
        self.name = name
        self.weeks = weeks
        self.target_week = target_week
        self.superflex = superflex
        self.exercises = exercises


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
}


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
            raise ValueError(f"cannot read raw file {fname}: {exc}") from None
        except json.JSONDecodeError as exc:
            raise ValueError(f"raw file {fname} is not valid JSON: {exc}") from None
    if not isinstance(raw["rosters"], list):
        raise ValueError("raw rosters.json must be a JSON array")
    for section in ("matchups", "transactions"):
        if not isinstance(raw[section], dict):
            raise ValueError(f"raw {section}_by_week.json must be a JSON object")
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
        # None of the five scenarios reaches the playoffs (bracket play is week 15+),
        # so the brackets are empty by construction; a future playoff-week fixture
        # would need bracket handling added here.
        "winners_bracket": [],
        "losers_bracket": [],
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
