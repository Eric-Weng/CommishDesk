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
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

from commishdesk.facts.schema import (
    WeeklyNarration,
    WeeklyNarrationGame,
    WeeklyNarrationLuck,
    WeeklyNarrationNextWeek,
    WeeklyNarrationPlayoff,
    WeeklyNarrationPower,
    WeeklyNarrationStanding,
)

__all__ = [
    "SECTION_HEADINGS",
    "WeeklyIssue",
    "WeeklySection",
    "parse_nudge_justifications",
    "parse_published_ranks",
    "render_weekly_issue",
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


class WeeklyIssue(_Frozen):
    """A rendered weekly Issue: a masthead title, a one-line dateline, and the
    seven reference sections."""

    title: str
    dateline: str
    sections: list[WeeklySection] = []


#: The seven reference sections, in order — this module's own section set, and
#: the shape :func:`weekly_issue_from_text` requires of a narrated Issue.
SECTION_HEADINGS: tuple[str, ...] = (
    "The Lead",
    "Around the League",
    "Standings and the Playoff Picture",
    "Power Rankings",
    "The Luck Index",
    "Next Week",
    "The Transaction Desk",
)


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

_QUIET_WIRE = "A quiet week on the wire: no moves this week and no recent trades."
_NO_LUCK = "Not enough games have been played to measure luck."
_NO_POWER = "No power rankings are available for this week."
_NO_STANDINGS = "No standings are available for this league yet."
_NO_PLAYOFFS = "No playoff picture is available for this league."
_NO_GAMES = "No games were played this week, and no storyline is running."
_NO_SCHEDULE = "No games are on the schedule for next week."


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #


def render_weekly_issue(narration: WeeklyNarration) -> WeeklyIssue:
    """Build the :class:`WeeklyIssue` from ``narration`` alone.

    Pure and deterministic — no timestamp, no network, no model. The caller (the
    weekly CLI path, Story 5.11a) stamps ``generated_at`` where it wants it; the
    narrator does not see it (it is not part of the ``narration`` projection).
    """
    title, dateline = _masthead(narration)
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


def weekly_issue_from_text(text: str, narration: WeeklyNarration) -> WeeklyIssue | None:
    """Parse a narrated weekly Issue's Markdown back into a :class:`WeeklyIssue`.

    Requires all seven :data:`SECTION_HEADINGS`, each with at least one block
    (the same "no empty section heading" rule the template narrator holds
    itself to); the sections are then re-ordered into the canonical order and any
    extra section the narrator added is dropped. The masthead is rebuilt from
    *narration*, never taken from the text.

    ``None`` when the text is not the expected shape — the caller then degrades
    to the template narrator rather than shipping a malformed dump.
    """
    sections = _split_sections(text)
    if sections is None:
        return None
    by_heading = {section.heading: section for section in sections}
    if any(not by_heading.get(name, WeeklySection(heading=name, blocks=[])).blocks for name in SECTION_HEADINGS):
        return None
    ordered = [by_heading[name] for name in SECTION_HEADINGS]
    title, dateline = _masthead(narration)
    return WeeklyIssue(title=title, dateline=dateline, sections=ordered)


def _power_section_lines(text: str) -> list[str]:
    """The raw lines under the "Power Rankings" heading, up to the next heading.
    Raw lines, not :func:`_split_sections` blocks: that splitter joins consecutive
    lines into one paragraph, which would hide every item of a tight list."""
    lines: list[str] = []
    inside = False
    for line in text.splitlines():
        match = _ATX_LINE.match(line)
        if match is not None:
            inside = match.group(1).strip() == "Power Rankings"
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


def _lead_section(narration: WeeklyNarration) -> WeeklySection:
    """The week in one line, then every deterministic lead angle Story 5.9's
    facts builder ranked (each hook is its own sentence)."""
    league = narration.league
    blocks = [
        f"Week {league.week}: {_count(len(narration.games), 'game')} played, "
        f"{_count(league.team_count, 'team')} in the standings."
    ]
    for candidate in narration.lead_candidates:
        if candidate.hook:
            blocks.append(candidate.hook)
    return WeeklySection(heading="The Lead", blocks=blocks)


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


def _game_line(game: WeeklyNarrationGame) -> str:
    """One game, in the sentence family its scoreline earned."""
    family = _game_family(game)
    winner = _label(game.winner)
    loser = _label(game.loser)
    if family == "tie":
        line = f"{loser} finished level at {game.loser_pts}."
    elif family == "close":
        line = f"{winner} edged {loser} by {game.margin}: {game.winner_pts}-{game.loser_pts}."
    elif family == "blowout":
        line = f"{winner} blew past {loser}, {game.winner_pts} to {game.loser_pts}."
    else:
        line = f"{winner} beat {loser}, {game.winner_pts} to {game.loser_pts}."
    if game.top:
        line += f" {game.top} topped the scoring."
    return line


def _around_section(narration: WeeklyNarration) -> WeeklySection:
    """Every game of the week, then the multi-week threads Story 5.9's weekly
    storyline lifecycle is carrying — skipping any hook the lead already used."""
    blocks: list[str] = [_game_line(game) for game in narration.games]
    lead_hooks = {candidate.hook for candidate in narration.lead_candidates if candidate.hook}
    for storyline in narration.storyline_candidates:
        hook = storyline.hook
        if hook and hook not in lead_hooks and hook not in blocks:
            blocks.append(hook)
    if not blocks:
        blocks.append(_NO_GAMES)
    return WeeklySection(heading="Around the League", blocks=blocks)


def _standings_line(row: WeeklyNarrationStanding) -> str:
    line = f"{row.rank}. {_label(row.team)} — {row.rec}, {row.pf} points for"
    if row.model_rank is not None:
        line += f", model rank {row.model_rank}"
    return line + "."


def _playoff_block(picture: WeeklyNarrationPlayoff | None) -> str:
    """The playoff picture in one block, or the stand-down line when the league
    has declared no bracket."""
    if picture is None:
        return _NO_PLAYOFFS
    parts = [f"The bracket takes {picture.format}."]
    if picture.byes:
        parts.append(f"First-round byes: {_join_names(picture.byes)}.")
    if picture.in_bracket:
        parts.append(f"In the bracket: {_join_names(picture.in_bracket)}.")
    if picture.first_out:
        parts.append(f"First team out: {picture.first_out}.")
    if picture.bubble:
        parts.append(f"On the bubble: {_join_names(picture.bubble)}.")
    if picture.seeding_unconfirmed:
        parts.append("Seeding unconfirmed — derived from the standings.")
    return " ".join(parts)


def _standings_section(narration: WeeklyNarration) -> WeeklySection:
    blocks = [_standings_line(row) for row in narration.standings]
    if not blocks:
        blocks.append(_NO_STANDINGS)
    blocks.append(_playoff_block(narration.playoff_picture))
    return WeeklySection(heading="Standings and the Playoff Picture", blocks=blocks)


def _power_line(row: WeeklyNarrationPower) -> str:
    tail = f"{_label(row.team)} — {row.rec}, {row.avg_pf} points a week"
    if row.model_rank is None:
        return f"{tail}, unranked."
    return f"{row.model_rank}. {tail}."


def _power_section(narration: WeeklyNarration) -> WeeklySection:
    """Model rank only, best first — no delta arrows, no published rank."""
    rows = sorted(narration.power, key=lambda row: (row.model_rank is None, row.model_rank or 0))
    blocks = [_power_line(row) for row in rows]
    if not blocks:
        blocks.append(_NO_POWER)
    return WeeklySection(heading="Power Rankings", blocks=blocks)


def _luck_line(row: WeeklyNarrationLuck) -> str:
    line = f"{_label(row.team)} — {row.rec}"
    if row.earned_wins is not None:
        line += f", {row.earned_wins} wins earned"
    return f"{line}, luck {row.luck:+}."


def _luck_section(narration: WeeklyNarration) -> WeeklySection:
    """The luck index. Rows whose ``luck`` is not yet measurable (below the
    games-played minimum) are skipped, so a cold start stands down rather than
    rendering a heading over nothing."""
    blocks = [_luck_line(row) for row in narration.luck if row.luck is not None]
    if not blocks:
        blocks.append(_NO_LUCK)
    return WeeklySection(heading="The Luck Index", blocks=blocks)


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
    return WeeklySection(heading="Next Week", blocks=blocks)


def _transactions_section(narration: WeeklyNarration) -> WeeklySection:
    """A quiet week is one deliberate line, never an empty heading."""
    desk = narration.transactions
    if desk.this_week_count == 0 and desk.recent_trade_count == 0:
        return WeeklySection(heading="The Transaction Desk", blocks=[_QUIET_WIRE])
    blocks = [
        f"{_count(desk.this_week_count, 'move').capitalize()} this week.",
        f"{_count(desk.recent_trade_count, 'trade').capitalize()} in the recent window.",
    ]
    if desk.last_trade_week is not None:
        line = f"The last trade landed in week {desk.last_trade_week}"
        if desk.weeks_since_last_trade is not None:
            line += f", {_count(desk.weeks_since_last_trade, 'week')} ago"
        blocks.append(line + ".")
    elif not desk.complete:
        blocks.append("The trade history is incomplete, so the market is hard to read.")
    return WeeklySection(heading="The Transaction Desk", blocks=blocks)
