"""Story 5.14a — the designed, self-contained weekly web page.

:func:`render_weekly_web` turns the weekly Facts JSON plus the narrated
:class:`~commishdesk.narrate.weekly_template.WeeklyIssue` into one
``<!doctype html>`` string: the approved Story 5.13 design (Tuesday Morning
theme, Editorial layout) with inline CSS, embedded fonts, hand-authored inline
SVG, no script and no external request.

**Section order** — masthead, lead (hero chosen by lead kind, plus the awards
row), around the league, standings, power rankings, luck index, next week,
transaction desk. A section whose data is absent renders nothing at all: no
heading, no empty frame.

**Where each value comes from.** Numbers and charts come from Facts. Narrated
prose comes from the Issue's sections, matched by
:data:`~commishdesk.narrate.weekly_template.SECTION_HEADINGS`. The power
rankings read the published rank and the cited nudge reason from
``narration.power`` (falling back to ``teams[*].season.power.published_rank``,
then to the model rank); a reason is rendered only when the narration carries
one. Luck plots ``season.luck`` as-is — this module computes no luck figure.

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
    WeeklyFacts,
    WeeklyMatchup,
    WeeklyMove,
    WeeklyNextWeekCard,
    WeeklyTeam,
    WeeklyTrade,
)
from commishdesk.narrate.weekly_template import SECTION_HEADINGS, WeeklyIssue
from commishdesk.render._body import _esc
from commishdesk.render.style import build_weekly_style

__all__ = ["render_weekly_web"]

#: The real minus sign (U+2212) for signed numerals.
_MINUS = "−"

_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
)

#: Next-week stake tags (``stats/stakes.py``'s vocabulary) as chip words. An
#: unknown tag falls back to its own words rather than being dropped.
_STAKE_LABELS = {
    "bye_seed": "Bye seed",
    "wildcard_race": "Wildcard race",
    "division_race": "Division race",
    "draft_position": "Draft position",
    "elimination": "Elimination",
}

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


def _spell(n: int) -> str:
    return _ONES[n] if 0 <= n < len(_ONES) else str(n)


def _pts(value: float) -> str:
    """A week score / margin: two decimals (``247.20``)."""
    return f"{value:.2f}"


def _whole(value: float) -> str:
    """A season total: rounded, thousands separated (``2,029``)."""
    return f"{round(value):,}"


def _pct(value: float) -> str:
    """A ``0..1`` ratio as a one-decimal percentage (``0.992 -> 99.2%``)."""
    return f"{value * 100:.1f}%"


def _signed(value: float) -> str:
    """A signed figure with a real minus: one decimal unless the value carries
    more (``+1.5``, ``−0.4``, ``0.0``)."""
    text = f"{abs(value):.1f}" if round(value, 1) == value else f"{abs(value):.2f}"
    if value > 0:
        return f"+{text}"
    if value < 0:
        return f"{_MINUS}{text}"
    return text


def _num(value: float) -> str:
    """An SVG coordinate: one decimal, no trailing noise."""
    return f"{value:.1f}"


def _record(w: int, l: int, t: int, *, full: bool = False) -> str:  # noqa: E741 -- W-L-T
    if full or t:
        return f"{w}-{l}-{t}"
    return f"{w}-{l}"


def _team_label(team: WeeklyTeam | None, roster_id: str | None = None) -> str:
    """The display label the narration uses: ``team_name or manager or
    "Roster <id>"``."""
    if team is None:
        return f"Roster {roster_id}" if roster_id else "An unclaimed roster"
    return team.team_name or team.manager or f"Roster {team.roster_id}"


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


def _stake_label(tag: str) -> str:
    return _STAKE_LABELS.get(tag, tag.replace("_", " ").capitalize())


# --------------------------------------------------------------------------- #
# Shared section scaffolding
# --------------------------------------------------------------------------- #


def _svg_open(width: float, height: float, aria: str) -> str:
    """Opening ``<svg>`` tag plus a ``<title>`` first child (mirrors
    ``render/web.py``)."""
    return (
        f'<svg class="chart" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
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
        self.teams = {team.roster_id: team for team in facts.teams}
        self.prose = {
            section.heading: list(section.blocks)
            for section in issue.sections
            if section.heading in SECTION_HEADINGS
        }

    def label(self, roster_id: str | None) -> str:
        return _team_label(self.teams.get(roster_id or ""), roster_id)

    def blocks(self, heading: str) -> list[str]:
        return self.prose.get(heading, [])


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
        '<header class="masthead"><div>'
        f'<p class="weekline">{_esc(weekline)}</p>'
        f'<h1 class="nameplate">{_esc(league.name)}</h1>'
        f"</div>{tile_html}</header>"
    )


def _notices(issue: WeeklyIssue) -> str:
    """The UNVERIFIED dateline stamp and any section outside the seven (a
    reissue's Correction), shown above the lead so they are never missed."""
    out: list[str] = []
    if issue.dateline.startswith("UNVERIFIED"):
        out.append(
            '<aside class="notice" role="note"><h2>Unverified</h2>'
            f"<p>{_esc(issue.dateline)}</p></aside>"
        )
    for section in issue.sections:
        if section.heading in SECTION_HEADINGS:
            continue
        body = "".join(f"<p>{_esc(block)}</p>" for block in section.blocks)
        out.append(f'<aside class="notice" role="note"><h2>{_esc(section.heading)}</h2>{body}</aside>')
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# The lead — a hero per lead kind, type-only fallback
# --------------------------------------------------------------------------- #


def _find_matchup(facts: WeeklyFacts, roster_ids: Sequence[str]) -> WeeklyMatchup | None:
    wanted = set(roster_ids[:2])
    for matchup in facts.matchups.this_week:
        if {matchup.home_roster_id, matchup.away_roster_id} == wanted:
            return matchup
    return None


def _winner_loser(matchup: WeeklyMatchup) -> tuple[str, float, str, float]:
    home = (matchup.home_roster_id, matchup.home_points)
    away = (matchup.away_roster_id, matchup.away_points)
    if matchup.winner_roster_id == matchup.away_roster_id or (
        matchup.winner_roster_id is None and away[1] > home[1]
    ):
        home, away = away, home
    return home[0], home[1], away[0], away[1]


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


def _key_number(ctx: _Ctx, lead: LeadCandidate) -> str:
    """The one number the type-only lead sets beside its hook, when there is one."""
    summary = ctx.facts.period.summary
    if lead.kind == "lineup_loss" and lead.roster_ids:
        team = ctx.teams.get(lead.roster_ids[0])
        if team and team.this_week and team.this_week.points_left_on_bench is not None:
            return _pts(team.this_week.points_left_on_bench)
    if lead.kind in ("biggest_blowout", "closest_game"):
        matchup = _find_matchup(ctx.facts, lead.roster_ids)
        if matchup is not None:
            return _pts(abs(matchup.margin))
    if lead.kind == "week_high_score" and summary.high is not None:
        return _pts(summary.high.points)
    return ""


def _lead_section(ctx: _Ctx) -> str:
    blocks = ctx.blocks("The Lead")
    leads = sorted(ctx.facts.lead_candidates, key=lambda c: c.rank)
    lead = next((candidate for candidate in leads if candidate.hook), None)
    awards = _awards(ctx)
    if lead is None:
        if not blocks:
            return awards
        return (
            '<section class="lead-wrap" aria-label="The lead"><div class="card lead-main">'
            f'{_head("The lead", blocks[0], "bad")}{_prose(blocks[1:], extra_cls="lead-prose")}'
            f"</div>{awards}</section>"
        )
    hook = lead.hook or ""
    prose = _prose(blocks, skip={hook}, extra_cls="lead-prose")
    builder = _HEROES.get(lead.kind)
    drawn = builder(ctx, lead) if builder is not None else None
    if drawn is None:
        key = _key_number(ctx, lead)
        key_html = f'<p class="key">{_esc(key)}</p>' if key else ""
        return (
            '<section class="lead-wrap" aria-label="The lead"><div class="card lead-main">'
            '<p class="eyebrow dash-bad">The lead</p>'
            f'<div class="lead-type"><h2 class="hook">{_esc(hook)}</h2>{key_html}</div>'
            f"{prose}</div>{awards}</section>"
        )
    svg, side = drawn
    return (
        '<section class="lead-wrap" aria-label="The lead"><div class="lead">'
        f'<div class="card lead-main">{_head("The lead", hook, "bad")}{prose}{svg}</div>'
        f'<div class="lead-side">{side}</div></div>{awards}</section>'
    )


# --------------------------------------------------------------------------- #
# Awards row (all from ``leaders``)
# --------------------------------------------------------------------------- #


_OF_BEST = "of the best possible lineup"


def _award(key: str, value: str, name: str, sub: str, cls: str) -> str:
    return (
        f'<div class="award {cls}"><div class="k">{_esc(key)}</div><div>'
        f'<div class="v">{_esc(value)}</div><div class="n">{_esc(name)}</div>'
        f'<div class="s">{_esc(sub)}</div></div></div>'
    )


def _awards(ctx: _Ctx) -> str:
    leaders = ctx.facts.leaders
    cards: list[str] = []
    best = leaders.best_coaching
    if best is not None and best.pct is not None:
        cards.append(
            _award("Coach of the week", _pct(best.pct), ctx.label(best.roster_id), _OF_BEST, "aw-good")
        )
    star = leaders.week_high_player
    if star is not None:
        sub = [star.pos or "", ctx.label(star.roster_id)]
        if star.is_season_high:
            sub.append("season high")
        cards.append(
            _award("Player of the week", _pts(star.points), star.name, " · ".join(s for s in sub if s), "aw-emph")
        )
    worst = leaders.worst_coaching
    if worst is not None and worst.pct is not None:
        cards.append(
            _award("Bust of the week", _pct(worst.pct), ctx.label(worst.roster_id), _OF_BEST, "aw-bad")
        )
    egg = leaders.worst_starters[0] if leaders.worst_starters else None
    if egg is not None and egg.points == 0:
        sub_text = " · ".join(s for s in (egg.pos or "", f"started by {ctx.label(egg.roster_id)}") if s)
        cards.append(_award("Goose egg club", _pts(egg.points), egg.name, sub_text, "aw-notable"))
    if not cards:
        return ""
    return f'<div class="awards">{"".join(cards)}</div>'


# --------------------------------------------------------------------------- #
# Around the league — one card per game
# --------------------------------------------------------------------------- #


def _game_card(ctx: _Ctx, matchup: WeeklyMatchup) -> str:
    summary = ctx.facts.period.summary
    win_id, win_pts, lose_id, lose_pts = _winner_loser(matchup)
    pair = {matchup.home_roster_id, matchup.away_roster_id}
    tag = ""
    if summary.high is not None and summary.high.roster_id in pair:
        tag = '<span class="chip chip-good">Week high</span>'
    elif summary.closest is not None and set(summary.closest.roster_ids) == pair:
        tag = '<span class="chip chip-notable">Nail-biter</span>'
    elif matchup.is_blowout:
        tag = '<span class="chip chip-ink">Blowout</span>'
    tied = matchup.winner_roster_id is None
    by = "Tied" if tied else f"Won by {_pts(abs(matchup.margin))}"
    win_name, lose_name = ctx.label(win_id), ctx.label(lose_id)
    win_cls = "side" if tied else "side win"
    return (
        f'<article class="card game"><div class="game-top"><span class="by">{_esc(by)}</span>{tag}</div>'
        f'<div class="{win_cls}">{_mg(win_name, "" if tied else "mg-l")}<span class="tn">{_esc(win_name)}</span>'
        f'<span class="sc">{_esc(_pts(win_pts))}</span></div>'
        f'<div class="side">{_mg(lose_name)}<span class="tn">{_esc(lose_name)}</span>'
        f'<span class="sc">{_esc(_pts(lose_pts))}</span></div></article>'
    )


def _around_section(ctx: _Ctx) -> str:
    games = ctx.facts.matchups.this_week
    blocks = ctx.blocks("Around the League")
    if not games:
        return ""
    cards = "".join(_game_card(ctx, matchup) for matchup in games)
    return (
        f'<section aria-label="Around the league">{_head("Around the league", "Results", "emph")}'
        f'<div class="grid3">{cards}</div>{_prose(blocks)}</section>'
    )


# --------------------------------------------------------------------------- #
# Standings
# --------------------------------------------------------------------------- #


def _standings_section(ctx: _Ctx) -> str:
    facts = ctx.facts
    order = [rid for rid in facts.standings.overall if rid in ctx.teams]
    if not order:
        return ""
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

    rows: list[str] = []
    for index, roster_id in enumerate(order, start=1):
        team = ctx.teams[roster_id]
        season = team.season
        name = _team_label(team)
        width = (season.points_for / max_pf * 100) if max_pf > 0 else 0.0
        chip = ""
        if roster_id in byes:
            chip = '<span class="chip chip-emph">Bye</span>'
        elif roster_id in bubble:
            chip = '<span class="chip chip-notable">Bubble</span>'
        dot = ""
        if team.division_id in dot_of:
            dot = f'<span class="dot {dot_of[team.division_id]}" aria-hidden="true"></span>'
        below = cut is not None and index > cut
        rec = season.record
        rows.append(
            f'<div class="st-row{" below" if below else ""}">'
            f'<span class="rk">{season.rank}</span>{_mg(name)}'
            f'<span class="tn">{dot}{_esc(name)}</span>'
            f'<span class="rec">{_esc(_record(rec.w, rec.l, rec.t))}</span>'
            f'<span class="bar" aria-hidden="true"><i style="width:{width:.1f}%"></i></span>'
            f'<span class="pf">{_esc(_whole(season.points_for))}</span>'
            f'<span class="tag">{chip}</span></div>'
        )
        if cut is not None and index == cut and index < len(order):
            rows.append('<div class="cutline" role="separator">Playoff line</div>')
    legend = ""
    if dot_of:
        names = {division.id: division.name for division in facts.league.format.divisions}
        legend = '<p class="legend">' + "".join(
            f'<span><span class="dot {cls}" aria-hidden="true"></span>'
            f"{_esc(names.get(div_id) or f'Division {div_id}')}</span>"
            for div_id, cls in dot_of.items()
        ) + '<span>Bar: points for</span></p>'
    return (
        f'<section class="card table-card" aria-label="Standings">{_head("Standings", title, "good")}'
        f'<div class="standings">{"".join(rows)}</div>{legend}'
        f"{_prose(ctx.blocks('Standings and the Playoff Picture'))}</section>"
    )


# --------------------------------------------------------------------------- #
# Power rankings — by published rank, nudge chip + cited reason
# --------------------------------------------------------------------------- #


def _power_section(ctx: _Ctx) -> str:
    facts = ctx.facts
    narrated = {row.roster_id: row for row in facts.narration.power}
    rows: list[tuple[int | None, int | None, WeeklyTeam, str | None]] = []
    for team in facts.teams:
        power = team.season.power
        row = narrated.get(team.roster_id)
        published = row.published_rank if row is not None and row.published_rank is not None else None
        if published is None:
            published = power.published_rank
        if published is None:
            published = power.model_rank
        reason = row.nudge_justification if row is not None else None
        rows.append((published, power.model_rank, team, reason))
    if not any(published is not None for published, _m, _t, _r in rows):
        return ""
    rows.sort(
        key=lambda item: (
            item[0] is None,
            item[0] or 0,
            item[1] is None,
            item[1] or 0,
            item[2].roster_id.zfill(8),
        )
    )
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
    head = (
        '<div class="pw-head" aria-hidden="true"><span class="r">Rk</span><span>Team</span><span>W-L</span>'
        '<span class="r">Avg PF</span><span class="ms">Model score</span><span class="r">Wk</span></div>'
    )
    return (
        f'<section class="card table-card" aria-label="Power rankings">'
        f'{_head("Power rankings", "Where the model and the desk land", "emph")}'
        f'{head}<div class="power">{"".join(items)}</div>'
        f"{_prose(ctx.blocks('Power Rankings'))}</section>"
    )


# --------------------------------------------------------------------------- #
# Luck index — diverging bar of ``season.luck``, most lucky first
# --------------------------------------------------------------------------- #


def _luck_section(ctx: _Ctx) -> str:
    rows = [(team.season.luck, team) for team in ctx.facts.teams if team.season.luck is not None]
    if not rows:
        return ""
    rows.sort(key=lambda item: (-(item[0] or 0.0), item[1].roster_id.zfill(8)))
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
        top = _L_TOP + index * _L_ROW + (_L_ROW - _L_BAR) / 2
        base = top + _L_BAR / 2 + 5
        length = abs(value) * scale
        text = _signed(value)
        cells = [
            f"<title>{_esc(f'{name} {text}')}</title>",
            f'<text class="name" x="0" y="{_num(base)}">{_esc(short)}</text>',
        ]
        if value > 0:
            cells.append(
                f'<rect class="luck-bar" data-luck="{_esc(repr(value))}" x="{_L_AXIS}" y="{_num(top)}" '
                f'width="{_num(max(length, 2))}" height="{_L_BAR}" rx="8" fill="var(--good)"/>'
            )
            cells.append(f'<text class="lbl" x="{_num(_L_AXIS + length + 10)}" y="{_num(base)}">{_esc(text)}</text>')
        elif value < 0:
            cells.append(
                f'<rect class="luck-bar" data-luck="{_esc(repr(value))}" x="{_num(_L_AXIS - length)}" y="{_num(top)}" '
                f'width="{_num(max(length, 2))}" height="{_L_BAR}" rx="8" fill="var(--bad)"/>'
            )
            cells.append(
                f'<text class="lbl" x="{_num(_L_AXIS - length - 10)}" y="{_num(base)}" text-anchor="end">'
                f"{_esc(text)}</text>"
            )
        else:
            cells.append(
                f'<rect class="luck-bar" data-luck="{_esc(repr(value))}" x="{_L_AXIS - 1}" y="{_num(top)}" '
                f'width="2" height="{_L_BAR}" fill="var(--ink-3)"/>'
            )
            cells.append(f'<text class="lbl" x="{_L_AXIS + 10}" y="{_num(base)}">{_esc(text)}</text>')
        parts.append(f"<g>{''.join(cells)}</g>")
    aria = "Luck index: actual wins minus expected wins, most lucky to least"
    svg = _svg_open(_L_W, height, aria) + "".join(parts) + "</svg>"
    return (
        f'<section class="card luck-card" aria-label="The luck index">'
        f'{_head("The luck index", "Who the schedule favoured", "notable")}{svg}'
        f"{_prose(ctx.blocks('The Luck Index'))}</section>"
    )


# --------------------------------------------------------------------------- #
# Next week — shared stakes once, differing stakes as chips
# --------------------------------------------------------------------------- #


def _shared_stakes(cards: Sequence[WeeklyNextWeekCard]) -> list[str]:
    if len(cards) < 2:
        return []  # one game: its stakes are its own chips, not "every game"
    shared = [tag for tag in cards[0].stakes if all(tag in card.stakes for card in cards[1:])]
    return list(dict.fromkeys(shared))


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
        for tag in dict.fromkeys(card.stakes)
        if tag not in shared
    )
    chip_row = f'<div class="nw-chips">{"".join(chips)}</div>' if chips else ""
    bye = ""
    if card.bye_impact:
        names = ", ".join(
            f"{player.name} ({' '.join(p for p in (player.pos, player.nfl_team) if p)})"
            if (player.pos or player.nfl_team)
            else player.name
            for player in card.bye_impact
        )
        bye = f'<p class="byeline"><b>On bye</b> {_esc(names)}</p>'
    cls = "card nw gotw" if card.game_of_week else "card nw"
    return (
        f'<article class="{cls}">{chip_row}<div class="nw-vs">'
        f"{_nw_side(ctx, card.a_roster_id, card.a_record, card.a_power_rank)}"
        '<span class="vs">vs</span>'
        f"{_nw_side(ctx, card.b_roster_id, card.b_record, card.b_power_rank)}"
        f"</div>{bye}</article>"
    )


def _next_week_section(ctx: _Ctx) -> str:
    cards = ctx.facts.matchups.next_week
    if not cards:
        return ""
    shared = _shared_stakes(cards)
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
        f'<section aria-label="Next week">{_head("Next week", "Next week" + chr(8217) + "s games", "bad")}'
        f'{shared_line}{byes_line}<div class="grid3">{body}</div>'
        f"{_prose(ctx.blocks('Next Week'))}</section>"
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
    desk = ctx.facts.transactions
    moves = [move for move in desk.this_week if move.type != "trade" and (move.adds or move.drops)]
    trades = sorted(desk.recent_trades, key=lambda trade: trade.week, reverse=True)
    if not moves and not trades:
        return ""
    cards = [_move_card(ctx, move) for move in moves]
    cards.extend(_trade_card(ctx, trade) for trade in trades if trade.week == trades[0].week)
    return (
        f'<section aria-label="The transaction desk">'
        f'{_head("The transaction desk", "What moved on the wire", "good")}'
        f'<div class="grid3">{"".join(cards)}</div>'
        f"{_prose(ctx.blocks('The Transaction Desk'))}</section>"
    )


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def render_weekly_web(facts: WeeklyFacts, issue: WeeklyIssue, *, output_id: str, generated_at: str) -> str:
    """Render one self-contained ``<!doctype html>`` weekly Issue page.

    ``facts`` supplies every number and chart; ``issue`` supplies the narrated
    prose (matched by section heading), the page title, and any stamp the run
    added (an ``UNVERIFIED`` dateline, a Correction section). ``generated_at``
    is the only time value; ``output_id`` is accepted for parity with
    :func:`~commishdesk.render.web.render_web` and is not rendered.
    """
    del output_id  # not rendered into the shareable page (parity with render_web)
    ctx = _Ctx(facts, issue)
    footer = (
        f"<footer><p>{_esc(facts.league.name)} · {_esc(facts.league.season)} · Week {facts.week} · "
        f"generated {_esc(generated_at)}</p>"
        "<p>Every number is computed from the league&rsquo;s own Sleeper record; "
        "the prose is the narrator&rsquo;s.</p></footer>"
    )
    sections = [
        _masthead(ctx),
        _notices(issue),
        _lead_section(ctx),
        _around_section(ctx),
        _standings_section(ctx),
        _power_section(ctx),
        _luck_section(ctx),
        _next_week_section(ctx),
        _transactions_section(ctx),
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
        "</style>",
        "</head>",
        "<body>",
        '<main class="paper weekly_issue">',
        *[part for part in sections if part],
        "</main>",
        "</body>",
        "</html>",
    ]
    return "\n".join(document) + "\n"
