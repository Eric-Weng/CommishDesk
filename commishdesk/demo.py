"""The committed ``demo`` draft recap — ``commishdesk --league demo --draft-recap``.

``--league demo`` runs the whole pipeline against the anonymized rookie-draft
fixture in ``tests/fixtures/``, offline and credential-free. It is a
contributor / CI convenience that runs **from a source checkout only**: CLAUDE.md
§1 keeps ``tests/`` out of the built wheel, so an installed copy has no fixture
and :func:`load_demo_bundle` / :func:`demo_consensus_slots` raise
:class:`~commishdesk.errors.CommishDeskError`.

The bundle reshape mirrors ``tests/test_facts.py::_bundle`` and the consensus
transform mirrors ``tests/test_facts.py::_synthetic_slots`` — the built Facts JSON
reconciles with ``tests/fixtures/facts/expected-draft-recap-facts.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from commishdesk.errors import CommishDeskError

__all__ = [
    "DEMO_CONSENSUS_AS_OF",
    "DEMO_CONSENSUS_SOURCE_NAME",
    "DEMO_LEAGUE_ID",
    "demo_consensus_slots",
    "load_demo_bundle",
]

DEMO_LEAGUE_ID = "demo"

#: What the demo Facts JSON records as its consensus source — the fixture's
#: consensus slots are a hand reconstruction, not a live FantasyCalc fetch.
DEMO_CONSENSUS_SOURCE_NAME = "synthetic rookie board"
DEMO_CONSENSUS_AS_OF = "2025-05"

_DRAFT_FIXTURE = "tests/fixtures/rookie-draft.json"
_CONSENSUS_FIXTURE = "tests/fixtures/consensus/fantasycalc-values.json"
_ABSENT = "demo data is only available from a source checkout"


def _repo_root() -> Path:
    """Walk up from this file for a directory that carries both the rookie-draft
    fixture and ``pyproject.toml`` (a source checkout). Raise
    :class:`~commishdesk.errors.CommishDeskError` when there is none (an installed
    wheel)."""
    for base in Path(__file__).resolve().parents:
        if (base / _DRAFT_FIXTURE).is_file() and (base / "pyproject.toml").is_file():
            return base
    raise CommishDeskError(_ABSENT)


def _read_fixture(relative_path: str) -> Any:
    """Read and JSON-decode one committed fixture. A missing file is the
    installed-wheel case (:data:`_ABSENT`); an unreadable or non-JSON file is a
    corrupt checkout — both surface as :class:`~commishdesk.errors.CommishDeskError`."""
    path = _repo_root() / relative_path
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CommishDeskError(_ABSENT) from exc
    except (OSError, ValueError) as exc:
        raise CommishDeskError(
            f"demo fixture is corrupt: {relative_path} ({type(exc).__name__})"
        ) from exc


def load_demo_bundle() -> dict[str, Any]:
    """Read ``rookie-draft.json`` and reshape it into the Story 2.2 ingest bundle
    (the six keys ``build_league_model`` consumes)."""
    raw = _read_fixture(_DRAFT_FIXTURE)
    try:
        return {
            "league": raw["league"],
            "draft": raw["draft"],
            "draft_picks": raw["draft_picks"],
            "rosters": raw["rosters"],
            "users": raw["users"],
            "previous_league_ids": [],
        }
    except (KeyError, TypeError) as exc:
        raise CommishDeskError(
            f"demo fixture is corrupt: {_DRAFT_FIXTURE} ({type(exc).__name__})"
        ) from exc


def demo_consensus_slots() -> dict[str, int]:
    """Read the committed FantasyCalc snapshot and rank it ``1..N`` by descending
    trade value — a ``sleeper_id -> slot`` mapping for
    ``compute_consensus_metrics``. When two entries share a ``sleeperId`` the
    higher-valued one (first in the descending sort) wins, mirroring
    ``consensus._rank_from_fantasycalc``."""
    board = _read_fixture(_CONSENSUS_FIXTURE)
    try:
        ordered = sorted(board, key=lambda entry: -entry["value"])
        slots: dict[str, int] = {}
        for slot, entry in enumerate(ordered, start=1):
            slots.setdefault(str(entry["player"]["sleeperId"]), slot)
        return slots
    except (KeyError, TypeError) as exc:
        raise CommishDeskError(
            f"demo fixture is corrupt: {_CONSENSUS_FIXTURE} ({type(exc).__name__})"
        ) from exc
