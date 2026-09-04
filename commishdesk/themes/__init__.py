"""Extension zone: output-surface renderers (zone dir ``themes/``; the protocol
is ``Renderer`` and owns the whole surface). Protocol only, one reference impl max."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from commishdesk.facts import FactsJSON

__all__ = ["Renderer"]


@runtime_checkable
class Renderer(Protocol):
    # v0 — later epics extend this when the reference impl lands (Epic 4).
    def render(self, facts: FactsJSON) -> str: ...
