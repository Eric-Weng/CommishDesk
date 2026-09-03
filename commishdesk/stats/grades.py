"""The editorial draft-grade algorithm (Story 2.4c).

One pure function, :func:`compute_draft_grades`, turns a stage-1
:class:`~commishdesk.ingest.LeagueModel` plus the Story 2.4b
:class:`~commishdesk.stats.consensus.ConsensusMetrics` into a frozen
:class:`DraftGrades`: one :class:`TeamGrade` per roster in ``league.teams`` order
plus the :class:`GradeMethod` descriptor that Story 2.5 drops verbatim into the
Facts JSON.

Raw ``delta = pick_no - consensus_slot`` (**positive = value**, negative = reach --
matches :mod:`commishdesk.stats.consensus`) over-rewards late-round picks, so the
grade is editorial, not raw ``delta``:

* ``draft_score`` -- tier-weighted ``round(Σ delta_i · weight_i, 1)`` over the
  roster's picks that have a ``consensus_slot``, where
  ``weight(slot) = max(0.15, (60 - slot) / 60)``. No-consensus picks contribute
  nothing.
* ``premium_picks`` -- count of the roster's picks with ``consensus_slot <= 10``.
* ``format_fit`` (integer) -- the one grade input that reads ``league.format``,
  because a draft has no points signal to normalize against:
  ``+2`` if a superflex/2QB roster drafted ``>= 1`` QB and ``+2`` more at ``>= 2``
  QB (QB count is by ``player.position``, consensus-independent); ``+1`` per
  drafted TE with ``consensus_slot <= 12`` in a TE-premium league; ``0`` otherwise.
* ``grade_input = round(draft_score + 3·premium_picks + format_fit, 1)``.
* ``letter`` -- the monotonic cut table below applied to ``grade_input`` (compared
  as the already-rounded 1-decimal value), then the floor: a roster with
  ``premium_picks >= 2`` never grades below ``"C"``.
* ``driving_picks`` -- the ``pick_no``s whose ``abs(delta_i · weight_i) >= 1.5``,
  at most four (largest magnitude first, ties toward the earlier ``pick_no``),
  returned ascending by ``pick_no``.

``grade_input -> letter`` cut table (settled 2026-09-03, ``"A-F+/-"`` 13-point
scale). The negative side is intentionally coarser -- an editorial recap does not
hand out ``D-`` / ``F`` on a scale where "reached on nearly every pick" bottoms
out near ``-7``::

    grade_input >= 16.0  -> A+        [-1.0, 1.0)  -> C
    [12.0, 16.0)         -> A         [-4.0, -1.0) -> C-
    [ 9.0, 12.0)         -> A-        [-8.0, -4.0) -> D+
    [ 6.0,  9.0)         -> B+        [-13.0, -8.0)-> D
    [ 4.5,  6.0)         -> B         [-18.0,-13.0)-> D-
    [ 3.0,  4.5)         -> B-        < -18.0      -> F
    [ 1.0,  3.0)         -> C+

Then the floor: ``premium_picks >= 2`` and ``letter`` in
``{C-, D+, D, D-, F}`` -> ``letter = "C"``.

This module stays inside the ``stats/`` fence (AD-1): it imports only stdlib,
pydantic, ``commishdesk.ingest``, and ``commishdesk.stats.consensus`` -- never
``commishdesk.consensus`` / ``adapters`` / ``store`` / a later pipeline stage. It
makes no network call, reads no clock, no PRNG, no filesystem. It does not
re-derive the consensus rank or ``delta`` -- it consumes ``ConsensusMetrics`` as
given.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from commishdesk.ingest import LeagueModel
from commishdesk.stats.consensus import ConsensusMetrics

__all__ = [
    "GRADE_METHOD",
    "THIRTEEN_POINT_SCALE",
    "DraftGrades",
    "GradeMethod",
    "TeamGrade",
    "compute_draft_grades",
]

PREMIUM_SLOT = 10
"""A pick at ``consensus_slot <= PREMIUM_SLOT`` is a premium pick."""

PREMIUM_TE_SLOT = 12
"""In a TE-premium league, a drafted TE at ``consensus_slot <= PREMIUM_TE_SLOT``
adds ``+1`` to ``format_fit``."""

PREMIUM_BONUS = 3
"""Each premium pick adds ``PREMIUM_BONUS`` to ``grade_input``."""

DRIVING_CUTOFF = 1.5
"""A pick is a driving pick when ``abs(delta · weight) >= DRIVING_CUTOFF``."""

MAX_DRIVING_PICKS = 4
"""``driving_picks`` holds at most this many pick numbers -- the largest-magnitude
``abs(delta · weight)`` contributions, ties broken toward the earlier ``pick_no``."""

_WEIGHT_FLOOR = 0.15
# Formula constant from facts-schema.md ("(60 - consensus_slot) / 60"): roughly five
# rounds of a 12-team board. NOT league.format.team_count -- the tier span is fixed.
_WEIGHT_SPAN = 60

# grade_input -> letter, as (inclusive lower bound, letter) in descending order.
_CUT_TABLE: tuple[tuple[float, str], ...] = (
    (16.0, "A+"),
    (12.0, "A"),
    (9.0, "A-"),
    (6.0, "B+"),
    (4.5, "B"),
    (3.0, "B-"),
    (1.0, "C+"),
    (-1.0, "C"),
    (-4.0, "C-"),
    (-8.0, "D+"),
    (-13.0, "D"),
    (-18.0, "D-"),
)
_LOWEST_LETTER = "F"

THIRTEEN_POINT_SCALE: frozenset[str] = frozenset(
    {letter for _, letter in _CUT_TABLE} | {_LOWEST_LETTER}
)
"""The settled ``"A-F+/-"`` scale -- every ``TeamGrade.letter`` is one of these."""

# Letters "below C" -- the floor raises any of these to "C" when premium_picks >= 2.
_FLOOR_LETTERS: frozenset[str] = frozenset({"C-", "D+", "D", "D-", "F"})
_FLOOR_LETTER = "C"


class _Frozen(BaseModel):
    """Immutable, closed to unknown keys -- matches ``stats/consensus.py``."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class TeamGrade(_Frozen):
    """One roster's editorial draft grade. ``draft_score`` and ``grade_input`` are
    rounded to one decimal; ``letter`` is one of :data:`THIRTEEN_POINT_SCALE`;
    ``driving_picks`` is ascending by ``pick_no`` and holds at most
    :data:`MAX_DRIVING_PICKS` entries."""

    roster_id: str
    draft_score: float
    premium_picks: int
    format_fit: int
    grade_input: float
    letter: str
    driving_picks: list[int]


class GradeMethod(_Frozen):
    """The grade-method descriptor. Story 2.5 puts this in the Facts JSON verbatim;
    it is a constant of the algorithm, not a per-league value."""

    letter_scale: str
    inputs: str
    floor_rule: str
    note: str


class DraftGrades(_Frozen):
    """The whole result: one :class:`TeamGrade` per roster in ``league.teams``
    order, plus the invariant :class:`GradeMethod` descriptor."""

    teams: list[TeamGrade]
    grade_method: GradeMethod


GRADE_METHOD = GradeMethod(
    letter_scale="A-F+/-",
    inputs=(
        "tier_weighted_delta_vs_consensus + 3*premium_picks(consensus_slot<=10) "
        "+ format_fit(2QB: +2 >=1 QB, +2 >=2 QB; "
        "TE-premium: +1 per TE consensus_slot<=12)"
    ),
    floor_rule=">=2 premium picks -> minimum C",
    note=(
        "letter is a documented monotonic function of grade_input in the engine; "
        "grade.rationale prose is the narrator's"
    ),
)


def _weight(consensus_slot: int) -> float:
    """Tier weight for a pick at ``consensus_slot``: ``max(0.15, (60 - slot) / 60)``.
    Early slots weigh near ``1.0``; everything past slot ~51 is pinned to the
    ``0.15`` floor."""
    return max(_WEIGHT_FLOOR, (_WEIGHT_SPAN - consensus_slot) / _WEIGHT_SPAN)


def _letter_for(grade_input: float) -> str:
    """The monotonic cut table applied to an already-rounded ``grade_input``."""
    for lower, letter in _CUT_TABLE:
        if grade_input >= lower:
            return letter
    return _LOWEST_LETTER


def compute_draft_grades(
    league: LeagueModel, consensus: ConsensusMetrics
) -> DraftGrades:
    """Compute one editorial :class:`TeamGrade` per roster in ``league``.

    Pure, deterministic, offline. A valid ``LeagueModel`` plus its
    ``ConsensusMetrics`` in, a ``DraftGrades`` out; two calls on one input return
    an equal ``model_dump()``. A roster whose every pick is ``no_consensus`` gets
    ``draft_score`` ``0.0``, ``premium_picks`` ``0``, ``driving_picks`` ``[]`` --
    ``format_fit`` still comes from the drafted positions -- and never raises.
    """
    superflex = league.format.is_superflex_or_2qb
    te_premium = league.format.te_premium

    position_by_pick_no = {pick.pick_no: (pick.player.position or "") for pick in league.picks}

    picks_by_roster: dict[str, list] = {}
    for pick_row in consensus.picks:
        picks_by_roster.setdefault(pick_row.roster_id, []).append(pick_row)

    qb_by_roster: dict[str, int] = {}
    for pick in league.picks:
        if pick.player.position == "QB":
            qb_by_roster[pick.roster_id] = qb_by_roster.get(pick.roster_id, 0) + 1

    team_grades: list[TeamGrade] = []
    for team in league.teams:
        roster_picks = picks_by_roster.get(team.roster_id, [])

        total = 0.0
        premium_picks = 0
        premium_te = 0
        contributions: list[tuple[float, int]] = []  # (abs contribution, pick_no)
        for pick_row in roster_picks:
            slot = pick_row.consensus_slot
            # premium_picks / premium_te are counted from consensus_slot alone
            # (spec: "count of picks with consensus_slot <= 10", no delta dependency).
            if slot is None:
                continue
            if slot <= PREMIUM_SLOT:
                premium_picks += 1
            if (
                slot <= PREMIUM_TE_SLOT
                and position_by_pick_no.get(pick_row.pick_no) == "TE"
            ):
                premium_te += 1
            # draft_score contribution + driving-pick math need delta.
            if pick_row.delta is None:
                continue
            contribution = pick_row.delta * _weight(slot)
            total += contribution
            if abs(contribution) >= DRIVING_CUTOFF:
                contributions.append((abs(contribution), pick_row.pick_no))

        draft_score = round(total, 1) or 0.0

        format_fit = 0
        if superflex:
            qb_count = qb_by_roster.get(team.roster_id, 0)
            if qb_count >= 1:
                format_fit += 2
            if qb_count >= 2:
                format_fit += 2
        if te_premium:
            format_fit += premium_te

        grade_input = round(draft_score + PREMIUM_BONUS * premium_picks + format_fit, 1) or 0.0

        letter = _letter_for(grade_input)
        if premium_picks >= 2 and letter in _FLOOR_LETTERS:
            letter = _FLOOR_LETTER

        contributions.sort(key=lambda item: (-item[0], item[1]))
        driving_picks = sorted(
            {pick_no for _, pick_no in contributions[:MAX_DRIVING_PICKS]}
        )

        team_grades.append(
            TeamGrade(
                roster_id=team.roster_id,
                draft_score=draft_score,
                premium_picks=premium_picks,
                format_fit=format_fit,
                grade_input=grade_input,
                letter=letter,
                driving_picks=driving_picks,
            )
        )

    return DraftGrades(teams=team_grades, grade_method=GRADE_METHOD)
