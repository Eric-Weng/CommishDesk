"""Extension zone: narrator voices — a system prompt plus banned topics. Protocol only, one reference impl max."""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

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


def load_default_voice(content: Literal["draft", "weekly"] = "draft") -> Voice:
    """The one default :class:`Voice` — the mild "beat writer".

    ``content`` selects which Issue the voice is instructed for: ``"draft"``
    (the draft recap, the default) or ``"weekly"`` (Story 5.12). Same personality,
    banned topics and ``voice_id`` either way; only the structure rules differ.

    Imported lazily so importing this zone package stays free of the reference
    implementation until a caller actually needs the prose voice.
    """
    from commishdesk.voices.beat_writer import BEAT_WRITER, BEAT_WRITER_WEEKLY

    if content == "weekly":
        return BEAT_WRITER_WEEKLY
    if content == "draft":
        return BEAT_WRITER
    raise ValueError(f"unknown voice content type {content!r}; expected 'draft' or 'weekly'")
