"""The one table of weekly Issue section ids and their display headings (AD-30).

A section's **id** is permanent (``lead``, ``around_league``, ``standings``,
``power``, ``luck``, ``next_week``, ``transactions``); its **heading** is display
text derived from the id. Week-1 cold start shares the warm ``standings`` id but
shows the heading "Standings" rather than "Standings and the Playoff Picture".

Stdlib only, on purpose: ``voices/beat_writer.py`` keeps an import fence (no
``commishdesk.narrate``), and the voice prompt must read the same table the
template, validator and renderers do. ``narrate/weekly_template.py`` re-exports
the derived heading tuples; nothing else spells a heading literal.
"""

from __future__ import annotations

from collections.abc import Iterable

__all__ = [
    "COLD_START_SECTION_IDS",
    "SECTION_HEADINGS_BY_ID",
    "SECTION_IDS",
    "effective_suppressions",
    "heading_for",
    "id_for_heading",
    "section_ids_for",
]

#: The seven reference sections, in canonical order.
SECTION_IDS: tuple[str, ...] = (
    "lead",
    "around_league",
    "standings",
    "power",
    "luck",
    "next_week",
    "transactions",
)

#: The four sections a Week-1 Issue carries.
COLD_START_SECTION_IDS: tuple[str, ...] = ("lead", "around_league", "standings", "next_week")

#: id -> warm display heading.
SECTION_HEADINGS_BY_ID: dict[str, str] = {
    "lead": "The Lead",
    "around_league": "Around the League",
    "standings": "Standings and the Playoff Picture",
    "power": "Power Rankings",
    "luck": "The Luck Index",
    "next_week": "Next Week",
    "transactions": "The Transaction Desk",
}

#: Cold-start headings that differ from the warm one.
_COLD_START_HEADING_OVERRIDES: dict[str, str] = {"standings": "Standings"}


def heading_for(section_id: str, *, cold_start: bool = False) -> str:
    """The display heading for *section_id* (``KeyError`` on an unknown id)."""
    if cold_start and section_id in _COLD_START_HEADING_OVERRIDES:
        return _COLD_START_HEADING_OVERRIDES[section_id]
    return SECTION_HEADINGS_BY_ID[section_id]


def id_for_heading(heading: str) -> str | None:
    """The id a display heading belongs to (warm or cold form), else ``None``."""
    for section_id in SECTION_IDS:
        if heading in (heading_for(section_id), heading_for(section_id, cold_start=True)):
            return section_id
    return None


def section_ids_for(has_prior_week: bool) -> tuple[str, ...]:
    """The Issue's section ids: seven warm, four at Week-1 cold start."""
    return SECTION_IDS if has_prior_week else COLD_START_SECTION_IDS


def effective_suppressions(
    suppressed: Iterable[str] | None, has_prior_week: bool
) -> frozenset[str]:
    """The suppressed ids that name a section this Issue actually carries — an id
    absent from the Issue's own set (``power`` in Week 1) is ignored."""
    if not suppressed:
        return frozenset()
    return frozenset(suppressed) & frozenset(section_ids_for(has_prior_week))
