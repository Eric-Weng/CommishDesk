"""Stage 3 — board-only lead angles (Story 2.6).

:func:`build_lead_candidates` ranks the story angles a draft-recap lead could open
on, computed **in code** from the stage results the builder already holds — no
LLM, no prompt (delta D7: keep editorial judgement in code, not the prompt). Each
angle is a :class:`~commishdesk.facts.schema.LeadCandidate`
(``{rank, kind, roster_ids, hook}``); the ``hook`` is a deterministic sentence
built from the numbers, factual and terminal, which the narrator may later
rewrite in voice.

Pure / deterministic / offline: a valid stage-result set in, a ranked list out.
Two calls on one input return equal ``model_dump()`` output. Every board number
is read off ``board`` / ``draft_summary`` / ``superlatives`` — nothing is
re-derived from ``league.picks`` except the single first-overall pick that
``draft_summary`` does not carry.

Ranking is the fixed kind priority in :data:`LEAD_KIND_PRIORITY`: a cross-cutting
board observation (``positional_run``) and a manager-approach angle both outrank
the single-name ``biggest_score`` floor. Only detectors that fire are emitted,
then ``rank`` is renumbered ``1..N``.

This module imports only stdlib + ``commishdesk.ingest`` + ``commishdesk.stats``
+ ``.schema`` — it stays inside the ``commishdesk/facts/*.py`` import fence
(``tests/test_facts.py::test_facts_package_import_fence``).
"""

from __future__ import annotations

from commishdesk.ingest import LeagueModel
from commishdesk.stats import BoardMetrics, ConsensusMetrics, DraftGrades

from .schema import DraftSummary, LeadCandidate, Superlatives

__all__ = ["LEAD_KIND_PRIORITY", "build_lead_candidates"]

LEAD_KIND_PRIORITY: tuple[str, ...] = (
    "positional_run",
    "manager_approach",
    "positional_hoard",
    "biggest_score",
)
"""Editorial priority order for lead angles (delta D7). A cross-cutting board
read leads; the marquee name is the floor, never the default. Every ``kind`` a
detector below emits must appear here."""

_MIN_R1_RB_RUN = 4
"""Running backs among the first ``team_count - 1`` picks, at or above this many,
is the cross-cutting board read. Gated on the same list the hook counts."""

_MIN_POSITION_HOARD = 4
"""One roster taking this many of a single real position is a lead angle."""

_UNK = "UNK"

_POSITION_PLURAL = {
    "QB": "quarterbacks",
    "RB": "running backs",
    "WR": "wide receivers",
    "TE": "tight ends",
    "K": "kickers",
    "DEF": "defenses",
    "DST": "defenses",
    "DL": "defensive linemen",
    "LB": "linebackers",
    "DB": "defensive backs",
    "IDP": "defensive players",
}

_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
)


def _spell(n: int) -> str:
    """Small non-negative integers as words (``5`` -> ``"five"``); anything out of
    range falls back to the digits. Keeps the hooks reading like prose without a
    dependency."""
    return _ONES[n] if 0 <= n < len(_ONES) else str(n)


def build_lead_candidates(
    league: LeagueModel,
    board: BoardMetrics,
    consensus: ConsensusMetrics,
    grades: DraftGrades,
    draft_summary: DraftSummary,
    superlatives: Superlatives,
) -> list[LeadCandidate]:
    """Ranked board-derived lead angles for the draft recap.

    ``consensus`` / ``grades`` / ``superlatives`` are accepted for symmetry with
    :func:`~commishdesk.facts.build.build_draft_recap_facts` and are reserved for
    the Story 3.1 storyline angles; the board angles here are derived from
    ``board`` / ``draft_summary`` / ``league``. Returns ``[]`` only for a draft
    with no picks; otherwise the ``biggest_score`` floor guarantees one entry.
    """
    approach = _manager_approach(board, draft_summary)
    angles = [
        _positional_run(league, draft_summary),
        approach,
        _positional_hoard(
            board, exclude=approach.roster_ids[0] if approach else None
        ),
        _biggest_score(league),
    ]
    fired = [a for a in angles if a is not None]
    fired.sort(key=lambda a: LEAD_KIND_PRIORITY.index(a.kind))
    return [a.model_copy(update={"rank": i}) for i, a in enumerate(fired, start=1)]


# --------------------------------------------------------------------------- #
# Detectors — each returns a LeadCandidate with a placeholder rank, or None
# --------------------------------------------------------------------------- #


def _candidate(kind: str, roster_ids: list[str], hook: str) -> LeadCandidate:
    return LeadCandidate(rank=0, kind=kind, roster_ids=roster_ids, hook=hook)


def _positional_run(
    league: LeagueModel, draft_summary: DraftSummary
) -> LeadCandidate | None:
    """A running-back cluster in the opening picks — the cross-cutting board
    read. The hook also names the round-1 quarterbacks, the counter-signal a
    human editor reaches for (the board and the format pulling opposite ways)."""
    rb_count = len(draft_summary.first11_running_backs)
    if rb_count < _MIN_R1_RB_RUN:
        return None
    window = league.format.team_count - 1
    qb_count = len(draft_summary.round1_qbs)
    hook = (
        f"{_spell(rb_count).capitalize()} of the first {_spell(window)} picks "
        f"were running backs"
    )
    if qb_count:
        noun = "manager" if qb_count == 1 else "managers"
        hook += f", and {_spell(qb_count)} {noun} took a quarterback in round 1."
    else:
        hook += "."
    return _candidate("positional_run", [], hook)


def _manager_approach(
    board: BoardMetrics, draft_summary: DraftSummary
) -> LeadCandidate | None:
    """The one manager who drafted differently — the unique pick-count leader who
    also poured an outsized share of the draft into a single round. Both signals
    are read straight off ``draft_summary`` (``round_concentration``) and
    ``board`` (``pick_count``); nothing is re-derived from ``league.picks``."""
    if not board.teams:
        return None
    top = max(t.pick_count for t in board.teams)
    leaders = [t for t in board.teams if t.pick_count == top]
    if len(leaders) != 1 or leaders[0].manager is None:
        return None
    leader = leaders[0]

    concentration = next(
        (rc for rc in draft_summary.round_concentration if rc.manager == leader.manager),
        None,
    )
    if concentration is None:
        return None

    hook = (
        f"{leader.manager} made {_spell(leader.pick_count)} picks, "
        f"{_spell(concentration.count)} of them in round {_spell(concentration.round)}."
    )
    return _candidate("manager_approach", [leader.roster_id], hook)


def _positional_hoard(
    board: BoardMetrics, *, exclude: str | None
) -> LeadCandidate | None:
    """The roster that stacked one real position higher than anyone else. The
    ``manager_approach`` subject and orphan rosters are skipped during the scan,
    so the angle lands on a real, distinct manager whenever one clears the bar."""
    best_count = 0
    best_position: str | None = None
    best_roster: str | None = None
    best_manager: str | None = None
    for team in board.teams:  # league.teams order breaks ties between rosters
        if team.roster_id == exclude or team.manager is None:
            continue
        for position in sorted(team.positional_counts):  # alpha breaks intra-team ties
            if position == _UNK:
                continue
            count = team.positional_counts[position]
            if count > best_count:
                best_count = count
                best_position = position
                best_roster = team.roster_id
                best_manager = team.manager
    if best_count < _MIN_POSITION_HOARD or best_roster is None or best_manager is None:
        return None
    noun = _POSITION_PLURAL.get(best_position or "", f"{best_position}s")
    hook = f"{best_manager} drafted {_spell(best_count)} {noun}."
    return _candidate("positional_hoard", [best_roster], hook)


def _biggest_score(league: LeagueModel) -> LeadCandidate | None:
    """The marquee name — the D7 floor, so the narrator always has a lead. The
    first overall pick; ``None`` only when the board is empty (matching the
    empty-draft contract: no picks, no angles)."""
    if not league.picks:
        return None
    pick = min(league.picks, key=lambda p: p.pick_no)
    return _candidate(
        "biggest_score",
        [pick.roster_id],
        f"{pick.player.name} went {pick.board_label}.",
    )
