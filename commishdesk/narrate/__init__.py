"""Stage 4 — turn the Facts JSON into prose via the zero-credential template narrator (the floor) and the opt-in LLM narrator.

The template narrator (:func:`render_draft_recap` / :func:`recap_to_text`) is
deterministic and credential-free — it reads only the ``narration`` projection of
the Facts JSON (AD-1). The LLM narrator is a later opt-in layer (Epic 3).
"""

from __future__ import annotations

from .template import Recap, Section, recap_to_text, render_draft_recap

__all__ = ["Recap", "Section", "recap_to_text", "render_draft_recap"]
