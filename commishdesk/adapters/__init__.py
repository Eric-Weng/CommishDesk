"""Extension zone: platform adapters — Facts pipeline input. Protocol only, one reference impl max."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

__all__ = ["Adapter"]


@runtime_checkable
class Adapter(Protocol):
    # v0 — later epics extend this when the reference impl lands (Sleeper, Epic 2).
    def fetch(self, league_id: str) -> Mapping[str, Any]: ...
