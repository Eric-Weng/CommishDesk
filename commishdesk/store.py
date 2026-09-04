"""The storage port and its one local implementation.

``Store`` is the single seam every stateful part of the engine writes against:
league config, the send ledger, storylines, and claim records. The hosted app
supplies its own backend by subclassing ``Store`` — engine code never names or
imports a hosted-infra service (AD-5).

Guarantees every ``Store`` implementation makes:

* **Read-after-write consistency.** A value written through a ``Store`` method is
  visible to an immediately-following read on the same object, in the same
  process, with no flush or reopen step required of the caller.
* **The send ledger is append-only.** ``append_ledger_entry`` only ever adds a
  line; there is no update or delete. Entries are always ``status="confirmed"`` —
  the ledger records sends that happened, never pending intent (AD-10).
* **Storyline writes are whole-set replacements.** ``write_storylines`` replaces
  the league's entire storyline set; the facts builder owns their maintenance
  (AD-14). A league that has never been written reads back as ``[]``.
* **Claims are read-only in the engine.** The engine surfaces claim records so
  the Generation Set constructor (elsewhere, AD-6) can derive from them; it never
  writes a claim.
* **The blob cache is a plain key/value store of raw upstream payloads.**
  ``write_cache`` records one JSON object under a ``(namespace, key)`` pair;
  ``read_cache`` returns it verbatim, or ``None`` when nothing was written.
  A written value is visible to an immediately-following ``read_cache`` for the
  same pair. It carries no schema and no expiry — the caller owns freshness.

``FileStore`` keeps everything as plain files under a root directory:
``leagues/<id>.toml``, ``ledger/<id>.jsonl``, ``storylines/<id>.json``,
``claims/<id>.json``, ``cache/<namespace>/<key>.json``. Whole-file writes go
through a temp file plus
``os.replace``; ledger appends write one line and flush. No locking — there is a
single writer per league (AD-6).
"""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar

from pydantic import AfterValidator, BaseModel, Field, PlainSerializer, TypeAdapter

from commishdesk.errors import StoreError

__all__ = ["LedgerEntry", "Storyline", "Claim", "Store", "FileStore"]

_T = TypeVar("_T")


# --- shared datetime handling --------------------------------------------------


def _to_utc(value: datetime) -> datetime:
    """Require a timezone-aware datetime; normalize it to UTC."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("datetime must be timezone-aware (UTC)")
    return value.astimezone(UTC)


def _iso_z(value: datetime) -> str:
    """Serialize a UTC datetime as ISO 8601 with a trailing ``Z``."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


UtcDateTime = Annotated[
    datetime,
    AfterValidator(_to_utc),
    PlainSerializer(_iso_z, return_type=str),
]


def _safe_league_id(league_id: str) -> str:
    """Return *league_id* unchanged, or raise ``StoreError`` if it could escape
    the store root as a path segment. This is the app-fed seam — a value that is
    empty, ``.``/``..``, or carries a separator or NUL byte is rejected."""
    if (
        not league_id
        or league_id in (".", "..")
        or "/" in league_id
        or "\\" in league_id
        or "\x00" in league_id
    ):
        raise StoreError(f"unsafe league id: {league_id!r}")
    return league_id


def _safe_segment(segment: str) -> str:
    """Return *segment* unchanged, or raise ``StoreError`` if it could escape
    the store root as a path segment. Same rule as ``_safe_league_id`` — this is
    the guard for a caller-fed ``namespace`` or ``key`` on the blob cache."""
    if (
        not segment
        or segment in (".", "..")
        or "/" in segment
        or "\\" in segment
        or "\x00" in segment
    ):
        raise StoreError(f"unsafe cache segment: {segment!r}")
    return segment


# --- record models -----------------------------------------------------------
#
# These are internal engine state, evolvable per story. They are deliberately
# NOT the Facts contract and carry no ``schema_version`` (AD-2).


class LedgerEntry(BaseModel):
    """One confirmed delivery, appended to the send ledger and never mutated."""

    league_id: str
    week: int = Field(ge=1, le=18)
    channel: str
    recipient: str
    status: Literal["confirmed"] = "confirmed"
    sent_at: UtcDateTime
    # Why this delivery was a deliberate re-issue; ``None`` for a first send (AD-10).
    reason: str | None = None


class Storyline(BaseModel):
    """A running thread the facts builder tracks across weeks for a league."""

    id: str
    league_id: str
    headline: str
    status: Literal["active", "resolved"]
    first_week: int = Field(ge=1, le=18)
    last_week: int = Field(ge=1, le=18)
    notes: str = ""


class Claim(BaseModel):
    """A leaguemate's request to receive the newspaper; read-only in the engine."""

    league_id: str
    roster_id: str
    email: str
    confirmed: bool = False
    claimed_at: UtcDateTime | None = None


_LEDGER_LINE = TypeAdapter(LedgerEntry)
_STORYLINE_LIST: TypeAdapter[list[Storyline]] = TypeAdapter(list[Storyline])
_CLAIM_LIST: TypeAdapter[list[Claim]] = TypeAdapter(list[Claim])


# --- the port --------------------------------------------------------------


class Store(ABC):
    """Abstract storage port. See the module docstring for the guarantees every
    implementation makes: read-after-write consistency, an append-only ledger,
    whole-set storyline replacement, and read-only claims."""

    @abstractmethod
    def read_config(self, league_id: str) -> dict[str, Any]:
        """Return the parsed league config, or raise ``StoreError`` if it is
        missing or unparseable."""

    @abstractmethod
    def read_ledger(self, league_id: str, week: int) -> list[LedgerEntry]:
        """Return the ledger entries for one league-week (empty if none)."""

    @abstractmethod
    def append_ledger_entry(self, entry: LedgerEntry) -> None:
        """Append one confirmed entry to the league's ledger. Append-only: the
        ledger is never updated or rewritten, and the entry is visible to an
        immediately-following ``read_ledger``."""

    @abstractmethod
    def read_storylines(self, league_id: str) -> list[Storyline]:
        """Return the league's storylines. A league never written reads as ``[]``."""

    @abstractmethod
    def write_storylines(self, league_id: str, storylines: list[Storyline]) -> None:
        """Replace the league's entire storyline set. The new set is visible to
        an immediately-following ``read_storylines``."""

    @abstractmethod
    def read_claims(self, league_id: str) -> list[Claim]:
        """Return the league's claim records (empty if none). Read-only: the
        engine has no claim-write path."""

    @abstractmethod
    def read_cache(self, namespace: str, key: str) -> dict[str, Any] | None:
        """Return the JSON object last written under ``(namespace, key)``, or
        ``None`` if nothing was written. Raises ``StoreError`` on unsafe
        ``namespace`` / ``key`` or an unreadable / malformed entry."""

    @abstractmethod
    def write_cache(self, namespace: str, key: str, value: Mapping[str, Any]) -> None:
        """Store one JSON object under ``(namespace, key)``, replacing any prior
        value. Visible to an immediately-following ``read_cache`` for the same
        pair. Raises ``StoreError`` on unsafe ``namespace`` / ``key`` or a write
        failure."""


# --- the one local implementation ------------------------------------------


class FileStore(Store):
    """Plain files under *root*. See the module docstring for the on-disk layout."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root)

    def _file(self, area: str, league_id: str, suffix: str) -> Path:
        return self._root / area / f"{_safe_league_id(league_id)}{suffix}"

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        """Replace *path*'s contents atomically: write a sibling temp file, then
        ``os.replace`` it into place so a reader sees either the whole prior file
        or the whole new one — never a partial write. This guarantees atomic
        *visibility* of the swap; it is not a durability claim about the rename
        surviving a crash. An ``OSError`` becomes a ``StoreError``."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            raise StoreError(f"cannot write {path.name}") from exc

    def read_config(self, league_id: str) -> dict[str, Any]:
        path = self._file("leagues", league_id, ".toml")
        try:
            with open(path, "rb") as handle:
                return tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise StoreError(f"cannot read league config for {league_id!r}") from exc

    def read_ledger(self, league_id: str, week: int) -> list[LedgerEntry]:
        path = self._file("ledger", league_id, ".jsonl")
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise StoreError(f"cannot read ledger for {league_id!r}") from exc
        # Split only on "\n" — splitlines() also breaks on \x85 / U+2028 / U+2029
        # / \v / \f, which can occur verbatim inside a JSON string field.
        segments = raw.split("\n")
        ends_clean = raw.endswith("\n")
        entries: list[LedgerEntry] = []
        for index, segment in enumerate(segments):
            line = segment.strip()
            if not line:
                continue
            try:
                entry = _LEDGER_LINE.validate_json(line)
            except ValueError as exc:
                # Tolerate exactly one unterminated trailing line — a crash
                # mid-append. A bad line anywhere else is real corruption.
                if index == len(segments) - 1 and not ends_clean:
                    continue
                raise StoreError(f"malformed ledger line for {league_id!r}") from exc
            if entry.week == week:
                entries.append(entry)
        return entries

    def append_ledger_entry(self, entry: LedgerEntry) -> None:
        _safe_league_id(entry.league_id)
        path = self._file("ledger", entry.league_id, ".jsonl")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(entry.model_dump_json() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise StoreError(f"cannot append ledger for {entry.league_id!r}") from exc

    def read_storylines(self, league_id: str) -> list[Storyline]:
        return self._read_list(
            self._file("storylines", league_id, ".json"), _STORYLINE_LIST, league_id
        )

    def write_storylines(self, league_id: str, storylines: list[Storyline]) -> None:
        for storyline in storylines:
            if storyline.league_id != league_id:
                raise StoreError(
                    f"storyline {storyline.id!r} is for league "
                    f"{storyline.league_id!r}, not {league_id!r}"
                )
        payload = _STORYLINE_LIST.dump_json(list(storylines), indent=2).decode("utf-8")
        self._atomic_write(self._file("storylines", league_id, ".json"), payload + "\n")

    def read_claims(self, league_id: str) -> list[Claim]:
        return self._read_list(
            self._file("claims", league_id, ".json"), _CLAIM_LIST, league_id
        )

    @staticmethod
    def _read_list(
        path: Path, adapter: TypeAdapter[list[_T]], league_id: str
    ) -> list[_T]:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise StoreError(f"cannot read {path.parent.name} for {league_id!r}") from exc
        if not raw.strip():  # empty / whitespace-only reads like a missing file
            return []
        try:
            return adapter.validate_json(raw)
        except ValueError as exc:
            raise StoreError(
                f"malformed {path.parent.name} JSON for {league_id!r}"
            ) from exc

    def _cache_file(self, namespace: str, key: str) -> Path:
        return (
            self._root / "cache" / _safe_segment(namespace) / f"{_safe_segment(key)}.json"
        )

    def read_cache(self, namespace: str, key: str) -> dict[str, Any] | None:
        path = self._cache_file(namespace, key)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as exc:
            raise StoreError(f"cannot read cache {namespace}/{key}") from exc
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise StoreError(f"malformed cache JSON for {namespace}/{key}") from exc
        if not isinstance(value, dict):
            raise StoreError(f"cache entry {namespace}/{key} is not a JSON object")
        return value

    def write_cache(self, namespace: str, key: str, value: Mapping[str, Any]) -> None:
        path = self._cache_file(namespace, key)
        try:
            payload = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, indent=2)
        except (TypeError, ValueError) as exc:
            raise StoreError(f"cache value for {namespace}/{key} is not JSON") from exc
        self._atomic_write(path, payload + "\n")
