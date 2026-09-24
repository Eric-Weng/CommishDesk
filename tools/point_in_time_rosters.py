#!/usr/bin/env python3
"""Rewrite a fixture's ``rosters`` totals to be *as of its target week*.

Sleeper's ``/rosters`` endpoint has no historical view, so every committed
fixture carries the season-final record beside mid-season matchups (see
``tests/fixtures/README.md``). For ``week10-blowout.json`` that made luck
(``wins - expected_wins``) disagree with a real week-10 pull. This tool folds the
fixture's own ``matchups`` for weeks ``1..target_week`` and overwrites, per roster:

* ``settings.wins`` / ``losses`` / ``ties``;
* ``settings.fpts`` / ``fpts_decimal`` and ``fpts_against`` / ``fpts_against_decimal``
  (integer part and hundredths, Sleeper's own split);
* ``metadata.record`` (one letter per folded game, weeks ``1..target_week``) and ``metadata.streak``.

Pairings are two rosters sharing a ``matchup_id`` in one week; a higher score wins
and equal scores tie. Everything else is untouched, and re-running is a no-op.

    uv run python tools/point_in_time_rosters.py tests/fixtures/week10-blowout.json

Standard library only; never packaged.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any


def _fold(bundle: dict[str, Any]) -> dict[int, dict[str, Any]]:
    target = int(bundle["meta"]["target_week"])
    totals: dict[int, dict[str, Any]] = defaultdict(
        lambda: {"pf": Decimal(0), "pa": Decimal(0), "results": []}
    )
    for week in range(1, target + 1):
        pairs: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in bundle["matchups"].get(str(week), []):
            if row.get("matchup_id") is not None:
                pairs[row["matchup_id"]].append(row)
        for rows in pairs.values():
            if len(rows) != 2:
                continue
            a, b = rows
            pa, pb = Decimal(str(a["points"])), Decimal(str(b["points"]))
            for me, mine, theirs in ((a, pa, pb), (b, pb, pa)):
                entry = totals[me["roster_id"]]
                entry["pf"] += mine
                entry["pa"] += theirs
                entry["results"].append("W" if mine > theirs else "L" if mine < theirs else "T")
    return totals


def _split(value: Decimal) -> tuple[int, int]:
    cents = int((value * 100).to_integral_value())
    return cents // 100, cents % 100


def _streak(results: list[str]) -> str:
    if not results:
        return ""
    last, count = results[-1], 0
    for outcome in reversed(results):
        if outcome != last:
            break
        count += 1
    return f"{count}{last}"


def apply(bundle: dict[str, Any]) -> dict[str, Any]:
    totals = _fold(bundle)
    for roster in bundle["rosters"]:
        entry = totals[roster["roster_id"]]
        results = entry["results"]
        settings = roster["settings"]
        settings["wins"] = results.count("W")
        settings["losses"] = results.count("L")
        settings["ties"] = results.count("T")
        settings["fpts"], settings["fpts_decimal"] = _split(entry["pf"])
        settings["fpts_against"], settings["fpts_against_decimal"] = _split(entry["pa"])
        roster["metadata"]["record"] = "".join(results)
        roster["metadata"]["streak"] = _streak(results)
    return bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fixture", type=Path)
    args = parser.parse_args(argv)
    bundle = apply(json.loads(args.fixture.read_text(encoding="utf-8")))
    # Same encoding as anonymize.py's final dump, so only the roster fields change.
    text = json.dumps(bundle, sort_keys=True, ensure_ascii=False) + "\n"
    args.fixture.write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
