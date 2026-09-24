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

Story 5.14b replaces Story 5.11a's weekly title-plus-lead summary with the
approved designed post, :func:`render_weekly_discord_post` (``Discord.dc.html``):
title, lead, a standings code block, results, one line each for power, luck,
next week and the wire, and an awards sign-off, trimmed in the approved order
to stay within 2,000 UTF-16 code units (:func:`utf16_len`). Its content choices
come from :mod:`commishdesk.render._weekly_model`, shared with the web page and
the email. Every league-supplied value outside the code block goes through
:func:`_escape_discord_markdown`.

**Pipeline fence (AD-1).** Standard library + :mod:`commishdesk.facts` schema
types + :class:`commishdesk.narrate.Recap` /
:class:`~commishdesk.narrate.weekly_template.WeeklyIssue` + the shared
``render`` helpers — nothing upstream of ``facts/``, no cloud or HTTP SDK.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from commishdesk.facts.schema import DraftRecapFacts, WeeklyFacts
from commishdesk.narrate import Recap
from commishdesk.narrate.weekly_template import SECTION_HEADINGS, WeeklyIssue
from commishdesk.render import _weekly_model as wm
from commishdesk.render._body import _plain, _sections_from_llm, _sections_from_recap

__all__ = ["render_discord_summary", "render_weekly_discord_post", "utf16_len"]

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


# --------------------------------------------------------------------------- #
# Story 5.14b — the designed weekly post (Discord.dc.html)
# --------------------------------------------------------------------------- #

#: UTF-16 code units reserved for the ``Full Issue → <url>`` link line.
LINK_RESERVE = 60
#: The weekly post's body budget: the 2,000-unit limit less the link reserve.
WEEKLY_BODY_BUDGET = _MAX - LINK_RESERVE
#: Standings rows never trimmed (the playoff line above them is kept too).
_CORE_STANDINGS_ROWS = 6
#: Longest team name inside the standings code block.
_CODE_NAME_MAX = 22
_PLAYOFF_RULE = "── playoff line ──────────────"
_FENCE = "```"

# Trim order, first to go first (design-decisions "Discord rules").
_DROP_AWARDS = 1
_DROP_WIRE = 2
_DROP_NEXT = 3
_DROP_LUCK = 4
_DROP_POWER = 5
_DROP_RESULTS = 6
_DROP_STANDINGS_ROW = 100  # + distance from the bottom, so the last row goes first


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units, the unit Discord counts (an emoji is 2)."""
    return len(text.encode("utf-16-le")) // 2


class _Block(NamedTuple):
    """One post line (or a few lines that live or die together).

    ``group`` separates paragraphs: consecutive blocks in one group join with a
    newline, a new group starts after a blank line. ``drop`` is the trim
    priority — lower goes first; ``None`` is never trimmed."""

    text: str
    group: int
    drop: int | None = None


def _join(blocks: Sequence[_Block]) -> str:
    out: list[str] = []
    previous: int | None = None
    for block in blocks:
        if previous is not None:
            out.append("\n" if block.group == previous else "\n\n")
        out.append(block.text)
        previous = block.group
    return "".join(out)


def _trim(blocks: Sequence[_Block], budget: int) -> list[_Block]:
    """Drop blocks lowest-``drop`` first until the joined post fits *budget*
    UTF-16 units, or nothing droppable is left. Pure; keeps block order."""
    kept = list(blocks)
    while utf16_len(_join(kept)) > budget:
        droppable = [(block.drop, index) for index, block in enumerate(kept) if block.drop is not None]
        if not droppable:
            break
        _, index = min(droppable)
        del kept[index]
    return kept


def _md(value: str) -> str:
    """A league-supplied value outside the code block: bidi-stripped, whitespace
    collapsed, Discord markdown escaped — ``[`` too, so a team name cannot
    post a masked ``[text](url)`` link."""
    return _escape_discord_markdown(" ".join(_plain(value).split())).replace("[", "\\[")


def _code_name(value: str) -> str:
    """A name inside the standings code block: not escaped (markdown is inert
    there), but a backtick could close the fence, so it becomes ``'``; capped at
    :data:`_CODE_NAME_MAX` characters with ``…``."""
    name = " ".join(_plain(value).split()).replace("`", "'")
    if len(name) > _CODE_NAME_MAX:
        name = name[: _CODE_NAME_MAX - 1] + "…"
    return name


def _clip_text(text: str, budget: int) -> str:
    """``text`` cut at a word boundary with ``…`` so it fits *budget* units."""
    if utf16_len(text) <= budget:
        return text
    out: list[str] = []
    used = 1  # the ellipsis
    for char in text:
        units = utf16_len(char)
        if used + units > budget:
            break
        out.append(char)
        used += units
    head = "".join(out)
    head = head.rsplit(" ", 1)[0].rstrip() or head
    return f"{head}…"


def _weekly_lead(facts: WeeklyFacts, issue: WeeklyIssue) -> _Block | None:
    lead = wm.choose_lead(facts)
    if lead is not None and lead.hook:
        text = _lead_sentence([("The Lead", [lead.hook])])
        chair = " 🪑" if lead.kind == "lineup_loss" else ""
        return _Block(f"**{_md(text)}**{chair}", 0)
    blocks = next((list(s.blocks) for s in issue.sections if s.heading == "The Lead"), [])
    text = _lead_sentence([("The Lead", blocks)])
    return _Block(f"**{_md(text)}**", 0) if text else None


def _weekly_standings(facts: WeeklyFacts) -> list[_Block]:
    order = wm.standings_order(facts)
    if not order:
        return []
    picture = facts.standings.playoff_picture
    cut = picture.cut_line_after_rank if picture is not None else None
    rows = []
    for team in order:
        season = team.season
        rows.append(
            (
                str(season.rank),
                _code_name(wm.team_label(team)),
                wm.record(season.record.w, season.record.l, season.record.t),
                wm.whole(season.points_for),
            )
        )
    rank_w = max(2, *(len(r[0]) for r in rows))
    name_w = max(len(r[1]) for r in rows)
    rec_w = max(len(r[2]) for r in rows)
    pf_w = max(len(r[3]) for r in rows)
    core = max(_CORE_STANDINGS_ROWS, cut or 0)
    blocks = [_Block("📊 **Standings**", 1), _Block(_FENCE, 1)]
    for index, (rank, name, rec, pf) in enumerate(rows, start=1):
        line = f"{rank:>{rank_w}} {name:<{name_w}}   {rec:<{rec_w}}   {pf:>{pf_w}}".rstrip()
        drop = None if index <= core else _DROP_STANDINGS_ROW + (len(rows) - index)
        blocks.append(_Block(line, 1, drop))
        if cut is not None and index == cut and index < len(rows):
            blocks.append(_Block(_PLAYOFF_RULE, 1))
    blocks.append(_Block(_FENCE, 1))
    return blocks


_REACTIONS = {"week_high": " 🔥", "closest": " 😬"}


def _weekly_results(facts: WeeklyFacts) -> _Block | None:
    games = facts.matchups.this_week
    if not games:
        return None
    label = wm.labeller(facts)
    lines = ["🏈 **Results**"]
    for matchup in games:
        win_id, win_pts, lose_id, lose_pts = wm.winner_loser(matchup)
        reaction = _REACTIONS.get(wm.game_tag(facts, matchup) or "", "")
        if matchup.winner_roster_id is None:
            lines.append(
                f"• {_md(label(win_id))} {wm.pts(win_pts)} tied {_md(label(lose_id))} {wm.pts(lose_pts)}{reaction}"
            )
        else:
            lines.append(
                f"• **{_md(label(win_id))}** {wm.pts(win_pts)} def. {_md(label(lose_id))} "
                f"{wm.pts(lose_pts)}{reaction}"
            )
    return _Block("\n".join(lines), 1, _DROP_RESULTS)


def _weekly_power(facts: WeeklyFacts) -> _Block | None:
    rows = [row for row in wm.power_rows(facts) if row.published is not None][:5]
    if not rows:
        return None
    items = " · ".join(f"{row.published}. {_md(wm.team_label(row.team))}" for row in rows)
    return _Block(f"📈 **Power top {len(rows)}**  {items}", 2, _DROP_POWER)


def _weekly_luck(facts: WeeklyFacts) -> _Block | None:
    rows = wm.luck_rows(facts)
    if not rows:
        return None
    first_value, first = rows[0]
    text = f"luckiest {_md(wm.team_label(first))} ({wm.signed(first_value)})"
    if len(rows) > 1:
        last_value, last = rows[-1]
        text += f", unluckiest {_md(wm.team_label(last))} ({wm.signed(last_value)})"
    return _Block(f"🍀 **Luck**  {text}", 2, _DROP_LUCK)


def _weekly_next(facts: WeeklyFacts) -> _Block | None:
    cards = facts.matchups.next_week
    if not cards:
        return None
    label = wm.labeller(facts)
    shared = wm.shared_stakes(cards)
    card = next((c for c in cards if c.game_of_week), None)
    lead_in = "Game of the week" if card is not None else "Up first"
    card = card or cards[0]
    text = f"{lead_in}: {_md(label(card.a_roster_id))} vs {_md(label(card.b_roster_id))}"
    own = wm.own_stakes(card, shared)
    if own:
        text += f" ({', '.join(_md(wm.stake_label(tag)) for tag in own)})"
    if shared:
        text += f" · On the line in every game: {', '.join(_md(wm.stake_label(tag)) for tag in shared)}"
    return _Block(f"👀 **Next week**  {text}", 2, _DROP_NEXT)


def _weekly_wire(facts: WeeklyFacts) -> _Block | None:
    pick = wm.wire_pick(facts)
    if pick is None:
        return None
    label = wm.labeller(facts)
    if pick.move is not None and pick.player is not None:
        team = _md(label(pick.move.roster_ids[0] if pick.move.roster_ids else pick.player.roster_id))
        verb = "claimed" if pick.move.type == "waiver" else "added"
        faab = f" (${pick.move.faab})" if pick.move.faab is not None else ""
        text = f"{team} {verb} {_md(pick.player.name)}{faab}"
    else:
        assert pick.trade is not None
        sides = []
        for side in pick.trade.sides:
            items = [_md(p.name) for p in side.players]
            items += [f"a {pick_ref.season} round {pick_ref.round} pick" for pick_ref in side.picks]
            if side.faab:
                items.append(f"${side.faab} FAAB")
            sides.append(f"{_md(label(side.roster_id))} gets {', '.join(items) or 'nothing listed'}")
        text = f"Week {pick.trade.week} trade: {'; '.join(sides)}"
    return _Block(f"🔄 **Wire**  {text}", 2, _DROP_WIRE)


def _weekly_awards(facts: WeeklyFacts) -> _Block | None:
    parts = []
    for award in wm.awards(facts):
        if award.kind == "coach":
            parts.append(f"🏆 Coach of the week: {_md(award.name)} ({award.value})")
        elif award.kind == "goose":
            parts.append(f"🥚 Goose egg: {_md(award.name)} ({award.value})")
    if not parts:
        return None
    return _Block("  ·  ".join(parts), 3, _DROP_AWARDS)


def render_weekly_discord_post(facts: WeeklyFacts, issue: WeeklyIssue, *, issue_url: str | None = None) -> str:
    """Compose the designed weekly Discord text post (``Discord.dc.html``).

    ``## 🏈 {league} · Week {N}``, a correction line when the Issue carries a
    Correction (a reissue), the bold lead hook (🪑 for ``lineup_loss``), the
    📊 standings code block with the playoff line after the cut, the 🏈 results
    bullets (🔥 week high, 😬 closest game), then one line each for 📈 power
    top 5, 🍀 luck, 👀 next week and 🔄 wire, the 🏆 / 🥚 awards sign-off, and
    ``Full Issue → {issue_url}`` when a URL is given (no link line and no
    trailing blank line otherwise). A stood-down section drops its line and
    its emoji.

    The body is trimmed to :data:`WEEKLY_BODY_BUDGET` UTF-16 units in the
    approved order — awards, wire, next week, luck, power, results, then
    standings rows from the bottom up to row 7 (or the playoff cut, when that
    sits lower). The title, correction, lead, rows 1–6 and the playoff line
    are never trimmed. The whole post is at most 2,000 UTF-16 units.

    League-supplied values outside the code block are markdown-escaped; inside
    it, names are unescaped but backtick-free and capped at 22 characters.
    Mention suppression stays at the delivery layer. Deterministic.
    """
    league = facts.league
    blocks: list[_Block] = [_Block(f"## 🏈 {_md(league.name)} · Week {facts.week}", 0)]
    for section in issue.sections:
        if section.heading in SECTION_HEADINGS:
            continue
        text = " ".join(block for block in section.blocks if block.strip())
        if text:
            blocks.append(_Block(_md(text), 0))
    for block in (
        _weekly_lead(facts, issue),
        *_weekly_standings(facts),
        _weekly_results(facts),
        _weekly_power(facts),
        _weekly_luck(facts),
        _weekly_next(facts),
        _weekly_wire(facts),
        _weekly_awards(facts),
    ):
        if block is not None:
            blocks.append(block)

    link = ""
    if issue_url is not None and issue_url.strip():
        link = f"Full Issue → {''.join(_plain(issue_url).split())}"
    link_units = utf16_len("\n\n" + link) if link else 0
    budget = min(WEEKLY_BODY_BUDGET, _MAX - link_units) if link else WEEKLY_BODY_BUDGET
    body = _join(_trim(blocks, budget))
    if link and utf16_len(body) + link_units <= _MAX:
        body = f"{body}\n\n{link}"
    # Last resort only (a pathological title or correction): never exceed the limit.
    return _clip_text(body, _MAX)


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
