#!/usr/bin/env python3
"""Check the demo season's invented names against a real NFL player list.

The demo season (``commishdesk/demo_season/``) is fiction, so no player, manager or team
in it may share a name with a real person. This tool reads the names out of the committed
``league.json`` and compares each against a Sleeper ``players/nfl`` JSON export that you
fetch by hand at authoring time (it is large and third-party, so it is never committed):

    curl -o players-nfl.json https://api.sleeper.app/v1/players/nfl
    uv run python tools/check_demo_names.py players-nfl.json            # write the report
    uv run python tools/check_demo_names.py players-nfl.json --check    # fail if it would change

A collision is an invented player whose normalized first and last name equal a real
player's, or an invented manager, team or league name equal to a real player's full name.
Normalizing is case-folding, accent-stripping and dropping punctuation and generational
suffixes ("Jr.", "III"), so "D.J. O'Neal Jr." and "dj oneal" compare equal. Any collision
exits non-zero and names it.

The committed report (``docs/demo-season-name-check.txt``) lists every name checked and pins
the real list by its sha256 and size, so the check is repeatable against the same export.
Standard library only; never packaged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = REPO_ROOT / "commishdesk" / "demo_season" / "league.json"
DEFAULT_REPORT = REPO_ROOT / "docs" / "demo-season-name-check.txt"

_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})


def normalize(text: str) -> str:
    """Case-folded, accent-free, punctuation-free, suffix-free form of a name."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    tokens = re.sub(r"[^a-z0-9]+", " ", re.sub(r"[.'’]", "", plain.casefold())).split()
    while len(tokens) > 1 and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def demo_names(league_bundle: dict[str, Any]) -> list[tuple[str, str]]:
    """``(kind, name)`` for every invented name, in a fixed order."""
    names: list[tuple[str, str]] = [("league", league_bundle["league"]["name"])]
    users = league_bundle["users"]
    names += [("manager", user["display_name"]) for user in users]
    names += [("team", user["metadata"]["team_name"]) for user in users]
    for pid in sorted(league_bundle["players"]):
        record = league_bundle["players"][pid]
        names.append(("player", f"{record['first_name']} {record['last_name']}".strip()))
    return names


def read_real_list(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: not a Sleeper players/nfl export (expected a JSON object)")
    return data, hashlib.sha256(raw).hexdigest()


def real_names(players: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    for record in players.values():
        if not isinstance(record, dict):
            continue
        first, last = record.get("first_name"), record.get("last_name")
        if isinstance(first, str) and isinstance(last, str) and (first or last):
            found.add(normalize(f"{first} {last}"))
        full = record.get("full_name")
        if isinstance(full, str) and full:
            found.add(normalize(full))
    found.discard("")
    return found


def find_collisions(names: list[tuple[str, str]], real: set[str]) -> list[tuple[str, str]]:
    return [(kind, name) for kind, name in names if normalize(name) in real]


def render_report(names: list[tuple[str, str]], list_sha256: str, list_records: int, list_names: int, hits: int) -> str:
    counts = {kind: sum(1 for k, _ in names if k == kind) for kind in ("player", "manager", "team", "league")}
    lines = [
        "CommishDesk demo season name check",
        "real list: Sleeper players/nfl JSON export, fetched by hand and not committed",
        f"real list sha256: {list_sha256}",
        f"real list records: {list_records}",
        f"real list distinct names: {list_names}",
        f"names checked: {len(names)} (players {counts['player']}, managers {counts['manager']}, "
        f"teams {counts['team']}, league {counts['league']})",
        f"collisions: {hits}",
        "",
        *[f"[{kind}] {name}" for kind, name in names],
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("players_json", type=Path, help="a Sleeper players/nfl JSON export (not committed)")
    parser.add_argument("--check", action="store_true", help="fail if the committed report would change")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA, help="the demo league.json to read names from")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="the report file")
    args = parser.parse_args(argv)

    league_bundle = json.loads(args.data.read_text(encoding="utf-8"))
    players, digest = read_real_list(args.players_json)
    real = real_names(players)
    names = demo_names(league_bundle)
    collisions = find_collisions(names, real)
    if collisions:
        for kind, name in collisions:
            print(f"collision: {kind} {name!r} matches a real NFL player", file=sys.stderr)
        return 1

    report = render_report(names, digest, len(players), len(real), 0)
    if args.check:
        committed = args.report.read_text(encoding="utf-8") if args.report.is_file() else None
        if committed != report:
            print(f"{args.report}: the committed report differs from a re-run", file=sys.stderr)
            return 1
        print("zero collisions; report unchanged")
        return 0
    args.report.write_text(report, encoding="utf-8", newline="\n")
    print(f"zero collisions across {len(names)} names; wrote {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
