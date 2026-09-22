#!/usr/bin/env python3
"""Generate the published JSON Schema for the Facts contracts.

Not part of the installed ``commishdesk`` package — it ships in ``tools/`` (no
``__init__.py``) so the build backend never packages it, and it depends only on
the standard library plus ``pydantic`` v2 (via ``commishdesk.facts.schema``,
itself stdlib + pydantic only) — no new dependency, mirroring ``tools/anonymize.py``.

Since Story 5.8 it renders **one** schema for **both** documents — the
``draft_recap`` root and the standalone ``weekly`` root — as a union
(``TypeAdapter(DraftRecapFacts | WeeklyFacts)``), so a consumer can validate
either. It writes that to ``docs/facts-schema.json`` alongside
``docs/EXTENDING.md``. ``tests/test_facts.py`` fails if the committed file drifts
from what this script would produce.

Usage::

    python tools/generate_facts_schema.py            # rewrite docs/facts-schema.json
    python tools/generate_facts_schema.py --check    # exit 1 if it is stale, no write
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import TypeAdapter

from commishdesk.facts.schema import DraftRecapFacts, WeeklyFacts

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "facts-schema.json"

_FACTS = TypeAdapter(DraftRecapFacts | WeeklyFacts)


def render() -> str:
    """The canonical serialization of the generated schema (trailing newline)."""
    schema = _FACTS.json_schema()
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="generate_facts_schema.py",
        description="Write (or --check) docs/facts-schema.json from DraftRecapFacts | WeeklyFacts.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if docs/facts-schema.json is stale; do not write it",
    )
    args = parser.parse_args(argv)

    rendered = render().encode("utf-8")
    if args.check:
        # byte comparison — a CRLF checkout is caught as stale rather than
        # normalized away on read (and ``read_bytes`` needs no 3.13+ ``newline`` kwarg).
        current = OUTPUT_PATH.read_bytes() if OUTPUT_PATH.is_file() else b""
        if current != rendered:
            print(
                f"stale: {OUTPUT_PATH} — run `python tools/generate_facts_schema.py`",
                file=sys.stderr,
            )
            return 1
        return 0

    # bytes straight from ``render()`` — always LF on disk, whatever platform
    # regenerates it (no universal-newline translation).
    OUTPUT_PATH.write_bytes(rendered)
    print(f"wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
