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

``FileStore`` keeps everything as plain files under a root directory:
``leagues/<id>.toml``, ``ledger/<id>.jsonl``, ``storylines/<id>.json``,
``claims/<id>.json``. Whole-file writes go through a temp file plus
``os.replace`` (atomic on POSIX and Windows); ledger appends write one line and
flush. No locking — there is a single writer per league (AD-6).
"""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, PlainSerializer, TypeAdapter

from commishdesk.errors import StoreError

__all__ = ["LedgerEntry", "Storyline", "Claim", "Store", "FileStore"]


# --- shared datetime handling --------------------------------------------------


def _to_utc(value: datetime) -> datetime:
    """Require a timezone-aware datetime; normalize it to UTC."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("datetime must be timezone-aware (UTC)")
    return value.astimezone(timezone.utc)


def _iso_z(value: datetime) -> str:
    """Serialize a UTC datetime as ISO 8601 with a trailing ``Z``."""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


UtcDateTime = Annotated[
    datetime,
    AfterValidator(_to_utc),
    PlainSerializer(_iso_z, return_type=str),
]


# --- record models -----------------------------------------------------------
#
# These are internal engine state, evolvable per story. They are deliberately
# NOT the Facts contract and carry no ``schema_version`` (AD-2).


class LedgerEntry(BaseModel):
    """One confirmed delivery, appended to the send ledger and never mutated."""

    league_id: str
    week: int
    channel: str
    recipient: str
    status: Literal["confirmed"] = "confirmed"
    sent_at: UtcDateTime
    reason: str | None = None


class Storyline(BaseModel):
    """A running thread the facts builder tracks across weeks for a league."""

    id: str
    league_id: str
    headline: str
    status: Literal["active", "resolved"]
    first_week: int
    last_week: int
    notes: str = ""


class Claim(BaseModel):
    """A leaguemate's request to receive the newspaper; read-only in the engine."""

    league_id: str
    roster_id: str
    email: str
    confirmed: bool = False
    claimed_at: UtcDateTime | None = None


_LEDGER_LINE = TypeAdapter(LedgerEntry)
_STORYLINE_LIST = TypeAdapter(list[Storyline])
_CLAIM_LIST = TypeAdapter(list[Claim])


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


# --- the one local implementation ------------------------------------------


class FileStore(Store):
    """Plain files under *root*. See the module docstring for the on-disk layout."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root)

    def _file(self, area: str, league_id: str, suffix: str) -> Path:
        return self._root / area / f"{league_id}{suffix}"

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        """Write *text* to *path* via a temp file in the same directory + ``os.replace``."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
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
        entries: list[LedgerEntry] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = _LEDGER_LINE.validate_json(line)
            except ValueError as exc:
                raise StoreError(f"malformed ledger line for {league_id!r}") from exc
            if entry.week == week:
                entries.append(entry)
        return entries

    def append_ledger_entry(self, entry: LedgerEntry) -> None:
        path = self._file("ledger", entry.league_id, ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(entry.model_dump_json() + "\n")
            handle.flush()

    def read_storylines(self, league_id: str) -> list[Storyline]:
        return self._read_list(self._file("storylines", league_id, ".json"), _STORYLINE_LIST, league_id)

    def write_storylines(self, league_id: str, storylines: list[Storyline]) -> None:
        payload = _STORYLINE_LIST.dump_json(list(storylines), indent=2).decode("utf-8")
        self._atomic_write(self._file("storylines", league_id, ".json"), payload + "\n")

    def read_claims(self, league_id: str) -> list[Claim]:
        return self._read_list(self._file("claims", league_id, ".json"), _CLAIM_LIST, league_id)

    @staticmethod
    def _read_list(path: Path, adapter: TypeAdapter, league_id: str) -> list:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise StoreError(f"cannot read {path.parent.name} for {league_id!r}") from exc
        try:
            return adapter.validate_json(raw)
        except ValueError as exc:
            raise StoreError(f"malformed {path.parent.name} JSON for {league_id!r}") from exc
