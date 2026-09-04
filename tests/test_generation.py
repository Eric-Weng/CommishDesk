"""Story 2.7 — the single Generation Set constructor (AD-6 / I1, structural).

Empty candidates -> empty set; every candidate deactivated -> empty set; and an
AST sweep of the package asserting ``GenerationSet(...)`` is constructed in
exactly one module.
"""

from __future__ import annotations

import ast
from pathlib import Path

from commishdesk import generation
from commishdesk.generation import GenerationSet, build_generation_set

PKG_ROOT = Path(generation.__file__).resolve().parent


def test_empty_candidates_yield_an_empty_set() -> None:
    result = build_generation_set([])
    assert isinstance(result, GenerationSet)
    assert result.league_ids == ()


def test_every_candidate_deactivated_yields_an_empty_set() -> None:
    result = build_generation_set(
        (str(n) for n in range(10_000)), is_activated=lambda _id: False
    )
    assert result.league_ids == ()


def test_default_activation_keeps_explicit_ids_deduped_in_order() -> None:
    assert build_generation_set(["9", "9", "4"]).league_ids == ("9", "4")


def test_partial_activation_filters_to_the_activated_ids() -> None:
    result = build_generation_set(
        ["1", "2", "3"], is_activated=lambda league_id: league_id in {"2", "3"}
    )
    assert result.league_ids == ("2", "3")


def test_generation_set_is_constructed_in_exactly_one_module() -> None:
    constructors: list[str] = []
    for path in sorted(PKG_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "GenerationSet"
            ):
                constructors.append(path.relative_to(PKG_ROOT).as_posix())
    assert constructors == ["generation.py"], constructors
