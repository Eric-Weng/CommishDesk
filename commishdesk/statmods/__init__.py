"""Extension zone: community stat modules (``statmods/``, distinct from the
``stats/`` compute package). Protocol only, one reference impl max.

A module file (or subpackage) exposes a ``STAT_MODULE`` instance. The weekly facts builder
loads modules from the engine's own ``statmods/`` or, when ``COMMISHDESK_STATMOD_DIRS`` is set,
from the directories it lists (which replace the default), and calls ``compute`` with a
deep copy of the weekly Facts JSON.
``compute`` returns ``{module_id: block}`` and nothing else; the block lands in the Facts JSON
under ``module_id``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from commishdesk.facts import FactsJSON

__all__ = ["StatModule"]


@runtime_checkable
class StatModule(Protocol):
    # v1 — adds module_version so a module can seed itself from its own version (Story 9.3).
    module_id: str  # output keys are namespaced by this; additive only, never a prompt or renderer (AD-23)
    module_version: str  # the module's own version string; not a Facts schema version

    def compute(self, facts: FactsJSON) -> Mapping[str, object]: ...
