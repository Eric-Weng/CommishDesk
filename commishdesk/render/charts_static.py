"""Story 8.15 — static chart images and the social-card PNGs.

Renders the weekly Issue's two chart panels (the luck index and the power
rankings) as PNGs, and builds the 1200x630 social card for each one from the
Facts JSON alone. The finding sentence on a card (also its alt text and Open
Graph description) is computed from the facts: deterministic, no model call, no
network, no font download.

Palette and number formats are the web render's, not copies: colors are read
from :mod:`commishdesk.render.style` (``WEEKLY_LIGHT_TOKENS`` for the chart
panels, ``WEEKLY_DARK_TOKENS`` for the card, which is a fixed "desk" surface in
both themes) and every figure goes through :mod:`commishdesk.render._weekly_model`
(``signed``, ``team_label``, ``record``, ``luck_rows``, ``power_rows``).

``matplotlib`` is the opt-in ``charts`` extra and is imported only inside the
functions below, so ``import commishdesk`` (and this module) stay matplotlib-free;
a missing extra raises :class:`ChartsUnavailableError`. Output is byte-stable: a
fixed ``Agg`` canvas, fonts read from the packaged TTFs (``render/fonts/``, OFL
licences beside them) and no PNG metadata.
"""

from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING, Any, Literal

from commishdesk.facts.schema import WeeklyFacts
from commishdesk.render import _weekly_model as wm
from commishdesk.render.style import WEEKLY_DARK_TOKENS, WEEKLY_LIGHT_TOKENS
from commishdesk.render.weekly_web import _L_NAME_MAX

if TYPE_CHECKING:  # pragma: no cover - typing only; never imported at runtime
    from matplotlib.axes import Axes
    from matplotlib.figure import Figure

__all__ = [
    "CARD_HEIGHT",
    "CARD_WIDTH",
    "PANELS",
    "Card",
    "ChartsUnavailableError",
    "available_panels",
    "base_fig",
    "finding",
    "panel_png",
    "render_cards",
    "social_card",
]

_LOG = logging.getLogger(__name__)

Panel = Literal["luck", "power"]

#: The chart panels that have an image form, in card order.
PANELS: tuple[Panel, ...] = ("luck", "power")

CARD_WIDTH = 1200
CARD_HEIGHT = 630
_DPI = 100

#: The default footer URL when the caller has none (the public site).
_DEFAULT_URL = "commishdesk.com"

_EYEBROW = {"luck": "LUCK INDEX", "power": "POWER RANKINGS"}

_FONTS = {
    "display": "BricolageGrotesque-ExtraBold.ttf",
    "sans": "DMSans-Regular.ttf",
    "sans-bold": "DMSans-Bold.ttf",
    "mono": "IBMPlexMono-Medium.ttf",
}


class ChartsUnavailableError(ImportError):
    """The ``charts`` extra (matplotlib) is not installed."""


@dataclass(frozen=True)
class Card:
    """One rendered social card: PNG bytes plus the text that describes it."""

    panel: str
    png: bytes
    alt: str
    description: str

    @property
    def bytes_sha256(self) -> str:
        return hashlib.sha256(self.png).hexdigest()

    def manifest_entry(self) -> dict[str, str]:
        """The ``cards.json`` value for this card."""
        return {"alt": self.alt, "description": self.description, "bytes_sha256": self.bytes_sha256}


# --------------------------------------------------------------------------- #
# matplotlib plumbing (lazy)
# --------------------------------------------------------------------------- #


def _require_matplotlib() -> None:
    try:
        import matplotlib  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised via a blocked import in tests
        raise ChartsUnavailableError(
            "static charts need the 'charts' extra: install commishdesk[charts] (matplotlib)"
        ) from exc


def _font(kind: str, px: float) -> Any:
    """A ``FontProperties`` for a packaged face at *px* pixels (at the fixed DPI)."""
    from matplotlib.font_manager import FontProperties

    path = resources.files("commishdesk.render").joinpath("fonts", _FONTS[kind])
    return FontProperties(fname=str(path), size=px * 72 / _DPI)


def base_fig(width_px: int, height_px: int, *, background: str) -> tuple[Figure, Axes]:
    """A figure with one pixel-coordinate axes (origin top-left, y down) and no
    chrome: the editorial-clean base every static chart draws on.

    The canvas is the fixed ``Agg`` one, with no pyplot state, so output does not
    depend on a global backend.
    """
    _require_matplotlib()
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    fig = Figure(figsize=(width_px / _DPI, height_px / _DPI), dpi=_DPI, facecolor=background)
    FigureCanvasAgg(fig)
    ax = fig.add_axes((0.0, 0.0, 1.0, 1.0))
    ax.set_xlim(0, width_px)
    ax.set_ylim(height_px, 0)
    ax.axis("off")
    ax.set_facecolor(background)
    return fig, ax


def _png(fig: Figure) -> bytes:
    buffer = io.BytesIO()
    # ``Software: None`` drops the matplotlib-version tEXt chunk; PNG carries no timestamp.
    fig.savefig(buffer, format="png", dpi=_DPI, facecolor=fig.get_facecolor(), metadata={"Software": None})
    return buffer.getvalue()


def _text_width(fig: Figure, text: str, font: Any) -> float:
    renderer = fig.canvas.get_renderer()  # type: ignore[attr-defined]
    artist = fig.text(0, 0, text, fontproperties=font)
    width = float(artist.get_window_extent(renderer).width)
    artist.remove()
    return width


def _wrap(fig: Figure, text: str, font: Any, max_width: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}" if current else word
        if current and _text_width(fig, trial, font) > max_width:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def _draw_text(
    ax: Axes,
    x: float,
    y: float,
    text: str,
    font: Any,
    color: str,
    *,
    ha: str = "left",
) -> None:
    ax.text(x, y, text, fontproperties=font, color=color, ha=ha, va="baseline")


def _triangle(ax: Axes, x: float, y: float, size: float, direction: str, color: str) -> None:
    """A solid ▲ ▼ ◀ ▶ marker centred on (x, y): the packaged faces carry no such glyphs."""
    from matplotlib.patches import Polygon

    half = size / 2
    points = {
        "up": [(x - half, y + half * 0.8), (x + half, y + half * 0.8), (x, y - half)],
        "down": [(x - half, y - half * 0.8), (x + half, y - half * 0.8), (x, y + half)],
        "left": [(x + half * 0.8, y - half), (x + half * 0.8, y + half), (x - half, y)],
        "right": [(x - half * 0.8, y - half), (x - half * 0.8, y + half), (x + half, y)],
    }[direction]
    ax.add_patch(Polygon(points, closed=True, facecolor=color, edgecolor="none"))


def _bar(ax: Axes, x0: float, x1: float, y: float, height: float, color: str, radius: float = 8) -> None:
    from matplotlib.patches import FancyBboxPatch

    left, width = min(x0, x1), max(abs(x1 - x0), 2.0)
    rounding = min(radius, width / 2, height / 2)
    ax.add_patch(
        FancyBboxPatch(
            (left, y - height / 2),
            width,
            height,
            boxstyle=f"round,pad=0,rounding_size={rounding}",
            facecolor=color,
            edgecolor="none",
        )
    )


def _rect(ax: Axes, x: float, y: float, w: float, h: float, face: str, edge: str | None = None) -> None:
    from matplotlib.patches import Rectangle

    ax.add_patch(Rectangle((x, y), w, h, facecolor=face, edgecolor=edge or "none", linewidth=1.0 if edge else 0.0))


# --------------------------------------------------------------------------- #
# Facts -> findings
# --------------------------------------------------------------------------- #


def _short(name: str, limit: int | None) -> str:
    """The web chart's truncation: ``…`` after *limit* characters."""
    if limit is None or len(name) <= limit:
        return name
    return name[: limit - 1].rstrip() + "…"


def available_panels(facts: WeeklyFacts) -> list[Panel]:
    """The panels with an image form for these facts. Week 1 (no prior week) has none."""
    if not facts.period.has_prior_week:
        return []
    panels: list[Panel] = []
    if wm.luck_rows(facts):
        panels.append("luck")
    if wm.power_rows(facts):
        panels.append("power")
    return panels


def _luck_finding(facts: WeeklyFacts, name_max: int | None) -> str:
    rows = wm.luck_rows(facts)
    top_luck, top = rows[0]
    bottom_luck, bottom = rows[-1]
    top_name = _short(wm.team_label(top), name_max)
    bottom_name = _short(wm.team_label(bottom), name_max)
    if top_luck > 0 and bottom_luck < 0:
        return (
            f"Luckiest is {top_name} at {wm.signed(top_luck)} wins; "
            f"unluckiest is {bottom_name} at {wm.signed(bottom_luck)}."
        )
    if top_luck > 0:
        return f"Luckiest is {top_name} at {wm.signed(top_luck)} wins; nobody has been robbed."
    if bottom_luck < 0:
        return f"Unluckiest is {bottom_name} at {wm.signed(bottom_luck)} wins; nobody is ahead of the schedule."
    return "Every team is right on its expected record."


def _power_mover(facts: WeeklyFacts) -> tuple[wm.PowerRow, int] | None:
    """The team with the biggest model-rank move this week (earliest published rank wins a tie)."""
    best: tuple[wm.PowerRow, int] | None = None
    for row in wm.power_rows(facts):
        delta = row.team.season.power.week_delta
        if delta and (best is None or abs(delta) > abs(best[1])):
            best = (row, delta)
    return best


def _power_finding(facts: WeeklyFacts, name_max: int | None) -> str:
    rows = wm.power_rows(facts)
    lead = rows[0]
    lead_name = _short(wm.team_label(lead.team), name_max)
    mover = _power_mover(facts)

    def move(delta: int) -> str:
        spots = "spot" if abs(delta) == 1 else "spots"
        return f"{'up' if delta > 0 else 'down'} {abs(delta)} {spots}"

    if mover is None:
        return f"#1 in the power rankings is {lead_name}; no team moved this week."
    mover_row, delta = mover
    if mover_row.team.roster_id == lead.team.roster_id:
        return f"#1 in the power rankings is {lead_name}, {move(delta)}."
    mover_name = _short(wm.team_label(mover_row.team), name_max)
    return f"#1 is {lead_name}; the biggest move is {mover_name}, {move(delta)}."


def finding(facts: WeeklyFacts, panel: Panel, *, name_max: int | None = None) -> str:
    """The one-sentence finding for *panel*, computed from the facts (no model)."""
    if panel == "luck":
        return _luck_finding(facts, name_max)
    return _power_finding(facts, name_max)


def _alt(facts: WeeklyFacts, panel: Panel) -> str:
    return f"{_EYEBROW[panel].capitalize()}, {facts.league.name}, week {facts.period.week}: {finding(facts, panel)}"


# --------------------------------------------------------------------------- #
# The social card
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Row:
    label: str
    word: str
    direction: str | None  # "up" | "down" | None
    tone: str  # "good" | "bad" | "muted"
    value: str
    frac: float  # luck: -1..1 around the axis; power: 0..1 from the left edge


def _luck_card_rows(facts: WeeklyFacts) -> list[_Row]:
    rows = wm.luck_rows(facts)
    picks = [rows[0], rows[-1]] if len(rows) > 1 else [rows[0]]
    rest = rows[1:-1]
    if rest:
        picks.append(max(rest, key=lambda row: abs(row[0])))  # first of a tie, deterministic
    picks.sort(key=lambda row: -row[0])
    peak = max(abs(luck) for luck, _team in picks) or 1.0
    out: list[_Row] = []
    for luck, team in picks:
        if luck > 0:
            word, direction, tone = "BLESSED", "up", "good"
        elif luck < 0:
            word, direction, tone = "ROBBED", "down", "bad"
        else:
            word, direction, tone = "EVEN", None, "muted"
        out.append(_Row(wm.team_label(team), word, direction, tone, wm.signed(luck), luck / peak))
    return out


def _power_card_rows(facts: WeeklyFacts) -> list[_Row]:
    out: list[_Row] = []
    for row in wm.power_rows(facts)[:3]:
        power = row.team.season.power
        delta = power.week_delta
        if delta is None or delta == 0:
            word, direction, tone = "HOLDS", None, "muted"
        elif delta > 0:
            word, direction, tone = f"UP {delta}", "up", "good"
        else:
            word, direction, tone = f"DOWN {-delta}", "down", "bad"
        rank = row.published if row.published is not None else "–"
        record = row.team.season.record
        score = power.model_score
        frac = max(0.0, min(1.0, score)) if score is not None else 0.0
        out.append(
            _Row(
                f"#{rank} {wm.team_label(row.team)}",
                word,
                direction,
                tone,
                wm.record(record.w, record.l, record.t),
                frac,
            )
        )
    return out


def _wrapped_finding(fig: Figure, facts: WeeklyFacts, panel: Panel, font: Any, max_width: float) -> list[str]:
    """The finding wrapped to at most four lines, shortening team names until it fits."""
    lines: list[str] = []
    for limit in (_L_NAME_MAX, 16, 10, 6):
        lines = _wrap(fig, finding(facts, panel, name_max=limit), font, max_width)
        if len(lines) <= 4:
            break
    return lines[:4]


def social_card(facts: WeeklyFacts, panel: Panel, *, url: str = _DEFAULT_URL) -> Card:
    """The 1200x630 social card for *panel*: mono eyebrow, the finding in Bricolage
    ExtraBold at 72px, three bars with up/down words, a footer with a letter badge and
    the URL. Desk colors, fixed in both themes."""
    tokens = WEEKLY_DARK_TOKENS
    if panel == "luck":
        rows = _luck_card_rows(facts) if wm.luck_rows(facts) else []
    else:
        rows = _power_card_rows(facts) if wm.power_rows(facts) else []
    if not rows:
        raise ValueError(f"no {panel} data for a social card")

    fig, ax = base_fig(CARD_WIDTH, CARD_HEIGHT, background=tokens["--paper"])
    margin = 60

    league = _short(facts.league.name.upper(), 28)
    eyebrow = f"{_EYEBROW[panel]} · {league} · WEEK {facts.period.week}"
    _draw_text(ax, margin, 62, eyebrow, _font("mono", 22), tokens["--ink-2"])

    display = _font("display", 72)
    lines = _wrapped_finding(fig, facts, panel, display, CARD_WIDTH - 2 * margin)
    for index, line in enumerate(lines):
        _draw_text(ax, margin - 3, 140 + index * 74, line, display, tokens["--ink"])

    # The bars panel.
    panel_top, row_pitch = 384, 52
    panel_height = 12 + row_pitch * len(rows) + 4
    _rect(ax, margin, panel_top, CARD_WIDTH - 2 * margin, panel_height, tokens["--card"], tokens["--line"])
    name_font, word_font, value_font = _font("sans-bold", 24), _font("mono", 15), _font("mono", 24)
    tone_text = {"good": tokens["--good-text"], "bad": tokens["--bad-text"], "muted": tokens["--ink-2"]}
    tone_fill = {"good": tokens["--good"], "bad": tokens["--bad"], "muted": tokens["--ink-3"]}
    if panel == "luck":
        axis, half = 780.0, 240.0
        ax.plot(
            [axis, axis],
            [panel_top + 8, panel_top + panel_height - 8],
            color=tokens["--ink-3"],
            linewidth=1.5 * 72 / _DPI,
        )
    else:
        left, span = 540.0, 480.0
    for index, row in enumerate(rows):
        center = panel_top + 12 + row_pitch * index + row_pitch / 2
        _draw_text(ax, margin + 24, center - 5, _short_label(row.label), name_font, tokens["--ink"])
        word_x = margin + 24
        if row.direction:
            _triangle(ax, word_x + 6, center + 11, 11, row.direction, tone_text[row.tone])
            word_x += 20
        _draw_text(ax, word_x, center + 17, row.word, word_font, tone_text[row.tone])
        if panel == "luck":
            end = axis + row.frac * half
            if row.frac == 0:
                _rect(ax, axis - 1, center - 14, 2, 28, tokens["--ink-3"])
                _draw_text(ax, axis + 12, center + 8, row.value, value_font, tokens["--ink"])
            elif row.frac > 0:
                _bar(ax, axis, end, center, 28, tone_fill[row.tone])
                _draw_text(ax, end + 12, center + 8, row.value, value_font, tokens["--ink"])
            else:
                _bar(ax, end, axis, center, 28, tone_fill[row.tone])
                _draw_text(ax, end - 12, center + 8, row.value, value_font, tokens["--ink"], ha="right")
        else:
            end = left + max(row.frac, 0.0) * span
            _bar(ax, left, end, center, 28, tokens["--bar"])
            _draw_text(ax, end + 12, center + 8, row.value, value_font, tokens["--ink"])

    # Footer: letter badge + URL.
    from matplotlib.patches import Circle

    badge_y = 596.0
    ax.add_patch(Circle((margin + 20, badge_y), 18, facecolor=tokens["--emph"], edgecolor="none"))
    letter = next((ch for ch in facts.league.name if ch.isalnum()), "C").upper()
    ax.text(
        margin + 20,
        badge_y + 8,
        letter,
        fontproperties=_font("sans-bold", 24),
        color=tokens["--on-emph"],
        ha="center",
        va="baseline",
    )
    _draw_text(ax, margin + 56, badge_y + 8, url, _font("mono", 22), tokens["--ink-2"])

    return Card(panel=panel, png=_png(fig), alt=_alt(facts, panel), description=finding(facts, panel))


def _short_label(label: str) -> str:
    return _short(label, _L_NAME_MAX + 3) if label.startswith("#") else _short(label, _L_NAME_MAX)


def render_cards(facts: WeeklyFacts, *, url: str = _DEFAULT_URL) -> dict[str, Card]:
    """One social card per panel that has data, ``{}`` for a week with none.

    A panel that fails to render is logged and left out; a missing matplotlib
    raises :class:`ChartsUnavailableError` for the caller to log once.
    """
    panels = available_panels(facts)
    if panels:
        _require_matplotlib()
    cards: dict[str, Card] = {}
    for panel in panels:
        try:
            cards[panel] = social_card(facts, panel, url=url)
        except ChartsUnavailableError:
            raise
        except Exception:
            _LOG.exception("social card for panel %r failed; skipped", panel)
    return cards


# --------------------------------------------------------------------------- #
# The chart panels as images
# --------------------------------------------------------------------------- #

_PANEL_W = 1200
_PANEL_PAD = 36


def panel_png(facts: WeeklyFacts, panel: Panel) -> bytes:
    """The whole *panel* (every team) as a PNG, on the Issue's paper palette."""
    if panel == "luck":
        return _luck_panel(facts)
    return _power_panel(facts)


def _luck_panel(facts: WeeklyFacts) -> bytes:
    rows = wm.luck_rows(facts)
    if not rows:
        raise ValueError("no luck data for a chart panel")
    tokens = WEEKLY_LIGHT_TOKENS
    top, pitch, bar = 64, 60, 36
    height = top + len(rows) * pitch + _PANEL_PAD
    fig, ax = base_fig(_PANEL_W, height, background=tokens["--card"])
    axis, half = 640.0, 330.0
    peak = max(abs(luck) for luck, _team in rows) or 1.0
    ax.plot([axis, axis], [top - 8, height - _PANEL_PAD + 8], color=tokens["--ink-3"], linewidth=1.5 * 72 / _DPI)
    label_font = _font("mono", 20)
    robbed, blessed = _text_width(fig, "ROBBED", label_font), _text_width(fig, "BLESSED", label_font)
    _draw_text(ax, axis - 10, 43, "ROBBED", label_font, tokens["--ink-2"], ha="right")
    _triangle(ax, axis - 10 - robbed - 14, 36, 14, "left", tokens["--ink-2"])
    _draw_text(ax, axis + 10, 43, "BLESSED", label_font, tokens["--ink-2"])
    _triangle(ax, axis + 10 + blessed + 14, 36, 14, "right", tokens["--ink-2"])
    for index, (luck, team) in enumerate(rows):
        center = top + index * pitch + pitch / 2
        _draw_text(
            ax,
            _PANEL_PAD,
            center + 8,
            _short(wm.team_label(team), _L_NAME_MAX),
            _font("sans-bold", 26),
            tokens["--ink"],
        )
        end = axis + luck / peak * half
        text = wm.signed(luck)
        value_font = _font("mono", 26)
        if luck > 0:
            _bar(ax, axis, end, center, bar, tokens["--good"])
            _draw_text(ax, end + 14, center + 9, text, value_font, tokens["--ink"])
        elif luck < 0:
            _bar(ax, end, axis, center, bar, tokens["--bad"])
            _draw_text(ax, end - 14, center + 9, text, value_font, tokens["--ink"], ha="right")
        else:
            _rect(ax, axis - 1, center - bar / 2, 2, bar, tokens["--ink-3"])
            _draw_text(ax, axis + 14, center + 9, text, value_font, tokens["--ink"])
    return _png(fig)


def _power_panel(facts: WeeklyFacts) -> bytes:
    rows = wm.power_rows(facts)
    if not rows:
        raise ValueError("no power data for a chart panel")
    tokens = WEEKLY_LIGHT_TOKENS
    top, pitch = 64, 56
    height = top + len(rows) * pitch + _PANEL_PAD
    fig, ax = base_fig(_PANEL_W, height, background=tokens["--card"])
    head = _font("mono", 18)
    for x, text, align in (
        (_PANEL_PAD + 40, "RK", "right"),
        (_PANEL_PAD + 64, "TEAM", "left"),
        (600, "W-L", "left"),
        (700, "MODEL SCORE", "left"),
        (_PANEL_W - _PANEL_PAD, "WK", "right"),
    ):
        _draw_text(ax, x, 40, text, head, tokens["--ink-2"], ha=align)
    _rect(ax, _PANEL_PAD, top - 8, _PANEL_W - 2 * _PANEL_PAD, 1.5, tokens["--line"])
    for index, row in enumerate(rows):
        center = top + index * pitch + pitch / 2
        power = row.team.season.power
        rank = str(row.published) if row.published is not None else "–"
        _draw_text(ax, _PANEL_PAD + 40, center + 9, rank, _font("mono", 26), tokens["--ink"], ha="right")
        _draw_text(
            ax,
            _PANEL_PAD + 64,
            center + 9,
            _short(wm.team_label(row.team), _L_NAME_MAX),
            _font("sans-bold", 26),
            tokens["--ink"],
        )
        record = row.team.season.record
        _draw_text(ax, 600, center + 8, wm.record(record.w, record.l, record.t), _font("mono", 24), tokens["--ink-2"])
        score = power.model_score
        width = (max(0.0, min(1.0, score)) if score is not None else 0.0) * 300
        _rect(ax, 700, center - 4, 300, 8, tokens["--barlo"])
        if width:
            _bar(ax, 700, 700 + width, center, 8, tokens["--bar"], radius=4)
        delta = power.week_delta
        right = _PANEL_W - _PANEL_PAD
        if delta:
            color = tokens["--good-text"] if delta > 0 else tokens["--bad-text"]
            _draw_text(ax, right, center + 8, str(abs(delta)), _font("mono", 24), color, ha="right")
            _triangle(ax, right - 40, center, 14, "up" if delta > 0 else "down", color)
        elif delta == 0:
            _draw_text(ax, right, center + 8, "–", _font("mono", 24), tokens["--ink-2"], ha="right")
    return _png(fig)
