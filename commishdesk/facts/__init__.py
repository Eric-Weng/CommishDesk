"""Stage 3 — emit a versioned, self-validated Facts JSON, the single contract every narrator and renderer reads.

``FactsJSON`` is the loose input alias the ``Renderer`` / ``StatModule``
extension zones import (AD-2); it stays a bare stdlib type so those zones import
without pydantic. The validated contracts — :class:`~commishdesk.facts.schema.DraftRecapFacts`,
:class:`~commishdesk.facts.schema.WeeklyFacts`,
:data:`~commishdesk.facts.schema.SCHEMA_VERSION`,
:func:`~commishdesk.facts.build.build_draft_recap_facts` and
:func:`~commishdesk.facts.weekly.build_weekly_facts` — are re-exported lazily
(as in ``commishdesk.stats``) so ``from commishdesk.facts import DraftRecapFacts``
resolves while ``import commishdesk.facts`` for ``FactsJSON`` alone does not pull
in pydantic.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from commishdesk.errors import SchemaValidationError

type FactsJSON = Mapping[str, Any]
"""Loose input contract for the ``Renderer`` and ``StatModule`` extension zones (AD-2).
Epic 2 tightens this into the validated Pydantic model in ``facts/schema.py``."""

__all__ = [
    "SCHEMA_VERSION",
    "DraftRecapFacts",
    "FactsJSON",
    "SchemaValidationError",
    "WeeklyFacts",
    "build_draft_recap_facts",
    "build_weekly_facts",
]

_LAZY = {
    "DraftRecapFacts": ("commishdesk.facts.schema", "DraftRecapFacts"),
    "SCHEMA_VERSION": ("commishdesk.facts.schema", "SCHEMA_VERSION"),
    "WeeklyFacts": ("commishdesk.facts.schema", "WeeklyFacts"),
    "build_draft_recap_facts": ("commishdesk.facts.build", "build_draft_recap_facts"),
    "build_weekly_facts": ("commishdesk.facts.weekly", "build_weekly_facts"),
}

if TYPE_CHECKING:
    from commishdesk.facts.build import build_draft_recap_facts
    from commishdesk.facts.schema import SCHEMA_VERSION, DraftRecapFacts, WeeklyFacts
    from commishdesk.facts.weekly import build_weekly_facts


def __getattr__(name: str) -> object:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module, attr = target
    return getattr(importlib.import_module(module), attr)


def __dir__() -> list[str]:
    return sorted(__all__)
