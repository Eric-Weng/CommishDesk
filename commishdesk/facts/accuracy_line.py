"""The one place the Issue's accuracy line is defined (Story 9B.5, AR-74).

Every Issue surface (weekly web, email HTML, ``.email.txt``, ``.txt``, the weekly Discord post) and
the draft recap (web, email, Discord summary) prints :data:`ACCURACY_LINE` from here; none retypes
it. The copy is Eric's approved line (9B.1, 2026-11-10). It names no rival, never says "Sleeper data
only" and promises nothing the engine does not enforce; a test pins the exact string.

The line is render-only: it is never part of the narrated prose, the narration payload, the safety
scan or the claim check. Web and email HTML add a separate :data:`ACCURACY_LINK_TEXT` link to
:data:`DOCS_INDEX`; plain text, Discord and the draft recap carry the line alone.

This module imports nothing, so ``render/`` can import it freely.
"""

from __future__ import annotations

__all__ = ["ACCURACY_LINE", "ACCURACY_LINK_TEXT", "DOCS_INDEX"]

ACCURACY_LINE = "Real numbers from your league, every week."
ACCURACY_LINK_TEXT = "How we compute"
DOCS_INDEX = "/docs/"
