"""Stage 4 — the zero-credential weekly template narrator (AD-1 / FR-15 / FR-51).

:func:`render_weekly_issue` turns the sanitized ``WeeklyNarration`` projection of
a weekly Facts document into a :class:`WeeklyIssue` — a masthead title, a
dateline, and the seven reference sections: the lead, around the league, the
standings and the playoff picture, the power rankings, the luck index, next week,
and the transaction desk. :func:`weekly_issue_to_text` flattens a
:class:`WeeklyIssue` to plain prose for stdout and for
:func:`~commishdesk.narrate.safety.check_narration`.

**AD-1 pipeline-isolation fence.** This module imports only
:mod:`commishdesk.facts.schema` schema types + the standard library. It reads
**only** :class:`~commishdesk.facts.schema.WeeklyNarration` — never a
``WeeklyFacts`` ``teams`` / ``matchups`` / ``standings`` / ``leaders`` block.
There is no LLM, no prompt, no network, and no clock here: the template narrator
is the deterministic floor the LLM narrator layers on top of. Two calls on one
``narration`` return an equal ``model_dump()``.

**Closed-world safe by construction.** Every digit the prose carries is either
emitted verbatim from the ``WeeklyNarration`` (so the token is in the payload by
definition) or a small count rendered as a word by :func:`_spell`. No aggregate
is ever computed and printed as a numeral — ``check_narration``'s closed-world
check refutes any digit-bearing token the payload does not contain, and a sum of
two real numbers is a number nothing in this world happened at.

:func:`_spell` is a copy of ``commishdesk.facts.leads._spell`` and
:data:`_BLOWOUT_LOSER_RATIO` is a copy of ``stats/weekly.py``'s
``blowout_threshold`` — copied, not imported, so the fence holds. The threshold
lives on ``period.summary``, which is not part of the narration projection, so it
is out of reach on this side of the fence.

**No empty section.** Every section carries at least one block: where there is
nothing to report it renders one deliberate stand-down line instead.

Story 5.12 adds the two shape helpers the weekly **LLM** narrator's caller needs:
:func:`weekly_issue_from_text` parses narrated Markdown back into the same
:class:`WeeklyIssue` this module emits (so every downstream surface — the
UNVERIFIED dateline stamp, the Correction prepend, the HTML/text writers and
``render_weekly_discord_post`` — is shared unchanged between the two
narrators), and :func:`parse_published_ranks` reads the published power ranks
back out of that text.

Story 5.15 adds one sentence to the playoff picture when
``WeeklyNarrationPlayoff.seeding_unconfirmed`` is true, so a bracket that was
only *derived* — never confirmed, never overridden — says so plainly.

Story 5.16 adds the Week-1 cold-start section set
(:data:`COLD_START_SECTION_HEADINGS`): four sections, no Power Rankings, no Luck
Index, no playoff picture and no transaction desk, chosen through one shared
derivation of ``has_prior_week`` for narrators and renderers.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

from commishdesk.facts.schema import (
    WeeklyNarration,
    WeeklyNarrationGame,
    WeeklyNarrationLuck,
    WeeklyNarrationNextWeek,
    WeeklyNarrationPlayoff,
    WeeklyNarrationPower,
    WeeklyNarrationStanding,
    WeeklyNarrationTradeGrade,
)
from commishdesk.sections import (
    COLD_START_SECTION_IDS,
    SECTION_IDS,
    effective_suppressions,
    heading_for,
    id_for_heading,
    section_ids_for,
)

__all__ = [
    "COLD_START_SECTION_HEADINGS",
    "SECTION_HEADINGS",
    "WeeklyIssue",
    "WeeklySection",
    "parse_nudge_justifications",
    "parse_published_ranks",
    "cold_start_standings",
    "render_weekly_issue",
    "revert_sections_to_template",
    "section_headings_for_has_prior_week",
    "section_headings_for_narration",
    "weekly_issue_from_text",
    "weekly_issue_to_text",
]


# --------------------------------------------------------------------------- #
# Output model
# --------------------------------------------------------------------------- #


class _Frozen(BaseModel):
    """Immutable, closed to unknown keys — matches the house model style."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class WeeklySection(_Frozen):
    """One newsletter section: a heading and its ordered prose blocks."""

    heading: str
    blocks: list[str] = []

    @property
    def section_id(self) -> str | None:
        """The permanent id behind this heading (AD-30), or ``None`` for a section
        outside the seven (a reissue's Correction)."""
        return id_for_heading(self.heading)


class WeeklyIssue(_Frozen):
    """A rendered weekly Issue: a masthead title, a one-line dateline, and the
    seven reference sections."""

    title: str
    dateline: str
    sections: list[WeeklySection] = []


#: The seven reference sections' display headings, in order — derived from the
#: one id table in :mod:`commishdesk.sections`; the shape
#: :func:`weekly_issue_from_text` requires of a narrated Issue.
SECTION_HEADINGS: tuple[str, ...] = tuple(heading_for(section_id) for section_id in SECTION_IDS)

#: The four sections a Week-1 Issue carries (Story 5.16). Power, luck,
#: storylines, playoff picture and the transaction desk stand down entirely.
COLD_START_SECTION_HEADINGS: tuple[str, ...] = tuple(
    heading_for(section_id, cold_start=True) for section_id in COLD_START_SECTION_IDS
)

#: The week at which all-play / expected-wins / luck become meaningful. The
#: weekly narration projection carries ``league.week`` but not the Facts
#: ``period.has_prior_week`` flag, so this is the projection's equivalent
#: derivation, copied from ``stats/weekly.py::MEANINGFUL_FROM_WEEK``.
_MEANINGFUL_FROM_WEEK = 2


def section_headings_for_has_prior_week(
    has_prior_week: bool, suppressed: Iterable[str] | None = None
) -> tuple[str, ...]:
    """The Issue's section set for a weekly run.

    ``has_prior_week=False`` is the Story 5.16 cold-start marker: four sections,
    no Power/Luck/playoff/transaction headings. ``True`` keeps the seven-section
    warm set.

    *suppressed* (AD-30) is a set of section ids the LLM is not asked for: their
    headings are left out. An id outside this Issue's own set is ignored, and
    ``None`` / empty returns the full set.
    """
    if not suppressed:
        return SECTION_HEADINGS if has_prior_week else COLD_START_SECTION_HEADINGS
    dropped = effective_suppressions(suppressed, has_prior_week)
    return tuple(
        heading_for(section_id, cold_start=not has_prior_week)
        for section_id in section_ids_for(has_prior_week)
        if section_id not in dropped
    )


def section_headings_for_narration(
    narration: WeeklyNarration,
    has_prior_week: bool | None = None,
    suppressed: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """The section set for *narration*, through the shared cold-start switch.

    Callers holding the Facts pass ``period.has_prior_week``; the narration
    projection does not carry it, so ``None`` falls back to the equivalent
    ``league.week`` derivation.
    """
    if has_prior_week is None:
        has_prior_week = narration.league.week >= _MEANINGFUL_FROM_WEEK
    return section_headings_for_has_prior_week(has_prior_week, suppressed)


# --------------------------------------------------------------------------- #
# Number words — a copy of commishdesk.facts.leads._spell (AD-1: do NOT import)
# --------------------------------------------------------------------------- #

_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
)


def _spell(n: int) -> str:
    """Small non-negative integers as words (``5`` -> ``"five"``); anything out
    of range falls back to the digits."""
    return _ONES[n] if 0 <= n < len(_ONES) else str(n)


def _join_names(names: list[str]) -> str:
    """``["a"]`` -> ``"a"``; ``["a", "b"]`` -> ``"a and b"``; longer -> Oxford list."""
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])}, and {names[-1]}"


def _count(n: int, singular: str) -> str:
    """``_count(1, "move")`` -> ``"one move"``; ``_count(3, "move")`` -> ``"three
    moves"`` — a spelled count with the noun agreeing in number."""
    return f"{_spell(n)} {singular if n == 1 else singular + 's'}"


def _label(value: str | None) -> str:
    """A never-empty display label — the narration's own ``None`` never reaches
    the prose."""
    return value if value else _UNCLAIMED


# --------------------------------------------------------------------------- #
# Tunables (copies — AD-1 keeps the fence; see the module docstring)
# --------------------------------------------------------------------------- #

#: The loser/winner score ratio below which a game is a blowout. A copy of
#: ``stats/weekly.py``'s ``blowout_threshold``, which rides on the non-narration
#: ``period.summary`` block and is therefore unreachable here.
_BLOWOUT_LOSER_RATIO = 0.65

#: A margin this small or smaller is a nail-biter. The matrix's one-point game
#: (``week02-nailbiter.json``) sits well inside it; the ordinary games in
#: ``week10-blowout.json`` (closest margin 6.06) sit outside it.
_CLOSE_GAME_MARGIN = 3.0

_UNCLAIMED = "an unclaimed roster"

#: Storyline kind of the transient lopsided-trade candidate (a copy of
#: ``facts.weekly.TRADE_LOPSIDED_KIND``, which is out of reach here: AD-1). The transactions
#: section states it as a fact line, so Around the League skips it.
_TRADE_LOPSIDED_KIND = "trade_lopsided"

_QUIET_WIRE = "A quiet week on the wire: no moves this week and no recent trades."
_NO_LUCK = "Not enough games have been played to measure luck."
_NO_POWER = "No power rankings are available for this week."
_NO_STANDINGS = "No standings are available for this league yet."
_NO_PLAYOFFS = "No playoff picture is available for this league."
_NO_GAMES = "No games were played this week, and no storyline is running."
_NO_SCHEDULE = "No games are on the schedule for next week."
_SEEDING_NOTE = "Seeding unconfirmed, derived from the standings."


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #


def render_weekly_issue(
    narration: WeeklyNarration, *, has_prior_week: bool | None = None
) -> WeeklyIssue:
    """Build the :class:`WeeklyIssue` from ``narration`` alone.

    Pure and deterministic — no timestamp, no network, no model. The caller (the
    weekly CLI path, Story 5.11a) stamps ``generated_at`` where it wants it; the
    narrator does not see it (it is not part of the ``narration`` projection).
    """
    title, dateline = _masthead(narration)
    cold_start = section_headings_for_narration(narration, has_prior_week) == COLD_START_SECTION_HEADINGS
    if cold_start:
        sections = [
            _lead_section(narration),
            _around_section(narration, include_storylines=False),
            _standings_section(narration, cold_start=True),
            _next_week_section(narration),
        ]
    else:
        sections = [
            _lead_section(narration),
            _around_section(narration),
            _standings_section(narration),
            _power_section(narration),
            _luck_section(narration),
            _next_week_section(narration),
            _transactions_section(narration),
        ]
    return WeeklyIssue(title=title, dateline=dateline, sections=sections)


def weekly_issue_to_text(issue: WeeklyIssue) -> str:
    """Flatten a :class:`WeeklyIssue` to plain UTF-8 prose (``\\n`` newlines)."""
    lines: list[str] = [issue.title, issue.dateline]
    for section in issue.sections:
        lines.append("")
        lines.append(f"## {section.heading}")
        for block in section.blocks:
            lines.append("")
            lines.append(block)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Narrated Markdown -> WeeklyIssue (Story 5.12; the weekly LLM narrator's caller)
# --------------------------------------------------------------------------- #

#: A Markdown ATX heading line (``## Power Rankings``), capturing its text.
_ATX_LINE = re.compile(r"^[ \t]*#{1,6}[ \t]+(\S.*?)[ \t]*$")

#: One numbered list item inside the power-rankings section: a leading ordinal,
#: its ``.`` / ``)``, then the line's remaining text.
_RANK_LINE = re.compile(r"^[ \t]*\**[ \t]*(\d{1,2})[.)][ \t]+(\S.*)$")


def _split_sections(text: str) -> list[WeeklySection] | None:
    """Split narrated Markdown into :class:`WeeklySection`\\ s.

    A run of consecutive non-blank lines becomes one block (a paragraph); blank
    lines separate blocks. Everything before the first ``## `` heading is the
    Issue's own title line and is dropped — the masthead is rebuilt from the
    narration so a parsed Issue and the template's own Issue are indistinguishable
    downstream.

    ``None`` when the text carries no ``## `` heading at all, or repeats one:
    there is nothing safe to parse, and guessing would be worse than falling
    back to the template.
    """
    sections: list[WeeklySection] = []
    heading: str | None = None
    blocks: list[str] = []
    paragraph: list[str] = []
    saw_heading = False

    def flush() -> None:
        if paragraph:
            blocks.append(" ".join(paragraph).strip())
            paragraph.clear()

    for line in text.split("\n"):
        match = _ATX_LINE.match(line)
        if match is not None and heading is None and line.lstrip().startswith("# "):
            continue  # a ``# `` title line before the first ``## `` section is the masthead, not a section
        if match is not None:
            saw_heading = True
            flush()
            if heading is not None:
                sections.append(WeeklySection(heading=heading, blocks=blocks))
            heading = match.group(1).strip()
            if any(section.heading == heading for section in sections):
                return None  # a repeated heading: guessing which one wins is worse than the template
            blocks = []
            continue
        if heading is None:
            continue
        if not line.strip():
            flush()
            continue
        paragraph.append(line.strip())
    flush()
    if heading is not None:
        sections.append(WeeklySection(heading=heading, blocks=blocks))
    if not saw_heading:
        return None
    return sections


def weekly_issue_from_text(
    text: str,
    narration: WeeklyNarration,
    *,
    has_prior_week: bool | None = None,
    suppressed: Iterable[str] | None = None,
) -> WeeklyIssue | None:
    """Parse a narrated weekly Issue's Markdown back into a :class:`WeeklyIssue`.

    Requires every heading in :func:`section_headings_for_narration` (the seven
    warm sections, or the four cold-start sections at Week 1), each with at least
    one block (the same "no empty section heading" rule the template narrator
    holds itself to); the sections are then re-ordered into the canonical order
    and any extra section the narrator added is dropped warm, rejected cold. The
    masthead is rebuilt from *narration*, never taken from the text.

    *suppressed* (AD-30) names section ids the narrator was never asked for: the
    expected set shrinks by those headings, and each suppressed section is
    spliced in from :func:`render_weekly_issue` (template prose, its numbers
    intact) so the returned Issue always carries the full canonical set. A
    completion that writes a suppressed heading anyway has it ignored warm
    (the template's wins) and is rejected cold, like any other extra section.

    ``None`` when the text is not the expected shape — the caller then degrades
    to the template narrator rather than shipping a malformed dump.
    """
    sections = _split_sections(text)
    if sections is None:
        return None
    if has_prior_week is None:
        has_prior_week = narration.league.week >= _MEANINGFUL_FROM_WEEK
    cold_start = not has_prior_week
    dropped = effective_suppressions(suppressed, has_prior_week)
    full = section_headings_for_has_prior_week(has_prior_week)
    headings = section_headings_for_has_prior_week(has_prior_week, dropped)
    by_heading = {section.heading: section for section in sections}
    if any(not by_heading.get(name, WeeklySection(heading=name, blocks=[])).blocks for name in headings):
        return None
    if cold_start:
        # A cold-start completion must not reintroduce a stood-down section.
        if {section.heading for section in sections} != set(headings):
            return None
    template_by_heading = (
        {section.heading: section for section in render_weekly_issue(narration, has_prior_week=has_prior_week).sections}
        if dropped
        else {}
    )
    ordered = [by_heading[name] if name in headings else template_by_heading[name] for name in full]
    title, dateline = _masthead(narration)
    return WeeklyIssue(title=title, dateline=dateline, sections=ordered)


def revert_sections_to_template(
    issue: WeeklyIssue,
    narration: WeeklyNarration,
    section_ids: Iterable[str],
    *,
    has_prior_week: bool | None = None,
) -> WeeklyIssue:
    """A copy of a finished *issue* whose sections named by *section_ids* carry the
    template narrator's prose (the AD-30 splice, applied after the fact).

    Each named section is replaced in place by the same-id section of
    :func:`render_weekly_issue`; every other section, the masthead and any section
    outside the seven (a reissue's Correction) are untouched. An id the Issue does
    not carry is ignored. The spliced text is the template's own, already gated by
    the template path, so it is not re-run through ``check_narration``.
    """
    if has_prior_week is None:
        has_prior_week = narration.league.week >= _MEANINGFUL_FROM_WEEK
    wanted = effective_suppressions(section_ids, has_prior_week)
    if not wanted:
        return issue
    template_by_id = {
        section.section_id: section
        for section in render_weekly_issue(narration, has_prior_week=has_prior_week).sections
    }
    sections = [
        template_by_id[sid] if (sid := section.section_id) is not None and sid in wanted else section
        for section in issue.sections
    ]
    return issue.model_copy(update={"sections": sections})


def _power_section_lines(text: str) -> list[str]:
    """The raw lines under the "Power Rankings" heading, up to the next heading.
    Raw lines, not :func:`_split_sections` blocks: that splitter joins consecutive
    lines into one paragraph, which would hide every item of a tight list."""
    lines: list[str] = []
    inside = False
    for line in text.splitlines():
        match = _ATX_LINE.match(line)
        if match is not None:
            inside = match.group(1).strip() == heading_for("power")
            continue
        if inside:
            lines.append(line)
    return lines


def _parse_rank_rows(text: str, narration: WeeklyNarration) -> dict[str, tuple[int, str]]:
    """``roster_id -> (published rank, the rest of that numbered line)`` from the
    "Power Rankings" section only. A line counts when it is numbered and its text
    after the number begins with one of the narration's own team labels (longest
    label first, so a label that is a prefix of another cannot shadow it)."""
    labels = sorted(
        ((row.team, row.roster_id) for row in narration.power if row.team),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    rows: dict[str, tuple[int, str]] = {}
    for line in _power_section_lines(text):
        match = _RANK_LINE.match(line)
        if match is None:
            continue
        rest = match.group(2).lstrip("*_ ")
        for label, roster_id in labels:
            if roster_id in rows:
                continue
            if rest.startswith(label):
                rows[roster_id] = (int(match.group(1)), rest[len(label) :].lstrip("*_ :—–-,.").strip())
                break
    return rows


def parse_published_ranks(text: str, narration: WeeklyNarration) -> dict[str, int]:
    """The published power ranks narrated Markdown states, as ``roster_id -> rank``.

    Anything else — a stray numbered line, a section that is missing, a roster the
    narrator never ranked — is simply absent from the result; the caller decides
    what a missing rank means.
    """
    return {roster_id: rank for roster_id, (rank, _rest) in _parse_rank_rows(text, narration).items()}


def parse_nudge_justifications(text: str, narration: WeeklyNarration) -> dict[str, str]:
    """``roster_id -> justification`` for each ranked team whose published rank
    differs from its ``model_rank``: the text of that numbered line after the team
    label. A team the narrator agreed with, or one with no text after its label,
    is absent."""
    by_roster = {row.roster_id: row for row in narration.power}
    out: dict[str, str] = {}
    for roster_id, (rank, rest) in _parse_rank_rows(text, narration).items():
        row = by_roster.get(roster_id)
        if row is not None and row.model_rank is not None and rank != row.model_rank and rest:
            out[roster_id] = rest
    return out


# --------------------------------------------------------------------------- #
# Section builders — each a pure function of the narration
# --------------------------------------------------------------------------- #


def _masthead(narration: WeeklyNarration) -> tuple[str, str]:
    """The nameplate: a title and a one-line dateline.

    The masthead always uses the league's own (already sanitized) name — an
    optional ``paper_name`` override is deferred work, because no league-config
    reading mechanism exists anywhere in the repo yet.
    """
    league = narration.league
    title = f"{league.name} — Week {league.week} Recap"
    dateline = (
        f"{league.name} · {league.season} season · Week {league.week} · "
        f"{league.team_count} teams"
    )
    return title, dateline


def _pick(frames: tuple[str, ...], narration: WeeklyNarration, *parts: object) -> str:
    """One frame from *frames*, chosen by a stable hash of the league, season, week
    and *parts* (never ``hash()``, which is salted per process), so one frozen
    input renders byte-identically (I4)."""
    league = narration.league
    key = "|".join(str(part) for part in (league.name, league.season, league.week, *parts))
    return frames[int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16) % len(frames)]


def _lead_section(narration: WeeklyNarration) -> WeeklySection:
    """Every deterministic lead angle Story 5.9's facts builder ranked, one sentence
    each, best first. The renderers already print ``printed_headline`` above The
    Lead, so that hook is not repeated here; the short count line stands in only
    when no other hook remains."""
    league = narration.league
    ranked = sorted(narration.lead_candidates, key=lambda candidate: candidate.rank)
    blocks = [
        candidate.hook
        for candidate in ranked
        if candidate.hook and candidate.hook != narration.printed_headline
    ]
    if not blocks:
        blocks.append(
            f"Week {league.week}: {_count(len(narration.games), 'game')} played, "
            f"{_count(league.team_count, 'team')} in the standings."
        )
    return WeeklySection(heading=heading_for("lead"), blocks=blocks)


def _game_family(game: WeeklyNarrationGame) -> str:
    """Which sentence family a scoreline earns: ``"tie"``, ``"close"``,
    ``"blowout"`` or ``"ordinary"``.

    Close is checked before blowout so a game that is both (a two-point game
    between two very low scores) reads as the nail-biter it is.
    """
    if game.winner is None:
        return "tie"
    if game.margin <= _CLOSE_GAME_MARGIN:
        return "close"
    if game.winner_pts > 0 and game.loser_pts < _BLOWOUT_LOSER_RATIO * game.winner_pts:
        return "blowout"
    return "ordinary"


#: Sentence frames per family. Fields: ``w`` winner, ``l`` loser, ``wp`` / ``lp``
#: their points, ``m`` the margin. Each family's verbs are its own.
_GAME_FRAMES: dict[str, tuple[str, ...]] = {
    "tie": (
        "{l} finished level at {lp}.",
        "Neither side blinked: {l} ended level at {lp}.",
        "{l} ended in a dead heat at {lp}.",
    ),
    "close": (
        "{w} edged {l} by {m}: {wp}-{lp}.",
        "{w} held off {l}, {wp} to {lp}. The margin was {m}.",
        "{l} came up {m} short against {w}, {lp} to {wp}.",
        "{w} got there by {m} against {l}, {wp} to {lp}.",
    ),
    "blowout": (
        "{w} blew past {l}, {wp} to {lp}.",
        "{w} ran {l} off the field, {wp} to {lp}.",
        "{l} never got going against {w}, {lp} to {wp}.",
        "{w} made it look easy against {l}, {wp} to {lp}.",
        "{w} rolled {l} by {m}, {wp} to {lp}.",
    ),
    "ordinary": (
        "{w} beat {l}, {wp} to {lp}.",
        "{w} got past {l}, {wp} to {lp}.",
        "{w} took down {l}, {wp} to {lp}.",
        "{l} fell to {w}, {lp} to {wp}.",
        "{w} handled {l}, {wp} to {lp}.",
    ),
}

#: The winner's record movement; ``{rec}`` is the standings record after the week.
#: The frames never repeat the winner's label: the game sentence just named it.
_RECORD_FRAMES = (
    "That makes it {rec}.",
    "Now {rec} on the season.",
    "The record reads {rec} after it.",
)

#: The top scorer, with and without the team that scorer plays for.
_TOP_FRAMES = (
    "{top} topped the scoring.",
    "{top} led all scorers.",
    "{top} had the game's best score.",
)
_TOP_TEAM_FRAMES = (
    "{top} topped the scoring for {tt}.",
    "{top} led all scorers for {tt}.",
    "{tt} got the game's best score from {top}.",
)
#: When the top scorer's team is the winner or the loser the game sentence already
#: named it, so the clause says which side instead of repeating the label.
_TOP_WINNER_FRAMES = (
    "{top} led the way.",
    "{top} had the game's best score.",
    "{top} topped the scoring.",
)
_TOP_LOSER_FRAMES = (
    "{top} led all scorers in defeat.",
    "{top} had the game's best score in a losing effort.",
    "{top} topped the scoring on the losing side.",
)


def _game_line(
    game: WeeklyNarrationGame,
    narration: WeeklyNarration,
    *,
    index: int = 0,
    records: dict[str, str] | None = None,
) -> str:
    """One game, in the sentence family its scoreline earned. The frame is chosen by
    a stable hash of the league, season, week and matchup id (the game's position
    when it carries no id). *records* maps a team label to its post-week record;
    when it holds the winner, the line carries the record movement."""
    family = _game_family(game)
    anchor = game.matchup_id if game.matchup_id is not None else f"g{index}"
    fields = {
        "w": _label(game.winner),
        "l": _label(game.loser),
        "wp": game.winner_pts,
        "lp": game.loser_pts,
        "m": game.margin,
    }
    line = _pick(_GAME_FRAMES[family], narration, "game", anchor).format(**fields)
    rec = records.get(game.winner) if records and game.winner else None
    if rec:
        line += " " + _pick(_RECORD_FRAMES, narration, "rec", anchor).format(rec=rec, **fields)
    if game.top:
        if game.top_team and game.winner and game.top_team == game.winner:
            frame = _pick(_TOP_WINNER_FRAMES, narration, "top", anchor)
        elif game.top_team and game.loser and game.winner and game.top_team == game.loser:
            frame = _pick(_TOP_LOSER_FRAMES, narration, "top", anchor)
        elif game.top_team:
            frame = _pick(_TOP_TEAM_FRAMES, narration, "top", anchor)
        else:
            frame = _pick(_TOP_FRAMES, narration, "top", anchor)
        line += " " + frame.format(top=game.top, tt=game.top_team)
    return line


def _unique_records(rows: list[WeeklyNarrationStanding]) -> dict[str, str]:
    """``team label -> record`` for the labels that occur exactly once: a shared
    label cannot be joined to a record without guessing, so it is left out."""
    seen: dict[str, int] = {}
    for row in rows:
        seen[row.team] = seen.get(row.team, 0) + 1
    return {row.team: row.rec for row in rows if seen[row.team] == 1 and row.rec}


def _around_section(
    narration: WeeklyNarration, *, include_storylines: bool = True
) -> WeeklySection:
    """Every game of the week, then the multi-week threads Story 5.9's weekly
    storyline lifecycle is carrying: skipping any hook the lead already used.

    ``include_storylines=False`` is the cold-start call (Story 5.16): Week 1 has
    no prior week, so no multi-week thread can exist and the section is the
    scores alone, with no record movement either."""
    records = _unique_records(narration.standings) if include_storylines else None
    blocks: list[str] = [
        _game_line(game, narration, index=index, records=records)
        for index, game in enumerate(narration.games)
    ]
    lead_hooks = {candidate.hook for candidate in narration.lead_candidates if candidate.hook}
    if include_storylines:
        for storyline in narration.storyline_candidates:
            hook = storyline.hook
            if storyline.kind == _TRADE_LOPSIDED_KIND:
                continue
            if hook and hook not in lead_hooks and hook not in blocks:
                blocks.append(hook)
    if not blocks:
        blocks.append(_NO_GAMES)
    return WeeklySection(heading=heading_for("around_league"), blocks=blocks)


def _record_runs(rows: list[WeeklyNarrationStanding]) -> list[tuple[str, list[str]]]:
    """Consecutive standings rows that share a record, in rank order."""
    runs: list[tuple[str, list[str]]] = []
    for row in rows:
        if runs and runs[-1][0] == row.rec:
            runs[-1][1].append(_label(row.team))
        else:
            runs.append((row.rec, [_label(row.team)]))
    return runs


_TOP_RUN_ONE = ("{n} leads the league at {rec}.", "{n} sits on top at {rec}.")
_TOP_RUN_MANY = ("{n} lead the way at {rec}.", "{n} are level at {rec}, the best mark in the league.")
_RUN_ONE = ("{n} is {rec}.", "{n} sits at {rec}.", "{n} comes in at {rec}.")
_RUN_MANY = ("{n} are all {rec}.", "{n} sit level at {rec}.", "{n} are bunched at {rec}.")
_COLD_FIRST = ("{n} opens on top at {rec}, with {pf} points.",)
_COLD_ROW = (
    "{n} is {rec} with {pf} points.",
    "{n} sits at {rec} on {pf} points.",
    "{n} has {pf} points at {rec}.",
)


def _standings_blocks(
    rows: list[WeeklyNarrationStanding], narration: WeeklyNarration, *, cold_start: bool
) -> list[str]:
    """The standings in grouped sentences: teams sharing a record read as one
    sentence, in rank order. Week 1 ranks by points, so each team gets its own
    sentence with its points."""
    if cold_start:
        return [
            _pick(_COLD_FIRST if index == 0 else _COLD_ROW, narration, "standing", row.roster_id).format(
                n=_label(row.team), rec=row.rec, pf=row.pf
            )
            if row.rec
            else f"{_label(row.team)} has {row.pf} points."
            for index, row in enumerate(rows)
        ]
    blocks: list[str] = []
    frames: tuple[str, ...]
    for index, (rec, names) in enumerate(_record_runs(rows)):
        if index == 0:
            frames = _TOP_RUN_ONE if len(names) == 1 else _TOP_RUN_MANY
        else:
            frames = _RUN_ONE if len(names) == 1 else _RUN_MANY
        if not rec:  # no record to state: name the teams without one
            blocks.append(f"{_join_names(names)} {'is' if len(names) == 1 else 'are'} in the standings.")
            continue
        blocks.append(_pick(frames, narration, "standing", index).format(n=_join_names(names), rec=rec))
    return blocks


_BRACKET_FRAMES = ("The bracket takes {fmt}.", "The playoff field is {fmt}.")
_BYE_FRAMES = ("{n} {has} a first-round bye.", "{n} would skip the first round.")
_REST_FRAMES = ("{n} {is} also in the bracket.", "Behind them, {n} {is} in the bracket.")
_IN_FRAMES = ("{n} {is} in the bracket.", "The bracket holds {n}.")
_BUBBLE_FRAMES = ("{n} {is} on the bubble.", "The bubble holds {n}.")
_OUT_FRAMES = ("{n} is the first team out.", "{n} sits just outside, the first team out.")


def _verbs(names: list[str]) -> dict[str, str]:
    plural = len(names) != 1
    return {"is": "are" if plural else "is", "has": "have" if plural else "has"}


def _playoff_block(picture: WeeklyNarrationPlayoff | None, narration: WeeklyNarration) -> str:
    """The playoff picture in grouped sentences (byes, the rest of the bracket, the
    bubble, first out), or the stand-down line when the league has declared no
    bracket. No digit beyond the payload's own format string."""
    if picture is None:
        return _NO_PLAYOFFS
    parts = [_pick(_BRACKET_FRAMES, narration, "playoff", "format").format(fmt=picture.format)]
    if picture.byes:
        parts.append(
            _pick(_BYE_FRAMES, narration, "playoff", "byes").format(
                n=_join_names(picture.byes), **_verbs(picture.byes)
            )
        )
    byes = set(picture.byes)
    rest = [name for name in picture.in_bracket if name not in byes]
    if picture.byes and rest:
        parts.append(
            _pick(_REST_FRAMES, narration, "playoff", "rest").format(n=_join_names(rest), **_verbs(rest))
        )
    elif not picture.byes and picture.in_bracket:
        parts.append(
            _pick(_IN_FRAMES, narration, "playoff", "in").format(
                n=_join_names(picture.in_bracket), **_verbs(picture.in_bracket)
            )
        )
    if picture.bubble:
        parts.append(
            _pick(_BUBBLE_FRAMES, narration, "playoff", "bubble").format(
                n=_join_names(picture.bubble), **_verbs(picture.bubble)
            )
        )
    if picture.first_out:
        parts.append(_pick(_OUT_FRAMES, narration, "playoff", "out").format(n=picture.first_out))
    if picture.seeding_unconfirmed:
        parts.append(_SEEDING_NOTE)
    return " ".join(parts)


def cold_start_standings(rows: list[WeeklyNarrationStanding]) -> list[WeeklyNarrationStanding]:
    """Week-1 standings "by points": ordered by points for, ranked by position.

    The one ordering rule the template narrator and the cold-start LLM payload
    share, so a completion cannot narrate ranks that contradict the table."""
    ordered = sorted(rows, key=lambda r: (-r.pf, r.roster_id.zfill(8)))
    return [row.model_copy(update={"rank": position}) for position, row in enumerate(ordered, start=1)]


def _standings_section(
    narration: WeeklyNarration, *, cold_start: bool = False
) -> WeeklySection:
    """The standings in grouped sentences, then the playoff picture.

    ``cold_start=True`` (Week 1) drops the playoff block entirely and uses the
    plain "Standings" heading: there is no bracket yet to describe, and the
    heading is the cold-start set's own (Story 5.16)."""
    rows = cold_start_standings(narration.standings) if cold_start else narration.standings
    blocks = _standings_blocks(rows, narration, cold_start=cold_start)
    if not blocks:
        blocks.append(_NO_STANDINGS)
    if not cold_start:
        blocks.append(_playoff_block(narration.playoff_picture, narration))
    heading = heading_for("standings", cold_start=cold_start)
    return WeeklySection(heading=heading, blocks=blocks)


_POWER_FRAMES = (
    "{team} is {rec} and scores {avg} points a week.",
    "{team} averages {avg} points a week at {rec}.",
    "{team} ({rec}) puts up {avg} points a week.",
    "{team} sits at {rec} on {avg} points a week.",
)
_ALL_PLAY_FRAMES = (" The all-play record is {ap}.", " In all-play, {ap}.", " All-play: {ap}.")


def _power_line(row: WeeklyNarrationPower, narration: WeeklyNarration) -> str:
    body = _pick(_POWER_FRAMES, narration, "power", row.roster_id).format(
        team=_label(row.team), rec=row.rec, avg=row.avg_pf
    )
    if row.all_play:
        body += _pick(_ALL_PLAY_FRAMES, narration, "power-ap", row.roster_id).format(ap=row.all_play)
    if row.model_rank is None:
        # No "N. " prefix: the web page's _power_notes leaves this one row as prose.
        return f"{body} Unranked."
    return f"{row.model_rank}. {body}"


def _power_section(narration: WeeklyNarration) -> WeeklySection:
    """Model rank only, best first: no delta arrows, no published rank."""
    rows = sorted(narration.power, key=lambda row: (row.model_rank is None, row.model_rank or 0))
    blocks = [_power_line(row, narration) for row in rows]
    if not blocks:
        blocks.append(_NO_POWER)
    return WeeklySection(heading=heading_for("power"), blocks=blocks)


#: Luck in words. Positive ``luck`` is wins above what the scores earned (lucky),
#: negative is below (unlucky), zero is even; the number keeps its printed sign.
_LUCKY_FRAMES = (
    "{team} is {rec} and has caught the breaks, luck {luck}.",
    "{team} ({rec}) has been lucky, {luck} by the index.",
    "Luck has favored {team} at {rec}, {luck}.",
    "The breaks keep finding {team} ({rec}), {luck}.",
    "{team} ({rec}) is ahead of the scores, {luck}.",
)
_UNLUCKY_FRAMES = (
    "{team} is {rec} and has been unlucky, luck {luck}.",
    "{team} ({rec}) deserved better, {luck} by the index.",
    "Luck has dodged {team} at {rec}, {luck}.",
    "The breaks have skipped {team} ({rec}), {luck}.",
    "{team} ({rec}) is behind the scores, {luck}.",
)
_EVEN_FRAMES = (
    "{team} is {rec} and exactly as lucky as the scores say, luck {luck}.",
    "{team} ({rec}) has had no luck either way, {luck}.",
)
_EARNED_FRAMES = (" Earned wins by the scores: {ew}.", " Earned wins: {ew}.")


def _luck_line(row: WeeklyNarrationLuck, narration: WeeklyNarration) -> str:
    luck = (row.luck if row.luck is not None else 0.0) + 0.0  # + 0.0 turns -0.0 into 0.0
    frames: tuple[str, ...]
    if luck > 0:
        frames = _LUCKY_FRAMES
    elif luck < 0:
        frames = _UNLUCKY_FRAMES
    else:
        frames = _EVEN_FRAMES
    line = _pick(frames, narration, "luck", row.roster_id).format(
        team=_label(row.team), rec=row.rec, luck=f"{luck:+}"
    )
    if row.earned_wins is not None:
        line += _pick(_EARNED_FRAMES, narration, "luck-ew", row.roster_id).format(ew=row.earned_wins)
    if row.all_play:
        line += _pick(_ALL_PLAY_FRAMES, narration, "luck-ap", row.roster_id).format(ap=row.all_play)
    return line


def _luck_section(narration: WeeklyNarration) -> WeeklySection:
    """The luck index. Rows whose ``luck`` is not yet measurable (below the
    games-played minimum) are skipped, so a cold start stands down rather than
    rendering a heading over nothing."""
    blocks = [_luck_line(row, narration) for row in narration.luck if row.luck is not None]
    if not blocks:
        blocks.append(_NO_LUCK)
    return WeeklySection(heading=heading_for("luck"), blocks=blocks)


#: `stats/stakes.py`'s tag vocabulary (`_TAG_ORDER`), in human words — a copy,
#: not an import (AD-1: the tags are stats/-side identifiers, out of reach on
#: this side of the fence). Unrecognized tags fall back to the raw string
#: rather than being dropped, so a new tag is visible, not silently missing.
_STAKES_PHRASES = {
    "bye_seed": "a first-round bye",
    "wildcard_race": "a wildcard spot",
    "division_race": "the division race",
    "draft_position": "draft position",
    "elimination": "elimination",
}


def _stakes_phrase(tags: list[str]) -> str:
    return _join_names([_STAKES_PHRASES.get(tag, tag) for tag in tags])


def _next_week_card(card: WeeklyNarrationNextWeek) -> str:
    parts = [f"{_label(card.a)} ({card.a_rec}) against {_label(card.b)} ({card.b_rec})"]
    if card.a_model_rank is not None and card.b_model_rank is not None:
        parts.append(f"power ranks {card.a_model_rank} and {card.b_model_rank}")
    line = ", ".join(parts) + "."
    if card.stakes:
        line += f" On the line: {_stakes_phrase(card.stakes)}."
    if card.game_of_week:
        line += " The game of the week."
    return line


def _next_week_section(narration: WeeklyNarration) -> WeeklySection:
    """Bye data is checked independently of the fantasy schedule: a week with
    known NFL byes but no fantasy matchups yet (or a reduction-ladder cut) must
    not silently drop the bye names along with the schedule."""
    blocks: list[str] = []
    byes = narration.next_week_nfl_byes
    if byes:
        blocks.append(f"On bye next week: {_join_names(sorted(byes))}.")
    blocks.extend(_next_week_card(card) for card in narration.next_week)
    if not blocks:
        blocks.append(_NO_SCHEDULE)
    return WeeklySection(heading=heading_for("next_week"), blocks=blocks)


_HORIZON_WORDS = {"this_season": "this season", "future": "the future"}


def _trade_grade_lines(grade: WeeklyNarrationTradeGrade) -> list[str]:
    """Numbers-only fact lines for one graded trade: each side's letter and word per horizon,
    then (when flagged) the standings context. Never a judgement, never veto wording."""
    if grade.status != "graded":
        return [f"The week {grade.week} trade could not be graded."]
    parts: list[str] = []
    for side in grade.sides:
        verdicts = [
            f"{v.letter}, {v.word.lower()}"
            + (f" for {_HORIZON_WORDS[v.horizon]}" if grade.dynasty else "")
            + (" (estimated)" if v.est else "")
            for v in side.verdicts
        ]
        if verdicts:
            parts.append(f"{_label(side.team)} {_join_names(verdicts)}")
    if not parts:
        return []
    lines = [f"The week {grade.week} trade grades: {'; '.join(parts)}."]
    if grade.context == "contender_vs_eliminated" and grade.eliminated:
        out = set(grade.eliminated)
        alive = [side.team for side in grade.sides if side.team not in out]
        if alive:
            lines.append(
                f"{_join_names([_label(t) for t in grade.eliminated])} are out of the playoff race and "
                f"{_join_names([_label(t) for t in alive])} are still in it. The grades are the same either way."
            )
    return lines


def _transactions_section(narration: WeeklyNarration) -> WeeklySection:
    """A quiet week is one deliberate line, never an empty heading."""
    desk = narration.transactions
    if desk.this_week_count == 0 and desk.recent_trade_count == 0 and not desk.trade_grades:
        return WeeklySection(heading=heading_for("transactions"), blocks=[_QUIET_WIRE])
    blocks = [
        f"{_count(desk.this_week_count, 'move').capitalize()} this week.",
        f"{_count(desk.recent_trade_count, 'trade').capitalize()} in the recent window.",
    ]
    if desk.last_trade_week is not None:
        line = f"The last trade landed in week {desk.last_trade_week}"
        if desk.weeks_since_last_trade == 0:
            line = "The last trade landed this week"
        elif desk.weeks_since_last_trade is not None:
            line += f", {_count(desk.weeks_since_last_trade, 'week')} ago"
        blocks.append(line + ".")
    elif not desk.complete:
        blocks.append("The trade history is incomplete, so the market is hard to read.")
    for grade in desk.trade_grades:
        blocks.extend(_trade_grade_lines(grade))
    return WeeklySection(heading=heading_for("transactions"), blocks=blocks)
