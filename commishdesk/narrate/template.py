"""Stage 4 — the zero-credential template narrator (AD-1 / AD-4 / I4).

:func:`render_draft_recap` turns the sanitized ``narration`` projection of the
Facts JSON into a :class:`Recap` — a title, a dateline, and the six reference
sections (lead, board round 1, superlatives, team grades, positional read, and
"the picks we'll be arguing about in December"). :func:`recap_to_text` flattens a
:class:`Recap` to plain prose for stdout.

**AD-1 pipeline-isolation fence.** This module imports only
:mod:`commishdesk.facts` schema types + the standard library. It reads **only**
:attr:`DraftRecapFacts.narration` — never ``.picks``, ``.teams``, or any raw
board column. There is no LLM, no prompt, no network, and no clock here: the
template narrator is the deterministic floor the LLM narrator (Epic 3) layers on
top of. Two calls on one ``narration`` return an equal ``model_dump()``.

The small integer-to-words helper :func:`_spell` is a copy of
``commishdesk.facts.leads._spell`` — copied, not imported, so the fence holds.
The "December" section is derived from ``superlatives`` + ``lead_candidates``
(there are no ``storyline_candidates`` until Story 3.1).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from commishdesk.facts.schema import (
    BoardPick,
    BoldestSwing,
    Narration,
    PositionalRunsSummary,
    Superlatives,
    SuperlativePick,
)

__all__ = ["Recap", "Section", "recap_to_text", "render_draft_recap"]


# --------------------------------------------------------------------------- #
# Output model
# --------------------------------------------------------------------------- #


class _Frozen(BaseModel):
    """Immutable, closed to unknown keys — matches the house model style."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Section(_Frozen):
    """One recap section: a heading and its ordered prose blocks."""

    heading: str
    blocks: list[str] = []


class Recap(_Frozen):
    """A rendered draft recap: a title, a one-line dateline, and the sections."""

    title: str
    dateline: str
    sections: list[Section] = []


# --------------------------------------------------------------------------- #
# Number words — a copy of commishdesk.facts.leads._spell (AD-1: do NOT import)
# --------------------------------------------------------------------------- #

_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
)


def _spell(n: int) -> str:
    """Small non-negative integers as words (``5`` -> ``"five"``); anything out of
    range falls back to the digits."""
    return _ONES[n] if 0 <= n < len(_ONES) else str(n)


_POSITION_PLURAL = {
    "QB": "quarterbacks",
    "RB": "running backs",
    "WR": "wide receivers",
    "TE": "tight ends",
    "K": "kickers",
    "DEF": "defenses",
    "DST": "defenses",
}


_UNLISTED = "unlisted picks"


def _plural(position: str | None) -> str:
    if not position or position == "UNK":  # the facts sentinel is never shown raw
        return _UNLISTED
    return _POSITION_PLURAL.get(position, f"{position}s")


def _count(n: int, singular: str) -> str:
    """``_count(1, "pick")`` -> ``"one pick"``; ``_count(3, "pick")`` -> ``"three
    picks"`` — a spelled count with the noun agreeing in number."""
    return f"{_spell(n)} {singular if n == 1 else singular + 's'}"


def _position_count(n: int, position: str | None) -> str:
    """``two quarterbacks`` / ``one running back`` — a spelled position count with
    the noun agreeing in number."""
    plural = _plural(position)
    noun = plural[:-1] if n == 1 and plural.endswith("s") else plural
    return f"{_spell(n)} {noun}"


def _join_names(names: list[str]) -> str:
    """``["a"]`` -> ``"a"``; ``["a", "b"]`` -> ``"a and b"``; longer -> Oxford list."""
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])}, and {names[-1]}"


def _delta_phrase(delta: int) -> str:
    """``delta = pick_no - consensus_slot``: positive = value (the player fell),
    negative = reach (the picker jumped early). Renders it as readable prose."""
    if delta == 0:
        return "right on the number"
    magnitude = abs(delta)
    slots = "slot" if magnitude == 1 else "slots"
    if delta > 0:
        return f"{_spell(magnitude)} {slots} of value"
    return f"{_spell(magnitude)} {slots} early"


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #


def render_draft_recap(narration: Narration) -> Recap:
    """Build the six-section :class:`Recap` from ``narration`` alone.

    Pure and deterministic — no timestamp, no network, no model. The caller (the
    CLI) stamps the ``generated_at`` into the dateline; the narrator does not see
    it (it is not part of the ``narration`` projection).
    """
    league = narration.league
    title = f"{league.name} — {league.season} Draft Recap"
    dateline = f"{league.name} · {league.season} season · {league.scoring_label}"
    sections = [
        _lead_section(narration),
        _board_section(narration),
        _superlatives_section(narration),
        _grades_section(narration),
        _positional_section(narration.positional_runs),
        _december_section(narration),
    ]
    return Recap(title=title, dateline=dateline, sections=sections)


def recap_to_text(recap: Recap) -> str:
    """Flatten a :class:`Recap` to plain UTF-8 prose (``\\n`` newlines)."""
    lines: list[str] = [recap.title, recap.dateline]
    for section in recap.sections:
        lines.append("")
        lines.append(f"## {section.heading}")
        for block in section.blocks:
            lines.append("")
            lines.append(block)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Section builders — each a pure function of the narration
# --------------------------------------------------------------------------- #


def _r1_breakdown(r1_positional: dict[str, int]) -> str:
    ordered = sorted(r1_positional.items(), key=lambda kv: (-kv[1], kv[0]))
    return _join_names([_position_count(count, pos) for pos, count in ordered])


def _lead_section(narration: Narration) -> Section:
    hn = narration.headline_numbers
    blocks: list[str] = []

    rounds_txt = f" over {_count(hn.rounds, 'round')}" if hn.rounds else ""
    opener = f"{_count(hn.picks_total, 'pick').capitalize()}{rounds_txt}."
    if hn.r1_positional:
        opener += f" Round 1 went {_r1_breakdown(hn.r1_positional)}."
    blocks.append(opener)

    leader, low = hn.pick_count_leader, hn.pick_count_low
    if leader and low and leader.manager and low.manager and leader.manager != low.manager:
        blocks.append(
            f"{leader.manager} made the most picks with {_spell(leader.pick_count)}; "
            f"{low.manager} made the fewest with {_spell(low.pick_count)}."
        )

    for candidate in narration.lead_candidates:
        if candidate.hook:
            blocks.append(candidate.hook)

    if not blocks:
        blocks.append("This draft has no picks to recap.")
    return Section(heading="The Lead", blocks=blocks)


def _board_line(pick: BoardPick) -> str:
    who = pick.manager if pick.manager else "an unclaimed roster"
    line = f"{pick.board_label} — {who}: {pick.player}"
    if pick.position:
        line += f", {pick.position}"
    line += "."
    if pick.consensus_label is not None and pick.delta is not None:
        line += f" Consensus had it at {pick.consensus_label} ({_delta_phrase(pick.delta)})."
    else:
        line += " No consensus slot for this player."
    return line


def _board_section(narration: Narration) -> Section:
    blocks = [_board_line(pick) for pick in narration.board_round1]
    if not blocks:
        blocks.append("Round 1 has no picks on the board.")
    return Section(heading="The Board — Round 1", blocks=blocks)


def _superlative_phrase(pick: SuperlativePick) -> str:
    who = f" to {pick.manager}" if pick.manager else ""
    position = f", {pick.position}," if pick.position else ""
    tail = ""
    if pick.consensus_label is not None and pick.delta is not None:
        tail = f" (consensus {pick.consensus_label}, {_delta_phrase(pick.delta)})"
    return f"{pick.player}{position}{who} at {pick.board_label}{tail}."


def _swing_phrase(swing: BoldestSwing) -> str:
    who = swing.manager or "an unclaimed roster"
    picks = swing.picks
    if len(picks) >= 2:
        first, second = picks[0], picks[1]
        return (
            f"{who}: {first.player} at {first.board_label} and {second.player} at "
            f"{second.board_label}, the widest spread between a value and a reach on "
            f"one roster."
        )
    if picks:
        return f"{who}: a draft built around {picks[0].player} at {picks[0].board_label}."
    return f"{who} made the boldest set of swings on the board."


def _superlatives_section(narration: Narration) -> Section:
    superlatives: Superlatives = narration.superlatives
    blocks: list[str] = []
    if superlatives.best_value:
        blocks.append("Best value — " + _superlative_phrase(superlatives.best_value))
    if superlatives.best_value_runner_up:
        blocks.append(
            "Best value, runner-up — "
            + _superlative_phrase(superlatives.best_value_runner_up)
        )
    if superlatives.biggest_reach:
        blocks.append("Biggest reach — " + _superlative_phrase(superlatives.biggest_reach))
    if superlatives.biggest_reach_runner_up:
        blocks.append(
            "Biggest reach, runner-up — "
            + _superlative_phrase(superlatives.biggest_reach_runner_up)
        )
    if superlatives.boldest_swing:
        blocks.append("Boldest swing — " + _swing_phrase(superlatives.boldest_swing))
    if not blocks:
        blocks.append(
            "No pick landed against a consensus slot, so there are no superlatives."
        )
    return Section(heading="Superlatives", blocks=blocks)


_GRADE_METHOD_BLOCK = (
    "Grades weigh each pick against the consensus board, the premium-position "
    "capital a roster spent, and how the roster fits the league's format; the "
    "scale runs A to F with plus/minus."
)


def _grades_section(narration: Narration) -> Section:
    blocks: list[str] = [_GRADE_METHOD_BLOCK]
    for team in narration.teams:
        if not team.manager:  # orphan-safe: never attribute a grade to no one
            continue
        counts = ", ".join(
            f"{team.positional_counts[pos]} {pos}"
            for pos in ("QB", "RB", "WR", "TE")
            if pos in team.positional_counts
        )
        detail = f" — {counts} across {_count(team.pick_count, 'pick')}" if counts else ""
        line = f"{team.manager}: {team.grade}{detail}."
        if team.grade_rationale:
            line += f" {team.grade_rationale}"
        blocks.append(line)
    if len(blocks) == 1:
        blocks.append("No roster carried a manager name to grade.")
    return Section(heading="Team Grades", blocks=blocks)


def _positional_section(runs: PositionalRunsSummary) -> Section:
    blocks: list[str] = []

    qb = runs.QB
    qb_line = (
        f"Quarterback: {_spell(qb.total)} drafted, {_spell(qb.by_end_round3)} through "
        f"the end of round 3"
    )
    if qb.first_label:
        qb_line += f" (first at {qb.first_label})"
    qb_line += "."
    if qb.left_waiting:
        qb_line += f" Still waiting after the run: {_join_names(list(qb.left_waiting))}."
    blocks.append(qb_line)

    rb = runs.RB
    rb_line = f"Running back: {_spell(rb.total)} drafted, {_spell(rb.in_round1)} in round 1"
    if rb.first_label:
        rb_line += f" (first at {rb.first_label})"
    rb_line += "."
    if rb.most_by_one_manager and rb.most_by_one_manager.manager:
        rb_line += (
            f" {rb.most_by_one_manager.manager} took the most with "
            f"{_spell(rb.most_by_one_manager.count)}."
        )
    blocks.append(rb_line)

    te = runs.TE
    te_line = f"Tight end: {_spell(te.total)} drafted."
    if te.early_window_labels:
        te_line += f" Early window: {', '.join(te.early_window_labels)}."
    if te.third_te_label and te.third_te_player:
        gap = f" — {_spell(te.gap_picks)} picks later" if te.gap_picks else ""
        te_line += (
            f" The third ({te.third_te_player}) did not go until {te.third_te_label}{gap}."
        )
    blocks.append(te_line)

    return Section(heading="Positional Read", blocks=blocks)


def _december_section(narration: Narration) -> Section:
    # Story 3.1: real ``storyline_candidates`` replace these derived sentences.
    superlatives = narration.superlatives
    blocks: list[str] = []

    swing = superlatives.boldest_swing
    if swing and len(swing.picks) >= 2:
        who = swing.manager or "an unclaimed roster"
        first, second = swing.picks[0], swing.picks[1]
        blocks.append(
            f"{who}'s swing: {first.player} at {first.board_label} against "
            f"{second.player} at {second.board_label}. One of those looks very "
            f"different by December."
        )

    reach = superlatives.biggest_reach
    if reach and reach.delta is not None and reach.delta < 0:
        who = f" by {reach.manager}" if reach.manager else ""
        blocks.append(
            f"The board's biggest reach{who}: {reach.player} at {reach.board_label}, "
            f"{_count(abs(reach.delta), 'slot')} ahead of consensus."
        )

    if not blocks:
        blocks.append("Nothing here is settled yet — check back in December.")
    return Section(
        heading="The Picks We'll Be Arguing About in December", blocks=blocks
    )
