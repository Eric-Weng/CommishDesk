"""Story 4.1 — the designed, self-contained web render (AD-1 / AD-2 / I4).

:func:`render_web` is a pure function of its arguments: the validated Facts JSON
plus **exactly one** narrated body (the template
:class:`~commishdesk.narrate.Recap` **or** the LLM narrator's plain-text prose).
It returns one ``<!doctype html>`` string with all CSS and data inlined — zero
external sub-resource requests, exactly one inline ``<style>``, no ``<script>`` —
and three hand-authored inline-SVG figures: the draft-board grid (the headline
visual), a per-team pick-count bar, and a positional-run timeline.

**Deterministic (I4 / AD-2).** The only time value is the caller-supplied
``generated_at`` string. Sorted iteration, fixed number formatting, no
``datetime.now()``, no RNG, no slug invention — ``output_id`` is supplied by the
caller and is **not** rendered into the shareable page.

**Pipeline fence (AD-1).** Imports the standard library, ``commishdesk.facts``
schema types, the narrator output type, and :mod:`commishdesk.render.style` only —
nothing from ``ingest`` / ``stats`` / ``narrate`` internals, no cloud or HTTP SDK.
The masthead title comes from ``facts.league`` (name + season); ``llm_text`` is
treated entirely as body prose — a leading ``## `` heading is a section header,
never the page title (closes epic-3 retro E4). Every interpolated value (player,
manager, league name, prose, SVG ``<text>``) is escaped, with Unicode bidi
controls stripped first.
"""

from __future__ import annotations

from commishdesk.facts.schema import (
    DraftRecapFacts,
    PickRow,
    QBRunSummary,
    RBRunSummary,
    TERunSummary,
)
from commishdesk.narrate import Recap
from commishdesk.render._body import (
    _esc,
    _sections_from_llm,
    _sections_from_recap,
    _surname,
)
from commishdesk.render.style import (
    POSITION_VAR,
    POSITIONS,
    build_style,
    fmt_signed,
    position_label,
)

__all__ = ["render_web"]

# Grid geometry (px, in the SVG's own coordinate space).
_G_GUT = 46
_G_CW = 108
_G_CH = 58
_G_PAD = 10
_G_HEAD = 24

# Pick-count bar geometry.
_B_ROW = 30
_B_GUT = 172
_B_BAR = 340
_B_PAD = 12
_B_NUM = 46

# Positional timeline geometry.
_T_ROW = 46
_T_GUT = 40
_T_TRACK = 640
_T_PAD = 14


def _effective_rounds(facts: DraftRecapFacts, picks: list[PickRow]) -> int:
    """The round count the board must span: the declared ``draft.rounds`` when it
    is a positive int, widened to the largest real pick round so a missing / zero
    / negative / too-small declared value never truncates the grid."""
    declared = facts.draft.rounds
    declared = declared if isinstance(declared, int) and declared > 0 else 0
    return max(declared, max((pick.round for pick in picks), default=0))


# --------------------------------------------------------------------------- #
# Narrated body
# --------------------------------------------------------------------------- #


def _render_body(sections: list[tuple[str | None, list[str]]]) -> str:
    out: list[str] = []
    for heading, blocks in sections:
        out.append('<section class="prose">')
        if heading:
            out.append(f"<h2>{_esc(heading)}</h2>")
        for block in blocks:
            out.append(f"<p>{_esc(block)}</p>")
        out.append("</section>")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Masthead + at-a-glance strip (render-side, no schema change)
# --------------------------------------------------------------------------- #


def _r1_split(round1_positional: dict[str, int]) -> str:
    ordered = sorted(round1_positional.items(), key=lambda kv: (-kv[1], kv[0]))
    return " / ".join(f"{count} {position_label(pos)}" for pos, count in ordered)


def _at_a_glance(facts: DraftRecapFacts, picks: list[PickRow], team_count: int, rounds: int) -> list[str]:
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


def _masthead(facts: DraftRecapFacts, picks: list[PickRow], team_count: int, rounds: int) -> str:
    league = facts.league
    dateline = (
        f"{_esc(league.season)} season &middot; {team_count} teams "
        f"&middot; {rounds} rounds &middot; {_esc(league.format.scoring_label)}"
    )
    strip = "\n".join(f"<li>{_esc(item)}</li>" for item in _at_a_glance(facts, picks, team_count, rounds))
    return "\n".join(
        [
            '<header class="masthead">',
            '<p class="kicker">Draft Recap</p>',
            f'<h1 class="nameplate">{_esc(league.name)}</h1>',
            f'<p class="dateline">{dateline}</p>',
            '<hr class="rule-heavy">',
            '<hr class="rule-hair">',
            '<ul class="ataglance">',
            strip,
            "</ul>",
            "</header>",
        ]
    )


# --------------------------------------------------------------------------- #
# Inline-SVG figures — hand-authored, theme-aware via CSS custom properties
# --------------------------------------------------------------------------- #


def _svg_open(width: float, height: float, aria: str) -> str:
    """Opening ``<svg>`` tag plus a ``<title>`` first child (cheap a11y)."""
    return (
        f'<svg class="chart" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'role="img" aria-label="{_esc(aria)}"><title>{_esc(aria)}</title>'
    )


def _figure(eyebrow: str, note: str, svg: str, legend: str = "") -> str:
    return "\n".join(
        [
            '<figure class="figure">',
            f'<figcaption class="fig-title">{_esc(eyebrow)}</figcaption>',
            f'<p class="fig-note">{_esc(note)}</p>',
            f'<div class="figwrap" tabindex="0" role="group" aria-label="{_esc(eyebrow)}">{svg}</div>',
            legend,
            "</figure>",
        ]
    )


def _empty_figure(eyebrow: str, note: str) -> str:
    return "\n".join(
        [
            '<figure class="figure">',
            f'<figcaption class="fig-title">{_esc(eyebrow)}</figcaption>',
            f'<p class="fig-note">{_esc(note)}</p>',
            "</figure>",
        ]
    )


def _is_snake(picks: list[PickRow], team_count: int) -> bool:
    """Derive snake vs linear from ``pick_no`` against ``slot`` (do not assume).

    In an even round a snake draft reverses seat order: ``pick_no`` for seat
    ``slot`` is ``(round-1)*team_count + (team_count - slot + 1)``. The first
    even-round pick that disambiguates decides.
    """
    for pick in picks:
        if pick.round % 2 != 0:
            continue
        base = (pick.round - 1) * team_count
        snake_no = base + (team_count - pick.slot + 1)
        linear_no = base + pick.slot
        if snake_no == linear_no:
            continue
        if pick.pick_no == snake_no:
            return True
        if pick.pick_no == linear_no:
            return False
    return False


def _seat(pick: PickRow, team_count: int, snake: bool) -> int:
    """The board column (drafting seat) for ``pick``: ``slot`` in an odd round or
    a linear draft; the mirrored ``team_count - slot + 1`` in a snake even round."""
    if snake and pick.round % 2 == 0:
        return team_count - pick.slot + 1
    return pick.slot


def _delta_mark(delta: int) -> str:
    return "var(--value)" if delta > 0 else "var(--reach)"


def _magnitude_radius(delta: int) -> float:
    """|δ| ≤ 2 fair, 3–8 slight, > 8 big."""
    magnitude = abs(delta)
    if magnitude <= 2:
        return 2.0
    if magnitude <= 8:
        return 3.2
    return 4.6


def _draft_grid(facts: DraftRecapFacts) -> str:
    picks = sorted(facts.picks, key=lambda p: p.pick_no)
    team_count = facts.league.format.team_count
    if not picks or team_count < 1:
        return _empty_figure("The draft board", "No picks landed on the board.")
    rounds = _effective_rounds(facts, picks)
    snake = _is_snake(picks, team_count)

    placed: list[tuple[PickRow, int]] = []
    unplaceable = 0
    max_col = team_count
    for pick in picks:
        seat = _seat(pick, team_count, snake)
        if seat >= 1 and pick.round >= 1:
            placed.append((pick, seat))
            max_col = max(max_col, seat)
        else:  # malformed slot / round — never silently drop it
            unplaceable += 1

    cols = max_col
    width = _G_GUT + cols * _G_CW + _G_PAD * 2
    height = _G_PAD * 2 + _G_HEAD + rounds * _G_CH + (18 if unplaceable else 0)
    aria = f"Draft board grid, {rounds} rounds by {team_count} teams"
    parts = [_svg_open(width, height, aria)]

    for col in range(cols):
        cx = _G_GUT + _G_PAD + col * _G_CW + _G_CW / 2
        parts.append(f'<text class="svg-col" x="{cx:.1f}" y="{_G_PAD + 13}">{col + 1}</text>')
    for rnd in range(1, rounds + 1):
        cy = _G_PAD + _G_HEAD + (rnd - 1) * _G_CH + _G_CH / 2
        parts.append(f'<text class="svg-round" x="{_G_PAD + 4}" y="{cy + 4:.1f}">R{rnd}</text>')

    for pick, seat in placed:
        x = _G_GUT + _G_PAD + (seat - 1) * _G_CW
        y = _G_PAD + _G_HEAD + (pick.round - 1) * _G_CH
        hue = POSITION_VAR.get((pick.player.position or "").upper(), "var(--ink-3)")
        parts.append(
            f'<rect class="grid-cell" x="{x + 2}" y="{y + 2}" width="{_G_CW - 4}" height="{_G_CH - 4}" '
            f'rx="3" fill="{hue}" fill-opacity="0.14" stroke="var(--line)"/>'
        )
        delta = pick.delta
        if delta is not None and delta != 0:
            mark = _delta_mark(delta)
            parts.append(
                f'<rect class="grid-mark" x="{x + 2}" y="{y + 2}" width="3" height="{_G_CH - 4}" '
                f'fill="{mark}"/>'
            )
            parts.append(
                f'<circle cx="{x + _G_CW - 9:.1f}" cy="{y + 9}" r="{_magnitude_radius(delta):.1f}" '
                f'fill="{mark}"/>'
            )
        parts.append(
            f'<text class="svg-name" x="{x + 8}" y="{y + 24}">{_esc(_surname(pick.player.name))}</text>'
        )
        meta = _esc(pick.board_label)
        if delta is not None:
            meta += "  " + _esc(fmt_signed(delta))
        parts.append(f'<text class="svg-meta" x="{x + 8}" y="{y + 42}">{meta}</text>')

    if unplaceable:
        plural = "s" if unplaceable != 1 else ""
        parts.append(
            f'<text class="svg-meta" x="{_G_PAD + 4}" y="{height - 5}">'
            f"{unplaceable} pick{plural} not shown (unresolved board position)</text>"
        )
    parts.append("</svg>")
    legend = (
        '<div class="chart-legend">'
        '<span><i class="sw-qb"></i>QB</span>'
        '<span><i class="sw-rb"></i>RB</span>'
        '<span><i class="sw-wr"></i>WR</span>'
        '<span><i class="sw-te"></i>TE</span>'
        '<span><i class="sw-value"></i>value</span>'
        '<span><i class="sw-reach"></i>reach</span>'
        "</div>"
    )
    return _figure(
        "The draft board",
        "Every pick, round by round; seats left to right, snake order followed. Cell tint "
        "is position; the left bar and corner dot mark value (green) or reach (red), sized "
        "by the gap to the consensus board.",
        "".join(parts),
        legend,
    )


def _pick_count_bar(facts: DraftRecapFacts) -> str:
    rank = list(facts.draft_summary.pick_count_rank)
    if not rank:
        return _empty_figure("Picks per team", "No pick-count data on the board.")
    top = max((row.pick_count for row in rank), default=0) or 1

    width = _B_PAD * 2 + _B_GUT + _B_BAR + _B_NUM
    height = _B_PAD * 2 + len(rank) * _B_ROW
    parts = [_svg_open(width, height, "Picks made per team")]
    for index, row in enumerate(rank):
        y = _B_PAD + index * _B_ROW
        mid = y + _B_ROW / 2
        name = _esc(row.manager or "an unclaimed roster")
        bar = max(2.0, _B_BAR * row.pick_count / top)
        parts.append(f'<text class="svg-barname" x="{_B_PAD + _B_GUT - 8}" y="{mid + 4:.1f}">{name}</text>')
        parts.append(
            f'<rect class="bar" x="{_B_PAD + _B_GUT}" y="{y + 6:.1f}" width="{bar:.1f}" '
            f'height="{_B_ROW - 12}" rx="2" fill="var(--ink-2)"/>'
        )
        parts.append(
            f'<text class="svg-barnum" x="{_B_PAD + _B_GUT + bar + 6:.1f}" y="{mid + 4:.1f}">'
            f'{row.pick_count}</text>'
        )
    parts.append("</svg>")
    return _figure(
        "Picks per team",
        "How many selections each roster actually made — a read on trade activity, not on "
        "where they picked.",
        "".join(parts),
    )


def _qb_caption(qb: QBRunSummary) -> str:
    bits = [f"{qb.total} drafted"]
    if qb.first_label:
        bits.append(f"first {qb.first_label}")
    if qb.by_end_round3:
        bits.append(f"{qb.by_end_round3} by end of round 3")
    return " · ".join(bits)


def _rb_caption(rb: RBRunSummary) -> str:
    bits = [f"{rb.total} drafted"]
    if rb.first_label:
        bits.append(f"first {rb.first_label}")
    if rb.in_round1:
        bits.append(f"{rb.in_round1} in round 1")
    return " · ".join(bits)


def _wr_caption(picks: list[PickRow]) -> str:
    labels = [p.board_label for p in picks if (p.player.position or "").upper() == "WR"]
    bits = [f"{len(labels)} drafted"]
    if labels:
        bits.append(f"first {labels[0]}")
    return " · ".join(bits)


def _te_caption(te: TERunSummary) -> str:
    bits = [f"{te.total} drafted"]
    if te.early_window_labels:
        bits.append("early window " + ", ".join(te.early_window_labels))
    if te.third_te_label:
        bits.append(f"third at {te.third_te_label}")
    return " · ".join(bits)


def _positional_timeline(facts: DraftRecapFacts) -> str:
    picks = sorted(facts.picks, key=lambda p: p.pick_no)
    if not picks:
        return _empty_figure("When each position went", "No picks to place on a timeline.")
    last = max(pick.pick_no for pick in picks)
    runs = facts.draft_summary.positional_runs
    captions = {
        "QB": _qb_caption(runs.QB),
        "RB": _rb_caption(runs.RB),
        "WR": _wr_caption(picks),
        "TE": _te_caption(runs.TE),
    }
    by_position: dict[str, list[int]] = {pos: [] for pos in POSITIONS}
    for pick in picks:
        position = (pick.player.position or "").upper()
        if position in by_position:
            by_position[position].append(pick.pick_no)

    width = _T_PAD * 2 + _T_GUT + _T_TRACK
    height = _T_PAD * 2 + len(POSITIONS) * _T_ROW
    x0 = _T_PAD + _T_GUT
    span = max(1, last - 1)

    def px(pick_no: int) -> float:
        return x0 + _T_TRACK * (pick_no - 1) / span

    parts = [_svg_open(width, height, f"When each position was drafted, pick 1 to {last}")]
    for index, position in enumerate(POSITIONS):
        y = _T_PAD + index * _T_ROW + _T_ROW / 2
        parts.append(f'<text class="svg-tlpos" x="{_T_PAD}" y="{y - 3:.1f}">{position}</text>')
        parts.append(
            f'<line x1="{x0}" y1="{y:.1f}" x2="{x0 + _T_TRACK}" y2="{y:.1f}" '
            f'stroke="var(--line)"/>'
        )
        for pick_no in by_position[position]:
            parts.append(
                f'<circle cx="{px(pick_no):.1f}" cy="{y:.1f}" r="3.5" '
                f'fill="{POSITION_VAR[position]}"/>'
            )
        parts.append(
            f'<text class="svg-tlnote" x="{x0}" y="{y + 15:.1f}">{_esc(captions[position])}</text>'
        )
    parts.append("</svg>")
    return _figure(
        "When each position went",
        "Every quarterback, running back, receiver and tight end placed on the draft's "
        "timeline, pick 1 on the left to the final pick on the right. Tight clusters are "
        "position runs.",
        "".join(parts),
    )


# --------------------------------------------------------------------------- #
# Footer
# --------------------------------------------------------------------------- #


def _footer(facts: DraftRecapFacts, team_count: int, rounds: int, generated_at: str) -> str:
    league = facts.league
    consensus = facts.consensus_source.name
    against = f" against {_esc(consensus)}" if consensus else ""
    # No output_id here — the page is built to be shared, and for a real league
    # output_id is the Sleeper league id (P10). Only league descriptors + the stamp.
    stamp = (
        f"{_esc(league.name)} &middot; {_esc(league.season)} &middot; {team_count} teams "
        f"&middot; {rounds} rounds &middot; generated {_esc(generated_at)}"
    )
    return "\n".join(
        [
            "<footer>",
            '<p class="prov">Every pick and board label is drawn straight from the draft '
            f"record; consensus deltas and grades are the engine's own, measured{against}.</p>",
            f'<p class="stamp">{stamp}</p>',
            "</footer>",
        ]
    )


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def render_web(
    facts: DraftRecapFacts,
    *,
    recap: Recap | None = None,
    llm_text: str | None = None,
    output_id: str,
    generated_at: str,
) -> str:
    """Render one self-contained ``<!doctype html>`` draft-recap page.

    **Exactly one** of ``recap`` (the template narrator's structured
    :class:`~commishdesk.narrate.Recap`) or ``llm_text`` (the validated LLM prose)
    must be supplied — passing neither or both raises :class:`ValueError`. The
    masthead, the at-a-glance strip, the three inline-SVG figures, and the footer
    stamp come from ``facts``. ``output_id`` is the caller's output identity (never
    invented here, and never rendered into the shareable page); ``generated_at`` is
    the only time value. Byte-identical for identical arguments.
    """
    if (recap is None) == (llm_text is None):
        which = "neither" if recap is None else "both"
        raise ValueError(
            f"render_web needs exactly one of recap= or llm_text= (got {which})"
        )

    league = facts.league
    picks = sorted(facts.picks, key=lambda p: p.pick_no)
    team_count = league.format.team_count
    rounds = _effective_rounds(facts, picks)

    sections = _sections_from_recap(recap) if recap is not None else _sections_from_llm(llm_text or "")
    body = _render_body(sections)

    title = f"{_esc(league.name)} — {_esc(league.season)} Draft Recap"

    document = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{title}</title>",
        "<style>",
        build_style(),
        "</style>",
        "</head>",
        "<body>",
        '<div class="paper">',
        _masthead(facts, picks, team_count, rounds),
        body,
        _draft_grid(facts),
        _pick_count_bar(facts),
        _positional_timeline(facts),
        _footer(facts, team_count, rounds, generated_at),
        "</div>",
        "</body>",
        "</html>",
    ]
    return "\n".join(part for part in document if part) + "\n"
