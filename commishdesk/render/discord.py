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

Story 5.11a adds the weekly twin: :func:`render_weekly_discord_summary` composes
the same shape — a title line, a blank line, and the first lead block, clipped
under 2000 characters — for a
:class:`~commishdesk.narrate.weekly_template.WeeklyIssue`, then runs the whole
composed string through :func:`_escape_discord_markdown` so a league-supplied
name (league / team / manager) can never italicize, embolden, quote or
table-ify a posted message. No surface escaped Discord markdown before this.

**Pipeline fence (AD-1).** Standard library + :mod:`commishdesk.facts` schema
types + :class:`commishdesk.narrate.Recap` /
:class:`~commishdesk.narrate.weekly_template.WeeklyIssue` + the shared
``render/_body`` helpers — nothing upstream of ``facts/``, no cloud or HTTP SDK.
"""

from __future__ import annotations

from collections.abc import Sequence

from commishdesk.facts.schema import DraftRecapFacts
from commishdesk.narrate import Recap
from commishdesk.narrate.weekly_template import WeeklyIssue
from commishdesk.render._body import _plain, _sections_from_llm, _sections_from_recap

__all__ = ["render_discord_summary", "render_weekly_discord_summary"]

#: Discord caps a message at 2000 characters; the summary stays well under it.
_MAX = 2000
#: Soft budget for the lead sentence before it is cut at a word boundary.
_LEAD_BUDGET = 320

#: The Discord-markdown control characters escaped in a composed summary —
#: ``*``/``_``/``~`` emphasis, ``\``` inline code, ``|`` a table cell, ``>`` a
#: block quote, ``#`` a heading (only live at the start of a line, but escaped
#: everywhere the same way every other character here is). The backslash is
#: deliberately first: it is escaped before the rest, so the backslashes this
#: helper inserts are never re-escaped. ``@`` needs no entry — Discord mention
#: parsing is already fully suppressed at the API layer
#: (``deliver/discord.py::post_discord_text``'s ``allowed_mentions: {"parse":
#: []}``), independent of anything in the text.
_DISCORD_MARKDOWN_CHARS = ("\\", "*", "_", "~", "`", "|", ">", "#")


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


def render_weekly_discord_summary(issue: WeeklyIssue, *, week: int) -> str:
    """Compose the plain-text Discord summary for a weekly Issue (Story 5.11a).

    A title line, a blank line, and the first lead block — the exact shape
    :func:`render_discord_summary` composes for a draft recap, clipped under
    2000 characters the same way. The title is *issue*'s own masthead title
    (``"<league> — Week <n> Recap"``, from
    ``narrate/weekly_template.py::_masthead``); ``week`` seeds a title only when
    the issue carries none.

    The whole composed string — masthead title included — is escaped through
    :func:`_escape_discord_markdown` *before* the 2000-character clip (escaping
    grows the string, so escaping after clipping could push an
    already-under-the-limit summary back over it), so a league-supplied name
    (league, team or manager) can never reformat the posted message.
    Deterministic; ``\\n`` newlines only; always shorter than 2000 characters.
    """
    title = " ".join(issue.title.split()) or f"Week {week} Recap"

    sections = [(section.heading, list(section.blocks)) for section in issue.sections]
    lead = _lead_sentence(sections)

    summary = _escape_discord_markdown(_plain(f"{title}\n\n{lead}" if lead else title))
    if len(summary) >= _MAX:
        clipped = summary[: _MAX - 2].rsplit(" ", 1)[0].rstrip() or summary[: _MAX - 2]
        summary = f"{clipped}…"
    return summary


def _escape_discord_markdown(text: str) -> str:
    """Backslash-escape every Discord-markdown control character in ``text``
    (:data:`_DISCORD_MARKDOWN_CHARS`), the backslash first so the escapes this
    helper inserts are never themselves re-escaped.

    Composers call this once on the whole composed summary, so no interpolated
    league-supplied name can italicize, embolden, quote, table-ify or code-span
    a posted message — the literal characters reach Discord intact."""
    for char in _DISCORD_MARKDOWN_CHARS:
        text = text.replace(char, f"\\{char}")
    return text


def _lead_sentence(sections: Sequence[tuple[str | None, list[str]]]) -> str:
    """The first non-empty block of ``sections[0]``, whitespace-collapsed and, if
    long, truncated at a word boundary near :data:`_LEAD_BUDGET` with an ellipsis.

    Declared over a covariant :class:`~collections.abc.Sequence` so both the
    draft path (``list[tuple[str | None, list[str]]]``, from the shared
    ``_body`` helpers) and the weekly path (``list[tuple[str, list[str]]]``, from
    the narrower ``WeeklySection`` heading) call it without a copy."""
    if not sections:
        return ""
    _, blocks = sections[0]
    block = next((b for b in blocks if b.strip()), "")
    text = " ".join(block.split())
    if len(text) <= _LEAD_BUDGET:
        return text
    head = text[:_LEAD_BUDGET].rsplit(" ", 1)[0].rstrip()
    return f"{head}…" if head else text[:_LEAD_BUDGET].rstrip()
