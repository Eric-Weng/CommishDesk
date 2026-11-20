"""The weekly Issue as plain text, with the history lines (Story 9B.4).

:func:`render_weekly_text` is :func:`~commishdesk.narrate.weekly_template.weekly_issue_to_text` plus the
history callbacks after the "Around the league" section: one line per game that has one, then a
"League history" block of up to three lines. Every line is the shared
``facts/history_wording.py`` wording, so it reads exactly like the web, email and chat lines. With no
history line the output is byte-identical to ``weekly_issue_to_text``.

The history lines are not part of the narrated prose, so they never reach the safety scan or the
claim check, which read the prose only.
"""

from __future__ import annotations

from commishdesk.facts.schema import WeeklyFacts
from commishdesk.narrate.weekly_template import WeeklyIssue, weekly_issue_to_text
from commishdesk.render import _weekly_model as wm
from commishdesk.sections import heading_for

__all__ = ["render_weekly_text"]


def render_weekly_text(facts: WeeklyFacts, issue: WeeklyIssue) -> str:
    view = wm.history_view(facts)
    if view is None:
        return weekly_issue_to_text(issue)
    extra: list[str] = []
    for matchup in facts.matchups.this_week:
        win_id, _, lose_id, _ = wm.winner_loser(matchup)
        line = view.for_game(win_id, lose_id)
        if line:
            extra += ["", line]
    if view.league:
        extra += ["", wm.HISTORY_HEADING, *(f"- {line}" for line in view.league)]
    if view.note:
        extra += ["", view.note]
    if not extra:
        return weekly_issue_to_text(issue)
    around = heading_for("around_league")
    lines: list[str] = [issue.title, issue.dateline]
    placed = False
    for section in issue.sections:
        lines += ["", f"## {section.heading}"]
        for block in section.blocks:
            lines += ["", block]
        if section.heading == around:
            lines += extra
            placed = True
    if not placed:
        lines += extra
    return "\n".join(lines) + "\n"
