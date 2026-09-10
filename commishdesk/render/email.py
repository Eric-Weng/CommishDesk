"""Story 4.2 — the email-deliverable render: client-safe HTML + a ``text/plain``
alternative (AD-1 / AD-2 / I4).

:func:`render_email` is a pure function of its arguments — the validated Facts
JSON plus **exactly one** narrated body (the template
:class:`~commishdesk.narrate.Recap` **or** the LLM narrator's plain-text prose) —
returning an :class:`EmailParts` ``(html, text)`` pair.

The HTML mirrors the committed reference
``brief/render-references/vintage-balls-2026-draft-recap-email.html`` *in kind*: a
~600px ``role="presentation"`` ``<table>`` layout with every visual style inlined
on the element, **no** ``<img>`` / ``<script>`` / ``<svg>`` / ``url(...)`` / CSS
custom properties / flex / grid. Charts are ``bgcolor`` nested-table bars. The
masthead declares ``color-scheme`` / ``supported-color-schemes`` and paints an
explicit ``bgcolor`` so it survives a client's dark-mode inversion without leaning
on a media query. A ``<style>`` head block carries only progressive enhancement
(an ``@media`` width tweak, an ``mso`` conditional) — never the sole copy of a
style that matters.

**No new runtime dependency.** ``epics.md`` names ``mrml`` (MJML-in-Rust); this
render is hand-authored table HTML, matching the Story 4.1 precedent and the
hand-authored committed reference. See the spec's Design Notes.

**Deterministic (I4 / AD-2).** The only time value is the caller-supplied
``generated_at`` string; sorted iteration, fixed formatting, no ``datetime.now()``
and no RNG. Identical arguments produce byte-identical ``(html, text)``.

**Pipeline fence (AD-1).** Imports the standard library, ``commishdesk.facts``
schema types, :class:`commishdesk.narrate.Recap` (+ :func:`recap_to_text`), the
shared body helpers, and :mod:`commishdesk.render.style` — nothing upstream of
``facts/``, no cloud or HTTP SDK. Every interpolated value is HTML-escaped with
Unicode bidi controls stripped first; the ``text`` part is never empty.
"""

from __future__ import annotations

from typing import NamedTuple

from commishdesk.facts.schema import DraftRecapFacts, PickRow
from commishdesk.narrate import Recap, recap_to_text
from commishdesk.render._body import (
    _esc,
    _plain,
    _sections_from_llm,
    _sections_from_recap,
    _strip_markers,
)
from commishdesk.render.style import (
    LIGHT_HEX,
    REACH_HEX,
    VALUE_HEX,
    fmt_signed,
    position_label,
)

__all__ = ["EmailParts", "render_email"]


class EmailParts(NamedTuple):
    """The two MIME bodies of a deliverable Issue: ``html`` (email-safe
    ``<table>`` markup) and ``text`` (the ``text/plain`` alternative, never
    empty). Delivery — SMTP / MIME assembly / the Send Ledger — is Stories
    4.3-4.6; this render just returns the strings."""

    html: str
    text: str


# --------------------------------------------------------------------------- #
# Palette (literal hex — email cannot resolve var(--*)) + type stacks
# --------------------------------------------------------------------------- #

_BODY_BG = LIGHT_HEX["paper-2"]  # outer wrapper
_SHEET_BG = LIGHT_HEX["paper"]  # the ~600px container
_INK = LIGHT_HEX["ink"]
_INK2 = LIGHT_HEX["ink-2"]
_INK3 = LIGHT_HEX["ink-3"]
_LINE = LIGHT_HEX["line"]
_RULE = LIGHT_HEX["rule"]

#: Verdict-chip washes — deliberately not in the shared token set (the web render
#: uses ``color-mix`` there); fixed literals keep the email output byte-stable.
_VALUE_WASH = "#E2ECE5"
_REACH_WASH = "#F1E3E0"

# Web-font <link>s are banned on this surface (a render-blocking sub-resource);
# the families are named first so a client that happens to have them uses them,
# every other falls straight through to the system stack.
_SANS = "'Archivo','Helvetica Neue',Helvetica,Arial,sans-serif"
_SERIF = "'Newsreader',Georgia,'Times New Roman',serif"
_MONO = "'Courier New',Courier,monospace"

_CONTAINER_W = 600


# --------------------------------------------------------------------------- #
# Small shared bits
# --------------------------------------------------------------------------- #


def _effective_rounds(facts: DraftRecapFacts, picks: list[PickRow]) -> int:
    """The declared ``draft.rounds`` when it is a positive int, widened to the
    largest real pick round so a missing / zero / too-small value never
    understates the draft."""
    declared = facts.draft.rounds
    declared = declared if isinstance(declared, int) and declared > 0 else 0
    return max(declared, max((pick.round for pick in picks), default=0))


def _round1(facts: DraftRecapFacts) -> list[PickRow]:
    return sorted((p for p in facts.picks if p.round == 1), key=lambda p: p.pick_no)


def _r1_split(round1_positional: dict[str, int]) -> str:
    ordered = sorted(round1_positional.items(), key=lambda kv: (-kv[1], kv[0]))
    return " / ".join(f"{count} {position_label(pos)}" for pos, count in ordered)


def _at_a_glance(
    facts: DraftRecapFacts, picks: list[PickRow], team_count: int, rounds: int
) -> str:
    bits = [f"{len(picks)} picks", f"{rounds} rounds", f"{team_count} teams"]
    r1 = facts.draft_summary.round1_positional
    if r1:
        bits.append(f"round 1 {_r1_split(r1)}")
    return "  ·  ".join(bits)


def _verdict(delta: int | None) -> tuple[str, str, str, str] | None:
    """``(label, text-colour, chip-background, extra-css)`` for the board's
    verdict chip, or ``None`` for no chip. Buckets mirror ``render/web.py`` and
    the committed reference: ``|delta| <= 2`` fair, ``delta > 2`` value (green),
    ``delta < -2`` reach (red)."""
    if delta is None:
        return None
    if delta > 2:
        return (f"value {fmt_signed(delta)}", VALUE_HEX, _VALUE_WASH, "")
    if delta < -2:
        return (f"reach {fmt_signed(delta)}", REACH_HEX, _REACH_WASH, "")
    # the fair wash is a hair off the sheet colour — a hairline border keeps the
    # chip legible where value/reach lean on their coloured fill instead
    return (f"fair {fmt_signed(delta)}", _INK3, _BODY_BG, f"border:1px solid {_LINE};")


# --------------------------------------------------------------------------- #
# HTML builders — every one returns table rows for the ~600px container
# --------------------------------------------------------------------------- #


def _email_masthead(
    facts: DraftRecapFacts, picks: list[PickRow], team_count: int, rounds: int
) -> str:
    league = facts.league
    kind = (facts.draft.type or "").strip()
    line2 = f"{rounds} Rounds"
    if kind:
        line2 += f" &middot; {_esc(kind.title())}"
    dateline = (
        f"{_esc(league.season)} Season &middot; {team_count} Teams &middot; "
        f"{_esc(league.format.scoring_label)}"
    )
    glance = _esc(_at_a_glance(facts, picks, team_count, rounds))
    return (
        # masthead — explicit bgcolor + inline colours so dark-mode inversion
        # cannot strand it; no media query carries its base appearance
        f'<tr><td class="px" bgcolor="{_SHEET_BG}" style="padding:40px 48px 0;'
        f'text-align:center;background-color:{_SHEET_BG};">'
        f'<div style="font-family:{_MONO};font-size:10px;letter-spacing:2px;'
        f'text-transform:uppercase;color:{_INK3};padding-bottom:14px;">Draft Recap</div>'
        f'<div style="font-family:{_SANS};font-weight:bold;font-size:44px;'
        f'line-height:1.0;letter-spacing:-1px;text-transform:uppercase;color:{_INK};">'
        f"{_esc(league.name)}</div>"
        f'<div style="font-family:{_MONO};font-size:10.5px;letter-spacing:1px;'
        f'text-transform:uppercase;color:{_INK2};padding-top:16px;line-height:1.7;">'
        f"{dateline}<br>{line2}</div>"
        f"</td></tr>"
        # heavy + hair rule pair, then the at-a-glance strip
        f'<tr><td class="px" style="padding:20px 48px 0;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td style="border-top:3px solid {_RULE};font-size:0;line-height:0;">&nbsp;</td>'
        f"</tr></table>"
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td style="border-top:1px solid {_LINE};padding-top:3px;font-size:0;'
        f'line-height:0;">&nbsp;</td>'
        f"</tr></table>"
        f'<div style="font-family:{_MONO};font-size:10.5px;letter-spacing:1px;'
        f'color:{_INK2};padding-top:12px;text-align:center;">{glance}</div>'
        f"</td></tr>"
    )


def _email_body(sections: list[tuple[str | None, list[str]]]) -> str:
    rows: list[str] = []
    for heading, blocks in sections:
        inner: list[str] = []
        if heading:
            inner.append(
                f'<div style="font-family:{_SANS};font-weight:bold;font-size:22px;'
                f"line-height:1.15;letter-spacing:-0.3px;color:{_INK};"
                f'padding:0 0 14px;">{_esc(heading)}</div>'
            )
        for block in blocks:
            inner.append(
                f'<p style="margin:0 0 16px;font-family:{_SERIF};font-size:17px;'
                f'line-height:1.6;color:{_INK};">{_esc(block)}</p>'
            )
        if inner:
            rows.append(
                '<tr><td class="px" style="padding:36px 48px 0;">'
                + "".join(inner)
                + "</td></tr>"
            )
    return "".join(rows)


def _section_head(label: str) -> str:
    return (
        f'<div style="font-family:{_MONO};font-size:10px;letter-spacing:3px;'
        f'text-transform:uppercase;color:{_INK3};padding-bottom:12px;">{_esc(label)}</div>'
    )


def _empty_state(text: str) -> str:
    return (
        f'<p style="margin:0;font-family:{_SERIF};font-size:16px;color:{_INK2};'
        f'font-style:italic;">{_esc(text)}</p>'
    )


def _board_table(facts: DraftRecapFacts) -> str:
    r1 = _round1(facts)
    head = '<tr><td class="px" style="padding:40px 48px 0;">' + _section_head(
        "The Board — Round 1"
    )
    if not r1:
        return head + _empty_state("No picks landed on the board.") + "</td></tr>"

    th = (
        f"padding:0 8px 7px 0;border-bottom:2px solid {_RULE};font-family:{_MONO};"
        f"font-size:9px;letter-spacing:1px;text-transform:uppercase;color:{_INK3};"
    )
    out = [
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="font-family:{_SERIF};"><tr>'
        f'<td style="{th}">Pk</td>'
        f'<td style="{th}">Team &amp; Player</td>'
        f'<td align="right" style="{th}">Verdict</td></tr>'
    ]
    for index, pick in enumerate(r1):
        border = (
            f"2px solid {_RULE}" if index == len(r1) - 1 else f"1px solid {_LINE}"
        )
        verdict = _verdict(pick.delta)
        if verdict is None:
            chip = "&nbsp;"
        else:
            label, fg, bg, extra = verdict
            chip = (
                f'<span style="font-family:{_MONO};font-size:9px;font-weight:bold;'
                f"letter-spacing:1px;text-transform:uppercase;color:{fg};"
                f'background-color:{bg};{extra}padding:3px 6px;">{_esc(label)}</span>'
            )
        pos = _esc(position_label(pick.player.position))
        manager = _esc(pick.manager or "an unclaimed roster")
        out.append(
            f"<tr>"
            f'<td style="padding:10px 8px 10px 0;border-bottom:{border};'
            f"font-family:{_MONO};font-weight:bold;font-size:13px;color:{_INK};"
            f'vertical-align:top;">{_esc(pick.board_label)}</td>'
            f'<td style="padding:10px 8px;border-bottom:{border};vertical-align:top;">'
            f'<b style="font-family:{_SANS};font-size:14px;color:{_INK};">{manager}</b><br>'
            f'<span style="font-family:{_SERIF};font-size:14px;color:{_INK2};">'
            f"{_esc(pick.player.name)}</span> "
            f'<span style="font-family:{_MONO};font-size:10px;color:{_INK3};">{pos}</span>'
            f"</td>"
            f'<td align="right" style="padding:10px 0 10px 8px;border-bottom:{border};'
            f'vertical-align:top;">{chip}</td>'
            f"</tr>"
        )
    out.append("</table>")
    return head + "".join(out) + "</td></tr>"


def _pick_count_bars(facts: DraftRecapFacts) -> str:
    rank = list(facts.draft_summary.pick_count_rank)
    head = '<tr><td class="px" style="padding:40px 48px 0;">' + _section_head(
        "Picks Per Team"
    )
    if not rank:
        return head + _empty_state("No pick-count data on the board.") + "</td></tr>"

    top = max((row.pick_count for row in rank), default=0) or 1
    out = [
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="font-family:{_SANS};">'
    ]
    for row in rank:
        filled = min(100, max(1, round(100 * row.pick_count / top)))
        rest = 100 - filled
        name = _esc(row.manager or "an unclaimed roster")
        bar = (
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
            f'<td width="{filled}%" bgcolor="{_INK2}" style="width:{filled}%;'
            f'background-color:{_INK2};height:12px;font-size:1px;line-height:12px;">&nbsp;</td>'
        )
        if rest > 0:
            bar += (
                f'<td width="{rest}%" style="width:{rest}%;font-size:1px;'
                f'line-height:12px;">&nbsp;</td>'
            )
        bar += "</tr></table>"
        out.append(
            f"<tr>"
            f'<td width="150" style="width:150px;font-weight:bold;font-size:13px;'
            f'color:{_INK};text-align:right;padding:4px 10px 4px 0;">{name}</td>'
            f'<td style="padding:4px 0;">{bar}</td>'
            f'<td width="34" style="width:34px;font-family:{_MONO};font-size:12px;'
            f'font-weight:bold;color:{_INK2};text-align:right;padding-left:8px;">'
            f"{row.pick_count}</td>"
            f"</tr>"
        )
    out.append("</table>")
    return head + "".join(out) + "</td></tr>"


def _footer(
    facts: DraftRecapFacts, team_count: int, rounds: int, generated_at: str
) -> str:
    league = facts.league
    consensus = facts.consensus_source.name
    against = f" against {_esc(consensus)}" if consensus else ""
    stamp = (
        f"{_esc(league.name)} &middot; {_esc(league.season)} &middot; {team_count} teams "
        f"&middot; {rounds} rounds &middot; generated {_esc(generated_at)}"
    )
    return (
        '<tr><td class="px" style="padding:44px 48px 44px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td style="border-top:1px solid {_LINE};padding-top:20px;font-family:{_MONO};'
        f'font-size:11px;line-height:1.7;color:{_INK3};">'
        f"Every pick and board label is drawn straight from the draft record; consensus "
        f"deltas and grades are the engine&rsquo;s own, measured{against}."
        f'<div style="color:{_INK2};padding-top:10px;">{stamp}</div>'
        f"</td></tr></table></td></tr>"
    )


_STYLE_BLOCK = (
    "body{margin:0;padding:0;width:100%;"
    "-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;}"
    "table{border-collapse:collapse;mso-table-lspace:0pt;mso-table-rspace:0pt;}"
    "@media only screen and (max-width:620px){"
    ".container{width:100%!important;}"
    ".px{padding-left:22px!important;padding-right:22px!important;}"
    "}"
)


def _document(title: str, preheader: str, rows: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en" xmlns="http://www.w3.org/1999/xhtml">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta http-equiv="X-UA-Compatible" content="IE=edge">\n'
        '<meta name="color-scheme" content="light">\n'
        '<meta name="supported-color-schemes" content="light">\n'
        f"<title>{title}</title>\n"
        "<!--[if mso]>\n<style>table{border-collapse:collapse;}</style>\n<![endif]-->\n"
        f"<style>\n{_STYLE_BLOCK}\n</style>\n"
        "</head>\n"
        f'<body style="margin:0;padding:0;background-color:{_BODY_BG};">\n'
        f'<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;'
        f'font-size:1px;line-height:1px;color:{_BODY_BG};">{preheader}</div>\n'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background-color:{_BODY_BG};">\n'
        '<tr><td align="center" style="padding:24px 12px 40px;">\n'
        f'<table role="presentation" class="container" width="{_CONTAINER_W}" '
        f'cellpadding="0" cellspacing="0" style="width:{_CONTAINER_W}px;'
        f'max-width:{_CONTAINER_W}px;background-color:{_SHEET_BG};">\n'
        f"{rows}\n"
        "</table>\n"
        "</td></tr>\n"
        "</table>\n"
        "</body>\n"
        "</html>\n"
    )


# --------------------------------------------------------------------------- #
# text/plain part
# --------------------------------------------------------------------------- #


def _plain_board(facts: DraftRecapFacts) -> str:
    r1 = _round1(facts)
    lines = ["THE BOARD"]
    if not r1:
        lines.append("No picks landed on the board.")
        return "\n".join(lines)
    for pick in r1:
        pos = position_label(pick.player.position)
        tail = f"  {fmt_signed(pick.delta)}" if pick.delta is not None else ""
        manager = pick.manager or "an unclaimed roster"
        lines.append(
            f"{pick.board_label}  {manager} — {pick.player.name} ({pos}){tail}"
        )
    return "\n".join(lines)


def _plain_counts(facts: DraftRecapFacts) -> str:
    rank = list(facts.draft_summary.pick_count_rank)
    lines = ["PICKS PER TEAM"]
    if not rank:
        lines.append("No pick-count data on the board.")
        return "\n".join(lines)
    for row in rank:
        lines.append(f"{row.manager or 'an unclaimed roster'}  {row.pick_count}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def render_email(
    facts: DraftRecapFacts,
    *,
    recap: Recap | None = None,
    llm_text: str | None = None,
    generated_at: str,
) -> EmailParts:
    """Render the draft recap as an :class:`EmailParts` ``(html, text)`` pair.

    **Exactly one** of ``recap`` (the template narrator's structured
    :class:`~commishdesk.narrate.Recap`) or ``llm_text`` (the validated LLM prose)
    must be supplied — passing neither or both raises :class:`ValueError` (the same
    shape as ``render_web``). The masthead title is the league name + season;
    ``llm_text`` is body prose only — a leading ``## `` is a section header, never
    the title. ``generated_at`` is the only time value; identical arguments give
    byte-identical output.
    """
    if (recap is None) == (llm_text is None):
        which = "neither" if recap is None else "both"
        raise ValueError(
            f"render_email needs exactly one of recap= or llm_text= (got {which})"
        )

    league = facts.league
    picks = sorted(facts.picks, key=lambda p: p.pick_no)
    team_count = league.format.team_count
    rounds = _effective_rounds(facts, picks)

    sections = (
        _sections_from_recap(recap)
        if recap is not None
        else _sections_from_llm(llm_text or "")
    )

    title = _esc(f"{league.name} — {league.season} Draft Recap")
    preheader = _esc(_at_a_glance(facts, picks, team_count, rounds))

    rows = (
        _email_masthead(facts, picks, team_count, rounds)
        + _email_body(sections)
        + _board_table(facts)
        + _pick_count_bars(facts)
        + _footer(facts, team_count, rounds, generated_at)
    )
    html_doc = _document(title, preheader, rows)

    # The text/plain identity block. The template narrator's ``recap_to_text``
    # already opens with a title line + a dateline line; the LLM branch has only
    # stripped prose, so prepend the same two lines in the same format (AC: the
    # text part must convey the masthead).
    if recap is not None:
        identity = recap_to_text(recap).rstrip("\n")
    else:
        title_line = f"{league.name} — {league.season} Draft Recap"
        dateline = (
            f"{league.name} · {league.season} season · {league.format.scoring_label}"
        )
        body_prose = _strip_markers(llm_text or "").strip("\n")
        identity = f"{title_line}\n{dateline}\n\n{body_prose}"

    text = _plain(
        f"generated {generated_at}\n\n"
        + identity
        + "\n\n"
        + _plain_board(facts)
        + "\n\n"
        + _plain_counts(facts)
        + "\n"
    )

    return EmailParts(html_doc, text)
