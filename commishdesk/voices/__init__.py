"""Extension zone: narrator voices — a system prompt plus banned topics. Protocol only, one reference impl max."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["Voice"]


@runtime_checkable
class Voice(Protocol):
    # v0 — later epics extend this when the reference impl lands (the default Voice, Story 3.3).
    system_prompt: str
    # an implementation that bans no extra topics uses an empty frozenset;
    # the set merges into the deterministic content-safety check (AD-12).
    banned_topics: frozenset[str]
