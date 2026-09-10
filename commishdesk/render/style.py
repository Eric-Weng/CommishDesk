"""Story 4.1 — the shared visual system: palette, type stacks, number/label
formatters, and the ``<style>`` builder (AR-14 / AD-16).

One source of truth for every render surface. :mod:`commishdesk.render.web`
imports it for the self-contained web page; a future
``commishdesk.render.charts_static`` (Story 4.2) imports the same palette +
formatters for the static-PNG helper — no import cycle, no shared mutable state.

Standard library only, credential-free. Numerals only: ``render`` emits digits in
data contexts, so this module carries **no** copy of
``commishdesk.facts.leads._spell`` (spec 4.1 boundary).
"""

from __future__ import annotations

__all__ = [
    "FONT_MONO",
    "FONT_SANS",
    "FONT_SERIF",
    "LIGHT_HEX",
    "POSITIONS",
    "POSITION_VAR",
    "REACH_HEX",
    "VALUE_HEX",
    "build_style",
    "fmt_signed",
    "ordinal",
    "pct",
    "position_label",
    "position_plural",
]

#: The real minus sign (U+2212), never the hyphen-minus, for signed numerals.
_MINUS = "−"

# System-font fallback stacks. 4.1 ships these instead of ~6 bundled woff2 faces
# (~500 KB per Issue); bundling the real families in ``render/assets/`` is a
# ``deferred-work.md`` follow-up. Order mirrors the reference stylesheet.
FONT_SANS = '"Archivo", "Helvetica Neue", Arial, system-ui, sans-serif'
FONT_SERIF = '"Newsreader", Georgia, "Times New Roman", serif'
FONT_MONO = '"IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, monospace'

#: The four skill positions the draft recap charts, in board order.
POSITIONS = ("QB", "RB", "WR", "TE")

#: Position -> the CSS custom property carrying its hue (light + dark defined in
#: :func:`build_style`). Deliberately not red/green — those encode reach/value.
POSITION_VAR = {
    "QB": "var(--pos-qb)",
    "RB": "var(--pos-rb)",
    "WR": "var(--pos-wr)",
    "TE": "var(--pos-te)",
}

_POSITION_PLURAL = {
    "QB": "quarterbacks",
    "RB": "running backs",
    "WR": "wide receivers",
    "TE": "tight ends",
    "K": "kickers",
    "DEF": "defenses",
    "DST": "defenses",
}

_ORDINAL_ONES = {1: "st", 2: "nd", 3: "rd"}


# --------------------------------------------------------------------------- #
# Formatters — numerals + position labels only
# --------------------------------------------------------------------------- #


def fmt_signed(n: int) -> str:
    """A signed integer with an explicit sign and a real minus (U+2212):
    ``fmt_signed(36) == "+36"``, ``fmt_signed(-57) == "−57"``,
    ``fmt_signed(0) == "0"``."""
    if n > 0:
        return f"+{n}"
    if n < 0:
        return f"{_MINUS}{-n}"
    return "0"


def ordinal(n: int) -> str:
    """``1 -> "1st"``, ``2 -> "2nd"``, ``3 -> "3rd"``, ``11 -> "11th"``,
    ``23 -> "23rd"``."""
    n = int(n)
    if 10 <= abs(n) % 100 <= 20:
        suffix = "th"
    else:
        suffix = _ORDINAL_ONES.get(abs(n) % 10, "th")
    return f"{n}{suffix}"


def pct(part: float, whole: float) -> str:
    """``part`` as a whole-number percentage of ``whole`` (``pct(3, 4) == "75%"``);
    ``"0%"`` when ``whole`` is falsy."""
    if not whole:
        return "0%"
    return f"{round(100 * part / whole)}%"


def position_label(position: str | None) -> str:
    """The bare position code, upper-cased; ``None`` and the ``"UNK"`` sentinel
    render as an em dash (never shown raw)."""
    if not position or position.upper() == "UNK":
        return "—"
    return position.upper()


def position_plural(position: str | None) -> str:
    """``"QB" -> "quarterbacks"``; an unknown / missing position -> ``"picks"``."""
    if not position or position.upper() == "UNK":
        return "picks"
    key = position.upper()
    return _POSITION_PLURAL.get(key, f"{key.lower()}s")


# --------------------------------------------------------------------------- #
# Palette — light on bare :root, dark echoed into the media query AND the
# [data-theme="dark"] block (mirrors the reference stylesheet's three-way
# structure verbatim in spirit).
# --------------------------------------------------------------------------- #

_LIGHT_TOKENS: dict[str, str] = {
    "--paper": "#ECEEE9",
    "--paper-2": "#E3E5DE",
    "--ink": "#1E2A31",
    "--ink-2": "#46524E",
    "--ink-3": "#6C766F",
    "--line": "#CDCFC5",
    "--rule": "#1E2A31",
    "--card": "#F3F4EF",
    "--reach": "#B23A2B",
    "--value": "#2E6A48",
    "--reach-wash": "#b23a2b1f",
    "--value-wash": "#2e6a481f",
    "--pos-qb": "#4A7BA6",
    "--pos-rb": "#C58A3D",
    "--pos-wr": "#7A5EA8",
    "--pos-te": "#9A8A6B",
    "--shadow": "0 1px 0 #ffffff inset, 0 1px 2px #1e2a3114",
}

_DARK_TOKENS: dict[str, str] = {
    "--paper": "#14181A",
    "--paper-2": "#191E20",
    "--ink": "#E7E9E3",
    "--ink-2": "#AEB6B0",
    "--ink-3": "#808A83",
    "--line": "#2B3133",
    "--rule": "#6E7A73",
    "--card": "#1B2123",
    "--reach": "#E0664F",
    "--value": "#5BA97C",
    "--reach-wash": "#e0664f24",
    "--value-wash": "#5ba97c24",
    "--pos-qb": "#7BA6C8",
    "--pos-rb": "#D6A662",
    "--pos-wr": "#A78FCE",
    "--pos-te": "#BCAD8C",
    "--shadow": "0 1px 0 #ffffff0a inset, 0 1px 2px #00000040",
}


#: The **light-theme** palette as literal hex, keyed without the ``--`` prefix
#: (``LIGHT_HEX["ink-2"]``). Email cannot use ``var(--*)`` — every colour has to be
#: an inline literal — so the email render reads its palette from here rather than
#: re-typing the values. One source of truth with :data:`_LIGHT_TOKENS`.
LIGHT_HEX: dict[str, str] = {
    name.removeprefix("--"): value for name, value in _LIGHT_TOKENS.items()
}

#: Reach (red) and value (green) as literal hex, for the email verdict chips /
#: bars where ``var(--reach)`` / ``var(--value)`` cannot resolve.
REACH_HEX: str = _LIGHT_TOKENS["--reach"]
VALUE_HEX: str = _LIGHT_TOKENS["--value"]


def _emit_tokens(tokens: dict[str, str], indent: str = "  ") -> str:
    return "\n".join(f"{indent}{name}: {value};" for name, value in tokens.items())


_BASE_CSS = f"""
*, *::before, *::after {{ box-sizing: border-box; }}
html {{ -webkit-text-size-adjust: 100%; }}
html, body {{ max-width: 100%; overflow-x: hidden; }}
body {{
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: {FONT_SERIF};
  font-size: 18px;
  line-height: 1.6;
  text-rendering: optimizeLegibility;
  -webkit-font-smoothing: antialiased;
}}

.paper {{
  max-width: 820px;
  margin: 0 auto;
  padding: clamp(20px, 4vw, 56px) clamp(16px, 5vw, 56px) 72px;
  background: var(--paper);
  overflow-x: hidden;
}}

a {{ color: inherit; }}

.masthead {{ text-align: center; padding-top: 4px; }}
.masthead .kicker {{
  font-family: {FONT_MONO};
  font-size: 11px;
  letter-spacing: .24em;
  text-transform: uppercase;
  color: var(--ink-3);
  margin: 0 0 12px;
}}
.nameplate {{
  font-family: {FONT_SANS};
  font-weight: 800;
  font-size: clamp(30px, 8vw, 60px);
  line-height: .98;
  letter-spacing: -0.02em;
  text-transform: uppercase;
  margin: 0;
  text-wrap: balance;
}}
.dateline {{
  margin: 16px 0 0;
  font-family: {FONT_MONO};
  font-size: 11.5px;
  letter-spacing: .06em;
  text-transform: uppercase;
  color: var(--ink-2);
}}
.rule-heavy {{ border: 0; border-top: 3px solid var(--rule); margin: 20px 0 0; }}
.rule-hair {{ border: 0; border-top: 1px solid var(--line); margin: 3px 0 0; }}

.ataglance {{
  list-style: none;
  margin: 16px 0 0;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 6px 10px;
  font-family: {FONT_MONO};
  font-size: 11.5px;
  letter-spacing: .03em;
  color: var(--ink-2);
}}
.ataglance li {{
  padding: 4px 10px;
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 3px;
  white-space: nowrap;
}}

section {{ margin-top: 44px; }}
h2 {{
  font-family: {FONT_SANS};
  font-weight: 700;
  font-size: clamp(22px, 5vw, 30px);
  line-height: 1.1;
  letter-spacing: -0.015em;
  margin: 0 0 14px;
  text-wrap: balance;
}}
p {{ margin: 0 0 16px; max-width: 66ch; overflow-wrap: break-word; }}
p:last-child {{ margin-bottom: 0; }}
.prose p:first-of-type {{ margin-top: 0; }}

figure.figure {{
  margin: 40px 0 0;
  padding: 18px;
  background: var(--card);
  border: 1px solid var(--line);
  border-top: 3px solid var(--rule);
  box-shadow: var(--shadow);
}}
.fig-title {{
  font-family: {FONT_MONO};
  font-size: 11px;
  letter-spacing: .2em;
  text-transform: uppercase;
  color: var(--ink-3);
  margin: 0 0 6px;
}}
.fig-note {{
  font-size: 14.5px;
  line-height: 1.5;
  color: var(--ink-2);
  margin: 0 0 14px;
  max-width: 62ch;
}}
.figwrap {{
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  padding-bottom: 4px;
}}
.figwrap svg {{ display: block; height: auto; }}

svg .svg-col {{ font-family: {FONT_MONO}; font-size: 10px; fill: var(--ink-3); text-anchor: middle; }}
svg .svg-round {{ font-family: {FONT_MONO}; font-size: 10px; fill: var(--ink-3); }}
svg .svg-name {{ font-family: {FONT_SANS}; font-weight: 700; font-size: 13px; fill: var(--ink); }}
svg .svg-meta {{ font-family: {FONT_MONO}; font-size: 10px; fill: var(--ink-3); }}
svg .svg-barname {{ font-family: {FONT_SANS}; font-weight: 600; font-size: 12px; fill: var(--ink); text-anchor: end; }}
svg .svg-barnum {{ font-family: {FONT_MONO}; font-size: 12px; fill: var(--ink-2); }}
svg .svg-tlpos {{ font-family: {FONT_SANS}; font-weight: 800; font-size: 12px; fill: var(--ink-2); }}
svg .svg-tlnote {{ font-family: {FONT_MONO}; font-size: 10px; fill: var(--ink-3); }}

.chart-legend {{
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  margin: 12px 0 0;
  font-family: {FONT_MONO};
  font-size: 10.5px;
  letter-spacing: .04em;
  text-transform: uppercase;
  color: var(--ink-3);
}}
.chart-legend span {{ display: inline-flex; align-items: center; gap: 6px; }}
.chart-legend i {{ width: 12px; height: 8px; border-radius: 2px; display: inline-block; }}
.chart-legend .sw-qb {{ background: var(--pos-qb); }}
.chart-legend .sw-rb {{ background: var(--pos-rb); }}
.chart-legend .sw-wr {{ background: var(--pos-wr); }}
.chart-legend .sw-te {{ background: var(--pos-te); }}
.chart-legend .sw-value {{ background: var(--value); }}
.chart-legend .sw-reach {{ background: var(--reach); }}

footer {{
  margin-top: 56px;
  padding-top: 18px;
  border-top: 1px solid var(--line);
  font-family: {FONT_MONO};
  font-size: 11px;
  line-height: 1.7;
  color: var(--ink-3);
}}
footer .prov {{ max-width: 64ch; margin: 0 0 8px; }}
footer .stamp {{ margin: 0; color: var(--ink-2); }}

@media (max-width: 520px) {{
  body {{ font-size: 17px; }}
  .paper {{ padding-left: 14px; padding-right: 14px; }}
}}
""".strip()


def build_style() -> str:
    """The full inline stylesheet (CSS text, no ``<style>`` tags) shared by every
    render surface — light on bare ``:root``, an
    ``@media (prefers-color-scheme: dark)`` block, and a ``[data-theme="dark"]``
    block, with ``[data-theme="light"]`` winning back by never being overridden."""
    dark = _emit_tokens(_DARK_TOKENS)
    return "\n".join(
        [
            ":root {",
            "  color-scheme: light dark;",
            _emit_tokens(_LIGHT_TOKENS),
            "}",
            "@media (prefers-color-scheme: dark) {",
            '  :root:not([data-theme="light"]) {',
            _emit_tokens(_DARK_TOKENS, indent="    "),
            "  }",
            "}",
            ':root[data-theme="dark"] {',
            dark,
            "}",
            _BASE_CSS,
        ]
    )
