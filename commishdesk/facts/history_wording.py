"""The one place a history callback becomes a line of words (Story 9B.4).

Every surface that prints a callback (the web Issue, the email, the plain text, the Discord post)
calls this module, so no surface words a callback itself and the same Facts give the same line
everywhere. It is pure: a :class:`~commishdesk.facts.schema.WeeklyHistoryBlock` and a
``roster_id -> label`` function in, strings out. Nothing here is read from anywhere but the block's
own keys (the closed-world rule): a number, season or label that is not a Facts key is never said.

Rules every line follows:

* Digits only, never spelled numbers.
* "all-time" appears only when the callback's ``wording`` is ``all_time``; ``seasons_on_record``
  says "in N seasons on record"; ``this_season`` says "this season". A one-season league therefore
  never prints "all-time", and a series is never printed as "0-0" (no meeting, no ``h2h``, no line).
* The wording uses no verb that must agree with a team name (a team may be one word or a plural),
  so each line reads the same for "Hail Mary Club" and "Herons".

Escaping is left to each surface: the strings here are plain text with the team labels unescaped.

This module imports only :mod:`commishdesk.facts.schema`, so both ``narrate/`` and ``render/`` can
use it without crossing the AD-1 stage fence.
"""

from __future__ import annotations

from collections.abc import Callable

from .schema import WeeklyHistoryBlock, WeeklyHistoryCallback, WeeklyHistoryMatchup

__all__ = [
    "MATCHUP_KINDS",
    "callback_line",
    "league_lines",
    "matchup_lines",
    "partial_note",
    "possessive",
    "scope_clause",
]

Label = Callable[[str | None], str]

#: The callback kinds a matchup can carry (the rest are league-level only).
MATCHUP_KINDS = frozenset(
    {
        "record_set",
        "record_tied",
        "first_win_ever",
        "first_win_since",
        "h2h_streak",
        "series_lead",
    }
)


def possessive(name: str) -> str:
    """``Herons`` -> ``Herons'``, ``Hail Mary Club`` -> ``Hail Mary Club's``."""
    return f"{name}'" if name.endswith(("s", "S")) else f"{name}'s"


def scope_clause(wording: str, seasons_on_record: int) -> str:
    """How far back a callback reaches, as a trailing phrase."""
    if wording == "all_time":
        return "all-time"
    if wording == "seasons_on_record":
        return f"in {seasons_on_record} seasons on record"
    return "this season"


def _points(value: float) -> str:
    """A score or margin: two decimals, the way the Issue prints every score."""
    return f"{value:.2f}"


def _count(value: float | None) -> str:
    return str(round(value)) if value is not None else ""


_RECORD_NOUN = {
    "highest_score": "highest score",
    "lowest_score": "lowest score",
    "biggest_margin": "biggest",
}


def _first_time(scope_wording: str, seasons_on_record: int, since: int | None) -> str:
    """``for the first time ever`` / ``since 2022`` / ``in N seasons on record``."""
    if since is not None:
        return f"for the first time since {since}"
    if scope_wording == "all_time":
        return "for the first time ever"
    return f"for the first time in {seasons_on_record} seasons on record"


def callback_line(
    block: WeeklyHistoryBlock,
    callback: WeeklyHistoryCallback,
    label: Label,
    matchup: WeeklyHistoryMatchup | None = None,
) -> str | None:
    """The line for one callback, or ``None`` when its keys do not carry enough to say it.

    *matchup* is the pairing a matchup-level callback belongs to (its series gives the numbers a
    ``series_lead`` needs); a league-level callback passes ``None``."""
    wording = callback.wording
    n = block.seasons_on_record
    scope = scope_clause(wording, n)
    who = label(callback.roster_id) if callback.roster_id else None
    opp = label(callback.opponent_roster_id) if callback.opponent_roster_id else None
    kind = callback.kind
    metric = callback.metric
    value = callback.value

    if kind in ("record_set", "record_tied") and metric is not None and value is not None and who:
        tied = kind == "record_tied"
        if metric in _RECORD_NOUN:
            noun = _RECORD_NOUN[metric]
            if metric == "biggest_margin":
                subject = f"{possessive(who)} {_points(value)}-point margin"
            else:
                subject = f"{possessive(who)} {_points(value)}"
            tail = f"ties the league's {noun}" if tied else f"is the league's {noun}"
            return f"{subject} {tail}, {scope}."
        games = _count(value)
        kind_word = "win" if metric == "win_streak" else "losing"
        streak = f"{possessive(who)} {games}-game {kind_word} streak"
        tail = "ties the league's longest" if tied else "is the league's longest"
        return f"{streak} {tail}, {scope}."

    if kind == "record_within_reach" and metric in ("win_streak", "loss_streak") and who and callback.record:
        what = "win" if metric == "win_streak" else "loss"
        longest = "win streak" if metric == "win_streak" else "losing streak"
        return (
            f"One more {what} ties {who} with the league's longest {longest}, "
            f"{_count(callback.record.value)} games, {scope}."
        )

    if kind == "h2h_streak" and who and opp and value is not None:
        return f"{who} won the last {_count(value)} meetings with {opp}, {scope}."

    if kind == "series_lead" and who and opp and matchup is not None and matchup.h2h is not None:
        h = matchup.h2h
        lead_is_a = callback.roster_id == matchup.a_roster_id
        w, l = (h.a_wins, h.b_wins) if lead_is_a else (h.b_wins, h.a_wins)  # noqa: E741 -- W-L
        score = f"{w}-{l}-{h.ties}" if h.ties else f"{w}-{l}"
        seasons = len(h.seasons)
        span = f" over {seasons} seasons" if seasons > 1 and wording == "all_time" else ""
        return f"Series: {who} {score} over {opp}{span}, {scope}."

    if kind == "first_win_ever" and who and opp:
        return f"{who} beat {opp} {_first_time(wording, n, None)}."
    if kind == "first_win_since" and who and opp and callback.since_season is not None:
        return f"{who} beat {opp} {_first_time(wording, n, callback.since_season)}."
    if kind == "first_top_ever" and who:
        return f"{who} moved into first place {_first_time(wording, n, None)}."
    if kind == "first_top_since" and who and callback.since_season is not None:
        return f"{who} moved into first place {_first_time(wording, n, callback.since_season)}."
    return None


def matchup_lines(block: WeeklyHistoryBlock | None, label: Label) -> dict[frozenset[str], str]:
    """The one line of each *this week* pairing that has one, keyed by the pairing's two roster ids.

    A pairing with no callback, or whose callback the keys cannot word, is absent: no empty
    entry, no placeholder."""
    out: dict[frozenset[str], str] = {}
    if block is None:
        return out
    for matchup in block.matchups:
        if matchup.when != "this_week" or matchup.callback is None:
            continue
        line = callback_line(block, matchup.callback, label, matchup)
        if line:
            out[frozenset((matchup.a_roster_id, matchup.b_roster_id))] = line
    return out


def league_lines(block: WeeklyHistoryBlock | None, label: Label) -> list[str]:
    """The league-level lines (at most the block's three), in the block's own order."""
    if block is None:
        return []
    lines: list[str] = []
    for callback in block.league_lines:
        line = callback_line(block, callback, label)
        if line and line not in lines:
            lines.append(line)
    return lines


def partial_note(block: WeeklyHistoryBlock | None) -> str | None:
    """The one-line note under a partial chain's history, or ``None`` when the chain is whole."""
    if block is None or block.wording != "seasons_on_record" or not block.seasons_covered:
        return None
    seasons = sorted(block.seasons_covered)
    contiguous = seasons[-1] - seasons[0] + 1 == len(seasons)
    span = f"{seasons[0]}-{seasons[-1]}" if contiguous else ", ".join(map(str, seasons))
    return f"History covers {span}. Earlier seasons aren't linked on Sleeper."
