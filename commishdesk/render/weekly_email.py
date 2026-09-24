"""Story 5.14c — the weekly Issue as the reduced web form email.

The renderer follows the approved Email v2 board: a dark masthead over a
rounded paper sheet, four tinted stat tiles, a lead card with a cell-width bench
bar and 2x2 award cards, tinted winner rows, BYE/BUBBLE cells on the standings
table, an emphasized section dash, a model-score power list, a diverging luck
index built from table cells, tinted next-week cards, and two short transaction
cards. It keeps the same content selectors and ``text/plain`` content as the
earlier Story 5.14b render.

A ``prefers-color-scheme: dark`` override in the head style block rewrites the
literal inline colours via ``[style*=...]`` selectors — no CSS custom property
is emitted, and the light values stay inline as the fallback.

Story 5.15 adds an unconfirmed-seeding note above the standings table when the
Facts playoff picture carries ``seeding_unconfirmed`` — one line in each part.

**Deterministic and escaped.** ``generated_at`` is the only time value. Every
interpolated HTML value goes through :func:`~commishdesk.render._body._esc`; the
text part is bidi-stripped through :func:`~commishdesk.render._body._plain`.

**Pipeline fence (AD-1).** Imports the standard library, ``commishdesk.facts``
schema types, the weekly narrator's output types, and ``commishdesk.render``
helpers only.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import NamedTuple

from commishdesk.facts.schema import LeadCandidate, WeeklyFacts, WeeklyMove, WeeklyTrade
from commishdesk.narrate.weekly_template import SECTION_HEADINGS, WeeklyIssue
from commishdesk.render import _weekly_model as wm
from commishdesk.render._body import _esc, _plain
from commishdesk.render.email import EmailParts, _document
from commishdesk.render.style import WEEKLY_DARK_TOKENS, WEEKLY_LIGHT_TOKENS

__all__ = ["render_weekly_email"]

# --------------------------------------------------------------------------- #
# Palette — literal hex from Tuesday Morning light, with a dark enhancement
# --------------------------------------------------------------------------- #

_T = WEEKLY_LIGHT_TOKENS
_D = WEEKLY_DARK_TOKENS

PAPER = _T["--paper"]
CARD = _T["--card"]
INK = _T["--ink"]
INK2 = _T["--ink-2"]
LINE = _T["--line"]
RULE = _T["--rule"]
EMPH = _T["--emph"]
GOOD = _T["--good"]
GOOD_TEXT = _T["--good-text"]
BAD = _T["--bad"]
BAD_TEXT = _T["--bad-text"]
NOTABLE = _T["--notable"]
BAR = _T["--bar"]
BARLO = _T["--barlo"]
TRACK = _T["--hi"]
CUT = _T["--cut"]

# Roles the shared token set doesn't carry — the approved Email v2 values.
EMPH_TEXT = "#3E4CC2"
GOOD_LAB = "#0E6B45"
BAD_LAB = "#A9282D"
MAST = "#1F2140"
ON_MAST = "#FFFFFF"
MAST_SUB = "#C9CBE0"

D_PAPER = _D["--paper"]
D_CARD = _D["--card"]
D_INK = _D["--ink"]
D_INK2 = _D["--ink-2"]
D_LINE = _D["--line"]
D_EMPH = _D["--emph"]
D_GOOD = _D["--good"]
D_GOOD_TEXT = _D["--good-text"]
D_BAD = _D["--bad"]
D_BAD_TEXT = _D["--bad-text"]
D_NOTABLE = _D["--notable"]
D_BAR = _D["--bar"]
D_BARLO = _D["--barlo"]
D_TRACK = _D["--hi"]
D_CUT = _D["--cut"]
D_MAST = "#0E0F1E"
D_ON_MAST = "#F0EEF8"
D_MAST_SUB = "#B2B4CE"
D_EMPH_TEXT = "#A9B2FF"
D_GOOD_LAB = "#4CC38D"
D_BAD_LAB = "#FF7A7F"


def _blend(fg: str, alpha: float, bg: str) -> str:
    """An ``#rrggbb`` wash: *fg* over *bg* at *alpha* opacity, composited to an
    opaque literal hex (email cannot resolve a ``color-mix``)."""
    f = tuple(int(fg[i : i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(bg[i : i + 2], 16) for i in (1, 3, 5))
    merged = tuple(round(f[i] * alpha + b[i] * (1 - alpha)) for i in range(3))
    return "#{:02X}{:02X}{:02X}".format(*merged)


_ALPHAS = {"emph": 0.11, "good": 0.086, "bad": 0.11, "notable": 0.19}
_D_ALPHA = 0.14

EMPH_WASH_CARD = _blend(EMPH, _ALPHAS["emph"], CARD)
GOOD_WASH_CARD = _blend(GOOD, _ALPHAS["good"], CARD)
BAD_WASH_CARD = _blend(BAD, _ALPHAS["bad"], CARD)
NOTABLE_WASH_CARD = _blend(NOTABLE, _ALPHAS["notable"], CARD)
EMPH_WASH_PAPER = _blend(EMPH, _ALPHAS["emph"], PAPER)
GOOD_WASH_PAPER = _blend(GOOD, _ALPHAS["good"], PAPER)
BAD_WASH_PAPER = _blend(BAD, _ALPHAS["bad"], PAPER)
NOTABLE_WASH_PAPER = _blend(NOTABLE, _ALPHAS["notable"], PAPER)
MONO_BG = _blend(INK2, 0.14, CARD)
WIN_TINT = _blend(EMPH, 0.07, CARD)

D_EMPH_WASH_CARD = _blend(D_EMPH, _D_ALPHA, D_CARD)
D_GOOD_WASH_CARD = _blend(D_GOOD, _D_ALPHA, D_CARD)
D_BAD_WASH_CARD = _blend(D_BAD, _D_ALPHA, D_CARD)
D_NOTABLE_WASH_CARD = _blend(D_NOTABLE, _D_ALPHA, D_CARD)
D_EMPH_WASH_PAPER = _blend(D_EMPH, _D_ALPHA, D_PAPER)
D_GOOD_WASH_PAPER = _blend(D_GOOD, _D_ALPHA, D_PAPER)
D_BAD_WASH_PAPER = _blend(D_BAD, _D_ALPHA, D_PAPER)
D_NOTABLE_WASH_PAPER = _blend(D_NOTABLE, _D_ALPHA, D_PAPER)
D_MONO_BG = _blend(D_INK2, 0.14, D_CARD)
D_WIN_TINT = _blend(D_EMPH, 0.07, D_CARD)

_WIDTH = 640
#: Luck bars: at most this many ``█`` in the text/plain part.
LUCK_BAR_MAX = 11

_DISPLAY = "'Bricolage Grotesque','Helvetica Neue',Helvetica,Arial,sans-serif"
_SANS = "'DM Sans','Helvetica Neue',Helvetica,Arial,sans-serif"
_MONO = "'IBM Plex Mono','Courier New',Courier,monospace"


def _text_map() -> dict[str, str]:
    """Light -> dark for the ``color:`` (text) property only."""
    return {
        INK: D_INK,
        INK2: D_INK2,
        EMPH_TEXT: D_EMPH_TEXT,
        GOOD_TEXT: D_GOOD_TEXT,
        GOOD_LAB: D_GOOD_LAB,
        BAD_TEXT: D_BAD_TEXT,
        BAD_LAB: D_BAD_LAB,
        ON_MAST: D_ON_MAST,
        MAST_SUB: D_MAST_SUB,
    }


def _background_map() -> dict[str, str]:
    """Light -> dark for ``background-color`` / ``bgcolor`` fills only."""
    return {
        PAPER: D_PAPER,
        CARD: D_CARD,
        MAST: D_MAST,
        EMPH: D_EMPH,
        GOOD: D_GOOD,
        BAD: D_BAD,
        NOTABLE: D_NOTABLE,
        BAR: D_BAR,
        BARLO: D_BARLO,
        TRACK: D_TRACK,
        EMPH_WASH_CARD: D_EMPH_WASH_CARD,
        GOOD_WASH_CARD: D_GOOD_WASH_CARD,
        BAD_WASH_CARD: D_BAD_WASH_CARD,
        NOTABLE_WASH_CARD: D_NOTABLE_WASH_CARD,
        EMPH_WASH_PAPER: D_EMPH_WASH_PAPER,
        GOOD_WASH_PAPER: D_GOOD_WASH_PAPER,
        BAD_WASH_PAPER: D_BAD_WASH_PAPER,
        NOTABLE_WASH_PAPER: D_NOTABLE_WASH_PAPER,
        MONO_BG: D_MONO_BG,
        WIN_TINT: D_WIN_TINT,
    }


def _border_map() -> dict[str, str]:
    """Light -> dark for border colours (``solid`` / ``dashed`` declarations)."""
    return {
        LINE: D_LINE,
        CUT: D_CUT,
        EMPH: D_EMPH,
        GOOD: D_GOOD,
        BAD: D_BAD,
        NOTABLE: D_NOTABLE,
    }


def _dark_overrides() -> str:
    """``prefers-color-scheme: dark`` overrides for the email-safe palette.

    Inline light styles are the unconditional fallback. The same light hex plays
    different roles (ink is both a text colour and, as the masthead, a fill), so
    text, fill and border are mapped separately: ``;color:X`` matches a text
    declaration only (never ``background-color:X``), ``background-color:X`` and
    ``bgcolor="X"`` match fills, and ``solid X`` / ``dashed X`` match borders.
    No CSS custom property is emitted (email clients don't resolve them)."""
    rules: list[str] = []
    for light, dark in _text_map().items():
        rules.append(f'[style*=";color:{light}"],[style^="color:{light}"]{{color:{dark}!important;}}')
    for light, dark in _background_map().items():
        rules.append(
            f'[style*="background-color:{light}"]{{background-color:{dark}!important;}}'
        )
    for light, dark in _border_map().items():
        rules.append(
            f'[style*="solid {light}"],[style*="dashed {light}"]{{border-color:{dark}!important;}}'
        )
    # Dark text on the amber game-of-the-week header keeps the board's on-emphasis ink.
    rules.append(
        f'[style*="background-color:{NOTABLE};color:{INK}"]{{color:{D_PAPER}!important;}}'
    )
    return "@media (prefers-color-scheme: dark){\n" + "\n".join(rules) + "\n}"


_STYLE_BLOCK = (
    "body{margin:0;padding:0;width:100%;"
    "-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;}"
    "table{mso-table-lspace:0pt;mso-table-rspace:0pt;}"
    "@media only screen and (max-width:660px){"
    ".container{width:100%!important;}"
    ".px{padding-left:20px!important;padding-right:20px!important;}"
    ".fw{width:100%!important;table-layout:auto!important;}"
    ".stack{display:block!important;width:100%!important;box-sizing:border-box;margin-bottom:12px;}"
    ".gap,.hide{display:none!important;}"
    ".lname{width:104px!important;}"
    "}"
)


def _build_style_block() -> str:
    return _STYLE_BLOCK + "\n" + _dark_overrides()


class _Section(NamedTuple):
    """One rendered section: its label, its HTML table rows, its text lines."""

    label: str
    html: str
    text: list[str]


# --------------------------------------------------------------------------- #
# HTML atoms
# --------------------------------------------------------------------------- #


def _tr(inner: str, pad: str = "0 28px") -> str:
    return f'<tr><td class="px" style="padding:{pad};">{inner}</td></tr>'


#: The 12px gutter between tile / award columns; hidden when the columns stack.
_GAP_CELL = (
    '<td class="gap" width="12" '
    'style="width:12px;min-width:12px;font-size:1px;line-height:1px;">&nbsp;</td>'
)


def _table(style: str = "", cls: str = "") -> str:
    attr = f' class="{cls}"' if cls else ""
    if style:
        return f'<table role="presentation"{attr} width="100%" cellpadding="0" cellspacing="0" style="{style}">'
    return f'<table role="presentation"{attr} width="100%" cellpadding="0" cellspacing="0">'


def _para(text: str, colour: str = INK2, size: int = 15, top: int = 12) -> str:
    return (
        f'<p style="margin:{top}px 0 0;font-family:{_SANS};font-size:{size}px;line-height:1.55;'
        f'color:{colour};">{_esc(text)}</p>'
    )


def _small(text: str, colour: str = INK2, top: int = 6) -> str:
    return (
        f'<div style="font-family:{_MONO};font-size:12px;line-height:1.5;color:{colour};'
        f'padding-top:{top}px;">{_esc(text)}</div>'
    )


def _b(text: str) -> str:
    return f'<b style="color:{INK};">{_esc(text)}</b>'


def _mono(text: str, colour: str = INK) -> str:
    return f'<span style="font-family:{_MONO};color:{colour};">{_esc(text)}</span>'


def _chip(text: str, fg: str, bg: str) -> str:
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" align="left"><tr>'
        f'<td bgcolor="{bg}" style="background-color:{bg};color:{fg};font-family:{_MONO};'
        f'font-size:10px;font-weight:bold;letter-spacing:.08em;text-transform:uppercase;'
        f'padding:3px 8px;border-radius:999px;white-space:nowrap;">{_esc(text)}</td></tr></table>'
    )


def _monogram(name: str) -> str:
    tokens = [token for token in re.split(r"[ -]", name) if token]
    if not tokens:
        return "??"
    if len(tokens) == 1:
        return (tokens[0] * 2)[:2].upper()
    return (tokens[0][0] + tokens[1][0]).upper()


def _mono_cell(name: str, w: int = 26) -> str:
    outer_w = w + 8
    return (
        f'<td width="{outer_w}" valign="middle" style="width:{outer_w}px;padding:0 8px 0 0;">'
        '<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
        f'<td width="{w}" height="{w}" align="center" bgcolor="{MONO_BG}" '
        f'style="width:{w}px;height:{w}px;background-color:{MONO_BG};border-radius:8px;'
        f'font-family:{_MONO};font-size:11px;font-weight:bold;color:{INK2};">'
        f'{_esc(_monogram(name))}</td></tr></table></td>'
    )


def _prose(
    blocks: Sequence[str], colour: str = INK2, size: int = 15, first_top: int = 16
) -> tuple[str, list[str]]:
    """Narrator prose as tidy body paragraphs (the board draws none; kept from 5.14b)."""
    kept = [block for block in blocks if block]
    html = "".join(
        _para(block, colour, size, first_top if index == 0 else 12)
        for index, block in enumerate(kept)
    )
    return html, kept


def _add_prose(text: list[str], prose: list[str]) -> None:
    if prose:
        if text:
            text.append("")
        text.extend(prose)


def _dash_cell() -> str:
    return (
        '<td width="26" valign="middle" style="width:26px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
        f'<td bgcolor="{EMPH}" style="width:20px;height:4px;line-height:4px;font-size:0;'
        f'background-color:{EMPH};border-radius:2px;">&nbsp;</td></tr></table></td>'
    )


def _section_header(label: str, title: str | None = None, sub: str | None = None) -> str:
    inner = (
        _table()
        + "<tr>"
        + _dash_cell()
        + f'<td valign="middle" style="font-family:{_MONO};font-size:11px;'
        f'letter-spacing:.16em;text-transform:uppercase;color:{EMPH_TEXT};font-weight:bold;">'
        f'{_esc(label)}</td></tr></table>'
    )
    if title:
        inner += (
            f'<div style="font-family:{_DISPLAY};font-weight:800;font-size:26px;'
            f'line-height:1.15;letter-spacing:-.01em;color:{INK};margin-top:12px;">'
            f'{_esc(title)}</div>'
        )
    if sub:
        inner += (
            f'<div style="font-family:{_SANS};font-size:14px;line-height:1.45;'
            f'color:{INK2};margin-top:8px;">{_esc(sub)}</div>'
        )
    return _tr(inner, "44px 28px 18px 28px")


def _week_line(facts: WeeklyFacts) -> str:
    parts = [f"Week {facts.week}", f"Season {facts.league.season}"]
    if facts.period.type and facts.period.type != "regular":
        parts.append(facts.period.type.capitalize())
    parts.append(f"{facts.league.format.team_count} teams")
    return " · ".join(parts)


# --------------------------------------------------------------------------- #
# Masthead + stat tiles
# --------------------------------------------------------------------------- #


def _masthead(facts: WeeklyFacts) -> str:
    return (
        f'<tr><td class="px" bgcolor="{MAST}" align="center" style="padding:34px 28px 30px;'
        f'background-color:{MAST};color:{ON_MAST};text-align:center;border-radius:14px 14px 0 0;">'
        f'<div style="font-family:{_MONO};font-size:11px;letter-spacing:.24em;'
        f'text-transform:uppercase;color:{MAST_SUB};">Commishdesk</div>'
        f'<div style="font-family:{_DISPLAY};font-weight:800;font-size:44px;line-height:1;'
        f'letter-spacing:-.02em;text-transform:uppercase;color:{ON_MAST};margin-top:12px;">'
        f'{_esc(facts.league.name)}</div>'
        f'<div style="font-family:{_MONO};font-size:12px;letter-spacing:.1em;'
        f'text-transform:uppercase;color:{MAST_SUB};margin-top:12px;">{_esc(_week_line(facts))}</div>'
        f"</td></tr>"
    )


def _tiles(facts: WeeklyFacts) -> str:
    summary = facts.period.summary
    games = facts.matchups.this_week
    high = wm.pts(summary.high.points) if summary.high is not None else "–"
    closest = "–"
    if summary.closest is not None:
        matchup = wm.find_matchup(facts, summary.closest.roster_ids)
        if matchup is not None:
            closest = wm.pts(abs(matchup.margin))
    blowouts = str(sum(1 for matchup in games if matchup.is_blowout))
    points = wm.whole(sum(m.home_points + m.away_points for m in games))
    tiles = [("High", high), ("Closest", closest), ("Blowouts", blowouts), ("Pts scored", points)]

    cells: list[str] = []
    for index, (label, value) in enumerate(tiles):
        cells.append(
            f'<td class="stack" width="109" valign="top" bgcolor="{EMPH_WASH_CARD}" '
            f'style="width:109px;background-color:{EMPH_WASH_CARD};border-top:3px solid {EMPH};'
            f'border-radius:8px;padding:14px;">'
            f'<div style="font-family:{_MONO};font-size:10px;letter-spacing:.14em;'
            f'text-transform:uppercase;color:{INK2};">{_esc(label)}</div>'
            f'<div style="font-family:{_DISPLAY};font-weight:800;font-size:26px;'
            f'line-height:1.1;color:{INK};margin-top:8px;">{_esc(value)}</div></td>'
        )
        if index < 3:
            cells.append(_GAP_CELL)

    return (
        _tr(
            _table("table-layout:fixed;width:584px;", "fw") + "<tr>" + "".join(cells) + "</tr></table>",
            "28px 28px 0 28px",
        )
    )


def _notices(issue: WeeklyIssue) -> list[_Section]:
    out: list[_Section] = []
    if issue.dateline.startswith("UNVERIFIED"):
        body = _para(issue.dateline)
        out.append(
            _Section(
                "Unverified",
                _section_header("Unverified") + _tr(body, "0 28px 18px 28px"),
                [issue.dateline],
            )
        )
    for section in issue.sections:
        if section.heading in SECTION_HEADINGS:
            continue
        body, lines = _prose(section.blocks)
        out.append(
            _Section(
                section.heading,
                _section_header(section.heading) + _tr(body, "0 28px 18px 28px"),
                lines,
            )
        )
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


def _bench_bar_html(split: wm.BenchSplit) -> str:
    """The bench bar: started fill / gap / a fixed opponent marker on a track.

    Cell widths come from the started / opponent split (each at least 1% so the
    bar never collapses), with a set 3px marker cell between the two."""

    total = split.started + split.bench
    started_pct = 1
    opp_pct = 1
    if total > 0:
        started_pct = min(99, max(1, round(split.started / total * 100)))
        opp_pct = min(99, max(1, round(split.opponent / total * 100)))
    gap_pct = max(1, opp_pct - started_pct)

    bar = (
        "<div>"
        + _table("margin-top:22px;")
        + "<tr>"
        + f'<td width="{started_pct}%" height="18" bgcolor="{BAR}" '
        f'style="background-color:{BAR};height:18px;font-size:0;'
        f'line-height:18px;border-radius:9px 0 0 9px;">&nbsp;</td>'
        + f'<td width="{gap_pct}%" height="18" bgcolor="{BARLO}" '
        f'style="background-color:{BARLO};height:18px;font-size:0;'
        f'line-height:18px;">&nbsp;</td>'
        + f'<td width="3" height="18" bgcolor="{BAD}" '
        f'style="background-color:{BAD};height:18px;font-size:0;'
        f'line-height:18px;">&nbsp;</td>'
        + f'<td height="18" bgcolor="{BARLO}" '
        f'style="background-color:{BARLO};height:18px;font-size:0;'
        f'line-height:18px;border-radius:0 9px 9px 0;">&nbsp;</td>'
        + "</tr></table></div>"
    )

    labels = [
        ("Started", wm.pts(split.started), EMPH_TEXT, "33%", "left"),
        ("On the bench", wm.pts(split.bench), INK, "34%", "left"),
        ("Opponent", wm.pts(split.opponent), BAD_TEXT, "33%", "right"),
    ]
    stats = _table("margin-top:6px;") + "<tr>"
    for label, value, colour, width, align in labels:
        stats += (
            f'<td width="{width}" valign="top" align="{align}" '
            f'style="width:{width};padding:8px 0 0;text-align:{align};">'
            f'<div style="font-family:{_MONO};font-size:10px;letter-spacing:.12em;'
            f'text-transform:uppercase;color:{INK2};">{_esc(label)}</div>'
            f'<div style="font-family:{_DISPLAY};font-weight:800;font-size:20px;'
            f'color:{colour};margin-top:4px;">{value}</div></td>'
        )
    stats += "</tr></table>"
    return bar + stats


def _award_card(award: wm.Award, last_col: bool) -> str:
    kind = award.kind
    if kind == "coach":
        bg, label_colour, edge = GOOD_WASH_PAPER, GOOD_LAB, GOOD
    elif kind == "player":
        bg, label_colour, edge = EMPH_WASH_PAPER, EMPH_TEXT, EMPH
    elif kind == "bust":
        bg, label_colour, edge = BAD_WASH_PAPER, BAD_LAB, BAD
    else:
        bg, label_colour, edge = NOTABLE_WASH_PAPER, INK, NOTABLE

    if award.sub.startswith("of "):
        sub = f"{award.value} {award.sub}"
    else:
        sub = " · ".join(part for part in (award.value, award.sub) if part)

    cell = (
        f'<td class="stack" width="250" valign="top" bgcolor="{bg}" style="width:250px;background-color:{bg};'
        f'border-left:4px solid {edge};border-radius:10px;padding:16px;">'
        f'<div style="font-family:{_MONO};font-size:10px;letter-spacing:.12em;'
        f'text-transform:uppercase;font-weight:bold;color:{label_colour};">{_esc(award.key)}</div>'
        f'<div style="font-family:{_DISPLAY};font-weight:800;font-size:17px;line-height:1.2;'
        f'color:{INK};margin-top:8px;">{_esc(award.name)}</div>'
        f'<div style="font-family:{_SANS};font-size:13px;line-height:1.35;color:{INK2};'
        f'margin-top:5px;">{_esc(sub)}</div></td>'
    )
    if not last_col:
        cell += _GAP_CELL
    return cell


def _awards_html(awards: Sequence[wm.Award]) -> str:
    if not awards:
        return ""
    html = _table("table-layout:fixed;width:584px;", "fw")
    for index in range(0, len(awards), 2):
        pair: list[wm.Award | None] = list(awards[index : index + 2])
        if len(pair) == 1:
            pair.append(None)
        html += "<tr>"
        for j, award in enumerate(pair):
            if award is None:
                html += '<td class="gap" width="250" style="width:250px;">&nbsp;</td>'
                continue
            html += _award_card(award, j == len(pair) - 1)
        html += "</tr>"
        if index + 2 < len(awards):
            html += (
                '<tr class="gap"><td colspan="3" height="12" '
                'style="height:12px;font-size:0;line-height:12px;">&nbsp;</td></tr>'
            )
    html += "</table>"
    return html


def _lead(facts: WeeklyFacts, blocks: list[str]) -> _Section | None:
    lead = wm.choose_lead(facts)
    award_lines = [_award_line(award) for award in wm.awards(facts)]

    if lead is not None and lead.hook:
        hook = lead.hook
        prose = [block for block in blocks if block != hook]
    elif blocks:
        hook, prose = blocks[0], blocks[1:]
    elif award_lines:
        hook, prose = "", []
    else:
        return None

    text: list[str] = []
    if hook:
        text.append(hook)

    card_parts: list[str] = []
    if hook:
        card_parts.append(
            f'<div style="font-family:{_DISPLAY};font-weight:800;font-size:22px;'
            f'line-height:1.22;color:{INK};">{_esc(hook)}</div>'
        )

    split = wm.bench_split(facts, lead) if lead is not None else None
    if split is not None:
        card_parts.append(_bench_bar_html(split))
        caption = (
            f"started {wm.pts(split.started)} · left on bench {wm.pts(split.bench)} · "
            f"opponent {wm.pts(split.opponent)}"
        )
        text.append(caption)  # text/plain only: the HTML's labelled figures replace it
    elif lead is not None:
        numbers = _lead_numbers(facts, lead)
        if numbers:
            card_parts.append(_small(numbers))
            text.append(numbers)

    prose_html, prose_lines = _prose(prose, INK, 16, 20)
    card_parts.append(prose_html)
    _add_prose(text, prose_lines)

    if award_lines:
        if text:
            text.append("")
        text.extend(award_lines)

    html = _section_header("The lead")
    if any(card_parts):
        card = (
            _table()
            + f'<tr><td bgcolor="{CARD}" style="background-color:{CARD};border:1px solid {LINE};'
            f'border-radius:12px;padding:24px;">'
            + "".join(card_parts)
            + "</td></tr></table>"
        )
        html += _tr(card, "0 28px")
    awards_html = _awards_html(wm.awards(facts))
    if awards_html:
        html += _tr(awards_html, "18px 28px 0")
    return _Section("The lead", html, text)


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #

_TAG_WORDS = {
    "week_high": ("Week high", INK, NOTABLE_WASH_CARD),
    "closest": ("Nail-biter", EMPH_TEXT, EMPH_WASH_CARD),
    "blowout": ("Blowout", BAD_TEXT, BAD_WASH_CARD),
}


def _results_title(games: Sequence[object]) -> str:
    n = len(games)
    title = f"{wm.spell(n).capitalize()} game{'s' if n != 1 else ''}"
    blowouts = sum(1 for game in games if getattr(game, "is_blowout", False))
    if blowouts:
        title += f", {wm.spell(blowouts)} that {'wasn’t' if blowouts == 1 else 'weren’t'} close"
    return title


def _game_side(name: str, points: str, *, winner: bool, tie: bool) -> str:
    indicator = "T" if tie else ("W" if winner else "L")
    indicator_colour = GOOD_TEXT if winner and not tie else INK2
    return (
        _table()
        + f'<tr>{_mono_cell(name)}'
        f'<td style="font-family:{_SANS};font-weight:{"800" if winner and not tie else "500"};'
        f'font-size:15px;color:{INK if winner and not tie else INK2};">{_esc(name)}</td>'
        f'<td width="70" align="right" style="width:70px;font-family:{_MONO};font-size:15px;'
        f'font-weight:bold;color:{INK if winner and not tie else INK2};">{points}</td>'
        f'<td width="24" align="right" style="width:24px;font-family:{_MONO};font-size:11px;'
        f'font-weight:bold;color:{indicator_colour};">{indicator}</td></tr></table>'
    )


def _results(facts: WeeklyFacts, blocks: list[str]) -> _Section | None:
    games = facts.matchups.this_week
    if not games:
        return None
    label = wm.labeller(facts)
    text: list[str] = []
    cards: list[str] = []

    for matchup in games:
        win_id, win_pts, lose_id, lose_pts = wm.winner_loser(matchup)
        tie = matchup.winner_roster_id is None
        tag = _TAG_WORDS.get(wm.game_tag(facts, matchup) or "")

        winner_name = label(win_id)
        loser_name = label(lose_id)
        winner_bg = CARD if tie else WIN_TINT

        top = (
            f'<tr><td bgcolor="{winner_bg}" style="background-color:{winner_bg};'
            f'border-radius:11px 11px 0 0;padding:12px 16px;">'
            + _game_side(winner_name, wm.pts(win_pts), winner=True, tie=tie)
            + "</td></tr>"
        )

        tag_html = ""
        if tag:
            tag_html = (
                '<div style="margin-top:10px;padding-left:38px;">'
                + _chip(tag[0], tag[1], tag[2])
                + '<div style="clear:both;font-size:0;height:0;">&nbsp;</div></div>'
            )
        bottom = (
            '<tr><td style="padding:12px 16px;">'
            + _game_side(loser_name, wm.pts(lose_pts), winner=False, tie=tie)
            + tag_html
            + "</td></tr>"
        )
        cards.append(
            f'<tr><td bgcolor="{CARD}" style="background-color:{CARD};border:1px solid {LINE};'
            f'border-radius:12px;padding:0;"><table role="presentation" width="100%" '
            f'cellpadding="0" cellspacing="0">{top}{bottom}</table></td></tr>'
            '<tr><td height="12" style="height:12px;font-size:0;line-height:12px;">&nbsp;</td></tr>'
        )

        if tie:
            text.append(
                f"{winner_name} {wm.pts(win_pts)} tied {loser_name} {wm.pts(lose_pts)}"
                + (f" [{tag[0]}]" if tag else "")
            )
        else:
            text.append(
                f"{winner_name} {wm.pts(win_pts)} over {loser_name} {wm.pts(lose_pts)}"
                + (f" [{tag[0]}]" if tag else "")
            )

    prose_html, prose_lines = _prose(blocks)
    _add_prose(text, prose_lines)

    html = _section_header("Around the league", _results_title(games))
    html += _tr(_table() + "".join(cards) + "</table>", "0 28px 0")
    if prose_html:
        html += _tr(prose_html, "0 28px")
    return _Section("Around the league", html, text)


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
    text: list[str] = [_standings_title(facts)]
    unconfirmed_note = "seeding unconfirmed — derived from the standings"
    if picture is not None and picture.seeding_unconfirmed:
        text.append(unconfirmed_note)

    cell = f"padding:12px 6px;border-top:1px solid {LINE};"
    rows: list[str] = []
    rows.append(
        f'<tr><td style="font-family:{_MONO};font-size:10px;letter-spacing:.1em;'
        f'text-transform:uppercase;color:{INK2};padding:0 0 10px 10px;">#</td>'
        f'<td style="font-family:{_MONO};font-size:10px;letter-spacing:.1em;'
        f'text-transform:uppercase;color:{INK2};padding-bottom:10px;">Team</td>'
        f'<td align="right" style="font-family:{_MONO};font-size:10px;letter-spacing:.1em;'
        f'text-transform:uppercase;color:{INK2};padding-bottom:10px;">W-L</td>'
        f'<td class="hide" style="font-family:{_MONO};font-size:10px;letter-spacing:.1em;'
        f'text-transform:uppercase;color:{INK2};padding:0 0 10px 12px;">Points for</td>'
        f'<td align="right" style="font-family:{_MONO};font-size:10px;letter-spacing:.1em;'
        f'text-transform:uppercase;color:{INK2};padding-bottom:10px;"></td>'
        f'<td style="font-family:{_MONO};font-size:10px;letter-spacing:.1em;'
        f'text-transform:uppercase;color:{INK2};padding-bottom:10px;"></td></tr>'
    )

    for index, team in enumerate(order, start=1):
        season = team.season
        name = wm.team_label(team)
        rec = wm.record(season.record.w, season.record.l, season.record.t)
        below = cut is not None and index > cut
        filled = round(season.points_for / max_pf * 100) if max_pf > 0 else 0
        marker = ""
        marker_text = ""
        if team.roster_id in byes:
            marker = _chip("Bye", GOOD_TEXT, GOOD_WASH_CARD)
            marker_text = " [BYE]"
        elif team.roster_id in bubble:
            marker = _chip("Bubble", INK, NOTABLE_WASH_CARD)
            marker_text = " [BUBBLE]"

        bar = _table() + "<tr>"
        if filled > 0:
            bar += (
                f'<td width="{filled}%" height="8" bgcolor="{BAR}" '
                f'style="background-color:{BAR};height:8px;font-size:0;line-height:8px;'
                f'border-radius:4px;">&nbsp;</td>'
            )
        if filled < 100:
            bar += '<td style="font-size:0;">&nbsp;</td>'
        bar += "</tr></table>"

        bg = WIN_TINT if marker_text == " [BYE]" else CARD
        weight = "800" if not below else "500"
        rows.append(
            "<tr>"
            f'<td bgcolor="{bg}" width="30" style="background-color:{bg};width:30px;'
            f'padding:12px 4px 12px 10px;'
            f'border-top:1px solid {LINE};border-radius:8px 0 0 8px;font-family:{_MONO};'
            f'font-size:13px;color:{INK2};">{index}</td>'
            f'<td bgcolor="{bg}" style="background-color:{bg};border-top:1px solid {LINE};{cell}'
            f'font-family:{_SANS};font-size:15px;font-weight:{weight};color:{INK};">{_esc(name)}</td>'
            f'<td bgcolor="{bg}" width="44" align="right" style="background-color:{bg};width:44px;'
            f'padding:12px 6px;border-top:1px solid {LINE};font-family:{_MONO};font-size:13px;'
            f'color:{INK};">{_esc(rec)}</td>'
            f'<td class="hide" bgcolor="{bg}" width="96" style="background-color:{bg};width:96px;'
            f'border-top:1px solid {LINE};padding:12px 6px 12px 12px;">{bar}</td>'
            f'<td bgcolor="{bg}" width="46" align="right" style="background-color:{bg};width:46px;'
            f'padding:12px 6px;border-top:1px solid {LINE};font-family:{_MONO};font-size:12px;color:{INK2};">'
            f'{_esc(wm.whole(season.points_for))}</td>'
            f'<td bgcolor="{bg}" width="66" style="background-color:{bg};width:66px;'
            f'border-top:1px solid {LINE};border-radius:0 8px 8px 0;padding:10px 8px 10px 4px;'
            f'vertical-align:middle;">{marker or "&nbsp;"}</td></tr>'
        )
        text.append(f"{season.rank:>2}. {name} — {rec}, {wm.whole(season.points_for)} PF{marker_text}")
        if cut is not None and index == cut and index < len(order):
            rows.append(
                f'<tr><td colspan="6" style="padding:10px 0;">'
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
                f'<td style="border-top:2px dashed {CUT};font-family:{_MONO};font-size:10px;'
                f'letter-spacing:.14em;text-transform:uppercase;color:{INK2};padding-top:8px;'
                f'font-weight:bold;">Playoff line</td></tr></table></td></tr>'
            )
            text.append("--- playoff line ---")

    table = _table("border-collapse:separate;border-spacing:0;") + "".join(rows) + "</table>"
    prose_html, prose_lines = _prose(blocks)
    _add_prose(text, prose_lines)
    html = _section_header("Standings", _standings_title(facts))
    if picture is not None and picture.seeding_unconfirmed:
        html += _tr(_para(unconfirmed_note), "0 28px")
    html += _tr(table, "2px 28px 0")
    if prose_html:
        html += _tr(prose_html, "0 28px")
    return _Section("Standings", html, text)


# --------------------------------------------------------------------------- #
# Power
# --------------------------------------------------------------------------- #


def _power(facts: WeeklyFacts, blocks: list[str]) -> _Section | None:
    rows = wm.power_rows(facts)
    if not rows:
        return None
    text: list[str] = []
    table_rows: list[str] = []

    for row in rows:
        team = row.team
        power = team.season.power
        rec = wm.record(team.season.record.w, team.season.record.l, team.season.record.t)
        score = f"{power.model_score:.2f}" if power.model_score is not None else "–"
        rank = str(row.published) if row.published is not None else "–"
        delta = power.week_delta
        arrow_html = ""
        arrow_text = ""
        if delta:
            arrow_text = f"▲{delta}" if delta > 0 else f"▼{-delta}"
            arrow_html = " " + _mono(arrow_text, GOOD_TEXT if delta > 0 else BAD_TEXT)
        name = wm.team_label(team)
        text.append(f"{rank:>2}. {name} — {rec} · model {score}{' ' + arrow_text if arrow_text else ''}")

        table_rows.append(
            "<tr>"
            f'<td bgcolor="{CARD}" width="34" style="background-color:{CARD};width:34px;'
            f'border-top:1px solid {LINE};padding:14px 4px 14px 14px;'
            f'font-family:{_SANS};font-weight:800;font-size:18px;color:{INK};">{_esc(rank)}</td>'
            f'<td bgcolor="{CARD}" style="background-color:{CARD};border-top:1px solid {LINE};'
            f'padding:14px 6px;"><div style="font-family:{_SANS};font-weight:bold;font-size:15px;'
            f'color:{INK};">{_esc(name)}</div><div style="font-family:{_MONO};font-size:11px;'
            f'color:{INK2};margin-top:4px;">{_esc(rec)}</div></td>'
            f'<td bgcolor="{CARD}" width="50" align="right" style="background-color:{CARD};width:50px;'
            f'border-top:1px solid {LINE};padding:14px 6px;font-family:{_MONO};font-size:14px;'
            f'font-weight:bold;color:{INK};">{_esc(score)}<div style="font-size:9px;'
            f'letter-spacing:.1em;color:{INK2};font-weight:normal;">MODEL</div></td>'
            f'<td bgcolor="{CARD}" width="44" align="right" style="background-color:{CARD};width:44px;'
            f'border-top:1px solid {LINE};padding:14px 14px 14px 6px;font-family:{_MONO};'
            f'font-size:12px;">{arrow_html}</td></tr>'
        )

        if row.published is not None and row.model is not None and row.published != row.model:
            up = row.published < row.model
            fg = GOOD_TEXT if up else BAD_TEXT
            bg = GOOD_WASH_CARD if up else BAD_WASH_CARD
            chip = f"{'▲ Nudged up' if up else '▼ Nudged down'} · model #{row.model}"
            reason = row.reason
            table_rows.append(
                f'<tr><td bgcolor="{bg}" colspan="4" style="background-color:{bg};'
                f'padding:12px 16px 14px 52px;">'
                f'<div style="font-family:{_MONO};font-size:10px;letter-spacing:.1em;'
                f'text-transform:uppercase;font-weight:bold;color:{fg};">{_esc(chip)}</div>'
                f'<div style="font-family:{_SANS};font-size:13px;line-height:1.4;'
                f'color:{INK};margin-top:6px;">{_esc(reason) if reason else ""}</div></td></tr>'
            )
            text.append(f"    {chip}" + (f" — {reason}" if reason else ""))

    prose_html, prose_lines = _prose(blocks)
    _add_prose(text, prose_lines)

    table = (
        _table("border:1px solid " + LINE + ";border-radius:12px;border-collapse:separate;")
        + "".join(table_rows)
        + "</table>"
    )
    html = _section_header("Power rankings", "Who’s actually good")
    html += _tr(table, "2px 28px 0")
    if prose_html:
        html += _tr(prose_html, "0 28px")
    return _Section("Power rankings", html, text)


# --------------------------------------------------------------------------- #
# Luck — diverging cell-width bars
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
    text: list[str] = []
    html_rows: list[str] = []

    header = (
        "<tr>"
        f'<td colspan="3" style="padding:0 0 12px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td style="font-family:{_MONO};font-size:10px;letter-spacing:.12em;'
        f'color:{BAD_TEXT};font-weight:bold;">◀ ROBBED</td>'
        f'<td align="right" style="font-family:{_MONO};font-size:10px;letter-spacing:.12em;'
        f'color:{GOOD_TEXT};font-weight:bold;">BLESSED ▶</td></tr></table></td></tr>'
    )
    html_rows.append(header)

    for value, team in rows:
        name = wm.team_label(team)
        block_bar = luck_bar(value, peak)
        text.append(f"{name:<{width}}  {wm.signed(value):>5}  {block_bar}")

        wd = max(2, round(abs(value) / peak * 100)) if peak > 0 else 0
        sign = ("−" if value < 0 else "+") + f"{abs(value):.1f}"
        colour = BAD_TEXT if value < 0 else GOOD_TEXT if value > 0 else INK2
        if value >= 0:
            bar = (
                f'<td width="50%" style="width:50%;">&nbsp;</td>'
                f'<td width="50%" style="width:50%;border-left:1px solid {CUT};">'
                f'<table role="presentation" cellpadding="0" cellspacing="0" width="{wd}%"><tr>'
                f'<td height="14" bgcolor="{GOOD}" style="background-color:{GOOD};height:14px;'
                f'font-size:0;line-height:14px;border-radius:0 7px 7px 0;">&nbsp;</td></tr></table></td>'
            )
        else:
            bar = (
                f'<td width="50%" align="right" style="width:50%;">'
                f'<table role="presentation" align="right" cellpadding="0" cellspacing="0" '
                f'width="{wd}%"><tr><td height="14" bgcolor="{BAD}" style="background-color:{BAD};'
                f'height:14px;font-size:0;line-height:14px;border-radius:7px 0 0 7px;">&nbsp;</td></tr></table></td>'
                f'<td width="50%" style="width:50%;border-left:1px solid {CUT};">&nbsp;</td>'
            )
        html_rows.append(
            "<tr>"
            f'<td class="lname" width="150" style="width:150px;padding:9px 0;border-top:1px solid {LINE};'
            f'font-family:{_SANS};font-size:14px;color:{INK};">{_esc(name)}</td>'
            f'<td width="44" align="right" style="width:44px;padding:9px 10px 9px 0;'
            f'border-top:1px solid {LINE};font-family:{_MONO};font-size:13px;font-weight:bold;'
            f'color:{colour};">{sign}</td>'
            f'<td style="border-top:1px solid {LINE};padding:9px 0;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
            f'{bar}</tr></table></td></tr>'
        )

    prose_html, prose_lines = _prose(blocks)
    _add_prose(text, prose_lines)
    table = _table() + "".join(html_rows) + "</table>"
    html = _section_header("The luck index", "Who the schedule favoured", "Wins minus expected wins.")
    html += _tr(table, "6px 28px 0")
    if prose_html:
        html += _tr(prose_html, "0 28px")
    return _Section("The luck index", html, text)


# --------------------------------------------------------------------------- #
# Next week
# --------------------------------------------------------------------------- #


def _next_week(facts: WeeklyFacts, blocks: list[str]) -> _Section | None:
    cards = facts.matchups.next_week
    if not cards:
        return None
    label = wm.labeller(facts)
    shared = wm.shared_stakes(cards)
    text: list[str] = []
    cards_html: list[str] = []
    stakes_row = ""

    shared_line = ""
    if shared:
        shared_line = "On the line in every game: " + ", ".join(wm.stake_label(tag) for tag in shared)
        text.append(shared_line)

    byes_line = ""
    if facts.period.nfl_byes_next_week:
        byes_line = "NFL teams on bye: " + ", ".join(sorted(facts.period.nfl_byes_next_week))
        text.append(byes_line)

    if shared_line or byes_line:
        block_inner = ""
        if shared_line:
            head, rest = shared_line.split(": ", 1)
            block_inner += f'<b style="color:{EMPH_TEXT};">{_esc(head)}:</b> {_esc(rest.replace(", ", " · "))}'
        if byes_line:
            if block_inner:
                block_inner += "<br>"
            block_inner += f'<span style="color:{INK2};">{_esc(byes_line)}</span>'
        stakes_row = (
            _tr(
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
                f'<td bgcolor="{EMPH_WASH_CARD}" style="background-color:{EMPH_WASH_CARD};'
                f'border-radius:10px;padding:14px 16px;font-family:{_SANS};font-size:14px;'
                f'line-height:1.5;color:{INK};">{block_inner}</td></tr></table>',
                "0 28px 18px 28px",
            )
        )

    power_rank = {row.team.roster_id: row.published for row in wm.power_rows(facts)}

    for card in cards:
        a, b = label(card.a_roster_id), label(card.b_roster_id)
        gotw = card.game_of_week
        edge = f"2px solid {NOTABLE}" if gotw else f"1px solid {LINE}"
        head = ""
        if gotw:
            head = (
                f'<tr><td colspan="3" bgcolor="{NOTABLE}" style="background-color:{NOTABLE};'
                f'color:{INK};padding:4px 14px;border-radius:9px 9px 0 0;font-family:{_MONO};'
                f'font-size:10px;letter-spacing:.14em;text-transform:uppercase;font-weight:bold;">'
                f'★ Game of the week</td></tr>'
            )
        a_rank = power_rank.get(card.a_roster_id)
        b_rank = power_rank.get(card.b_roster_id)
        a_power = f" · power #{a_rank}" if a_rank is not None else ""
        b_power = f" · power #{b_rank}" if b_rank is not None else ""
        bye = ""
        own = [wm.stake_label(tag) for tag in wm.own_stakes(card, shared)]
        line = (
            f"{a} ({card.a_record}) vs {b} ({card.b_record})"
            + (" [Game of the week]" if gotw else "")
            + "".join(f" [{stake}]" for stake in own)
        )
        if card.bye_impact:
            names = ", ".join(
                f"{player.name} ({' '.join(p for p in (player.pos, player.nfl_team) if p)})"
                if (player.pos or player.nfl_team)
                else player.name
                for player in card.bye_impact
            )
            bye = "On bye: " + ", ".join(player.name for player in card.bye_impact)
            text.append(line)
            text.append(f"    On bye: {names}")
        else:
            text.append(line)

        cards_html.append(
            f'<tr><td style="padding-bottom:14px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="{CARD}" '
            f'style="background-color:{CARD};border:{edge};border-radius:12px;border-collapse:separate;">'
            f"{head}"
            f'<tr><td width="46%" valign="top" style="padding:16px 4px 16px 16px;">'
            f'<div style="font-family:{_SANS};font-weight:bold;font-size:15px;color:{INK};">{_esc(a)}</div>'
            f'<div style="font-family:{_MONO};font-size:11px;color:{INK2};margin-top:5px;">'
            f'{_esc(card.a_record)}{a_power}</div></td>'
            f'<td width="8%" align="center" valign="middle" style="font-family:{_MONO};font-size:11px;'
            f'letter-spacing:.1em;color:{INK2};">VS</td>'
            f'<td width="46%" valign="top" align="right" style="padding:16px 16px 16px 4px;">'
            f'<div style="font-family:{_SANS};font-weight:bold;font-size:15px;color:{INK};">{_esc(b)}</div>'
            f'<div style="font-family:{_MONO};font-size:11px;color:{INK2};margin-top:5px;">'
            f'{_esc(card.b_record)}{b_power}</div></td></tr>'
            + (
                f'<tr><td colspan="3" style="padding:0 16px 10px;font-family:{_MONO};font-size:11px;'
                f'letter-spacing:.08em;text-transform:uppercase;color:{INK2};">'
                f'{_esc(" · ".join(own))}</td></tr>'
                if own
                else ""
            )
            + (
                f'<tr><td colspan="3" style="padding:0 16px 14px;font-family:{_SANS};font-size:12px;'
                f'color:{INK2};">{_esc(bye)}</td></tr>'
                if bye
                else ""
            )
            + "</table></td></tr>"
        )

    prose_html, prose_lines = _prose(blocks)
    _add_prose(text, prose_lines)
    html = _section_header("Next week", "Byes, wildcards and a division race")
    if shared_line or byes_line:
        html += stakes_row
    html += _tr(_table() + "".join(cards_html) + "</table>", "0 28px")
    if prose_html:
        html += _tr(prose_html, "0 28px")
    return _Section("Next week", html, text)


# --------------------------------------------------------------------------- #
# Transactions
# --------------------------------------------------------------------------- #


def _who(name: str, pos: str | None) -> str:
    return f"{name} {pos}" if pos else name


def _moves_table(facts: WeeklyFacts, moves: list[WeeklyMove]) -> tuple[str, list[str]]:
    label = wm.labeller(facts)
    body_rows: list[str] = []
    text = ["This week:"]
    for move in moves:
        kind = {"waiver": "Waiver", "free_agent": "Free agent"}.get(
            move.type, move.type.replace("_", " ").capitalize()
        )
        team = label(move.roster_ids[0]) if move.roster_ids else ""
        players_add = [f"Add {_who(p.name, p.pos)}" for p in move.adds]
        players_drop = [f"Drop {_who(p.name, p.pos)}" for p in move.drops]
        faab = f"${move.faab}" if move.faab is not None else "–"

        def _row(chip: str, player: object, trailing: str) -> str:
            pos = getattr(player, "pos", None)
            return (
                f'<tr><td width="54" style="width:54px;padding:5px 0;">{chip}</td>'
                f'<td style="font-family:{_SANS};font-size:14px;color:{INK};">'
                f'<b>{_esc(getattr(player, "name", ""))}</b>{" " + _esc(pos) if pos else ""}</td>'
                f'<td align="right" style="font-family:{_MONO};font-size:12px;color:{INK2};">'
                f"{trailing}</td></tr>"
            )

        add_chip = _chip("Add", GOOD_TEXT, GOOD_WASH_CARD)
        drop_chip = _chip("Drop", BAD_TEXT, BAD_WASH_CARD)
        faab_html = _esc(f"FAAB {faab}") if move.faab is not None else ""
        for index, player in enumerate(move.adds):
            body_rows.append(_row(add_chip, player, faab_html if index == 0 else ""))
        for player in move.drops:
            body_rows.append(_row(drop_chip, player, ""))
        body_rows.append(
            f'<tr><td colspan="3" style="font-family:{_SANS};font-size:13px;color:{INK2};padding-top:6px;">'
            f'{_esc(team)} · {_esc(kind)}</td></tr>'
        )
        joined = "; ".join(players_add + players_drop)
        text.append(f"  {team} · {kind} · {joined} · FAAB {faab}")

    table = _table("margin-top:12px;") + "".join(body_rows) + "</table>"
    return table, text


def _trades_table(facts: WeeklyFacts, trades: list[WeeklyTrade]) -> tuple[str, list[str], str]:
    label = wm.labeller(facts)
    ago = facts.week - trades[0].week
    when = "this week" if ago <= 0 else ("1 week ago" if ago == 1 else f"{ago} weeks ago")
    heading = f"Latest trade · week {trades[0].week} · {when}"
    rows: list[str] = []
    text = [f"{heading}:"]
    for trade in trades:
        for side in trade.sides:
            items = [_who(p.name, p.pos) for p in side.players]
            items += [f"{pick.season} round {pick.round} pick" for pick in side.picks]
            if side.faab:
                items.append(f"FAAB ${side.faab}")
            row_text = ", ".join(items) or "nothing listed"
            rows.append(
                f'<tr><td style="font-family:{_SANS};font-size:14px;color:{INK};padding:4px 0;">'
                f'<b>{_esc(label(side.roster_id))}</b> receives {_esc(row_text)}</td></tr>'
            )
            text.append(f"  {label(side.roster_id)} receives {row_text}")

    table = _table("margin-top:12px;") + "".join(rows) + "</table>"
    return table, text, heading


def _tcard(title: str, body: str) -> str:
    return (
        f'<tr><td style="padding-bottom:14px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="{CARD}" '
        f'style="background-color:{CARD};border:1px solid {LINE};border-radius:12px;border-collapse:separate;">'
        f'<tr><td style="padding:16px 18px;">'
        f'<div style="font-family:{_MONO};font-size:10px;letter-spacing:.12em;text-transform:uppercase;'
        f'color:{INK2};font-weight:bold;">{_esc(title)}</div>{body}</td></tr></table></td></tr>'
    )


def _transactions(facts: WeeklyFacts, blocks: list[str]) -> _Section | None:
    moves = wm.week_moves(facts)
    trades = wm.latest_trades(facts)
    if not moves and not trades:
        return None
    text: list[str] = []
    cards: list[str] = []
    if moves:
        body, lines = _moves_table(facts, moves)
        cards.append(_tcard("This week", body))
        text.extend(lines)
    if trades:
        body, lines, heading = _trades_table(facts, trades)
        cards.append(_tcard(heading, body))
        text.extend(lines)

    prose_html, prose_lines = _prose(blocks)
    _add_prose(text, prose_lines)
    html = _section_header("The transaction desk", "One move, one trade")
    html += _tr(_table() + "".join(cards) + "</table>", "0 28px")
    if prose_html:
        html += _tr(prose_html, "0 28px")
    return _Section("The transaction desk", html, text)


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
        section.heading: list(section.blocks)
        for section in issue.sections
        if section.heading in SECTION_HEADINGS
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
    footer = _tr(
        f'<div style="border-top:1px solid {LINE};padding-top:12px;font-family:{_MONO};'
        f'font-size:11px;line-height:1.5;color:{INK2};">'
        "Every number is computed from the league&rsquo;s own Sleeper record; "
        "the prose is the narrator&rsquo;s.<br>Also as plain text."
        f'<div style="color:{INK2};padding-top:6px;">{_esc(footer_stamp)}</div></div>',
        "24px 28px 30px 28px",
    )

    lead = wm.choose_lead(facts)
    preheader = lead.hook if lead is not None and lead.hook else _week_line(facts)

    rows = (
        _masthead(facts)
        + _tiles(facts)
        + "".join(section.html for section in sections)
        + footer
    )
    html_doc = _document(
        _esc(issue.title),
        _esc(preheader),
        rows,
        body_bg=PAPER,
        sheet_bg=PAPER,
        width=_WIDTH,
        style_block=_build_style_block(),
        container_extra="border-radius:14px;table-layout:fixed;",
    )

    parts = [f"{league.name}\n{_week_line(facts)}\ngenerated {generated_at}"]
    for section in sections:
        parts.append("\n".join([section.label.upper(), *section.text]))
    parts.append(
        "Every number is computed from the league's own Sleeper record; the prose is the narrator's."
    )
    return EmailParts(html_doc, _plain("\n\n".join(parts) + "\n"))
