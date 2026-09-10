"""Story 4.3 — the Discord surface: a deterministic sub-2000-character plain-text
summary of a draft recap (AD-1 / AD-2 / I4).

:func:`render_discord_summary` is a pure function of its arguments — the validated
Facts JSON plus **exactly one** narrated body (the template
:class:`~commishdesk.narrate.Recap` **or** the LLM narrator's plain-text prose).
It returns a ``str``: a title line naming the league and season, a blank line, and
the first sentence/paragraph of the narrated Lead truncated at a word boundary.

**MVP scope (sprint-change-proposal-2026-09-10).** Text only — no embed, no
infographic PNG, no hosted-link line. FR-23's "links to the full web Issue by
Slug" needs a hosted URL the MVP cannot mint; the polished embed lands in v1.

**Deterministic (I4 / AD-2).** No clock, RNG, or network; identical arguments
produce a byte-identical ``str``; ``\\n`` newlines only; the result is always
``< 2000`` characters.

**Pipeline fence (AD-1).** Standard library + :mod:`commishdesk.facts` schema
types + :class:`commishdesk.narrate.Recap` + the shared ``render/_body`` helpers —
nothing upstream of ``facts/``, no cloud or HTTP SDK.
"""

from __future__ import annotations

from commishdesk.facts.schema import DraftRecapFacts
from commishdesk.narrate import Recap
from commishdesk.render._body import _plain, _sections_from_llm, _sections_from_recap

__all__ = ["render_discord_summary"]

#: Discord caps a message at 2000 characters; the summary stays well under it.
_MAX = 2000
#: Soft budget for the lead sentence before it is cut at a word boundary.
_LEAD_BUDGET = 320


def render_discord_summary(
    facts: DraftRecapFacts,
    *,
    recap: Recap | None = None,
    llm_text: str | None = None,
) -> str:
    """Compose the plain-text Discord summary for a draft recap.

    **Exactly one** of ``recap`` (the template narrator's structured
    :class:`~commishdesk.narrate.Recap`) or ``llm_text`` (the validated LLM prose)
    must be supplied — passing neither or both raises :class:`ValueError` (the same
    shape as ``render_web`` / ``render_email``). The first line is
    ``"<league name> — <season> Draft Recap"``; a blank line and one lead sentence
    follow when the narrated body has one. The return value is deterministic,
    uses ``\\n`` newlines only, and is always shorter than 2000 characters.
    """
    if (recap is None) == (llm_text is None):
        which = "neither" if recap is None else "both"
        raise ValueError(
            f"render_discord_summary needs exactly one of recap= or llm_text= (got {which})"
        )

    league = facts.league
    # Collapse any whitespace (a stray newline in ``name`` / ``season`` would
    # split the first line and break the LF-only, single-title-line contract).
    title = " ".join(f"{league.name} — {league.season} Draft Recap".split())

    sections = (
        _sections_from_recap(recap)
        if recap is not None
        else _sections_from_llm(llm_text or "")
    )
    lead = _lead_sentence(sections)

    summary = _plain(f"{title}\n\n{lead}" if lead else title)
    if len(summary) >= _MAX:
        clipped = summary[: _MAX - 2].rsplit(" ", 1)[0].rstrip() or summary[: _MAX - 2]
        summary = f"{clipped}…"
    return summary


def _lead_sentence(sections: list[tuple[str | None, list[str]]]) -> str:
    """The first non-empty block of ``sections[0]``, whitespace-collapsed and, if
    long, truncated at a word boundary near :data:`_LEAD_BUDGET` with an ellipsis."""
    if not sections:
        return ""
    _, blocks = sections[0]
    block = next((b for b in blocks if b.strip()), "")
    text = " ".join(block.split())
    if len(text) <= _LEAD_BUDGET:
        return text
    head = text[:_LEAD_BUDGET].rsplit(" ", 1)[0].rstrip()
    return f"{head}…" if head else text[:_LEAD_BUDGET].rstrip()
