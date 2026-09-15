"""Story 4.2 — body-parsing + escaping primitives shared by the render surfaces.

``render/web.py`` (Story 4.1) and ``render/email.py`` (Story 4.2) turn the *same*
narrated body — the template narrator's :class:`~commishdesk.narrate.Recap`
sections **or** the LLM narrator's plain-text prose — into one ordered list of
``(heading, blocks)`` runs, and both escape every interpolated value identically
(HTML-escape after stripping Unicode bidirectional control characters). This
module also carries the facts-derived *logic and text* the two surfaces would
otherwise reimplement independently (and drift apart on — epic-4 retro item
60): the effective round count, the at-a-glance strip content, the
verdict-chip/board-mark delta bucketing, and the footer stamp + provenance
sentence. Never markup — each surface keeps its own HTML. The returned text is
still HTML-entity-encoded (``&middot;``, ``&rsquo;``), not raw plain text — a
future plain-text consumer must not reuse these helpers verbatim.

**Pipeline fence (AD-1).** Standard library, :class:`commishdesk.narrate.Recap`,
``commishdesk.facts.schema`` types, and :mod:`commishdesk.render.style` only —
the latter reachable solely for ``position_label`` (used by ``_r1_split``), not
a precedent for broader ``render.style`` imports. Nothing from ``ingest`` /
``stats`` / ``narrate`` internals, no cloud or HTTP SDK.
"""

from __future__ import annotations

import html
import re
from typing import Literal

from commishdesk.facts.schema import DraftRecapFacts, PickRow
from commishdesk.narrate import Recap
from commishdesk.render.style import position_label

__all__ = [
    "_at_a_glance_items",
    "_effective_rounds",
    "_esc",
    "_footer_stamp",
    "_plain",
    "_provenance_sentence",
    "_sections_from_llm",
    "_sections_from_recap",
    "_strip_markers",
    "_surname",
    "_verdict_bucket",
]

#: A Markdown ATX heading line with real text after the marker (``## The Lead``).
#: A bare marker (``##`` / ``## ``) does not match — it falls through to a ``<p>``.
_HEADING_LINE = re.compile(r"^#{1,6}[ \t]+(\S.*?)\s*$")

#: A leading ATX marker, for stripping ``## The Lead`` down to ``The Lead`` in the
#: plain-text surface (where a heading has no markup to carry it).
_LEADING_MARKER = re.compile(r"^#{1,6}[ \t]+")

#: Name suffixes that are kept attached to the surname ("Marvin Harrison Jr.").
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
#: Nobiliary / lowercase particles kept attached to the surname ("Amon-Ra St. Brown").
_NAME_PARTICLES = {"van", "von", "de", "del", "der", "la", "le", "ter", "di", "da"}

#: Unicode bidirectional control characters — stripped from every interpolated
#: value so a lone directional mark cannot reorder surrounding markup / labels.
_BIDI_CONTROLS = dict.fromkeys(
    [0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)]
)


def _esc(value: object) -> str:
    """Strip Unicode bidi controls from ``value`` then HTML/SVG-escape it (``<``,
    ``>``, ``&``, quotes)."""
    return html.escape(str(value).translate(_BIDI_CONTROLS), quote=True)


def _plain(value: object) -> str:
    """Strip Unicode bidi controls from ``value`` and return it as a plain string —
    **no** HTML-escaping: the caller is writing a ``text/plain`` surface where
    ``<`` and ``&`` are literal, but a lone directional mark can still reorder the
    line."""
    return str(value).translate(_BIDI_CONTROLS)


def _strip_markers(text: str) -> str:
    """Normalise newlines to ``\\n`` and drop a leading ``#``-``######`` ATX
    marker from every line — the plain-text surface has no markup to carry a
    heading, so ``## The Lead`` becomes ``The Lead``. Bidi controls are stripped
    too (a plain-text file is still reorder-able by a lone directional mark)."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").translate(_BIDI_CONTROLS)
    return "\n".join(_LEADING_MARKER.sub("", line) for line in normalized.split("\n"))


def _sections_from_recap(recap: Recap) -> list[tuple[str | None, list[str]]]:
    return [(section.heading, list(section.blocks)) for section in recap.sections]


def _sections_from_llm(text: str) -> list[tuple[str | None, list[str]]]:
    """Parse the LLM narrator's plain-text prose into ``(heading, blocks)`` runs.

    Newlines normalised to ``\\n``; a ``#``-``######`` line with text after the
    marker opens a new section; every other blank-line-separated run of prose is a
    block (wrapped lines joined with a single space). The first line is **body
    prose**, never the title — a leading untitled empty run is dropped.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    sections: list[tuple[str | None, list[str]]] = [(None, [])]
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            sections[-1][1].append(" ".join(paragraph))
            paragraph.clear()

    for raw in normalized.split("\n"):
        line = raw.strip()
        if not line:
            flush()
            continue
        heading = _HEADING_LINE.match(line)
        if heading:
            flush()
            sections.append((heading.group(1), []))
        else:
            paragraph.append(line)
    flush()

    if sections and sections[0][0] is None and not sections[0][1]:
        sections.pop(0)
    return sections


def _surname(name: str) -> str:
    """The display surname for a compact label: the last token, pulling in a
    trailing nobiliary particle ("Amon-Ra St. Brown" -> "St. Brown") and keeping a
    generational suffix attached ("Marvin Harrison Jr." -> "Harrison Jr.")."""
    parts = [token for token in name.split() if token]
    if not parts:
        return name

    def is_particle(token: str) -> bool:
        return token.endswith(".") or token.lower() in _NAME_PARTICLES

    if len(parts) >= 2 and parts[-1].lower().strip(".") in _NAME_SUFFIXES:
        start = -3 if len(parts) >= 3 and is_particle(parts[-3]) else -2
        return " ".join(parts[start:])
    if len(parts) >= 2 and is_particle(parts[-2]):
        return " ".join(parts[-2:])
    return parts[-1]


# --------------------------------------------------------------------------- #
# Facts-derived helpers shared by render/web.py and render/email.py — logic
# and text only, never markup (retro item 60: these drifted apart when each
# surface reimplemented them independently).
# --------------------------------------------------------------------------- #


def _effective_rounds(facts: DraftRecapFacts, picks: list[PickRow]) -> int:
    """The round count the board must span: the declared ``draft.rounds`` when it
    is a positive int, widened to the largest real pick round so a missing / zero
    / negative / too-small declared value never truncates the grid."""
    declared = facts.draft.rounds
    declared = declared if isinstance(declared, int) and declared > 0 else 0
    return max(declared, max((pick.round for pick in picks), default=0))


def _r1_split(round1_positional: dict[str, int]) -> str:
    ordered = sorted(round1_positional.items(), key=lambda kv: (-kv[1], kv[0]))
    return " / ".join(f"{count} {position_label(pos)}" for pos, count in ordered)


def _at_a_glance_items(
    facts: DraftRecapFacts, picks: list[PickRow], team_count: int, rounds: int
) -> list[str]:
    """The at-a-glance strip content, shared by both surfaces: pick/round/team
    counts, the round-1 positional split, and (when present) who made the most
    and the fewest picks. Each surface joins these items its own way (web: a
    separate strip item per entry; email: joined with ``"  ·  "``)."""
    summary = facts.draft_summary
    items = [
        f"{len(picks)} picks",
        f"{rounds} rounds",
        f"{team_count} teams",
    ]
    if summary.round1_positional:
        items.append(f"round 1: {_r1_split(summary.round1_positional)}")
    rank = summary.pick_count_rank
    if rank:
        leader, low = rank[0], rank[-1]
        if leader.manager:
            items.append(f"most picks: {leader.manager} ({leader.pick_count})")
        if low.manager and low is not leader:
            items.append(f"fewest: {low.manager} ({low.pick_count})")
    return items


def _verdict_bucket(delta: int) -> Literal["fair", "value", "reach"]:
    """The verdict-chip / board-mark bucket for a consensus delta: ``|delta| <=
    2`` is a neutral "fair" pick, ``delta > 2`` is "value" (drafted later than
    consensus), ``delta < -2`` is a "reach" (drafted earlier). One threshold for
    both render surfaces (retro item 60: web and email disagreed on this)."""
    if delta > 2:
        return "value"
    if delta < -2:
        return "reach"
    return "fair"


def _footer_stamp(
    facts: DraftRecapFacts, team_count: int, rounds: int, generated_at: str
) -> str:
    league = facts.league
    return (
        f"{_esc(league.name)} &middot; {_esc(league.season)} &middot; {team_count} teams "
        f"&middot; {rounds} rounds &middot; generated {_esc(generated_at)}"
    )


def _provenance_sentence(against: str) -> str:
    """The footer provenance sentence — identical wording and the typographic
    apostrophe (``&rsquo;``) on both surfaces. ``against`` is the already-escaped
    ``" against {consensus source}"`` clause (or ``""``); each surface wraps this
    text in its own markup."""
    return (
        "Every pick and board label is drawn straight from the draft record; "
        f"consensus deltas and grades are the engine&rsquo;s own, measured{against}."
    )
