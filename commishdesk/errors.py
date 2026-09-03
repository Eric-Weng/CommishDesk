"""Typed exception hierarchy for the engine.

Every error the engine raises for an expected condition derives from
``CommishDeskError`` so the orchestrator can catch faults per league and skip
one league without taking down the batch (CLAUDE.md §5, AD-9). No bare
``except``; no engine code raises a bare ``Exception`` for a domain fault.

``StoreError`` is the first concrete member: any failure reading or parsing a
backing store (missing/unparseable config, malformed JSON) surfaces as a
``StoreError`` chained from the original OS or decode error.

``AdapterError`` is the second: any failure fetching or parsing a platform
adapter's raw data (a non-2xx response, a transport failure, a malformed body,
or a malformed/missing id in the platform's own response shape) surfaces as an
``AdapterError`` chained from the underlying exception. This module still
imports only stdlib + pydantic + commishdesk (``tests/test_store.py``); wrapping
``httpx`` and shape exceptions happens at the call site in
``commishdesk/adapters/sleeper.py``, not here.
"""

from __future__ import annotations

__all__ = ["AdapterError", "CommishDeskError", "StoreError"]


class CommishDeskError(Exception):
    """Base class for every expected, typed engine fault."""


class AdapterError(CommishDeskError):
    """A platform adapter could not fetch or parse a league's raw data."""


class StoreError(CommishDeskError):
    """A backing store could not be read or its contents could not be parsed."""
