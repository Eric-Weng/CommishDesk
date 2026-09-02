"""Stage 3 — emit a versioned, self-validated Facts JSON, the single contract every narrator and renderer reads.

Epic 2 replaces the ``FactsJSON`` alias below with the validated ``facts.schema`` model.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeAlias

FactsJSON: TypeAlias = Mapping[str, Any]
"""Loose input contract for the ``Renderer`` and ``StatModule`` extension zones (AD-2).
Epic 2 tightens this into the validated Pydantic model in ``facts/schema.py``."""

__all__ = ["FactsJSON"]
