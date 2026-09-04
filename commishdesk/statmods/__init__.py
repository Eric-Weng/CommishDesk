"""Extension zone: community stat modules (``statmods/``, distinct from the
``stats/`` compute package). Protocol only, one reference impl max."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from commishdesk.facts import FactsJSON

__all__ = ["StatModule"]


@runtime_checkable
class StatModule(Protocol):
    # v0 — later epics extend this when the reference impl lands (playoff-odds, v1).
    module_id: str  # output keys are namespaced by this; additive only, never a prompt or renderer (AD-23)

    def compute(self, facts: FactsJSON) -> Mapping[str, object]: ...
