"""Story 5.14b — the weekly Issue as an email: client-safe HTML plus ``text/plain``.

:func:`render_weekly_email` turns the weekly Facts JSON plus the narrated
:class:`~commishdesk.narrate.weekly_template.WeeklyIssue` into an
:class:`~commishdesk.render.email.EmailParts` ``(html, text)`` pair, following
the approved ``Email.dc.html`` board and the design-decisions per-section table
(the table wins where they disagree):

* a 640px single-column ``role="presentation"`` table layout, every style
  inline, no script, ``<svg>``, ``<img>``, ``url(``, CSS custom property, flex
  or grid;
* the masthead is the nameplate plus the week line, no tiles, and every
  masthead cell paints an explicit ``bgcolor`` plus an inline ``color`` so a
  dark-mode client that inverts the page still has a concrete pair to keep;
* sections in the web page's order — lead (bench bar as a two-cell row, other
  kinds as a text line, then the awards as text lines), results, standings
  (bar = table-cell width), power (model score as text, nudge as a text line
  with its cited reason when the narration carries one), luck (``█`` bars),
  next week (shared stakes once above the matchups), and two short transaction
  tables. A section whose data is absent renders nothing, in either part.

The content choices (lead, awards, tags, power order, luck order, shared
stakes) come from :mod:`commishdesk.render._weekly_model`, the same selectors
the web page and the Discord post use. Narrated prose comes from the Issue's
:data:`~commishdesk.narrate.weekly_template.SECTION_HEADINGS` sections; numbers
come from Facts. Text colours are the AA-safe Tuesday Morning roles
(``--good-text`` / ``--bad-text``, ink-2 rather than ink-3 for information).

**Deterministic and escaped.** ``generated_at`` is the only time value. Every
interpolated HTML value goes through :func:`~commishdesk.render._body._esc`; the
text part is bidi-stripped through :func:`~commishdesk.render._body._plain`.

**Pipeline fence (AD-1).** Imports the standard library, ``commishdesk.facts``
schema types, the weekly narrator's output types, and ``commishdesk.render``
helpers only.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from commishdesk.facts.schema import LeadCandidate, WeeklyFacts, WeeklyMove, WeeklyTrade
from commishdesk.narrate.weekly_template import SECTION_HEADINGS, WeeklyIssue
from commishdesk.render import _weekly_model as wm
from commishdesk.render._body import _esc, _plain
from commishdesk.render.email import EmailParts, _document
from commishdesk.render.style import WEEKLY_LIGHT_TOKENS

__all__ = ["render_weekly_email"]

# --------------------------------------------------------------------------- #
# Palette (literal hex from Tuesday Morning light — email cannot resolve var())
# --------------------------------------------------------------------------- #

_T = WEEKLY_LIGHT_TOKENS
PAPER = _T["--paper"]
CARD = _T["--card"]
INK = _T["--ink"]
INK2 = _T["--ink-2"]
LINE = _T["--line"]
RULE = _T["--rule"]
BAR = _T["--bar"]
BARLO = _T["--barlo"]
TRACK = _T["--hi"]
BAD_FILL = _T["--bad"]
GOOD_TEXT = _T["--good-text"]
BAD_TEXT = _T["--bad-text"]
EMPH = _T["--emph"]
CUT = _T["--cut"]

_WIDTH = 640
#: Luck bars: at most this many ``█``, at least one.
LUCK_BAR_MAX = 11

_DISPLAY = "'Bricolage Grotesque','Helvetica Neue',Helvetica,Arial,sans-serif"
_SANS = "'DM Sans','Helvetica Neue',Helvetica,Arial,sans-serif"
_MONO = "'IBM Plex Mono','Courier New',Courier,monospace"

_STYLE_BLOCK = (
    "body{margin:0;padding:0;width:100%;"
    "-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;}"
    "table{border-collapse:collapse;mso-table-lspace:0pt;mso-table-rspace:0pt;}"
    "@media only screen and (max-width:660px){"
    ".container{width:100%!important;}"
    ".px{padding-left:20px!important;padding-right:20px!important;}"
    "}"
)

_TABLE = '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"'


class _Section(NamedTuple):
    """One rendered section: its label, its HTML table rows, its text lines."""

    label: str
    html: str
    text: list[str]


# --------------------------------------------------------------------------- #
# HTML atoms
# --------------------------------------------------------------------------- #


def _row(inner: str, pad: str = "28px 40px 0") -> str:
    return f'<tr><td class="px" style="padding:{pad};">{inner}</td></tr>'


def _label(text: str) -> str:
    return (
        f'<div style="font-family:{_MONO};font-size:11px;letter-spacing:2px;text-transform:uppercase;'
        f'color:{INK2};border-top:2px solid {RULE};padding-top:8px;">{_esc(text)}</div>'
    )


def _headline(text: str, size: int = 19) -> str:
    return (
        f'<div style="font-family:{_DISPLAY};font-weight:bold;font-size:{size}px;line-height:1.25;'
        f'color:{INK};padding-top:8px;">{_esc(text)}</div>'
    )


def _para(text: str) -> str:
    return (
        f'<p style="margin:12px 0 0;font-family:{_SANS};font-size:16px;line-height:1.55;'
        f'color:{INK};">{_esc(text)}</p>'
    )


def _small(text: str, colour: str = INK2, top: int = 6) -> str:
    return (
        f'<div style="font-family:{_MONO};font-size:12px;line-height:1.5;color:{colour};'
        f'padding-top:{top}px;">{_esc(text)}</div>'
    )


def _line(inner: str) -> str:
    return (
        f'<div style="padding:8px 0;border-top:1px solid {LINE};font-family:{_SANS};font-size:15px;'
        f'line-height:1.45;color:{INK};">{inner}</div>'
    )


def _b(text: str) -> str:
    return f'<b style="color:{INK};">{_esc(text)}</b>'


def _mono(text: str, colour: str = INK) -> str:
    return f'<span style="font-family:{_MONO};color:{colour};">{_esc(text)}</span>'


def _tag(text: str, colour: str = INK2) -> str:
    return (
        f'<span style="font-family:{_MONO};font-size:11px;letter-spacing:1px;text-transform:uppercase;'
        f'color:{colour};">{_esc(text)}</span>'
    )


def _prose(blocks: Sequence[str]) -> tuple[str, list[str]]:
    kept = [block for block in blocks if block]
    return "".join(_para(block) for block in kept), kept


def _add_prose(text: list[str], prose: list[str]) -> None:
    """Narrated paragraphs in the text part: a blank line, then one per line."""
    if prose:
        if text:
            text.append("")
        text.extend(prose)


# --------------------------------------------------------------------------- #
# Masthead + notices
# --------------------------------------------------------------------------- #


def _week_line(facts: WeeklyFacts) -> str:
    parts = [f"Week {facts.week}", f"Season {facts.league.season}"]
    if facts.period.type and facts.period.type != "regular":
        parts.append(facts.period.type.capitalize())
    parts.append(f"{facts.league.format.team_count} teams")
    return " · ".join(parts)


def _mast_cell(inner: str, pad: str, style: str) -> str:
    # explicit bgcolor + inline colour on every masthead cell: an inverting
    # dark-mode client always has a concrete pair to keep
    return (
        f'<tr><td class="px" bgcolor="{CARD}" align="center" style="padding:{pad};'
        f'background-color:{CARD};text-align:center;{style}">{inner}</td></tr>'
    )


def _masthead(facts: WeeklyFacts) -> str:
    return (
        _mast_cell(
            "Commishdesk",
            "36px 40px 0",
            f"font-family:{_MONO};font-size:11px;letter-spacing:2px;text-transform:uppercase;color:{INK2};",
        )
        + _mast_cell(
            _esc(facts.league.name),
            "10px 40px 0",
            f"font-family:{_DISPLAY};font-weight:800;font-size:38px;line-height:1.05;"
            f"letter-spacing:-0.5px;text-transform:uppercase;color:{INK};",
        )
        + _mast_cell(
            _esc(_week_line(facts)),
            "8px 40px 0",
            f"font-family:{_MONO};font-size:11px;letter-spacing:1px;text-transform:uppercase;color:{INK2};",
        )
        + _mast_cell(
            f'{_TABLE}><tr><td bgcolor="{RULE}" style="background-color:{RULE};color:{RULE};height:3px;'
            'font-size:0;line-height:0;">&nbsp;</td></tr></table>',
            "18px 40px 0",
            f"color:{INK};",
        )
    )


def _notices(issue: WeeklyIssue) -> list[_Section]:
    out: list[_Section] = []
    if issue.dateline.startswith("UNVERIFIED"):
        out.append(_Section("Unverified", _row(_label("Unverified") + _para(issue.dateline)), [issue.dateline]))
    for section in issue.sections:
        if section.heading in SECTION_HEADINGS:
            continue
        body, lines = _prose(section.blocks)
        out.append(_Section(section.heading, _row(_label(section.heading) + body), lines))
    return out


# --------------------------------------------------------------------------- #
# Lead + awards
# --------------------------------------------------------------------------- #


def _lead_numbers(facts: WeeklyFacts, lead: LeadCandidate) -> str:
    """The key numbers of a lead with no bench bar, as one text line."""
    label = wm.labeller(facts)
    if lead.kind in ("biggest_blowout", "closest_game"):
        matchup = wm.find_matchup(facts, lead.roster_ids)
        if matchup is not None:
            win_id, win_pts, lose_id, lose_pts = wm.winner_loser(matchup)
            return (
                f"{label(win_id)} {wm.pts(win_pts)} · {label(lose_id)} {wm.pts(lose_pts)} · "
                f"margin {wm.pts(abs(matchup.margin))}"
            )
    summary = facts.period.summary
    if lead.kind == "week_high_score" and summary.high is not None:
        return (
            f"{label(summary.high.roster_id)} {wm.pts(summary.high.points)} · "
            f"league average {wm.pts(summary.avg_team_score)}"
        )
    key = wm.key_number(facts, lead)
    if lead.kind == "lineup_loss" and key:
        return f"left on bench {key}"
    return key


def _award_line(award: wm.Award) -> str:
    if award.sub.startswith("of "):
        return f"{award.key}: {award.name}, {award.value} {award.sub}"
    tail = f" ({award.sub})" if award.sub else ""
    return f"{award.key}: {award.name}, {award.value}{tail}"


def _lead(facts: WeeklyFacts, blocks: list[str]) -> _Section | None:
    lead = wm.choose_lead(facts)
    award_lines = [_award_line(award) for award in wm.awards(facts)]
    html: list[str] = [_label("The lead")]
    text: list[str] = []
    if lead is not None and lead.hook:
        hook = lead.hook
        prose = [block for block in blocks if block != hook]
    elif blocks:
        hook, prose = blocks[0], blocks[1:]
    elif award_lines:
        hook, prose = "", []
    else:
        return None
    if hook:
        html.append(_headline(hook))
        text.append(hook)
    split = wm.bench_split(facts, lead) if lead is not None else None
    if split is not None:
        started = min(99, max(1, round(split.started / (split.started + split.bench) * 100)))
        html.append(
            f'<div style="padding-top:12px;">{_TABLE}><tr>'
            f'<td width="{started}%" bgcolor="{BAR}" style="width:{started}%;background-color:{BAR};height:14px;'
            'font-size:1px;line-height:14px;">&nbsp;</td>'
            f'<td width="{100 - started}%" bgcolor="{BAD_FILL}" style="width:{100 - started}%;'
            f'background-color:{BAD_FILL};height:14px;font-size:1px;line-height:14px;">&nbsp;</td>'
            "</tr></table></div>"
        )
        caption = (
            f"started {wm.pts(split.started)} · left on bench {wm.pts(split.bench)} · "
            f"opponent {wm.pts(split.opponent)}"
        )
        html.append(_small(caption, top=4))
        text.append(caption)
    elif lead is not None:
        numbers = _lead_numbers(facts, lead)
        if numbers:
            html.append(_small(numbers))
            text.append(numbers)
    prose_html, prose_lines = _prose(prose)
    html.append(prose_html)
    _add_prose(text, prose_lines)
    if award_lines:
        html.append(
            '<div style="padding-top:14px;">'
            + "".join(_line(_esc(line)) for line in award_lines)
            + "</div>"
        )
        if text:
            text.append("")
        text.extend(award_lines)
    return _Section("The lead", _row("".join(html)), text)


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #

_TAG_WORDS = {"week_high": ("Week high", GOOD_TEXT), "closest": ("Nail-biter", INK2), "blowout": ("Blowout", INK2)}


def _results(facts: WeeklyFacts, blocks: list[str]) -> _Section | None:
    games = facts.matchups.this_week
    if not games:
        return None
    label = wm.labeller(facts)
    html: list[str] = [_label("Around the league")]
    text: list[str] = []
    for matchup in games:
        win_id, win_pts, lose_id, lose_pts = wm.winner_loser(matchup)
        tag = _TAG_WORDS.get(wm.game_tag(facts, matchup) or "")
        tag_html = f" {_tag(tag[0], tag[1])}" if tag else ""
        tag_text = f" [{tag[0]}]" if tag else ""
        if matchup.winner_roster_id is None:
            html.append(
                _line(
                    f"{_esc(label(win_id))} {_mono(wm.pts(win_pts))} tied {_esc(label(lose_id))} "
                    f"{_mono(wm.pts(lose_pts))}{tag_html}"
                )
            )
            text.append(f"{label(win_id)} {wm.pts(win_pts)} tied {label(lose_id)} {wm.pts(lose_pts)}{tag_text}")
        else:
            html.append(
                _line(
                    f"{_b(label(win_id))} {_mono(wm.pts(win_pts))} over {_esc(label(lose_id))} "
                    f"{_mono(wm.pts(lose_pts), INK2)}{tag_html}"
                )
            )
            text.append(f"{label(win_id)} {wm.pts(win_pts)} over {label(lose_id)} {wm.pts(lose_pts)}{tag_text}")
    prose_html, prose_lines = _prose(blocks)
    html.append(prose_html)
    _add_prose(text, prose_lines)
    return _Section("Around the league", _row("".join(html)), text)


# --------------------------------------------------------------------------- #
# Standings
# --------------------------------------------------------------------------- #


def _standings_title(facts: WeeklyFacts) -> str:
    playoff = facts.league.format.playoff
    if facts.standings.playoff_picture is not None and playoff is not None:
        title = f"{wm.spell(playoff.bracket_teams).capitalize()} make it"
        if playoff.byes:
            title += f", {wm.spell(playoff.byes)} get {'a bye' if playoff.byes == 1 else 'byes'}"
        return title
    return "The table"


def _standings(facts: WeeklyFacts, blocks: list[str]) -> _Section | None:
    order = wm.standings_order(facts)
    if not order:
        return None
    picture = facts.standings.playoff_picture
    byes = set(picture.byes) if picture else set()
    bubble = set(picture.bubble) if picture else set()
    cut = picture.cut_line_after_rank if picture else None
    max_pf = max(team.season.points_for for team in order)
    title = _standings_title(facts)
    cell = "padding:6px 4px;border-top:1px solid " + LINE + ";"
    rows: list[str] = []
    text: list[str] = [title]
    for index, team in enumerate(order, start=1):
        season = team.season
        name = wm.team_label(team)
        rec = wm.record(season.record.w, season.record.l, season.record.t)
        below = cut is not None and index > cut
        filled = max(1, min(100, round(season.points_for / max_pf * 100))) if max_pf > 0 else 0
        marker, marker_text = "", ""
        if team.roster_id in byes:
            marker, marker_text = _tag("Bye", EMPH), " [BYE]"
        elif team.roster_id in bubble:
            marker, marker_text = _tag("Bubble", INK2), " [BUBBLE]"
        bar = f'{_TABLE}><tr>'
        if filled:
            colour = BARLO if below else BAR
            bar += (
                f'<td width="{filled}%" bgcolor="{colour}" style="width:{filled}%;background-color:{colour};'
                'height:8px;font-size:1px;line-height:8px;">&nbsp;</td>'
            )
        if filled < 100:
            bar += (
                f'<td width="{100 - filled}%" bgcolor="{TRACK}" style="width:{100 - filled}%;'
                f'background-color:{TRACK};height:8px;font-size:1px;line-height:8px;">&nbsp;</td>'
            )
        bar += "</tr></table>"
        weight = "500" if below else "bold"
        rows.append(
            "<tr>"
            f'<td width="26" style="{cell}width:26px;font-family:{_MONO};color:{INK2};">{season.rank}</td>'
            f'<td style="{cell}font-family:{_SANS};font-weight:{weight};color:{INK};">{_esc(name)}</td>'
            f'<td width="44" style="{cell}width:44px;font-family:{_MONO};color:{INK};">{_esc(rec)}</td>'
            f'<td width="110" style="{cell}width:110px;">{bar}</td>'
            f'<td width="52" align="right" style="{cell}width:52px;text-align:right;font-family:{_MONO};'
            f'color:{INK2};">{_esc(wm.whole(season.points_for))}</td>'
            f'<td width="62" align="right" style="{cell}width:62px;text-align:right;">{marker or "&nbsp;"}</td>'
            "</tr>"
        )
        text.append(f"{season.rank:>2}. {name} — {rec}, {wm.whole(season.points_for)} PF{marker_text}")
        if cut is not None and index == cut and index < len(order):
            rows.append(
                f'<tr><td colspan="6" style="padding:4px 0;border-top:2px dashed {CUT};font-family:{_MONO};'
                f'font-size:10.5px;letter-spacing:2px;text-transform:uppercase;color:{CUT};">Playoff line</td></tr>'
            )
            text.append("--- playoff line ---")
    table = (
        f'{_TABLE} style="margin-top:6px;font-size:14px;">' + "".join(rows) + "</table>"
    )
    prose_html, prose_lines = _prose(blocks)
    _add_prose(text, prose_lines)
    return _Section(
        "Standings", _row(_label("Standings") + _headline(title, 17) + table + prose_html), text
    )


# --------------------------------------------------------------------------- #
# Power
# --------------------------------------------------------------------------- #


def _power(facts: WeeklyFacts, blocks: list[str]) -> _Section | None:
    rows = wm.power_rows(facts)
    if not rows:
        return None
    html: list[str] = [_label("Power rankings")]
    text: list[str] = []
    for row in rows:
        team = row.team
        power = team.season.power
        rec = wm.record(team.season.record.w, team.season.record.l, team.season.record.t)
        score = f"model {power.model_score:.2f}" if power.model_score is not None else "model –"
        rank = str(row.published) if row.published is not None else "–"
        delta = power.week_delta
        arrow_html, arrow_text = "", ""
        if delta:
            arrow_text = f"▲{delta}" if delta > 0 else f"▼{-delta}"
            arrow_html = " " + _mono(arrow_text, GOOD_TEXT if delta > 0 else BAD_TEXT)
            arrow_text = " " + arrow_text
        name = wm.team_label(team)
        inner = (
            f'<span style="display:inline-block;width:26px;font-family:{_MONO};color:{INK2};">{_esc(rank)}</span>'
            f"{_b(name)} {_mono(f'{rec} · {score}', INK2)}{arrow_html}"
        )
        text.append(f"{rank:>2}. {name} — {rec} · {score}{arrow_text}")
        if row.published is not None and row.model is not None and row.published != row.model:
            up = row.published < row.model
            chip = f"{'▲ Nudged up' if up else '▼ Nudged down'} · model #{row.model}"
            nudge = chip + (f" — {row.reason}" if row.reason else "")
            inner += (
                f'<div style="padding:4px 0 0 26px;font-family:{_SANS};font-size:13px;line-height:1.45;'
                f'color:{INK2};"><span style="font-family:{_MONO};color:{GOOD_TEXT if up else BAD_TEXT};">'
                f"{_esc(chip)}</span>{_esc(' — ' + row.reason) if row.reason else ''}</div>"
            )
            text.append(f"    {nudge}")
        html.append(_line(inner))
    prose_html, prose_lines = _prose(blocks)
    html.append(prose_html)
    _add_prose(text, prose_lines)
    return _Section("Power rankings", _row("".join(html)), text)


# --------------------------------------------------------------------------- #
# Luck — block-character bars
# --------------------------------------------------------------------------- #


def luck_bar(value: float, peak: float) -> str:
    """``█`` × a length scaled to at most :data:`LUCK_BAR_MAX`, never fewer than one."""
    scaled = round(abs(value) / peak * LUCK_BAR_MAX) if peak > 0 else 0
    return "█" * max(1, min(LUCK_BAR_MAX, scaled))


def _luck(facts: WeeklyFacts, blocks: list[str]) -> _Section | None:
    rows = wm.luck_rows(facts)
    if not rows:
        return None
    peak = max(abs(value) for value, _team in rows)
    width = max(len(wm.team_label(team)) for _value, team in rows)
    cells: list[str] = []
    text: list[str] = []
    for value, team in rows:
        name = wm.team_label(team)
        bar = luck_bar(value, peak)
        colour = GOOD_TEXT if value > 0 else BAD_TEXT if value < 0 else INK2
        cells.append(
            "<tr>"
            f'<td style="padding:4px 8px 4px 0;font-family:{_SANS};font-size:14px;color:{INK};">{_esc(name)}</td>'
            f'<td width="52" align="right" style="padding:4px 8px;width:52px;text-align:right;font-family:{_MONO};'
            f'font-size:13px;color:{INK};">{_esc(wm.signed(value))}</td>'
            f'<td width="150" style="padding:4px 0;width:150px;font-family:{_MONO};font-size:13px;'
            f'letter-spacing:-1px;color:{colour};">{bar}</td>'
            "</tr>"
        )
        text.append(f"{name:<{width}}  {wm.signed(value):>5}  {bar}")
    prose_html, prose_lines = _prose(blocks)
    _add_prose(text, prose_lines)
    table = f'{_TABLE} style="margin-top:6px;">' + "".join(cells) + "</table>"
    return _Section("The luck index", _row(_label("The luck index") + table + prose_html), text)


# --------------------------------------------------------------------------- #
# Next week — shared stakes once, differing stakes per matchup
# --------------------------------------------------------------------------- #


def _next_week(facts: WeeklyFacts, blocks: list[str]) -> _Section | None:
    cards = facts.matchups.next_week
    if not cards:
        return None
    label = wm.labeller(facts)
    shared = wm.shared_stakes(cards)
    html: list[str] = [_label("Next week")]
    text: list[str] = []
    if shared:
        line = "On the line in every game: " + ", ".join(wm.stake_label(tag) for tag in shared)
        html.append(_small(line, INK, top=8))
        text.append(line)
    if facts.period.nfl_byes_next_week:
        line = "NFL teams on bye: " + ", ".join(sorted(facts.period.nfl_byes_next_week))
        html.append(_small(line, top=4))
        text.append(line)
    html.append('<div style="padding-top:8px;">')
    for card in cards:
        extras = (["Game of the week"] if card.game_of_week else []) + [
            wm.stake_label(tag) for tag in wm.own_stakes(card, shared)
        ]
        a, b = label(card.a_roster_id), label(card.b_roster_id)
        inner = (
            f"{_b(a)} {_mono(f'({card.a_record})', INK2)} vs {_b(b)} {_mono(f'({card.b_record})', INK2)}"
            + "".join(f" {_tag(extra, EMPH if extra == 'Game of the week' else INK2)}" for extra in extras)
        )
        line = f"{a} ({card.a_record}) vs {b} ({card.b_record})" + "".join(f" [{extra}]" for extra in extras)
        text.append(line)
        if card.bye_impact:
            names = ", ".join(
                f"{player.name} ({' '.join(p for p in (player.pos, player.nfl_team) if p)})"
                if (player.pos or player.nfl_team)
                else player.name
                for player in card.bye_impact
            )
            inner += _small(f"On bye: {names}", top=2)
            text.append(f"    On bye: {names}")
        html.append(_line(inner))
    html.append("</div>")
    prose_html, prose_lines = _prose(blocks)
    html.append(prose_html)
    _add_prose(text, prose_lines)
    return _Section("Next week", _row("".join(html)), text)


# --------------------------------------------------------------------------- #
# Transactions — two short tables
# --------------------------------------------------------------------------- #


def _th(text: str, align: str = "left") -> str:
    return (
        f'<td align="{align}" style="padding:0 6px 6px 0;border-bottom:2px solid {RULE};font-family:{_MONO};'
        f'font-size:10px;letter-spacing:1px;text-transform:uppercase;color:{INK2};text-align:{align};">'
        f"{_esc(text)}</td>"
    )


def _td(inner: str, align: str = "left", extra: str = "") -> str:
    return (
        f'<td align="{align}" style="padding:7px 6px 7px 0;border-bottom:1px solid {LINE};font-family:{_SANS};'
        f'font-size:14px;line-height:1.4;color:{INK};vertical-align:top;text-align:{align};{extra}">{inner}</td>'
    )


def _who(name: str, pos: str | None) -> str:
    return f"{name} {pos}" if pos else name


def _moves_table(facts: WeeklyFacts, moves: list[WeeklyMove]) -> tuple[str, list[str]]:
    label = wm.labeller(facts)
    rows = [f"<tr>{_th('Team')}{_th('Move')}{_th('Players')}{_th('FAAB', 'right')}</tr>"]
    text = ["This week:"]
    for move in moves:
        kind = {"waiver": "Waiver", "free_agent": "Free agent"}.get(move.type, move.type.replace("_", " ").capitalize())
        team = label(move.roster_ids[0]) if move.roster_ids else ""
        players = [f"Add {_who(p.name, p.pos)}" for p in move.adds]
        players += [f"Drop {_who(p.name, p.pos)}" for p in move.drops]
        faab = f"${move.faab}" if move.faab is not None else "–"
        players_html = "<br>".join(
            f'<span style="font-family:{_MONO};font-size:11px;color:{GOOD_TEXT if p.startswith("Add") else BAD_TEXT};">'
            f"{_esc(p.split(' ', 1)[0])}</span> {_esc(p.split(' ', 1)[1])}"
            for p in players
        )
        rows.append(
            f"<tr>{_td(_b(team))}{_td(_esc(kind))}{_td(players_html)}"
            f"{_td(_esc(faab), 'right', 'font-family:' + _MONO + ';')}</tr>"
        )
        text.append(f"  {team} · {kind} · {'; '.join(players)} · FAAB {faab}")
    return f"{_TABLE}>" + "".join(rows) + "</table>", text


def _trades_table(facts: WeeklyFacts, trades: list[WeeklyTrade]) -> tuple[str, list[str]]:
    label = wm.labeller(facts)
    ago = facts.week - trades[0].week
    when = "this week" if ago <= 0 else ("1 week ago" if ago == 1 else f"{ago} weeks ago")
    heading = f"Latest trade · week {trades[0].week} · {when}"
    rows = [f"<tr>{_th('Team')}{_th('Receives')}</tr>"]
    text = [f"{heading}:"]
    for trade in trades:
        for side in trade.sides:
            items = [_who(p.name, p.pos) for p in side.players]
            items += [f"{pick.season} round {pick.round} pick" for pick in side.picks]
            if side.faab:
                items.append(f"FAAB ${side.faab}")
            rows.append(f"<tr>{_td(_b(label(side.roster_id)))}{_td(_esc(', '.join(items) or 'nothing listed'))}</tr>")
            text.append(f"  {label(side.roster_id)} receives {', '.join(items) or 'nothing listed'}")
    return (
        _small(heading, top=14) + f'{_TABLE} style="margin-top:6px;">' + "".join(rows) + "</table>",
        text,
    )


def _transactions(facts: WeeklyFacts, blocks: list[str]) -> _Section | None:
    moves = wm.week_moves(facts)
    trades = wm.latest_trades(facts)
    if not moves and not trades:
        return None
    html: list[str] = [_label("The transaction desk")]
    text: list[str] = []
    if moves:
        table, lines = _moves_table(facts, moves)
        html.append(_small("This week", top=10) + f'<div style="padding-top:6px;">{table}</div>')
        text.extend(lines)
    if trades:
        table, lines = _trades_table(facts, trades)
        html.append(table)
        text.extend(lines)
    prose_html, prose_lines = _prose(blocks)
    html.append(prose_html)
    _add_prose(text, prose_lines)
    return _Section("The transaction desk", _row("".join(html)), text)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def render_weekly_email(facts: WeeklyFacts, issue: WeeklyIssue, *, generated_at: str) -> EmailParts:
    """Render the weekly Issue as an :class:`EmailParts` ``(html, text)`` pair.

    ``facts`` supplies every number; ``issue`` supplies the narrated prose
    (matched by :data:`SECTION_HEADINGS`), the title, and any stamp the run
    added (an ``UNVERIFIED`` dateline, a Correction section). ``generated_at`` is
    the only time value; identical arguments give byte-identical output.
    """
    prose = {
        section.heading: list(section.blocks) for section in issue.sections if section.heading in SECTION_HEADINGS
    }

    def blocks(heading: str) -> list[str]:
        return prose.get(heading, [])

    built = [
        *_notices(issue),
        _lead(facts, blocks("The Lead")),
        _results(facts, blocks("Around the League")),
        _standings(facts, blocks("Standings and the Playoff Picture")),
        _power(facts, blocks("Power Rankings")),
        _luck(facts, blocks("The Luck Index")),
        _next_week(facts, blocks("Next Week")),
        _transactions(facts, blocks("The Transaction Desk")),
    ]
    sections = [section for section in built if section is not None]

    league = facts.league
    footer_stamp = f"{league.name} · {league.season} · Week {facts.week} · generated {generated_at}"
    footer = _row(
        f'<div style="border-top:1px solid {LINE};padding-top:12px;font-family:{_MONO};font-size:11px;'
        f'line-height:1.7;color:{INK2};">Also as plain text. Every number is computed from the '
        "league&rsquo;s own Sleeper record; the prose is the narrator&rsquo;s."
        f"<br>{_esc(footer_stamp)}</div>",
        "32px 40px 40px",
    )
    lead = wm.choose_lead(facts)
    preheader = lead.hook if lead is not None and lead.hook else _week_line(facts)
    html_doc = _document(
        _esc(issue.title),
        _esc(preheader),
        _masthead(facts) + "".join(section.html for section in sections) + footer,
        body_bg=PAPER,
        sheet_bg=CARD,
        width=_WIDTH,
        style_block=_STYLE_BLOCK,
    )

    parts = [f"{league.name}\n{_week_line(facts)}\ngenerated {generated_at}"]
    for section in sections:
        parts.append("\n".join([section.label.upper(), *section.text]))
    parts.append(
        "Every number is computed from the league's own Sleeper record; the prose is the narrator's."
    )
    return EmailParts(html_doc, _plain("\n\n".join(parts) + "\n"))
