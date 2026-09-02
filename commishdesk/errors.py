"""Typed exception hierarchy for the engine.

Every error the engine raises for an expected condition derives from
``CommishDeskError`` so the orchestrator can catch faults per league and skip
one league without taking down the batch (CLAUDE.md §5, AD-9). No bare
``except``; no engine code raises a bare ``Exception`` for a domain fault.

``StoreError`` is the first concrete member: any failure reading or parsing a
backing store (missing/unparseable config, malformed JSON) surfaces as a
``StoreError`` chained from the original OS or decode error.
"""

from __future__ import annotations

__all__ = ["CommishDeskError", "StoreError"]


class CommishDeskError(Exception):
    """Base class for every expected, typed engine fault."""


class StoreError(CommishDeskError):
    """A backing store could not be read or its contents could not be parsed."""
