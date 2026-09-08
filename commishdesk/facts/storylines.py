"""Stage 3 — the deterministic storyline lifecycle (Story 3.1, AD-14).

:func:`advance_storylines` takes a league's previous ``list[Storyline]`` plus the
stage results the facts builder already holds and returns the next set: it
**opens** a thread the period a signal first fires, **updates** ``last_week`` and
refreshes the ``headline`` while the signal keeps firing, and **closes** it
(``status="resolved"``) the period the signal stops. Pure, deterministic, and
idempotent — feeding the output back in with identical inputs returns an equal
list, so a resumed ``facts`` phase produces the same storyline set (AD-14).

:func:`project_storyline_candidates` narrows the *active* threads to the
:class:`~commishdesk.facts.schema.StorylineCandidate` shape the narrators read;
narrators reference storylines but never mutate them.

Signals, in the fixed priority :data:`STORYLINE_KIND_PRIORITY`:

* ``grade_extreme`` — the draft's best grade and its worst, each its own thread,
  but only when that grade is genuinely near a scale end (``A`` / ``A+`` at the
  top, ``D-`` / ``F`` at the bottom). A middling "lowest of the pack" grade
  (a ``B+``) never fires.
* ``boldest_swing`` — the roster from ``superlatives.boldest_swing``.
* ``round_stack`` — the manager who poured the most picks into one round
  (``draft_summary.round_concentration``'s top entry), when their name resolves
  to exactly one roster.

Every thread's ``id`` is ``"<kind>:<roster_id>"`` — :func:`project_storyline_candidates`
decodes ``kind`` and ``roster_ids`` straight back out of it.

Import fence (``tests/test_facts.py::test_facts_package_import_fence``): stdlib +
``commishdesk.ingest`` + ``commishdesk.stats`` + this package only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from commishdesk.stats import BoardMetrics, ConsensusMetrics, DraftGrades

from .leads import _spell
from .schema import DraftSummary, Storyline, StorylineCandidate, Superlatives

__all__ = [
    "DRAFT_RECAP_WEEK",
    "STORYLINE_KIND_PRIORITY",
    "advance_storylines",
    "project_storyline_candidates",
]

STORYLINE_KIND_PRIORITY: tuple[str, ...] = (
    "grade_extreme",
    "boldest_swing",
    "round_stack",
)
"""Editorial priority order for storyline threads — ``project`` and the narrator
keep this order, and it is the tie-break the reduction ladder's "keep only the
lead entry" step relies on."""

DRAFT_RECAP_WEEK = 1
"""The NFL week a draft recap's storylines anchor to — the draft precedes week 1,
so its threads open there. ``DraftRecapFacts.week`` itself stays ``None``; this is
only the internal :class:`Storyline` record's ``first_week`` / ``last_week``."""

# Best-to-worst; mirrors ``commishdesk.stats.grades`` letter scale. A fixed table,
# like ``leads.LEAD_KIND_PRIORITY``.
_GRADE_ORDER: tuple[str, ...] = (
    "A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "F",
)
_TOP_GRADES = frozenset({"A+", "A"})
_BOTTOM_GRADES = frozenset({"D-", "F"})


_KIND_ORDER = {kind: i for i, kind in enumerate(STORYLINE_KIND_PRIORITY)}


def _kind_of(storyline_id: str) -> str:
    """The ``kind`` half of a ``"<kind>:<roster_id>"`` storyline id."""
    kind, _, _ = storyline_id.partition(":")
    return kind


def _grade_rank(letter: str) -> int:
    """Position of ``letter`` in :data:`_GRADE_ORDER` (0 = best); an unknown
    letter sorts to the bottom."""
    try:
        return _GRADE_ORDER.index(letter)
    except ValueError:
        return len(_GRADE_ORDER)


@dataclass(frozen=True)
class _Signal:
    """One storyline trigger detected for the current period."""

    id: str
    kind: str
    roster_ids: tuple[str, ...]
    hook: str


def _grade_article(letter: str) -> str:
    """``"an "`` before a vowel-sounding grade letter (``A``, ``F``), else ``"a "``."""
    return "an " if letter[:1] in ("A", "F") else "a "


def _grade_extreme(
    grades: DraftGrades, managers: dict[str, str | None]
) -> list[_Signal]:
    """At most one thread for the draft's best grade and one for its worst — and
    only when that grade is at a genuine scale end. A mid-scale "lowest grade in
    the room" (a ``B+``) is not something anyone argues about in December, so it
    never fires."""
    teams = list(grades.teams)
    if not teams:
        return []
    best = min(teams, key=lambda t: (_grade_rank(t.letter), t.roster_id))
    worst = max(teams, key=lambda t: (_grade_rank(t.letter), t.roster_id))

    signals: list[_Signal] = []
    best_mgr = managers.get(best.roster_id)
    if best.letter in _TOP_GRADES and best_mgr is not None:
        signals.append(
            _Signal(
                id=f"grade_extreme:{best.roster_id}",
                kind="grade_extreme",
                roster_ids=(best.roster_id,),
                hook=(
                    f"{best_mgr} came away with the draft's top grade, "
                    f"{_grade_article(best.letter)}{best.letter}."
                ),
            )
        )
    worst_mgr = managers.get(worst.roster_id)
    if (
        worst.roster_id != best.roster_id
        and worst.letter in _BOTTOM_GRADES
        and worst_mgr is not None
    ):
        signals.append(
            _Signal(
                id=f"grade_extreme:{worst.roster_id}",
                kind="grade_extreme",
                roster_ids=(worst.roster_id,),
                hook=(
                    f"{worst_mgr} drew the draft's worst grade, "
                    f"{_grade_article(worst.letter)}{worst.letter}."
                ),
            )
        )
    return signals


def _boldest_swing(
    superlatives: Superlatives, managers: dict[str, str | None]
) -> list[_Signal]:
    swing = superlatives.boldest_swing
    if swing is None or len(swing.picks) < 2:
        return []
    manager = managers.get(swing.roster_id)
    if manager is None:  # never attribute a thread to no one (leads.py convention)
        return []
    first, second = swing.picks[0], swing.picks[1]
    hook = (
        f"{manager} made the draft's boldest swing: {first.player} at "
        f"{first.board_label} against {second.player} at {second.board_label}."
    )
    return [
        _Signal(
            id=f"boldest_swing:{swing.roster_id}",
            kind="boldest_swing",
            roster_ids=(swing.roster_id,),
            hook=hook,
        )
    ]


def _round_stack(
    draft_summary: DraftSummary,
    roster_ids_by_manager: dict[str, list[str]],
    managers: dict[str, str | None],
) -> list[_Signal]:
    concentration = draft_summary.round_concentration
    if not concentration:
        return []
    top = concentration[0]  # already ordered by descending count in the builder
    # Resolve the manager name to exactly one roster; skip rather than guess when
    # it is missing or ambiguous (two teams share the name).
    if top.manager is None:
        return []
    candidates = roster_ids_by_manager.get(top.manager, [])
    if len(candidates) != 1:
        return []
    roster_id = candidates[0]
    # Explicit ``is None`` (not ``or``) so an empty-but-present manager name is
    # kept, not swapped for the generic "Roster {id}" label.
    resolved = managers.get(roster_id)
    label = resolved if resolved is not None else f"Roster {roster_id}"
    hook = (
        f"{label} funneled {_spell(top.count)} of their picks into round "
        f"{_spell(top.round)}."
    )
    return [
        _Signal(
            id=f"round_stack:{roster_id}",
            kind="round_stack",
            roster_ids=(roster_id,),
            hook=hook,
        )
    ]


def _detect(
    *,
    board: BoardMetrics,
    grades: DraftGrades,
    draft_summary: DraftSummary,
    superlatives: Superlatives,
) -> list[_Signal]:
    """Every signal that fires this period, de-duplicated by ``id`` and ordered by
    :data:`STORYLINE_KIND_PRIORITY` then ``id``."""
    managers = {t.roster_id: t.manager for t in board.teams}
    roster_ids_by_manager: dict[str, list[str]] = {}
    for team in board.teams:
        if team.manager is not None:
            roster_ids_by_manager.setdefault(team.manager, []).append(team.roster_id)

    raw = [
        *_grade_extreme(grades, managers),
        *_boldest_swing(superlatives, managers),
        *_round_stack(draft_summary, roster_ids_by_manager, managers),
    ]
    raw.sort(key=lambda s: (STORYLINE_KIND_PRIORITY.index(s.kind), s.id))
    seen: set[str] = set()
    unique: list[_Signal] = []
    for signal in raw:
        if signal.id not in seen:
            seen.add(signal.id)
            unique.append(signal)
    return unique


def advance_storylines(
    previous: Sequence[Storyline],
    *,
    week: int,
    board: BoardMetrics,
    consensus: ConsensusMetrics,
    grades: DraftGrades,
    draft_summary: DraftSummary,
    superlatives: Superlatives,
) -> list[Storyline]:
    """Open / update / close a league's storyline threads for one period.

    ``consensus`` is accepted for symmetry with
    :func:`~commishdesk.facts.build.build_draft_recap_facts` and reserved for a
    future signal (the ``leads.build_lead_candidates`` reserved-params precedent);
    the signals here read ``board`` / ``grades`` / ``draft_summary`` /
    ``superlatives``.

    Pure, deterministic, idempotent: ``advance_storylines(advance_storylines(p,
    ...), ...)`` with identical keyword inputs equals ``advance_storylines(p,
    ...)``. New threads inherit ``league_id`` from ``previous`` (``""`` when
    ``previous`` is empty — the caller assigns it before persisting). A thread
    that stops firing is marked ``"resolved"`` and kept (never dropped); one whose
    signal returns re-activates in place, keeping its original ``first_week``.
    """
    del consensus  # reserved
    signals = _detect(
        board=board,
        grades=grades,
        draft_summary=draft_summary,
        superlatives=superlatives,
    )
    hook_by_id = {signal.id: signal.hook for signal in signals}
    league_id = previous[0].league_id if previous else ""
    prior_ids = {storyline.id for storyline in previous}

    result: list[Storyline] = []
    for storyline in previous:
        if storyline.id in hook_by_id:
            result.append(
                storyline.model_copy(
                    update={
                        "status": "active",
                        "last_week": max(storyline.last_week, week),
                        "headline": hook_by_id[storyline.id],
                    }
                )
            )
        elif storyline.status == "active":
            result.append(storyline.model_copy(update={"status": "resolved"}))
        else:  # already resolved and still not firing — untouched
            result.append(storyline)

    for signal in signals:
        if signal.id in prior_ids:
            continue
        result.append(
            Storyline(
                id=signal.id,
                league_id=league_id,
                headline=signal.hook,
                status="active",
                first_week=week,
                last_week=week,
            )
        )

    # Active threads first, then by editorial priority, then id — a stable,
    # deterministic order for the narrator projection and the store.
    result.sort(
        key=lambda s: (
            0 if s.status == "active" else 1,
            _KIND_ORDER.get(_kind_of(s.id), len(_KIND_ORDER)),
            s.id,
        )
    )
    return result


def project_storyline_candidates(
    storylines: Sequence[Storyline],
) -> list[StorylineCandidate]:
    """The *active* threads, as the narrator projection. ``id`` is decoded back
    into ``kind`` / ``roster_ids``; ``hook`` is the thread's headline sentence."""
    candidates: list[StorylineCandidate] = []
    for storyline in storylines:
        if storyline.status != "active":
            continue
        kind, _, roster_id = storyline.id.partition(":")
        candidates.append(
            StorylineCandidate(
                id=storyline.id,
                kind=kind,
                roster_ids=[roster_id] if roster_id else [],
                hook=storyline.headline,
            )
        )
    return candidates
