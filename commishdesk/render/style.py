"""Story 4.1 — the shared visual system: palette, type stacks, number/label
formatters, and the ``<style>`` builder (AR-14 / AD-16).

One source of truth for every render surface. :mod:`commishdesk.render.web`
imports it for the self-contained web page; a future
``commishdesk.render.charts_static`` (Story 4.2) imports the same palette +
formatters for the static-PNG helper — no import cycle, no shared mutable state.

Story 5.14a adds the weekly Issue's separate Tuesday Morning token set and
:func:`build_weekly_style`, which inlines the embedded WOFF2 faces shipped in
``render/fonts/`` (read through :mod:`importlib.resources`).

Standard library only, credential-free. Numerals only: ``render`` emits digits in
data contexts, so this module carries **no** copy of
``commishdesk.facts.leads._spell`` (spec 4.1 boundary).
"""

from __future__ import annotations

import base64
import functools
from importlib import resources

__all__ = [
    "FONT_MONO",
    "FONT_SANS",
    "FONT_SERIF",
    "LIGHT_HEX",
    "POSITIONS",
    "POSITION_VAR",
    "REACH_HEX",
    "VALUE_HEX",
    "WEEKLY_DARK_TOKENS",
    "WEEKLY_FONT_BODY",
    "WEEKLY_FONT_DISPLAY",
    "WEEKLY_FONT_FILES",
    "WEEKLY_LIGHT_TOKENS",
    "build_style",
    "build_weekly_style",
    "weekly_font_faces",
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
.chart-legend .sw-fair {{ background: var(--ink-3); }}
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


def _themed_tokens(light: dict[str, str], dark: dict[str, str]) -> str:
    """The three-block light / dark token pattern every stylesheet here shares:
    light on bare ``:root``, dark inside ``@media (prefers-color-scheme: dark)``
    (unless ``[data-theme="light"]`` pins light), and dark again on
    ``:root[data-theme="dark"]``."""
    return "\n".join(
        [
            ":root {",
            "  color-scheme: light dark;",
            _emit_tokens(light),
            "}",
            "@media (prefers-color-scheme: dark) {",
            '  :root:not([data-theme="light"]) {',
            _emit_tokens(dark, indent="    "),
            "  }",
            "}",
            ':root[data-theme="dark"] {',
            _emit_tokens(dark),
            "}",
        ]
    )


def build_style() -> str:
    """The full inline stylesheet (CSS text, no ``<style>`` tags) shared by every
    render surface — light on bare ``:root``, an
    ``@media (prefers-color-scheme: dark)`` block, and a ``[data-theme="dark"]``
    block, with ``[data-theme="light"]`` winning back by never being overridden."""
    return "\n".join([_themed_tokens(_LIGHT_TOKENS, _DARK_TOKENS), _BASE_CSS])


# --------------------------------------------------------------------------- #
# Story 5.14a — the weekly Issue's "Tuesday Morning" theme (values only) and
# its embedded type. A separate token set: the draft-recap tokens above are
# untouched.
# --------------------------------------------------------------------------- #

#: Display / body stacks for the weekly page. The named family is the embedded
#: WOFF2 face (:func:`weekly_font_faces`); system fallbacks follow.
WEEKLY_FONT_DISPLAY = '"Bricolage Grotesque", "Helvetica Neue", Arial, system-ui, sans-serif'
WEEKLY_FONT_BODY = '"DM Sans", "Helvetica Neue", Arial, system-ui, sans-serif'

#: Tuesday Morning, light. Surface / emphasis / status roles from the approved
#: design decisions; ``--good-text`` / ``--bad-text`` are the WCAG-AA text
#: variants (the design's good ``#1E9E6A`` and bad ``#E5484D`` are 3.41 and 3.91
#: on card, so they stay as fills and bars only). ``--good-fill`` is the
#: status fill that carries text (a stat tile, a solid chip).
WEEKLY_LIGHT_TOKENS: dict[str, str] = {
    "--paper": "#FBF7EF",
    "--paper-2": "#F4EEE0",
    "--card": "#FFFFFF",
    "--ink": "#1F2140",
    "--ink-2": "#5A5D7A",
    "--ink-3": "#9A9CB2",
    "--line": "#ECE4D4",
    "--rule": "#1F2140",
    "--emph": "#4C5BD4",
    "--on-emph": "#FFFFFF",
    "--emph-wash": "#4c5bd41c",
    "--good": "#1E9E6A",
    "--good-text": "#157F55",
    "--good-fill": "#157F55",
    "--on-good": "#FFFFFF",
    "--good-wash": "#1e9e6a16",  # lighter than the board's 1c so --good-text on it clears AA
    "--bad": "#E5484D",
    "--bad-text": "#C93338",
    "--bad-wash": "#e5484d1c",
    "--notable": "#F2B632",
    "--on-notable": "#1F2140",
    "--notable-wash": "#f2b63230",
    "--bar": "#4C5BD4",
    "--barlo": "#C9CBE0",
    "--cut": "#5A5D7A",
    "--hi": "#F0EADB",
    "--d1": "#3F6FD8",
    "--d2": "#8E52D0",
    "--d3": "#A08558",
    "--mono-bg": "#5a5d7a1c",
    "--mono-fg": "#5A5D7A",
    "--win-tint": "#F4EFFC",
    "--shadow": "0 1px 0 #ffffff inset, 0 1px 2px #1f214014",
}

#: Tuesday Morning, dark (the ``prefers-color-scheme`` variant). Paper, card,
#: ink, emphasis, good and bad are the decisions file's values; the rest are
#: the Power / Transactions boards' dark tokens.
WEEKLY_DARK_TOKENS: dict[str, str] = {
    "--paper": "#141527",
    "--paper-2": "#191A30",
    "--card": "#1D1F38",
    "--ink": "#F0EEF8",
    "--ink-2": "#B2B4CE",
    "--ink-3": "#7F819F",
    "--line": "#2C2E4C",
    "--rule": "#B2B4CE",
    "--emph": "#8C98FF",
    "--on-emph": "#141527",
    "--emph-wash": "#8c98ff24",
    "--good": "#4CC38D",
    "--good-text": "#4CC38D",
    "--good-fill": "#4CC38D",
    "--on-good": "#141527",
    "--good-wash": "#4cc38d24",
    "--bad": "#FF7A7F",
    "--bad-text": "#FF7A7F",
    "--bad-wash": "#ff7a7f24",
    "--notable": "#F5C65A",
    "--on-notable": "#141527",
    "--notable-wash": "#f5c65a24",
    "--bar": "#8C98FF",
    "--barlo": "#3D4066",
    "--cut": "#B2B4CE",
    "--hi": "#24264A",
    "--d1": "#7C9BEA",
    "--d2": "#B98CE8",
    "--d3": "#C2A97A",
    "--mono-bg": "#b2b4ce24",
    "--mono-fg": "#B2B4CE",
    "--win-tint": "#26284A",
    "--shadow": "0 1px 0 #ffffff0a inset, 0 1px 2px #00000040",
}

#: The embedded faces: ``(family, weight, file under render/fonts/)``. Latin
#: subsets of the OFL families (licences beside them in ``render/fonts/``).
WEEKLY_FONT_FILES: tuple[tuple[str, int, str], ...] = (
    ("Bricolage Grotesque", 700, "BricolageGrotesque-Bold.woff2"),
    ("Bricolage Grotesque", 800, "BricolageGrotesque-ExtraBold.woff2"),
    ("DM Sans", 400, "DMSans-Regular.woff2"),
    ("DM Sans", 500, "DMSans-Medium.woff2"),
    ("DM Sans", 700, "DMSans-Bold.woff2"),
    ("IBM Plex Mono", 400, "IBMPlexMono-Regular.woff2"),
    ("IBM Plex Mono", 500, "IBMPlexMono-Medium.woff2"),
)


def _font_bytes(filename: str) -> bytes:
    """One embedded face's bytes, read from the installed package."""
    return resources.files("commishdesk.render").joinpath("fonts", filename).read_bytes()


@functools.cache
def weekly_font_faces() -> str:
    """``@font-face`` rules for every embedded face, each an inline base64
    ``data:font/woff2`` URI — the only ``url(`` the weekly page carries."""
    rules = []
    for family, weight, filename in WEEKLY_FONT_FILES:
        encoded = base64.b64encode(_font_bytes(filename)).decode("ascii")
        rules.append(
            "@font-face {"
            f' font-family: "{family}"; font-style: normal; font-weight: {weight};'
            " font-display: swap;"
            f" src: url(data:font/woff2;base64,{encoded}) format(\"woff2\"); }}"
        )
    return "\n".join(rules)


_WEEKLY_CSS = f"""
*, *::before, *::after {{ box-sizing: border-box; }}
html {{ -webkit-text-size-adjust: 100%; }}
html, body {{ max-width: 100%; overflow-x: hidden; }}
body {{
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: {WEEKLY_FONT_BODY};
  font-size: 17px;
  line-height: 1.55;
  text-rendering: optimizeLegibility;
  -webkit-font-smoothing: antialiased;
}}
.paper {{
  max-width: 1180px;
  margin: 0 auto;
  padding: clamp(20px, 4vw, 48px) clamp(16px, 4vw, 48px) 64px;
  display: flex;
  flex-direction: column;
  gap: 44px;
}}
.mono {{ font-family: {FONT_MONO}; }}
.num {{ font-variant-numeric: tabular-nums; }}

/* masthead */
.masthead {{
  border-bottom: 3px solid var(--rule);
  padding: 8px 0 28px;
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: flex-end;
  gap: 24px 30px;
}}
.weekline {{
  margin: 0;
  font-family: {FONT_MONO};
  font-size: 12px;
  letter-spacing: .24em;
  text-transform: uppercase;
  color: var(--ink-2);
}}
.nameplate {{
  margin: 14px 0 0;
  font-family: {WEEKLY_FONT_DISPLAY};
  font-weight: 700;
  font-size: clamp(38px, 7vw, 64px);
  line-height: .95;
  letter-spacing: -.025em;
  overflow-wrap: anywhere;
}}
.tiles {{ list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 10px; }}
.tile {{ border-radius: 14px; padding: 10px 16px; min-width: 96px; }}
.tile .k {{
  display: block;
  font-family: {FONT_MONO};
  font-size: 10px;
  letter-spacing: .14em;
  text-transform: uppercase;
}}
.tile .v {{
  display: block;
  font-family: {WEEKLY_FONT_DISPLAY};
  font-weight: 700;
  font-size: 26px;
  line-height: 1.1;
  font-variant-numeric: tabular-nums;
}}
.t-good {{ background: var(--good-fill); color: var(--on-good); }}
.t-notable {{ background: var(--notable); color: var(--on-notable); }}
.t-ink {{ background: var(--ink); color: var(--paper); }}
.t-emph {{ background: var(--emph); color: var(--on-emph); }}

/* notices: unverified dateline, correction */
.notice {{
  margin: 0;
  padding: 16px 20px;
  border-radius: 14px;
  border: 2px solid var(--bad);
  background: var(--bad-wash);
}}
.notice h2 {{
  margin: 0 0 4px;
  font-family: {FONT_MONO};
  font-size: 12px;
  letter-spacing: .16em;
  text-transform: uppercase;
  color: var(--bad-text);
}}
.notice p {{ margin: 0; }}

/* section heads */
.eyebrow {{
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0;
  font-family: {FONT_MONO};
  font-size: 11.5px;
  letter-spacing: .18em;
  text-transform: uppercase;
  color: var(--ink-2);
}}
.eyebrow::before {{
  content: "";
  flex: none;
  width: 28px;
  height: 6px;
  border-radius: 3px;
  background: var(--dash, var(--emph));
}}
.dash-good {{ --dash: var(--good); }}
.dash-bad {{ --dash: var(--bad); }}
.dash-notable {{ --dash: var(--notable); }}
.dash-emph {{ --dash: var(--emph); }}
.title {{
  margin: 8px 0 18px;
  font-family: {WEEKLY_FONT_DISPLAY};
  font-weight: 700;
  font-size: 26px;
  line-height: 1.05;
  letter-spacing: -.01em;
  text-wrap: balance;
}}
.card {{ background: var(--card); border: 1px solid var(--line); border-radius: 20px; box-shadow: var(--shadow); }}
.prose {{ margin-top: 18px; color: var(--ink-2); font-size: 15.5px; }}
.prose p {{ margin: 0 0 10px; max-width: 70ch; overflow-wrap: break-word; break-inside: avoid; }}
.prose.cols {{ columns: 2 24rem; column-gap: 32px; }}
.chip {{
  display: inline-block;
  font-family: {FONT_MONO};
  font-size: 10.5px;
  letter-spacing: .08em;
  text-transform: uppercase;
  padding: 3px 10px;
  border-radius: 999px;
  white-space: nowrap;
  line-height: 1.4;
}}
.chip-emph {{ background: var(--emph); color: var(--on-emph); }}
.chip-notable {{ background: var(--notable); color: var(--on-notable); }}
.chip-ink {{ background: var(--ink); color: var(--paper); }}
.chip-good {{ background: var(--good-fill); color: var(--on-good); }}
.chip-up {{ background: var(--good-wash); color: var(--good-text); }}
.chip-down {{ background: var(--bad-wash); color: var(--bad-text); }}
.chip-plain {{ border: 1px solid var(--line); color: var(--ink-2); }}
.mg {{
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: none;
  width: 30px;
  height: 30px;
  border-radius: 10px;
  background: var(--mono-bg);
  color: var(--mono-fg);
  font-family: {WEEKLY_FONT_DISPLAY};
  font-weight: 700;
  font-size: 13px;
  letter-spacing: .02em;
}}
.mg-l {{ width: 36px; height: 36px; border-radius: 12px; font-size: 15px; }}
.mg-xl {{ width: 56px; height: 56px; border-radius: 18px; font-size: 23px; }}
.mg-xxl {{ width: 64px; height: 64px; border-radius: 20px; font-size: 26px; }}
svg.chart {{ display: block; max-width: 100%; height: auto; }}
svg .lbl {{ font-family: {FONT_MONO}; font-size: 15px; fill: var(--ink); }}
svg .lbl-2 {{ font-family: {FONT_MONO}; font-size: 14px; fill: var(--ink-2); }}
svg .lbl-b {{ font-family: {FONT_MONO}; font-size: 18px; font-weight: 500; }}
svg .lbl-big {{ font-family: {FONT_MONO}; font-size: 26px; font-weight: 500; fill: var(--ink); }}
svg .name {{ font-family: {WEEKLY_FONT_BODY}; font-size: 14px; font-weight: 700; fill: var(--ink); }}

/* lead */
.lead {{ display: grid; grid-template-columns: minmax(0, 1fr) 380px; gap: 28px; align-items: stretch; }}
.lead-main {{ padding: 34px 38px; }}
.lead-main .title {{ font-size: clamp(24px, 3vw, 30px); }}
.lead-main .prose {{ margin: 0 0 18px; font-size: 18px; line-height: 1.5; }}
.lead-side {{ display: flex; flex-direction: column; gap: 16px; }}
.keynum {{
  flex: 1;
  border-radius: 20px;
  padding: 24px 28px;
  display: flex;
  flex-direction: column;
  justify-content: center;
}}
.keynum .k {{ font-family: {FONT_MONO}; font-size: 11px; letter-spacing: .16em; text-transform: uppercase; }}
.keynum .v {{
  font-family: {WEEKLY_FONT_DISPLAY};
  font-weight: 700;
  font-size: clamp(44px, 6vw, 64px);
  line-height: .95;
  margin-top: 6px;
  font-variant-numeric: tabular-nums;
}}
.keynum .s {{ font-size: 15px; margin-top: 6px; color: var(--ink-2); }}
.kn-bad {{ background: var(--bad-wash); color: var(--bad-text); }}
.kn-emph {{ background: var(--emph-wash); color: var(--emph); }}
.kn-notable {{ background: var(--notable-wash); color: var(--ink); }}
.teamcard {{ padding: 20px 28px; display: flex; align-items: center; gap: 16px; }}
.teamcard .n {{ font-family: {WEEKLY_FONT_DISPLAY}; font-weight: 700; font-size: 22px; line-height: 1.1; }}
.teamcard .m {{ font-size: 14px; color: var(--ink-2); }}
.lead-type {{ display: flex; flex-wrap: wrap; align-items: baseline; gap: 12px 28px; margin-bottom: 18px; }}
.lead-type .hook {{
  flex: 1 1 24rem;
  margin: 8px 0 0;
  font-family: {WEEKLY_FONT_DISPLAY};
  font-weight: 800;
  font-size: clamp(28px, 4vw, 40px);
  line-height: 1.05;
  letter-spacing: -.015em;
}}
.lead-type .key {{
  margin: 0;
  font-family: {WEEKLY_FONT_DISPLAY};
  font-weight: 700;
  font-size: clamp(44px, 6vw, 64px);
  line-height: 1;
  color: var(--emph);
  font-variant-numeric: tabular-nums;
}}
.chart-note {{ margin: 6px 0 0; font-family: {FONT_MONO}; font-size: 11.5px; color: var(--ink-2); }}

/* awards */
.awards {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; margin-top: 16px; }}
.award {{
  border-radius: 20px;
  padding: 22px 24px;
  min-height: 176px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 12px;
}}
.award .k {{ font-family: {FONT_MONO}; font-size: 11px; letter-spacing: .16em; text-transform: uppercase; }}
.award .v {{
  font-family: {WEEKLY_FONT_DISPLAY};
  font-weight: 700;
  font-size: clamp(40px, 4.4vw, 56px);
  line-height: .95;
  font-variant-numeric: tabular-nums;
}}
.award .n {{ font-weight: 700; font-size: 16px; margin-top: 6px; color: var(--ink); overflow-wrap: anywhere; }}
.award .s {{ font-size: 13px; color: var(--ink-2); }}
.aw-good {{ background: var(--good-wash); color: var(--good-text); }}
.aw-emph {{ background: var(--emph-wash); color: var(--emph); }}
.aw-bad {{ background: var(--bad-wash); color: var(--bad-text); }}
.aw-notable {{ background: var(--notable-wash); color: var(--ink); }}

/* game and next-week cards */
.grid3 {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px; }}
.game {{
  min-height: 184px;
  padding: 18px 22px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 10px;
}}
.game-top {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; min-height: 24px; }}
.game-top .by {{
  font-family: {FONT_MONO};
  font-size: 11px;
  letter-spacing: .12em;
  text-transform: uppercase;
  color: var(--ink-2);
}}
.side {{ display: flex; align-items: center; gap: 12px; padding: 0 10px; }}
.side .tn {{ flex: 1; min-width: 0; overflow-wrap: anywhere; font-size: 15px; font-weight: 500; color: var(--ink-2); }}
.side .sc {{
  font-family: {WEEKLY_FONT_DISPLAY};
  font-weight: 700;
  font-size: 22px;
  color: var(--ink-2);
  font-variant-numeric: tabular-nums;
}}
.side.win {{ background: var(--win-tint); border-radius: 12px; padding: 8px 10px; }}
.side.win .tn {{ font-weight: 700; font-size: 16px; color: var(--ink); }}
.side.win .sc {{ font-size: 28px; color: var(--ink); }}

/* standings and power */
.table-card {{ padding: 30px; }}
.st-row {{
  display: grid;
  grid-template-columns: 26px 30px minmax(0, 190px) 46px minmax(48px, 1fr) 50px 64px;
  align-items: center;
  gap: 12px;
  min-height: 42px;
}}
.st-row .rk {{ font-family: {WEEKLY_FONT_DISPLAY}; font-weight: 700; font-size: 20px; text-align: right; }}
.st-row .tn {{ font-weight: 700; font-size: 15px; overflow-wrap: anywhere; }}
.st-row .rec, .st-row .pf {{ font-family: {FONT_MONO}; font-size: 13px; color: var(--ink-2); }}
.st-row .pf {{ text-align: right; font-size: 12.5px; }}
.st-row .tag {{ text-align: right; }}
.st-row.below .rk {{ color: var(--ink-2); }}
.st-row.below .tn {{ font-weight: 500; color: var(--ink-2); }}
.bar {{ display: block; height: 12px; background: var(--hi); border-radius: 6px; overflow: hidden; }}
.bar > i {{ display: block; height: 100%; border-radius: 6px; background: var(--bar); }}
.below .bar > i {{ background: var(--barlo); }}
.bar.thin {{ height: 8px; border-radius: 4px; }}
.cutline {{
  display: flex;
  align-items: center;
  gap: 10px;
  height: 30px;
  font-family: {FONT_MONO};
  font-size: 11px;
  letter-spacing: .16em;
  text-transform: uppercase;
  color: var(--ink-2);
}}
.cutline::before, .cutline::after {{ content: ""; flex: 1; border-top: 2px dashed var(--cut); }}
.dot {{
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 8px;
  vertical-align: middle;
}}
.dot-d1 {{ background: var(--d1); }}
.dot-d2 {{ background: var(--d2); }}
.dot-d3 {{ background: var(--d3); }}
.legend {{
  display: flex;
  flex-wrap: wrap;
  gap: 6px 18px;
  margin: 14px 0 0;
  font-family: {FONT_MONO};
  font-size: 11px;
  letter-spacing: .06em;
  color: var(--ink-2);
}}
.pw-head, .pw-row {{
  display: grid;
  grid-template-columns: 32px minmax(0, 200px) 46px 56px minmax(48px, 1fr) 34px;
  align-items: center;
  gap: 12px;
}}
.pw-head {{
  margin-top: 4px;
  font-family: {FONT_MONO};
  font-size: 11px;
  letter-spacing: .18em;
  text-transform: uppercase;
  color: var(--ink-2);
}}
.pw-item {{ padding: 6px 0; border-top: 1px solid var(--line); }}
.pw-row {{ min-height: 30px; }}
.pw-row .rk {{ font-family: {WEEKLY_FONT_DISPLAY}; font-weight: 800; font-size: 18px; text-align: right; }}
.pw-row .tn {{ font-family: {WEEKLY_FONT_DISPLAY}; font-weight: 700; font-size: 15px; overflow-wrap: anywhere; }}
.pw-row .rec, .pw-row .avg, .pw-row .wk {{ font-family: {FONT_MONO}; font-size: 12.5px; color: var(--ink-2); }}
.pw-row .avg, .pw-row .wk {{ text-align: right; }}
.up {{ color: var(--good-text); }}
.down {{ color: var(--bad-text); }}
.nudge {{ margin: 4px 0 2px 44px; font-size: 13px; color: var(--ink-2); }}
.nudge .why {{ margin-left: 6px; }}
.r {{ text-align: right; }}

/* luck */
.luck-card {{ padding: 30px; }}

/* next week */
.stakes-shared {{
  margin: 0 0 16px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  font-size: 15px;
  color: var(--ink-2);
}}
.byes-next {{ margin: -6px 0 16px; font-family: {FONT_MONO}; font-size: 12px; color: var(--ink-2); }}
.nw {{ padding: 16px 22px 18px; display: flex; flex-direction: column; gap: 12px; }}
.nw.gotw {{ border: 2px solid var(--emph); }}
.nw-chips {{ display: flex; flex-wrap: wrap; gap: 6px; }}
.nw-vs {{
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
  align-items: start;
  gap: 8px;
  flex: 1;
}}
.nw-side {{ text-align: center; }}
.nw-side .tn {{ font-weight: 700; font-size: 15px; margin-top: 10px; line-height: 1.2; overflow-wrap: anywhere; }}
.nw-side .meta {{ font-family: {FONT_MONO}; font-size: 12px; color: var(--ink-2); margin-top: 2px; }}
.nw-vs .vs {{
  align-self: start;
  margin-top: 16px;
  font-family: {WEEKLY_FONT_DISPLAY};
  font-weight: 700;
  font-size: 26px;
  color: var(--ink-2);
}}
.byeline {{ margin: 0; font-size: 13px; color: var(--ink-2); }}
.byeline b {{
  font-family: {FONT_MONO};
  font-size: 11px;
  letter-spacing: .1em;
  text-transform: uppercase;
  color: var(--ink);
}}

/* transactions */
.tx {{ padding: 18px 20px; }}
.tx-top {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; }}
.lab {{
  font-family: {FONT_MONO};
  font-size: 11px;
  letter-spacing: .18em;
  text-transform: uppercase;
  color: var(--ink-2);
}}
.tx .tn {{
  font-family: {WEEKLY_FONT_DISPLAY};
  font-weight: 700;
  font-size: 17px;
  margin-top: 8px;
  overflow-wrap: anywhere;
}}
.tx ul {{ list-style: none; margin: 8px 0 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }}
.tx li.mono {{ font-size: 12.5px; }}
.tx .pos {{ font-family: {FONT_MONO}; font-size: 12px; color: var(--ink-2); }}
.trade {{
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
  gap: 20px;
  margin-top: 10px;
  align-items: start;
}}
.trade .tn {{ font-size: 15px; margin-top: 0; }}
.trade .swap {{ font-family: {FONT_MONO}; color: var(--ink-2); margin-top: 22px; }}
.trade ul, .trade-multi ul {{ margin-top: 2px; }}
.trade-multi {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 16px;
  margin-top: 10px;
}}
.trade-multi .tn {{ font-size: 15px; margin-top: 0; }}

footer {{
  padding-top: 18px;
  border-top: 1px solid var(--line);
  font-family: {FONT_MONO};
  font-size: 11px;
  line-height: 1.7;
  color: var(--ink-2);
}}
footer p {{ margin: 0; }}

@media (max-width: 900px) {{
  .lead {{ grid-template-columns: minmax(0, 1fr); }}
  .awards {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
}}
@media (max-width: 560px) {{
  body {{ font-size: 16px; }}
  .lead-main, .table-card, .luck-card {{ padding: 22px 18px; }}
  .awards {{ grid-template-columns: minmax(0, 1fr); }}
  .grid3 {{ grid-template-columns: minmax(0, 1fr); }}
  .st-row {{ grid-template-columns: 22px minmax(0, 1fr) 36px 42px auto; gap: 8px; }}
  .st-row .bar, .st-row .mg {{ display: none; }}
  .pw-head, .pw-row {{ grid-template-columns: 26px minmax(0, 1fr) 40px 48px 30px; }}
  .pw-head .ms, .pw-row .bar {{ display: none; }}
  .nudge {{ margin-left: 0; }}
}}
""".strip()


def build_weekly_style() -> str:
    """The weekly Issue's inline stylesheet (CSS text, no ``<style>`` tags):
    the embedded ``@font-face`` rules, the Tuesday Morning tokens in the same
    three-block light / dark pattern as :func:`build_style`, then the page CSS.
    Themes are values only — every rule reads a ``var(--…)`` token."""
    return "\n".join(
        [
            weekly_font_faces(),
            _themed_tokens(WEEKLY_LIGHT_TOKENS, WEEKLY_DARK_TOKENS),
            _WEEKLY_CSS,
        ]
    )
