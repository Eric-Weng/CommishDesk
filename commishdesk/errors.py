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

``IngestError`` is the third: any structural failure building the stage-1
``LeagueModel`` from a raw platform bundle — a missing or mistyped ``league`` /
``rosters`` / ``users`` / ``draft`` section, a non-object list item, a field the
Pydantic model rejects — surfaces as an ``IngestError`` chained (where an
underlying exception exists) from the original ``KeyError`` / ``TypeError`` /
``ValueError`` / ``OverflowError`` / ``ValidationError``. The wrapping happens in
``commishdesk/ingest/build.py``, not here; no partial model is returned.

``ConsensusError`` is the fourth: any failure fetching, parsing, or ranking an
external pre-draft consensus board — a non-2xx response, a transport failure, a
malformed body, an already-closed client, or a payload that ranks none of the
drafted players — surfaces as a ``ConsensusError`` chained (where an underlying
exception exists) from ``httpx`` / ``RuntimeError`` / ``ValueError``. It is raised
only when *every* source fails against a cold cache; a single source failing is
swallowed and the fallback taken. The wrapping happens at the call site in
``commishdesk/consensus.py``, not here.

``SchemaValidationError`` is the fifth: the stage-3 Facts JSON builder
(``commishdesk/facts/build.py``) validates its own output and fails loud — any
``pydantic.ValidationError`` raised while constructing the ``DraftRecapFacts``
model, or while round-tripping ``model_validate(doc.model_dump())``, surfaces as a
``SchemaValidationError`` chained from that ``ValidationError``. No partial
document is returned; no consumer downstream of ``facts/`` re-validates (AD-2).
The wrapping happens in ``commishdesk/facts/build.py``, not here.
"""

from __future__ import annotations

__all__ = [
    "AdapterError",
    "CommishDeskError",
    "ConsensusError",
    "IngestError",
    "SchemaValidationError",
    "StoreError",
]


class CommishDeskError(Exception):
    """Base class for every expected, typed engine fault."""


class AdapterError(CommishDeskError):
    """A platform adapter could not fetch or parse a league's raw data."""


class ConsensusError(CommishDeskError):
    """No external consensus ranking could be fetched or parsed from any source."""


class IngestError(CommishDeskError):
    """A raw platform bundle could not be turned into a valid ``LeagueModel``."""


class SchemaValidationError(CommishDeskError):
    """The Facts JSON builder produced a document the schema rejects."""


class StoreError(CommishDeskError):
    """A backing store could not be read or its contents could not be parsed."""
