"""Stage 6 — idempotent delivery via a Send Ledger so each recipient gets each Issue exactly once."""

from __future__ import annotations

from commishdesk.deliver.discord import post_discord_text

__all__ = ["post_discord_text"]
