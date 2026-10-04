"""Story 5.14a — the designed, self-contained weekly web page.

:func:`render_weekly_web` turns the weekly Facts JSON plus the narrated
:class:`~commishdesk.narrate.weekly_template.WeeklyIssue` into one
``<!doctype html>`` string: the approved Story 5.13 design (Tuesday Morning
theme, Editorial layout) with inline CSS, embedded fonts, hand-authored inline
SVG and no external request. Story 6.0c (AD-40) moves the interaction script
and its motion CSS behind the optional
:class:`~commishdesk.render.enhancer.WebEnhancer` seam: without one the page
is static, with zero ``<script>``; the history below describes the interaction layer the
app now supplies. Story 5B.2 adds exactly one small inline script
(reveal / draw-in motion); the page is complete and fully visible without it —
the script only sets ``html.js-reveal`` and adds ``is-revealed`` as sections
scroll into view. Story 5B.3 adds the power-rank bump chart (model trail +
published overrules) and grows the same one inline script with its hover / pin
detail layer. Story 5B.4 adds two more layers to that same script: the luck
index's focus / tap preview and the standings' actual-vs-all-play toggle (both
progressively enhanced — the page still renders complete and readable with
script off, and the toggle stays hidden until the script can drive it).
Story 5B.5 adds expandable matchup and next-week cards: an in-card CSS-revealed
detail panel (never a layout-expanding accordion, so cards don't reorder or
resize on hover/pin), with the same one inline script driving the pin state.

**Section order** — masthead, lead (hero chosen by lead kind, plus the awards
row), around the league, standings, power rankings (with the bump chart),
luck index, next week, transaction desk. A section whose data is absent renders
nothing at all: no heading, no empty frame.

**Where each value comes from.** Numbers and charts come from Facts. Narrated
prose comes from the Issue's sections, matched by
:func:`~commishdesk.narrate.weekly_template.section_headings_for_has_prior_week`.
The power rankings read the published rank and the cited nudge reason from
``narration.power`` (falling back to ``teams[*].season.power.published_rank``,
then to the model rank); a reason is rendered only when the narration carries
one. Luck plots ``season.luck`` as-is — this module computes no luck figure.
Story 5.15's unconfirmed-seeding note comes straight off the Facts playoff
picture's ``seeding_unconfirmed`` flag.

**Deterministic and escaped.** The only time value is the caller's
``generated_at``; ``output_id`` is not rendered into the shareable page. Every
interpolated value — HTML text, SVG text and attribute values — goes through
:func:`~commishdesk.render._body._esc`.

**Pipeline fence (AD-1).** Imports the standard library, ``commishdesk.facts``
schema types, the weekly narrator's output types, and ``commishdesk.render``
helpers only.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence

from commishdesk.facts.schema import (
    LeadCandidate,
    WeeklyByeImpact,
    WeeklyByeStarter,
    WeeklyFacts,
    WeeklyMatchup,
    WeeklyMove,
    WeeklyNextWeekCard,
    WeeklyTeam,
    WeeklyTrade,
)
from commishdesk.narrate.weekly_template import WeeklyIssue, section_headings_for_has_prior_week
from commishdesk.render import _weekly_model as wm
from commishdesk.render._body import _esc
from commishdesk.render._weekly_model import find_matchup as _find_matchup
from commishdesk.render._weekly_model import key_number
from commishdesk.render._weekly_model import pct as _pct
from commishdesk.render._weekly_model import pts as _pts
from commishdesk.render._weekly_model import record as _record
from commishdesk.render._weekly_model import signed as _signed
from commishdesk.render._weekly_model import spell as _spell
from commishdesk.render._weekly_model import stake_label as _stake_label
from commishdesk.render._weekly_model import team_label as _team_label
from commishdesk.render._weekly_model import whole as _whole
from commishdesk.render._weekly_model import winner_loser as _winner_loser
from commishdesk.render.enhancer import WebEnhancer, validate_enhancer, validate_page
from commishdesk.render.style import build_weekly_style
from commishdesk.sections import heading_for

__all__ = ["render_weekly_web"]

# Lead-hero geometry (px, in each SVG's own coordinate space).
_H_W = 800
_H_X0 = 50
_H_SPAN = 700

# Luck diverging-bar geometry.
_L_W = 680
_L_AXIS = 380
_L_HALF = 140
_L_ROW = 40
_L_BAR = 26
_L_TOP = 30
_L_NAME_MAX = 24



# --------------------------------------------------------------------------- #
# Small formatters
# --------------------------------------------------------------------------- #


def _num(value: float) -> str:
    """An SVG coordinate: one decimal, no trailing noise."""
    return f"{value:.1f}"


_WORD_SPLIT = re.compile(r"[\s\-‐‑‒–—―_/]+")


def _monogram(label: str) -> str:
    """Two letters for a neutral team monogram: the first letters of the first
    two words (``Kneel-Down Koalas -> KD``), else the first two letters."""
    letters = []
    for word in _WORD_SPLIT.split(label.strip()):
        first = next((ch for ch in word if ch.isalnum()), "")
        if first:
            letters.append(first)
    if len(letters) >= 2:
        return (letters[0] + letters[1]).upper()
    alnum = [ch for ch in label if ch.isalnum()]
    return "".join(alnum[:2]).upper() or "?"


def _mg(label: str, size: str = "") -> str:
    cls = f"mg {size}".strip()
    return f'<span class="{cls}" aria-hidden="true">{_esc(_monogram(label))}</span>'


def _diamond(cx: float, cy: float, r: float = 7.0) -> str:
    """An SVG diamond path centered on *cx*, *cy*."""
    return (
        f"M {_num(cx)} {_num(cy - r)} L {_num(cx + r)} {_num(cy)} "
        f"L {_num(cx)} {_num(cy + r)} L {_num(cx - r)} {_num(cy)} Z"
    )


def _polyline_length(points: Sequence[tuple[float, float]]) -> float:
    """Total Euclidean length of a traced polyline.

    Story 5B.3's per-mark draw-in computes each line's own dash length from
    its own points, never a shared guessed constant (epic-5B-context.md
    Technical Decisions)."""
    total = 0.0
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        total += math.hypot(x1 - x0, y1 - y0)
    return total


# --------------------------------------------------------------------------- #
# Shared section scaffolding
# --------------------------------------------------------------------------- #


def _svg_open(width: float, height: float, aria: str) -> str:
    """Opening ``<svg>`` tag plus a ``<title>`` first child (mirrors
    ``render/web.py``)."""
    return (
        f'<svg class="chart" data-draw viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'role="img" aria-label="{_esc(aria)}"><title>{_esc(aria)}</title>'
    )


def _head(eyebrow: str, title: str, dash: str, *, level: str = "h2") -> str:
    return (
        f'<p class="eyebrow dash-{dash}">{_esc(eyebrow)}</p>'
        f'<{level} class="title">{_esc(title)}</{level}>'
    )


def _prose(blocks: Sequence[str], *, skip: set[str] | None = None, extra_cls: str = "") -> str:
    kept = [block for block in blocks if block and block not in (skip or set())]
    if not kept:
        return ""
    cls = "prose"
    if extra_cls:
        cls += f" {extra_cls}"
    elif len(kept) > 3:
        cls += " cols"
    body = "".join(f"<p>{_esc(block)}</p>" for block in kept)
    return f'<div class="{cls}">{body}</div>'


class _Ctx:
    """Lookups the section builders share."""

    def __init__(self, facts: WeeklyFacts, issue: WeeklyIssue) -> None:
        self.facts = facts
        self.teams = wm.team_index(facts)
        self.sections = section_headings_for_has_prior_week(facts.period.has_prior_week)
        self.prose = {
            section.heading: list(section.blocks)
            for section in issue.sections
            if section.heading in self.sections
        }

    def label(self, roster_id: str | None) -> str:
        return _team_label(self.teams.get(roster_id or ""), roster_id)

    def blocks(self, heading: str) -> list[str]:
        return self.prose.get(heading, [])


def _bye_impact_text(players: Sequence[WeeklyByeStarter | WeeklyByeImpact]) -> str:
    return ", ".join(
        f"{player.name} ({' '.join(part for part in (player.pos, player.nfl_team) if part)})"
        if (player.pos or player.nfl_team)
        else player.name
        for player in players
    )


def _card_expand() -> str:
    return (
        '<button type="button" class="card-expand" aria-expanded="false">'
        "<span>Details</span>"
        '<span class="card-expand-state" aria-hidden="true"></span></button>'
    )


def _game_side_detail(ctx: _Ctx, roster_id: str) -> str:
    team = ctx.teams.get(roster_id)
    game = team.this_week if team else None
    name = ctx.label(roster_id)
    if game is None:
        return f'<div class="game-side-detail"><p class="d-team">{_esc(name)}</p></div>'

    lines: list[str] = []
    if game.top_performers:
        performers = " · ".join(
            f"{_esc(player.name)} {_pts(player.points)}" for player in game.top_performers
        )
        lines.append(f'<p class="d-line"><span class="d-k">Top</span>{performers}</p>')
    if game.bench_regret is not None:
        bench = game.bench_regret
        lines.append(
            f'<p class="d-line"><span class="d-k">Bench regret</span>'
            f"{_esc(bench.name)} {_pts(bench.points)}</p>"
        )
    return (
        f'<div class="game-side-detail"><p class="d-team">{_esc(name)}</p>'
        f'{"".join(lines)}</div>'
    )


def _game_detail(ctx: _Ctx, win_roster_id: str, lose_roster_id: str) -> str:
    return (
        '<div class="game-detail">'
        f"{_game_side_detail(ctx, win_roster_id)}"
        f"{_game_side_detail(ctx, lose_roster_id)}"
        "</div>"
    )


def _nw_detail(card: WeeklyNextWeekCard, shared: set[str]) -> str:
    parts: list[str] = []
    own_stakes = wm.own_stakes(card, list(shared))
    if own_stakes:
        labels = " · ".join(_esc(_stake_label(tag)) for tag in own_stakes)
        parts.append(f'<p class="d-line"><span class="d-k">Stakes</span>{labels}</p>')
    if card.bye_impact:
        byes = _esc(_bye_impact_text(card.bye_impact))
        parts.append(f'<p class="d-line"><span class="d-k">On bye</span>{byes}</p>')
    if not parts:
        return ""
    return f'<div class="nw-detail">{"".join(parts)}</div>'


# --------------------------------------------------------------------------- #
# Masthead + notices
# --------------------------------------------------------------------------- #


def _masthead(ctx: _Ctx) -> str:
    facts = ctx.facts
    league = facts.league
    parts = [f"Week {facts.week}", f"Season {league.season}"]
    if facts.period.type and facts.period.type != "regular":
        parts.append(facts.period.type.capitalize())
    parts.append(f"{league.format.team_count} teams")
    weekline = " · ".join(parts)

    summary = facts.period.summary
    tiles: list[tuple[str, str, str]] = []
    if summary.high is not None:
        tiles.append(("High", _pts(summary.high.points), "t-good"))
    if summary.closest is not None:
        tiles.append(("Closest", _pts(summary.closest.margin), "t-notable"))
    if summary.games:
        tiles.append(("Blowouts", str(summary.blowout_count), "t-ink"))
        tiles.append(("Pts scored", _whole(summary.total_points), "t-emph"))
    tile_html = ""
    if tiles:
        items = "".join(
            f'<li class="tile {cls}"><span class="k">{_esc(key)}</span>'
            f'<span class="v">{_esc(value)}</span></li>'
            for key, value, cls in tiles
        )
        tile_html = f'<ul class="tiles" aria-label="The week at a glance">{items}</ul>'
    return (
        '<header class="masthead" data-reveal><div>'
        f'<p class="weekline">{_esc(weekline)}</p>'
        f'<h1 class="nameplate">{_esc(league.name)}</h1>'
        f"</div>{tile_html}</header>"
    )


def _notices(issue: WeeklyIssue, headings: tuple[str, ...]) -> str:
    """The UNVERIFIED dateline stamp and any section outside the current
    section set (a reissue's Correction), shown above the lead so they are
    never missed."""
    out: list[str] = []
    if issue.dateline.startswith("UNVERIFIED"):
        out.append(
            '<aside class="notice" role="note" data-reveal><h2>Unverified</h2>'
            f"<p>{_esc(issue.dateline)}</p></aside>"
        )
    for section in issue.sections:
        if section.heading in headings:
            continue
        body = "".join(f"<p>{_esc(block)}</p>" for block in section.blocks)
        out.append(f'<aside class="notice" role="note" data-reveal><h2>{_esc(section.heading)}</h2>{body}</aside>')
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# The lead — a hero per lead kind, type-only fallback
# --------------------------------------------------------------------------- #


def _hatch(x: float, y: float, w: float, h: float) -> str:
    """45-degree hatch lines clipped by hand to the rectangle (no ``<pattern>``:
    a pattern fill needs ``url(#…)``, and the page carries no ``url(`` beyond its
    embedded fonts)."""
    inset = 5.0
    y_top, y_bot = y + inset, y + h - inset
    rise = y_bot - y_top
    left, right = x + 3, x + w - 3
    lines = []
    u = left - rise
    while u < right:
        x1, y1 = u, y_bot
        x2, y2 = u + rise, y_top
        if x1 < left:
            y1 -= left - x1
            x1 = left
        if x2 > right:
            y2 += x2 - right
            x2 = right
        if x2 > x1:
            lines.append(
                f'<line x1="{_num(x1)}" y1="{_num(y1)}" x2="{_num(x2)}" y2="{_num(y2)}" '
                'stroke="var(--bad)" stroke-width="3" stroke-opacity=".55"/>'
            )
        u += 9
    return "".join(lines)


def _hero_lineup_loss(ctx: _Ctx, lead: LeadCandidate) -> tuple[str, str] | None:
    """Started-vs-bench bar plus the opponent's score as a tick."""
    if not lead.roster_ids:
        return None
    team = ctx.teams.get(lead.roster_ids[0])
    week = team.this_week if team else None
    if team is None or week is None:
        return None
    bench = week.points_left_on_bench
    opp = week.opponent_points
    if bench is None or bench <= 0 or opp is None or week.points <= 0:
        return None
    started = week.points
    name = _team_label(team)
    opp_name = ctx.label(week.opponent_roster_id)
    scale = _H_SPAN / max(started + bench, opp)
    ws, wb = started * scale, bench * scale
    y, h = 34, 60
    bx = _H_X0 + ws
    tick = _H_X0 + opp * scale
    started_label = f"STARTED {_pts(started)}"
    bench_label = f"STILL ON THE BENCH {_pts(bench)}"
    parts = [
        f'<rect x="{_H_X0}" y="{y}" width="{_num(ws)}" height="{h}" rx="14" fill="var(--emph)"/>',
        f'<rect x="{_num(bx)}" y="{y}" width="{_num(wb)}" height="{h}" rx="14" fill="var(--bad-wash)" '
        'stroke="var(--bad)" stroke-width="2"/>',
        _hatch(bx, y, wb, h),
    ]
    if ws >= 11 * len(started_label) + 30:
        parts.append(
            f'<text class="lbl-b" x="{_H_X0 + 20}" y="{y + 38}" fill="var(--on-emph)">{_esc(started_label)}</text>'
        )
    else:
        parts.append(f'<text class="lbl-b" x="{_H_X0}" y="{y - 12}" fill="var(--ink)">{_esc(started_label)}</text>')
    bench_x = bx + 18
    if bx - 4 <= tick <= bench_x + 4:
        bench_x = tick + 12  # keep the label clear of the opponent tick
    if bx + wb - bench_x >= 11 * len(bench_label) + 12:
        parts.append(
            f'<text class="lbl-b" x="{_num(bench_x)}" y="{y + 38}" fill="var(--bad-text)" '
            f'stroke="var(--card)" stroke-width="4" paint-order="stroke">{_esc(bench_label)}</text>'
        )
    else:
        parts.append(
            f'<text class="lbl-b" x="{_num(_H_X0 + _H_SPAN)}" y="{y - 12}" text-anchor="end" '
            f'fill="var(--bad-text)">{_esc(bench_label)}</text>'
        )
    result = "won by" if (week.margin or 0) < 0 else ("lost by" if (week.margin or 0) > 0 else "tied")
    anchor, tx = ("start", tick + 10) if tick <= 520 else ("end", tick - 10)
    parts.append(
        f'<line x1="{_num(tick)}" y1="18" x2="{_num(tick)}" y2="118" stroke="var(--ink)" '
        'stroke-width="2.5" stroke-dasharray="5 4"/>'
    )
    parts.append(
        f'<text class="lbl" x="{_num(tx)}" y="140" text-anchor="{anchor}">'
        f"{_esc(f'{opp_name} scored {_pts(opp)}')}</text>"
    )
    tail = f"and {result}" if result == "tied" else f"and {result} {_pts(abs(week.margin or 0))}"
    parts.append(f'<text class="lbl-2" x="{_num(tx)}" y="160" text-anchor="{anchor}">{_esc(tail)}</text>')
    aria = (
        f"{name} started {_pts(started)} and left {_pts(bench)} on the bench; "
        f"{opp_name} scored {_pts(opp)}"
    )
    svg = _svg_open(_H_W, 176, aria) + "".join(parts) + "</svg>"

    sub = ""
    if week.bench_regret is not None:
        sub = f"Including {week.bench_regret.name}, {_pts(week.bench_regret.points)} on the bench alone."
    side = _keynum("Left on the bench", _pts(bench), sub, "kn-bad")
    meta = []
    if week.coaching_efficiency is not None and week.coaching_efficiency.pct is not None:
        meta.append(f"Lineup efficiency {_pct(week.coaching_efficiency.pct)}")
    rec = team.season.record
    meta.append(_record(rec.w, rec.l, rec.t))
    side += _teamcard(name, " · ".join(meta))
    return svg, side


def _two_bar_hero(
    ctx: _Ctx, lead: LeadCandidate, *, zoom: bool
) -> tuple[str, str, float] | None:
    """Two bars (winner, loser). ``zoom`` starts the axis near the lower score
    and highlights the gap (closest game); otherwise the axis is zero-based and
    a bracket marks the margin (biggest blowout)."""
    matchup = _find_matchup(ctx.facts, lead.roster_ids)
    if matchup is None:
        return None
    win_id, win_pts, lose_id, lose_pts = _winner_loser(matchup)
    if win_pts <= 0:
        return None
    win_name, lose_name = ctx.label(win_id), ctx.label(lose_id)
    margin = win_pts - lose_pts
    floor = 0.0
    if zoom:
        floor = math.floor(lose_pts / 50) * 50
        if lose_pts - floor < 10:
            floor = max(0.0, floor - 50)
    span = 560 if zoom else _H_SPAN
    scale = span / (win_pts - floor) if win_pts > floor else 0
    w_win = (win_pts - floor) * scale
    w_lose = max(0.0, (lose_pts - floor) * scale)
    y1, y2, h = 14, 68, 38
    parts = [
        f'<rect x="{_H_X0}" y="{y1}" width="{_num(w_win)}" height="{h}" rx="10" fill="var(--emph)"/>',
        f'<rect x="{_H_X0}" y="{y2}" width="{_num(w_lose)}" height="{h}" rx="10" fill="var(--barlo)"/>',
    ]
    for bar_y, bar_w, text, fill in (
        (y1, w_win, f"{win_name} {_pts(win_pts)}", "var(--on-emph)"),
        (y2, w_lose, f"{lose_name} {_pts(lose_pts)}", "var(--ink)"),
    ):
        fits = bar_w >= 9.5 * len(text) + 28
        x = _H_X0 + 14 if fits else _H_X0 + bar_w + 10
        colour = fill if fits else "var(--ink)"
        parts.append(f'<text class="lbl" x="{_num(x)}" y="{bar_y + 25}" style="fill:{colour}">{_esc(text)}</text>')
    x_lose, x_win = _H_X0 + w_lose, _H_X0 + w_win
    if zoom:
        # behind the bars, so the highlighted gap never muddies a bar's fill
        parts.insert(
            0,
            f'<rect x="{_num(x_lose)}" y="8" width="{_num(max(x_win - x_lose, 2))}" height="104" rx="6" '
            'fill="var(--notable)" fill-opacity=".55"/>',
        )
        parts.append(f'<text class="lbl-big" x="{_num(x_win + 12)}" y="70">{_esc(_pts(margin))}</text>')
        parts.append(
            f'<text class="lbl-2" x="{_H_X0}" y="140">'
            f"{_esc(f'axis starts at {floor:g} to show the gap')}</text>"
        )
        aria = f"{win_name} {_pts(win_pts)} edged {lose_name} {_pts(lose_pts)} by {_pts(margin)}"
    else:
        parts.append(f'<path d="M {_num(x_lose)} 122 H {_num(x_win)}" stroke="var(--ink)" stroke-width="3"/>')
        parts.append(
            f'<path d="M {_num(x_lose)} 116 V 128 M {_num(x_win)} 116 V 128" stroke="var(--ink)" stroke-width="3"/>'
        )
        parts.append(
            f'<text class="lbl" x="{_num((x_lose + x_win) / 2)}" y="148" text-anchor="middle" '
            f'font-weight="500">{_esc("margin " + _pts(margin))}</text>'
        )
        aria = f"{win_name} {_pts(win_pts)} beat {lose_name} {_pts(lose_pts)} by {_pts(margin)}"
    svg = _svg_open(_H_W, 156, aria) + "".join(parts) + "</svg>"
    return svg, win_id, margin


def _hero_blowout(ctx: _Ctx, lead: LeadCandidate) -> tuple[str, str] | None:
    drawn = _two_bar_hero(ctx, lead, zoom=False)
    if drawn is None:
        return None
    svg, win_id, margin = drawn
    side = _keynum("Winning margin", _pts(margin), "", "kn-emph") + _teamcard_for(ctx, win_id)
    return svg, side


def _hero_closest(ctx: _Ctx, lead: LeadCandidate) -> tuple[str, str] | None:
    drawn = _two_bar_hero(ctx, lead, zoom=True)
    if drawn is None:
        return None
    svg, win_id, margin = drawn
    side = _keynum("Decided by", _pts(margin), "", "kn-notable") + _teamcard_for(ctx, win_id)
    return svg, side


def _hero_week_high(ctx: _Ctx, lead: LeadCandidate) -> tuple[str, str] | None:
    """Every team's score as a dot strip, the high score enlarged, and the league
    average as a tick."""
    scores = sorted(
        ((team.this_week.points, team.roster_id) for team in ctx.facts.teams if team.this_week is not None),
    )
    high = ctx.facts.period.summary.high
    if len(scores) < 2 or high is None:
        return None
    avg = ctx.facts.period.summary.avg_team_score
    lo, hi = scores[0][0], scores[-1][0]
    if hi <= lo:
        return None
    x0, x1, cy = 40.0, 760.0, 70
    scale = (x1 - x0) / (hi - lo)

    def at(value: float) -> float:
        return x0 + (value - lo) * scale

    parts = [f'<line x1="{_num(x0)}" y1="{cy}" x2="{_num(x1)}" y2="{cy}" stroke="var(--line)" stroke-width="3"/>']
    for points, roster_id in scores:
        if roster_id == high.roster_id:
            continue
        parts.append(
            f'<circle cx="{_num(at(points))}" cy="{cy}" r="9" fill="var(--ink-3)" fill-opacity=".7">'
            f"<title>{_esc(f'{ctx.label(roster_id)} {_pts(points)}')}</title></circle>"
        )
    if lo <= avg <= hi:
        ax = at(avg)
        parts.append(
            f'<line x1="{_num(ax)}" y1="34" x2="{_num(ax)}" y2="106" stroke="var(--ink)" '
            'stroke-dasharray="4 3" stroke-width="2"/>'
        )
        parts.append(
            f'<text class="lbl" x="{_num(ax)}" y="126" text-anchor="middle">'
            f"{_esc(f'league avg {_pts(avg)}')}</text>"
        )
    hx = at(high.points)
    high_name = ctx.label(high.roster_id)
    parts.append(f'<circle cx="{_num(hx)}" cy="{cy}" r="15" fill="var(--emph)"/>')
    anchor = "end" if hx > x1 - 150 else "middle"
    parts.append(
        f'<text class="lbl" x="{_num(min(hx, x1))}" y="36" text-anchor="{anchor}" font-weight="500">'
        f"{_esc(f'{high_name} {_pts(high.points)}')}</text>"
    )
    aria = (
        f"Every team's week {ctx.facts.week} score; {high_name} posted the high, "
        f"{_pts(high.points)}; the league averaged {_pts(avg)}"
    )
    svg = _svg_open(_H_W, 140, aria) + "".join(parts) + "</svg>"
    side = _keynum("Week high", _pts(high.points), "", "kn-emph") + _teamcard_for(ctx, high.roster_id)
    return svg, side


def _keynum(key: str, value: str, sub: str, cls: str) -> str:
    sub_html = f'<div class="s">{_esc(sub)}</div>' if sub else ""
    return (
        f'<div class="keynum {cls}"><div class="k">{_esc(key)}</div>'
        f'<div class="v">{_esc(value)}</div>{sub_html}</div>'
    )


def _teamcard(name: str, meta: str) -> str:
    return (
        f'<div class="card teamcard">{_mg(name, "mg-xl")}<div>'
        f'<div class="n">{_esc(name)}</div><div class="m">{_esc(meta)}</div></div></div>'
    )


def _teamcard_for(ctx: _Ctx, roster_id: str) -> str:
    team = ctx.teams.get(roster_id)
    name = ctx.label(roster_id)
    if team is None:
        return _teamcard(name, "")
    rec = team.season.record
    meta = [_record(rec.w, rec.l, rec.t)]
    if team.this_week is not None:
        meta.append(f"{_pts(team.this_week.points)} this week")
    return _teamcard(name, " · ".join(meta))


_HEROES = {
    "lineup_loss": _hero_lineup_loss,
    "biggest_blowout": _hero_blowout,
    "closest_game": _hero_closest,
    "week_high_score": _hero_week_high,
}


def _lead_section(ctx: _Ctx) -> str:
    blocks = ctx.blocks(heading_for("lead"))
    lead = wm.choose_lead(ctx.facts)
    awards = _awards(ctx)
    if lead is None:
        if not blocks:
            return f'<section aria-label="The lead" data-reveal>{awards}</section>' if awards else awards
        return (
            '<section class="lead-wrap" aria-label="The lead" data-reveal><div class="card lead-main">'
            f'{_head("The lead", blocks[0], "bad")}{_prose(blocks[1:], extra_cls="lead-prose")}'
            f"</div>{awards}</section>"
        )
    hook = lead.hook or ""
    prose = _prose(blocks, skip={hook}, extra_cls="lead-prose")
    builder = _HEROES.get(lead.kind)
    drawn = builder(ctx, lead) if builder is not None else None
    if drawn is None:
        key = key_number(ctx.facts, lead)
        key_html = f'<p class="key">{_esc(key)}</p>' if key else ""
        return (
            '<section class="lead-wrap" aria-label="The lead" data-reveal><div class="card lead-main">'
            '<p class="eyebrow dash-bad">The lead</p>'
            f'<div class="lead-type"><h2 class="hook">{_esc(hook)}</h2>{key_html}</div>'
            f"{prose}</div>{awards}</section>"
        )
    svg, side = drawn
    return (
        '<section class="lead-wrap" aria-label="The lead" data-reveal><div class="lead">'
        f'<div class="card lead-main">{_head("The lead", hook, "bad")}{prose}{svg}</div>'
        f'<div class="lead-side">{side}</div></div>{awards}</section>'
    )


# --------------------------------------------------------------------------- #
# Awards row (all from ``leaders``)
# --------------------------------------------------------------------------- #


_AWARD_CLS = {"coach": "aw-good", "player": "aw-emph", "bust": "aw-bad", "goose": "aw-notable"}


def _award(key: str, value: str, name: str, sub: str, cls: str) -> str:
    return (
        f'<div class="award {cls}"><div class="k">{_esc(key)}</div><div>'
        f'<div class="v">{_esc(value)}</div><div class="n">{_esc(name)}</div>'
        f'<div class="s">{_esc(sub)}</div></div></div>'
    )


def _awards(ctx: _Ctx) -> str:
    cards = [
        _award(award.key, award.value, award.name, award.sub, _AWARD_CLS[award.kind])
        for award in wm.awards(ctx.facts)
    ]
    if not cards:
        return ""
    return f'<div class="awards">{"".join(cards)}</div>'


# --------------------------------------------------------------------------- #
# Around the league — one card per game
# --------------------------------------------------------------------------- #


_GAME_TAGS = {
    "week_high": '<span class="chip chip-good">Week high</span>',
    "closest": '<span class="chip chip-notable">Nail-biter</span>',
    "blowout": '<span class="chip chip-ink">Blowout</span>',
}


def _game_card(ctx: _Ctx, matchup: WeeklyMatchup) -> str:
    win_id, win_pts, lose_id, lose_pts = _winner_loser(matchup)
    tag = _GAME_TAGS.get(wm.game_tag(ctx.facts, matchup) or "", "")
    tied = matchup.winner_roster_id is None
    by = "Tied" if tied else f"Won by {_pts(abs(matchup.margin))}"
    win_name, lose_name = ctx.label(win_id), ctx.label(lose_id)
    win_cls = "side" if tied else "side win"
    expand = _card_expand()
    detail = _game_detail(ctx, win_id, lose_id)
    return (
        f'<article class="card game"><div class="game-top"><span class="by">{_esc(by)}</span>{tag}{expand}</div>'
        f'<div class="{win_cls}">{_mg(win_name, "" if tied else "mg-l")}<span class="tn">{_esc(win_name)}</span>'
        f'<span class="sc">{_esc(_pts(win_pts))}</span></div>'
        f'<div class="side">{_mg(lose_name)}<span class="tn">{_esc(lose_name)}</span>'
        f'<span class="sc">{_esc(_pts(lose_pts))}</span></div>{detail}</article>'
    )


def _around_section(ctx: _Ctx) -> str:
    games = ctx.facts.matchups.this_week
    blocks = ctx.blocks(heading_for("around_league"))
    if not games:
        return ""
    cards = "".join(_game_card(ctx, matchup) for matchup in games)
    return (
        f'<section aria-label="Around the league" data-reveal>{_head("Around the league", "Results", "emph")}'
        f'<div class="grid3">{cards}</div>{_prose(blocks)}</section>'
    )


# --------------------------------------------------------------------------- #
# Standings
# --------------------------------------------------------------------------- #


def _standings_section(ctx: _Ctx) -> str:
    facts = ctx.facts
    cold_start = not facts.period.has_prior_week
    order = [team.roster_id for team in wm.standings_order(facts)]
    if not order:
        return ""
    if cold_start:
        picture = None
        playoff = None
        title = "Standings by points"
    else:
        picture = facts.standings.playoff_picture
        playoff = facts.league.format.playoff
        if picture is not None and playoff is not None:
            title = f"{_spell(playoff.bracket_teams).capitalize()} make it"
            if playoff.byes:
                title += f", {_spell(playoff.byes)} get {'a bye' if playoff.byes == 1 else 'byes'}"
        else:
            title = "The table"
    divisions = [division.id for division in facts.league.format.divisions]
    dot_of = {div_id: f"dot-d{index % 3 + 1}" for index, div_id in enumerate(divisions)} if len(divisions) > 1 else {}
    max_pf = max((ctx.teams[rid].season.points_for for rid in order), default=0.0)
    byes = set(picture.byes) if picture else set()
    bubble = set(picture.bubble) if picture else set()
    cut = picture.cut_line_after_rank if picture else None

    ap_rows = wm.all_play_rows(facts)
    all_play_by_team = {team.roster_id: all_play for all_play, team in ap_rows}
    has_all_play = not cold_start and bool(all_play_by_team)

    toggle_html = ""
    if has_all_play:
        toggle_html = (
            '<div class="standings-toggle" role="group" aria-label="Standings view">'
            '<button type="button" class="toggle-btn is-active" data-view="actual" '
            'aria-pressed="true">Actual</button>'
            '<button type="button" class="toggle-btn" data-view="allplay" '
            'aria-pressed="false">All-play</button>'
            "</div>"
        )

    rows: list[str] = []
    for index, roster_id in enumerate(order, start=1):
        team = ctx.teams[roster_id]
        season = team.season
        name = _team_label(team)
        width = (season.points_for / max_pf * 100) if max_pf > 0 else 0.0
        actual_rec = _record(season.record.w, season.record.l, season.record.t)
        all_play = all_play_by_team.get(roster_id)
        if all_play is not None:
            ap_rec = _record(all_play.w, all_play.l, all_play.t)
            ap_width = (all_play.pct or 0.0) * 100
            rec_html = (
                '<span class="rec rec-swap">'
                f'<span class="rec-act" aria-hidden="false">{_esc(actual_rec)}</span>'
                f'<span class="rec-ap" aria-hidden="true">{_esc(ap_rec)}</span></span>'
            )
            bar_html = (
                '<span class="bar bar-swap" aria-hidden="true">'
                f'<i class="fill-actual" style="width:{width:.1f}%"></i>'
                f'<i class="fill-allplay" style="width:{ap_width:.1f}%"></i>'
                "</span>"
            )
        else:
            rec_html = f'<span class="rec">{_esc(actual_rec)}</span>'
            bar_html = (
                '<span class="bar" aria-hidden="true">'
                f'<i style="width:{width:.1f}%"></i></span>'
            )
        chip = ""
        if roster_id in byes:
            chip = '<span class="chip chip-emph">Bye</span>'
        elif roster_id in bubble:
            chip = '<span class="chip chip-notable">Bubble</span>'
        dot = ""
        if team.division_id in dot_of:
            dot = f'<span class="dot {dot_of[team.division_id]}" aria-hidden="true"></span>'
        below = cut is not None and index > cut
        rows.append(
            f'<div class="st-row{" below" if below else ""}">'
            f'<span class="rk">{index if cold_start else season.rank}</span>{_mg(name)}'
            f'<span class="tn">{dot}{_esc(name)}</span>'
            f"{rec_html}"
            f"{bar_html}"
            f'<span class="pf">{_esc(_whole(season.points_for))}</span>'
            f'<span class="tag">{chip}</span></div>'
        )
        if cut is not None and index == cut and index < len(order):
            rows.append('<div class="cutline" role="separator">Playoff line</div>')

    legend_bits: list[str] = []
    if dot_of:
        names = {division.id: division.name for division in facts.league.format.divisions}
        legend_bits.append(
            "".join(
                f'<span><span class="dot {cls}" aria-hidden="true"></span>'
                f"{_esc(names.get(div_id) or f'Division {div_id}')}</span>"
                for div_id, cls in dot_of.items()
            )
        )
    if has_all_play:
        legend_bits.append(
            '<span class="legend-swap"><span class="legend-act" aria-hidden="false">Bar: points for</span>'
            '<span class="legend-ap" aria-hidden="true">Bar: all-play win pct</span></span>'
        )
    elif dot_of:
        legend_bits.append("<span>Bar: points for</span>")
    legend = f'<p class="legend">{"".join(legend_bits)}</p>' if legend_bits else ""

    note = ""
    if picture is not None and picture.seeding_unconfirmed:
        note = '<p class="seeding-note">Seeding unconfirmed — derived from the standings.</p>'
    prose_heading = heading_for("standings", cold_start=cold_start)
    return (
        '<section class="card table-card" data-standings aria-label="Standings" data-reveal>'
        f'{_head("Standings", title, "good")}'
        f'{toggle_html}<div class="standings">{"".join(rows)}</div>{legend}{note}'
        f"{_prose(ctx.blocks(prose_heading))}</section>"
    )


# --------------------------------------------------------------------------- #
# Power rankings — by published rank, nudge chip + cited reason + bump chart
# --------------------------------------------------------------------------- #


def _power_bump_chart(ctx: _Ctx, rows: Sequence[wm.PowerRow]) -> str:
    """The Story 5B.3 SVG bump chart, or ``""`` under 2 usable weeks."""
    series: list[tuple[wm.PowerRow, list[wm.PowerHistoryPoint]]] = []
    for row in rows:
        points = wm.power_history(row.team, row)
        if len(points) >= 2:
            series.append((row, points))

    if not series:
        return ""

    weeks = sorted({point.week for _, points in series for point in points})
    max_rank = max(
        [point.model for _, points in series for point in points]
        + [
            point.published
            for _, points in series
            for point in points
            if point.published is not None
        ],
        default=1,
    )

    x0, x1 = 60.0, 720.0
    top = 30.0
    row_h = 22.0
    plot_bottom = top + (max_rank - 1) * row_h
    height = int(plot_bottom + 62)
    step = (x1 - x0) / (len(weeks) - 1) if len(weeks) > 1 else 0.0

    def x(week: int) -> float:
        return x0 + weeks.index(week) * step

    def y(rank: int) -> float:
        return top + (rank - 1) * row_h

    parts: list[str] = []

    for rank in range(1, max_rank + 1):
        parts.append(
            f'<line class="bump-grid" x1="{_num(x0)}" y1="{_num(y(rank))}" '
            f'x2="{_num(x1)}" y2="{_num(y(rank))}"/>'
        )

    for rank in sorted({1, max_rank}):
        parts.append(
            f'<text class="bump-rank" x="{_num(x0 - 10)}" y="{_num(y(rank) + 4)}" '
            f'text-anchor="end">{rank}</text>'
        )

    for week in weeks:
        parts.append(
            f'<text class="bump-week" x="{_num(x(week))}" y="{_num(plot_bottom + 28)}" '
            f'text-anchor="middle">{_esc(str(week))}</text>'
        )

    def _pub_segment(segment: Sequence[wm.PowerHistoryPoint]) -> str:
        """A published-rank polyline (plus its wide invisible hit target)
        for one unbroken run of weeks -- callers never span a gap week
        across two segments, so the line breaks rather than interpolates."""
        seg_xy = [(x(p.week), y(p.published)) for p in segment if p.published is not None]
        pub_pts = " ".join(f"{_num(px)},{_num(py)}" for px, py in seg_xy)
        pub_len = _polyline_length(seg_xy)
        return (
            f'<polyline class="bump-hit" points="{pub_pts}"/>'
            f'<polyline class="bump-pub-line" style="--len:{_num(pub_len)}" points="{pub_pts}"/>'
        )

    for row, points in series:
        team = row.team
        team_id = _esc(team.roster_id)
        name = _team_label(team)

        parts.append(f'<g class="bump-team" data-team="{team_id}">')

        # Model line: a generous invisible hit-stroke layered under the thin
        # visible one (interaction-decisions.md), and a draw-in dash length
        # computed from this line's own traced path (never a shared constant).
        model_xy = [(x(point.week), y(point.model)) for point in points]
        model_pts = " ".join(f"{_num(px)},{_num(py)}" for px, py in model_xy)
        model_len = _polyline_length(model_xy)
        parts.append(
            f'<polyline class="bump-hit" points="{model_pts}"/>'
            f'<polyline class="bump-model" style="--len:{_num(model_len)}" points="{model_pts}"/>'
        )

        # Published-rank line: broken into one polyline per unbroken run of
        # weeks so a held/skipped week never interpolates across the gap.
        segment: list[wm.PowerHistoryPoint] = []
        for point in points:
            if point.published is not None:
                segment.append(point)
            else:
                if len(segment) >= 2:
                    parts.append(_pub_segment(segment))
                segment = []
        if len(segment) >= 2:
            parts.append(_pub_segment(segment))

        for point in points:
            has_pub = point.published is not None
            publish = point.published if has_pub else ""
            label = f"Week {point.week}: model {point.model}"
            if has_pub:
                label += f", published {point.published}"
            cx, cy = _num(x(point.week)), _num(y(point.model))
            parts.append(
                f'<circle class="bump-hit" cx="{cx}" cy="{cy}" r="10"/>'
                f'<circle class="bump-point bump-model-dot" data-team="{team_id}" '
                f'data-week="{point.week}" data-rank="{point.model}" data-pub="{publish}" '
                f'cx="{cx}" cy="{cy}" r="4">'
                f"<title>{_esc(label)}</title></circle>"
            )

        for point in points:
            if point.published is None or point.published == point.model:
                continue
            title = (
                f"{name} week {point.week}: published {point.published} "
                f"(model {point.model})"
            )
            dcx, dcy = x(point.week), y(point.published)
            parts.append(
                f'<circle class="bump-hit" cx="{_num(dcx)}" cy="{_num(dcy)}" r="10"/>'
                f'<path class="bump-point bump-pub" data-team="{team_id}" '
                f'data-week="{point.week}" data-rank="{point.published}" '
                f'data-model="{point.model}" d="{_diamond(dcx, dcy)}">'
                f"<title>{_esc(title)}</title></path>"
            )

        parts.append("</g>")

    detail = (
        '<div class="bump-detail" data-bump-detail aria-live="polite">'
        '<p class="bd-default">Hover or select a team to inspect its ranks.</p>'
        "</div>"
    )

    buttons: list[str] = []
    for row, _points in series:
        name = _team_label(row.team)
        buttons.append(
            f'<button type="button" class="hit pw-hit" data-team="{_esc(row.team.roster_id)}" '
            f'data-name="{_esc(name)}" aria-pressed="false">'
            f'<span class="hit-mg mg" aria-hidden="true">{_esc(_monogram(name))}</span>'
            f'<span class="hit-tn">{_esc(name)}</span>'
            '<span class="hit-state" aria-hidden="true"></span></button>'
        )
    hit_list = f'<div class="pw-hits" role="list">{"".join(buttons)}</div>'

    reason_html = ""
    reasons: list[str] = []
    for row, points in series:
        point = points[-1]
        if (
            point.reason
            and point.published is not None
            and point.published != point.model
        ):
            name = _team_label(row.team)
            reasons.append(f"<p><strong>{_esc(name)}</strong> {_esc(point.reason)}</p>")

    if reasons:
        reason_html = (
            '<div class="bump-callout" data-bump-callout>'
            '<p class="bd-title">This week&rsquo;s overrules</p>'
            f'{"".join(reasons)}</div>'
        )

    aria = "Power rankings bump chart: model rank trajectory and published-rank overrules"
    svg = _svg_open(760, height, aria) + "".join(parts) + "</svg>"

    return (
        '<div class="bump" data-reveal data-bump-chart>'
        f'<div class="bump-layout"><div class="bump-main">{svg}</div>'
        f'<div class="bump-side">{detail}{reason_html}{hit_list}</div>'
        "</div></div>"
    )


def _power_section(ctx: _Ctx) -> str:
    rows = wm.power_rows(ctx.facts)
    if not rows:
        return ""
    items: list[str] = []
    for published, model, team, reason in rows:
        power = team.season.power
        name = _team_label(team)
        rec = team.season.record
        score = power.model_score
        width = max(0.0, min(1.0, score)) * 100 if score is not None else 0.0
        delta = power.week_delta
        if delta is None or delta == 0:
            wk = '<span aria-label="no change">–</span>' if delta == 0 else ""
        elif delta > 0:
            wk = f'<span class="up" aria-label="up {delta}">▲{delta}</span>'
        else:
            wk = f'<span class="down" aria-label="down {-delta}">▼{-delta}</span>'
        rank_text = str(published) if published is not None else "–"
        line = (
            f'<div class="pw-row"><span class="rk">{_esc(rank_text)}</span>'
            f'<span class="tn">{_esc(name)}</span>'
            f'<span class="rec">{_esc(_record(rec.w, rec.l, rec.t))}</span>'
            f'<span class="avg">{_esc(f"{team.season.avg_for:.1f}")}</span>'
            f'<span class="bar thin" aria-hidden="true"><i style="width:{width:.1f}%"></i></span>'
            f'<span class="wk">{wk}</span></div>'
        )
        nudge = ""
        if published is not None and model is not None and published != model:
            if published < model:
                chip = f'<span class="chip chip-up">▲ Nudged up · model #{model}</span>'
            else:
                chip = f'<span class="chip chip-down">▼ Nudged down · model #{model}</span>'
            why = f'<span class="why">{_esc(reason)}</span>' if reason else ""
            nudge = f'<p class="nudge">{chip}{why}</p>'
        items.append(f'<div class="pw-item">{line}{nudge}</div>')
    bump = _power_bump_chart(ctx, rows)
    head = (
        '<div class="pw-head" aria-hidden="true"><span class="r">Rk</span><span>Team</span><span>W-L</span>'
        '<span class="r">Avg PF</span><span class="ms">Model score</span><span class="r">Wk</span></div>'
    )
    return (
        f'<section class="card table-card" aria-label="Power rankings" data-reveal>'
        f'{_head("Power rankings", "Where the model and the desk land", "emph")}'
        f"{bump}"
        f'{head}<div class="power">{"".join(items)}</div>'
        f"{_prose(ctx.blocks(heading_for("power")))}</section>"
    )


# --------------------------------------------------------------------------- #
# Luck index — diverging bar of ``season.luck``, most lucky first
# --------------------------------------------------------------------------- #


def _luck_preview(team: WeeklyTeam, value: float) -> str:
    """The deterministic click/tap/focus preview sentence, matching the
    template narrator's ``_luck_line`` voice without an LLM."""
    record_line = _record(
        team.season.record.w, team.season.record.l, team.season.record.t
    )
    line = f"{_team_label(team)} — {record_line}"
    if team.season.expected_wins is not None:
        line += f", {team.season.expected_wins} wins earned"
    return f"{line}, luck {_signed(value)}."


def _luck_section(ctx: _Ctx) -> str:
    rows = wm.luck_rows(ctx.facts)
    if not rows:
        return ""
    peak = max(abs(luck or 0.0) for luck, _team in rows) or 1.0
    scale = _L_HALF / peak
    height = _L_TOP + len(rows) * _L_ROW
    parts = [
        f'<line x1="{_L_AXIS}" y1="{_L_TOP - 6}" x2="{_L_AXIS}" y2="{height}" '
        'stroke="var(--ink-3)" stroke-width="1.5"/>',
        f'<text class="lbl-2" x="{_L_AXIS - 10}" y="14" text-anchor="end">◀ ROBBED</text>',
        f'<text class="lbl-2" x="{_L_AXIS + 10}" y="14">BLESSED ▶</text>',
    ]
    for index, (luck, team) in enumerate(rows):
        value = float(luck or 0.0)
        name = _team_label(team)
        short = name if len(name) <= _L_NAME_MAX else name[: _L_NAME_MAX - 1].rstrip() + "…"
        row_top = _L_TOP + index * _L_ROW
        top = row_top + (_L_ROW - _L_BAR) / 2
        base = top + _L_BAR / 2 + 5
        length = abs(value) * scale
        text = _signed(value)
        preview = _esc(_luck_preview(team, value))
        origin = "left center" if value >= 0 else "right center"
        cells = [
            f'<rect class="luck-hit" x="0" y="{_num(row_top)}" width="{_L_W}" '
            f'height="{_L_ROW}" fill="transparent"/>',
            f"<title>{_esc(f'{name} {text}')}</title>",
            f'<text class="name" x="0" y="{_num(base)}">{_esc(short)}</text>',
        ]
        if value > 0:
            cells.append(
                f'<rect class="luck-bar" data-luck="{_esc(repr(value))}" '
                f'x="{_L_AXIS}" y="{_num(top)}" width="{_num(max(length, 2))}" '
                f'height="{_L_BAR}" rx="8" fill="var(--good)" '
                f'style="--luck-len:1; --luck-origin:{origin}"/>'
            )
            cells.append(
                f'<text class="lbl" x="{_num(_L_AXIS + length + 10)}" '
                f'y="{_num(base)}">{_esc(text)}</text>'
            )
        elif value < 0:
            cells.append(
                f'<rect class="luck-bar" data-luck="{_esc(repr(value))}" '
                f'x="{_num(_L_AXIS - length)}" y="{_num(top)}" '
                f'width="{_num(max(length, 2))}" height="{_L_BAR}" rx="8" '
                f'fill="var(--bad)" style="--luck-len:1; --luck-origin:{origin}"/>'
            )
            cells.append(
                f'<text class="lbl" x="{_num(_L_AXIS - length - 10)}" '
                f'y="{_num(base)}" text-anchor="end">{_esc(text)}</text>'
            )
        else:
            cells.append(
                f'<rect class="luck-bar" data-luck="{_esc(repr(value))}" '
                f'x="{_L_AXIS - 1}" y="{_num(top)}" width="2" height="{_L_BAR}" '
                f'fill="var(--ink-3)" style="--luck-len:1; --luck-origin:{origin}"/>'
            )
            cells.append(
                f'<text class="lbl" x="{_L_AXIS + 10}" y="{_num(base)}">{_esc(text)}</text>'
            )
        cells_html = "".join(cells)
        parts.append(
            f'<g class="luck-row" data-luck-preview="{preview}">'
            f"{cells_html}</g>"
        )
    aria = "Luck index: actual wins minus expected wins, most lucky to least"
    svg = _svg_open(_L_W, height, aria) + "".join(parts) + "</svg>"
    return (
        '<section class="card luck-card" data-luck-section aria-label="The luck index" data-reveal>'
        f'{_head("The luck index", "Who the schedule favoured", "notable")}{svg}'
        '<p class="luck-preview" data-luck-preview hidden></p>'
        f"{_prose(ctx.blocks(heading_for("luck")))}</section>"
    )


# --------------------------------------------------------------------------- #
# Next week — shared stakes once, differing stakes as chips
# --------------------------------------------------------------------------- #


def _nw_side(ctx: _Ctx, roster_id: str, record: str, rank: int | None) -> str:
    name = ctx.label(roster_id)
    meta = record if rank is None else f"{record} · #{rank}"
    return (
        f'<div class="nw-side">{_mg(name, "mg-xxl")}<div class="tn">{_esc(name)}</div>'
        f'<div class="meta">{_esc(meta)}</div></div>'
    )


def _next_week_card(ctx: _Ctx, card: WeeklyNextWeekCard, shared: set[str]) -> str:
    chips: list[str] = []
    if card.game_of_week:
        chips.append('<span class="chip chip-emph">Game of the week</span>')
    chips.extend(
        f'<span class="chip chip-plain">{_esc(_stake_label(tag))}</span>'
        for tag in wm.own_stakes(card, list(shared))
    )
    chip_row = f'<div class="nw-chips">{"".join(chips)}</div>' if chips else ""
    bye = ""
    detail = _nw_detail(card, shared)
    expand = _card_expand() if detail else ""
    if card.bye_impact:
        bye = f'<p class="byeline"><b>On bye</b> {_esc(_bye_impact_text(card.bye_impact))}</p>'
    cls = "card nw gotw" if card.game_of_week else "card nw"
    return (
        f'<article class="{cls}">{chip_row}{expand}<div class="nw-vs">'
        f"{_nw_side(ctx, card.a_roster_id, card.a_record, card.a_power_rank)}"
        '<span class="vs">vs</span>'
        f"{_nw_side(ctx, card.b_roster_id, card.b_record, card.b_power_rank)}"
        f"</div>{bye}{detail}</article>"
    )


def _next_week_section(ctx: _Ctx) -> str:
    cards = ctx.facts.matchups.next_week
    if not cards:
        return ""
    shared = wm.shared_stakes(cards)
    shared_line = ""
    if shared:
        chips = "".join(f'<span class="chip chip-plain">{_esc(_stake_label(tag))}</span>' for tag in shared)
        shared_line = f'<p class="stakes-shared">On the line in every game: {chips}</p>'
    byes_line = ""
    if ctx.facts.period.nfl_byes_next_week:
        byes_line = (
            f'<p class="byes-next">NFL teams on bye: {_esc(", ".join(sorted(ctx.facts.period.nfl_byes_next_week)))}</p>'
        )
    body = "".join(_next_week_card(ctx, card, set(shared)) for card in cards)
    return (
        f'<section aria-label="Next week" data-reveal>{_head("Next week", "Next week" + chr(8217) + "s games", "bad")}'
        f'{shared_line}{byes_line}<div class="grid3">{body}</div>'
        f"{_prose(ctx.blocks(heading_for("next_week")))}</section>"
    )


# --------------------------------------------------------------------------- #
# Transaction desk
# --------------------------------------------------------------------------- #


def _player_li(chip: str, name: str, pos: str | None) -> str:
    pos_html = f' <span class="pos">{_esc(pos)}</span>' if pos else ""
    return f"<li>{chip} {_esc(name)}{pos_html}</li>"


def _move_card(ctx: _Ctx, move: WeeklyMove) -> str:
    kind = {"waiver": "Waiver", "free_agent": "Free agent"}.get(move.type, move.type.replace("_", " ").capitalize())
    faab = f'<span class="chip chip-plain">FAAB ${move.faab}</span>' if move.faab is not None else ""
    team = ctx.label(move.roster_ids[0]) if move.roster_ids else ""
    items = [_player_li('<span class="chip chip-up">Add</span>', p.name, p.pos) for p in move.adds]
    items += [_player_li('<span class="chip chip-down">Drop</span>', p.name, p.pos) for p in move.drops]
    return (
        f'<article class="card tx"><div class="tx-top"><span class="lab">{_esc(kind)} · this week</span>{faab}</div>'
        f'<div class="tn">{_esc(team)}</div><ul>{"".join(items)}</ul></article>'
    )


def _trade_card(ctx: _Ctx, trade: WeeklyTrade) -> str:
    ago = ctx.facts.week - trade.week
    when = "this week" if ago <= 0 else ("1 week ago" if ago == 1 else f"{ago} weeks ago")
    sides: list[str] = []
    for side in trade.sides:
        items = [_player_li("", p.name, p.pos) for p in side.players]
        items += [f"<li class=\"mono\">{_esc(f'{pick.season} round {pick.round} pick')}</li>" for pick in side.picks]
        if side.faab:
            items.append(f"<li class=\"mono\">{_esc(f'FAAB ${side.faab}')}</li>")
        sides.append(
            f'<div><div class="tn">{_esc(ctx.label(side.roster_id))}</div>'
            f'<div class="lab">Receives</div><ul>{"".join(items)}</ul></div>'
        )
    body = '<span class="swap" aria-hidden="true">⇄</span>'.join(sides) if len(sides) == 2 else "".join(sides)
    layout = "trade" if len(sides) == 2 else "trade-multi"
    return (
        f'<article class="card tx"><div class="tx-top"><span class="lab">{_esc(f"Trade · week {trade.week}")}</span>'
        f'<span class="chip chip-plain">{_esc(when)}</span></div><div class="{layout}">{body}</div></article>'
    )


def _transactions_section(ctx: _Ctx) -> str:
    moves = wm.week_moves(ctx.facts)
    trades = wm.latest_trades(ctx.facts)
    if not moves and not trades:
        return ""
    cards = [_move_card(ctx, move) for move in moves]
    cards.extend(_trade_card(ctx, trade) for trade in trades)
    return (
        f'<section aria-label="The transaction desk" data-reveal>'
        f'{_head("The transaction desk", "What moved on the wire", "good")}'
        f'<div class="grid3">{"".join(cards)}</div>'
        f"{_prose(ctx.blocks(heading_for("transactions")))}</section>"
    )


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def render_weekly_web(
    facts: WeeklyFacts,
    issue: WeeklyIssue,
    *,
    output_id: str,
    generated_at: str,
    enhancer: WebEnhancer | None = None,
) -> str:
    """Render one self-contained ``<!doctype html>`` weekly Issue page.

    ``facts`` supplies every number and chart; ``issue`` supplies the narrated
    prose (matched by section heading), the page title, and any stamp the run
    added (an ``UNVERIFIED`` dateline, a Correction section). ``generated_at``
    is the only time value; ``output_id`` is accepted for parity with
    :func:`~commishdesk.render.web.render_web` and is not rendered.

    Story 5.16: when ``facts.period.has_prior_week`` is false (Week 1), the power
    rankings, luck index and transaction desk are omitted entirely, the standings
    section stands down its playoff picture, and the section set used to pick
    the narrated prose is the four-section cold-start set.

    Story 6.0c (AD-40): with ``enhancer=None`` the page is complete and static —
    zero ``<script>``, no motion, no control that does nothing. An
    :class:`~commishdesk.render.enhancer.WebEnhancer` adds its CSS at the end of
    the one ``<style>`` and its JS in one ``<script>`` just before ``</body>``.
    Both the enhancer's strings and the finished page are validated here;
    a breach raises :class:`~commishdesk.render.enhancer.EnhancerRejected`.
    """
    enhancement = validate_enhancer(enhancer) if enhancer is not None else None
    del output_id  # not rendered into the shareable page (parity with render_web)
    headings = section_headings_for_has_prior_week(facts.period.has_prior_week)
    cold_start = not facts.period.has_prior_week
    ctx = _Ctx(facts, issue)
    footer = (
        f'<footer data-reveal><p>{_esc(facts.league.name)} · {_esc(facts.league.season)} · Week {facts.week} · '
        f"generated {_esc(generated_at)}</p>"
        "<p>Every number is computed from the league&rsquo;s own Sleeper record; "
        "the prose is the narrator&rsquo;s.</p></footer>"
    )
    sections = [
        _masthead(ctx),
        _notices(issue, headings),
        _lead_section(ctx),
        _around_section(ctx),
        _standings_section(ctx),
        _power_section(ctx) if not cold_start else "",
        _luck_section(ctx) if not cold_start else "",
        _next_week_section(ctx),
        _transactions_section(ctx) if not cold_start else "",
        footer,
    ]
    document = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(issue.title)}</title>",
        "<style>",
        build_weekly_style(),
        *([enhancement[0]] if enhancement is not None and enhancement[0] else []),
        "</style>",
        "</head>",
        "<body>",
        '<main class="paper weekly_issue">',
        *[part for part in sections if part],
        "</main>",
        *(["<script>", enhancement[1], "</script>"] if enhancement is not None else []),
        "</body>",
        "</html>",
    ]
    page = "\n".join(document) + "\n"
    validate_page(page, enhanced=enhancement is not None)
    return page
