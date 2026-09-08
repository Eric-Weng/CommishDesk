"""Extension zone: narrator voices — a system prompt plus banned topics. Protocol only, one reference impl max."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["Voice", "load_default_voice"]


@runtime_checkable
class Voice(Protocol):
    # v0 — the reference impl (the mild "beat writer" default) landed in Story 3.3;
    # later epics extend this protocol when a consumer needs more.
    system_prompt: str
    # an implementation that bans no extra topics uses an empty frozenset;
    # the set merges into the deterministic content-safety check (AD-12).
    banned_topics: frozenset[str]
    # stable id for the Epic-6 selector / Epic-4 Issue provenance — no consumer yet.
    voice_id: str


def load_default_voice() -> Voice:
    """The one default :class:`Voice` — the mild "beat writer" singleton.

    Imported lazily so importing this zone package stays free of the reference
    implementation until a caller actually needs the prose voice.
    """
    from commishdesk.voices.beat_writer import BEAT_WRITER

    return BEAT_WRITER
