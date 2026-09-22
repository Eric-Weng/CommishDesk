"""Stage 3 — the deterministic storyline lifecycle (Story 3.1, extended Story 5.9).

:func:`advance_storylines` takes a league's previous ``list[Storyline]`` plus the
stage results the facts builder already holds and returns the next set: it
**opens** a thread the period a signal first fires, **updates** ``last_week`` and
refreshes the ``headline`` while the signal keeps firing, and **closes** it
(``status="resolved"``) the period the signal stops. Pure, deterministic, and
idempotent — feeding the output back in with identical inputs returns an equal
list, so a resumed ``facts`` phase produces the same storyline set (AD-14). A
resolved thread older than :data:`STORYLINE_PRUNE_AFTER_WEEKS` is dropped from
the returned set (Story 5.9).

:func:`project_storyline_candidates` narrows the *active* threads to the
:class:`~commishdesk.facts.schema.StorylineCandidate` shape the narrators read;
narrators reference storylines but never mutate them.

The entry point is **kind-aware** (Story 5.1): every read and write is scoped to
rows whose ``kind`` matches the call's. A ``draft_recap`` call detects the draft
signals below; a ``weekly`` call (Story 5.9) detects the weekly signals in
:data:`WEEKLY_STORYLINE_KIND_PRIORITY`. The lifecycle — open / update / close /
prune / sort — is identical across kinds.

Draft signals, in the fixed priority :data:`STORYLINE_KIND_PRIORITY`:

* ``grade_extreme`` — the draft's best grade and its worst, each its own thread,
  but only when that grade is genuinely near a scale end (``A`` / ``A+`` at the
  top, ``D-`` / ``F`` at the bottom). A middling "lowest of the pack" grade
  (a ``B+``) never fires.
* ``boldest_swing`` — the roster from ``superlatives.boldest_swing``.
* ``round_stack`` — the manager who poured the most picks into one round
  (``draft_summary.round_concentration``'s top entry), when their name resolves
  to exactly one roster.

Weekly signals, in the fixed priority :data:`WEEKLY_STORYLINE_KIND_PRIORITY`:

* ``luck_extreme`` — the season's luckiest and unluckiest rosters, each its own
  thread, when ``abs(season.luck) >= 1.5`` (a cold start carries no luck).
* ``streak`` — the single longest active streak of length ``>= 3``.
* ``power_climb`` — the single largest ``abs(season.power.week_delta) >= 3``.

Every thread's ``id`` is ``"<kind>:<roster_id>"`` — :func:`project_storyline_candidates`
decodes ``kind`` and ``roster_ids`` straight back out of it.

Import fence (``tests/test_facts.py::test_facts_package_import_fence``): stdlib +
``commishdesk.ingest`` + ``commishdesk.stats`` + this package only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from commishdesk.stats import BoardMetrics, ConsensusMetrics, DraftGrades

from .leads import _roster_key, _spell, _team_label
from .schema import (
    DraftSummary,
    Storyline,
    StorylineCandidate,
    Superlatives,
    WeeklyPeriod,
    WeeklyTeam,
)

# Independently defined (not imported from ``facts/schema.py``'s private
# ``_IssueType``) — same values, kept as two separate symbols on purpose so
# this module never reaches into another module's private name. Story 5.1.
_StorylineKind = Literal["draft_recap", "weekly"]

__all__ = [
    "DRAFT_RECAP_WEEK",
    "STORYLINE_KIND_PRIORITY",
    "STORYLINE_PRUNE_AFTER_WEEKS",
    "WEEKLY_STORYLINE_KIND_PRIORITY",
    "advance_storylines",
    "project_storyline_candidates",
]

STORYLINE_KIND_PRIORITY: tuple[str, ...] = (
    "grade_extreme",
    "boldest_swing",
    "round_stack",
)
"""Editorial priority order for draft-recap storyline threads — ``project`` and
the narrator keep this order, and it is the tie-break the reduction ladder's
"keep only the lead entry" step relies on."""

WEEKLY_STORYLINE_KIND_PRIORITY: tuple[str, ...] = (
    "luck_extreme",
    "streak",
    "power_climb",
)
"""Editorial priority order for weekly storyline threads (Story 5.9). A disjoint
namespace from :data:`STORYLINE_KIND_PRIORITY`; both feed the one merged
:data:`_KIND_ORDER` the final sort reads."""

STORYLINE_PRUNE_AFTER_WEEKS = 4
"""A resolved thread this many weeks behind the current week is dropped from the
returned set (Story 5.9, deferred-work.md spec-3-1). The prune lives in the pure
``advance_storylines``, not in ``Store.write_storylines``."""

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

# Weekly-signal thresholds (Story 5.9).
_LUCK_THRESHOLD = 1.5
"""A season ``luck`` (wins over expectation) at or beyond this, in either
direction, is an extreme worth a thread."""

_STREAK_THRESHOLD = 3
"""A win / loss / tie streak this long is a thread."""

_POWER_DELTA_THRESHOLD = 3
"""A model-rank move this large, in either direction, is a thread."""

_STREAK_VERBS = {"W": "won", "L": "lost", "T": "tied"}

_KIND_ORDER = {
    kind: i
    for i, kind in enumerate(STORYLINE_KIND_PRIORITY + WEEKLY_STORYLINE_KIND_PRIORITY)
}
"""One merged priority table over both kinds' disjoint namespaces."""


def _kind_of(storyline_id: str) -> str:
    """The ``kind`` half of a ``"<kind>:<roster_id>"`` storyline id."""
    kind, _, _ = storyline_id.partition(":")
    return kind


def _id_roster_key(storyline_id: str) -> tuple[int, object]:
    """Numeric-aware ordering for a ``"<kind>:<roster_id>"`` id's roster half
    (deferred-work.md spec-3-1; mirrors ``facts/weekly.py``'s ``_sort_key``) so a
    10+-roster league orders ``2`` before ``10`` within a kind."""
    _, _, roster_id = storyline_id.partition(":")
    return _roster_key(roster_id)


def _signed(value: float) -> str:
    """A win-over-expectation figure with its sign (``+2.3`` / ``-1.7``)."""
    return f"{value:+.1f}"


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


def _detect_draft_recap(
    *,
    board: BoardMetrics,
    grades: DraftGrades,
    draft_summary: DraftSummary,
    superlatives: Superlatives,
) -> list[_Signal]:
    """Every draft-recap signal that fires this period, de-duplicated by ``id``
    and ordered by :data:`STORYLINE_KIND_PRIORITY` then roster id."""
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
    raw.sort(key=lambda s: (STORYLINE_KIND_PRIORITY.index(s.kind), _id_roster_key(s.id)))
    seen: set[str] = set()
    unique: list[_Signal] = []
    for signal in raw:
        if signal.id not in seen:
            seen.add(signal.id)
            unique.append(signal)
    return unique


# --------------------------------------------------------------------------- #
# Story 5.9 — weekly signals
# --------------------------------------------------------------------------- #


def _luck_extreme(teams: Sequence[WeeklyTeam]) -> list[_Signal]:
    """At most one thread for the season's luckiest roster (``luck >=
    _LUCK_THRESHOLD``) and one for its unluckiest (``luck <=
    -_LUCK_THRESHOLD``) — two independent conditions, not best/worst of one
    ``abs(luck)``-filtered set: in a league where every qualifying team runs
    lucky, the least-lucky *positive* team is not mislabeled "the league's
    worst luck". A cold start carries no ``luck`` (``None``), so neither
    fires. Ties break to the lower roster id (the roster-id-sorted input to
    ``max``/``min`` keeps the first match)."""
    ordered = sorted(teams, key=lambda t: _roster_key(t.roster_id))
    lucky = [
        (t.season.luck, t) for t in ordered if t.season.luck is not None and t.season.luck >= _LUCK_THRESHOLD
    ]
    unlucky = [
        (t.season.luck, t) for t in ordered if t.season.luck is not None and t.season.luck <= -_LUCK_THRESHOLD
    ]

    signals: list[_Signal] = []
    if lucky:
        luck, team = max(lucky, key=lambda item: item[0])
        signals.append(
            _Signal(
                id=f"luck_extreme:{team.roster_id}",
                kind="luck_extreme",
                roster_ids=(team.roster_id,),
                hook=(
                    f"{_team_label(team)} has run the league's best luck, "
                    f"{_signed(luck)} wins over expectation."
                ),
            )
        )
    if unlucky:
        luck, team = min(unlucky, key=lambda item: item[0])
        signals.append(
            _Signal(
                id=f"luck_extreme:{team.roster_id}",
                kind="luck_extreme",
                roster_ids=(team.roster_id,),
                hook=(
                    f"{_team_label(team)} has run the league's worst luck, "
                    f"{_signed(luck)} wins over expectation."
                ),
            )
        )
    return signals


def _streak(teams: Sequence[WeeklyTeam]) -> list[_Signal]:
    """The single longest active win / loss / tie streak, when it is at least
    :data:`_STREAK_THRESHOLD` long."""
    best: tuple[int, WeeklyTeam] | None = None
    for team in sorted(teams, key=lambda t: _roster_key(t.roster_id)):
        streak = team.season.streak
        if streak is None or streak.count < _STREAK_THRESHOLD:
            continue
        if best is None or streak.count > best[0]:
            best = (streak.count, team)
    if best is None:
        return []
    count, team = best
    streak = team.season.streak
    assert streak is not None
    verb = _STREAK_VERBS.get(streak.type, "put together")
    hook = f"{_team_label(team)} has {verb} {_spell(count)} straight."
    return [
        _Signal(
            id=f"streak:{team.roster_id}",
            kind="streak",
            roster_ids=(team.roster_id,),
            hook=hook,
        )
    ]


def _power_climb(teams: Sequence[WeeklyTeam]) -> list[_Signal]:
    """The single largest model-rank move since the prior week, when
    ``abs(season.power.week_delta)`` is at least :data:`_POWER_DELTA_THRESHOLD`
    (``None`` on a cold start)."""
    best: tuple[int, WeeklyTeam] | None = None
    for team in sorted(teams, key=lambda t: _roster_key(t.roster_id)):
        delta = team.season.power.week_delta
        if delta is None or abs(delta) < _POWER_DELTA_THRESHOLD:
            continue
        if best is None or abs(delta) > best[0]:
            best = (abs(delta), team)
    if best is None:
        return []
    _, team = best
    delta = team.season.power.week_delta
    assert delta is not None
    if delta > 0:
        hook = f"{_team_label(team)} climbed {_spell(delta)} spots in the power ranks."
    else:
        hook = f"{_team_label(team)} slid {_spell(-delta)} spots in the power ranks."
    return [
        _Signal(
            id=f"power_climb:{team.roster_id}",
            kind="power_climb",
            roster_ids=(team.roster_id,),
            hook=hook,
        )
    ]


def _detect_weekly(teams: Sequence[WeeklyTeam]) -> list[_Signal]:
    """Every weekly signal that fires this period, de-duplicated by ``id`` and
    ordered by :data:`WEEKLY_STORYLINE_KIND_PRIORITY` then roster id."""
    raw = [
        *_luck_extreme(teams),
        *_streak(teams),
        *_power_climb(teams),
    ]
    raw.sort(key=lambda s: (WEEKLY_STORYLINE_KIND_PRIORITY.index(s.kind), _id_roster_key(s.id)))
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
    kind: _StorylineKind,
    week: int,
    board: BoardMetrics | None = None,
    consensus: ConsensusMetrics | None = None,
    grades: DraftGrades | None = None,
    draft_summary: DraftSummary | None = None,
    superlatives: Superlatives | None = None,
    teams: Sequence[WeeklyTeam] | None = None,
    period: WeeklyPeriod | None = None,
) -> list[Storyline]:
    """Open / update / close / prune a league's storyline threads for one period.

    ``kind`` (Story 5.1) scopes every read and write to a matching-``kind``
    subset of ``previous``: a different-``kind`` row (e.g. a ``weekly`` row
    encountered while advancing ``draft_recap``) passes through completely
    untouched — it is never updated, resolved, pruned, or treated as a prior id
    a new thread must avoid colliding with. This is what keeps a draft-time and
    a week-1 storyline that share an ``id`` (the same firing signal) as two
    independent rows (AC3) rather than merging into one.

    The stage-result kwargs are optional and read by whichever detector the
    ``kind`` selects: a ``draft_recap`` call reads ``board`` / ``grades`` /
    ``draft_summary`` / ``superlatives`` (Story 3.1); a ``weekly`` call (Story
    5.9) reads ``teams``. ``consensus`` and ``period`` are accepted for symmetry
    with :func:`~commishdesk.facts.build.build_draft_recap_facts` /
    :func:`~commishdesk.facts.weekly.build_weekly_facts` and reserved for a
    future signal (the ``leads.build_lead_candidates`` reserved-params
    precedent).

    Pure, deterministic, idempotent: ``advance_storylines(advance_storylines(p,
    ...), ...)`` with identical keyword inputs (``kind`` included) equals
    ``advance_storylines(p, ...)``. New threads inherit ``league_id`` from
    ``previous`` (``""`` when ``previous`` is empty — the caller assigns it
    before persisting) and stamp the current ``kind``. A thread that stops
    firing is marked ``"resolved"`` and kept (never dropped) — until it ages out
    (``week - last_week > STORYLINE_PRUNE_AFTER_WEEKS``), when this call drops it
    from the returned set. One whose signal returns re-activates in place,
    keeping its original ``first_week``.
    """
    del consensus  # reserved
    if kind == "weekly":
        signals = _detect_weekly([] if teams is None else list(teams))
    else:
        if board is None or grades is None or draft_summary is None or superlatives is None:
            raise TypeError(
                "advance_storylines(kind='draft_recap') requires board, grades, "
                "draft_summary and superlatives"
            )
        signals = _detect_draft_recap(
            board=board,
            grades=grades,
            draft_summary=draft_summary,
            superlatives=superlatives,
        )
    hook_by_id = {signal.id: signal.hook for signal in signals}
    league_id = previous[0].league_id if previous else ""
    scoped = [storyline for storyline in previous if storyline.kind == kind]
    passthrough = [storyline for storyline in previous if storyline.kind != kind]
    prior_ids = {storyline.id for storyline in scoped}

    result: list[Storyline] = list(passthrough)
    for storyline in scoped:
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
                kind=kind,
            )
        )

    # Active threads first, then by editorial priority, then roster id — a
    # stable, deterministic order for the narrator projection and the store.
    result.sort(
        key=lambda s: (
            0 if s.status == "active" else 1,
            _KIND_ORDER.get(_kind_of(s.id), len(_KIND_ORDER)),
            _id_roster_key(s.id),
        )
    )

    # Age out only this call's own kind's resolved threads; a passthrough row of
    # another kind is never pruned by this call (Story 5.9).
    return [
        storyline
        for storyline in result
        if not (
            storyline.kind == kind
            and storyline.status == "resolved"
            and week - storyline.last_week > STORYLINE_PRUNE_AFTER_WEEKS
        )
    ]


def project_storyline_candidates(
    storylines: Sequence[Storyline],
    *,
    kind: _StorylineKind,
) -> list[StorylineCandidate]:
    """The *active*, matching-``kind`` threads, as the narrator projection.
    ``id`` is decoded back into ``kind`` / ``roster_ids`` (the storyline
    *signal* kind, unrelated to the ``kind`` parameter here); ``hook`` is the
    thread's headline sentence; ``weeks_running`` (``last_week - first_week +
    1``, Story 5.9) is the calendar span since ``first_week``, not a count of
    continuously active weeks — see :attr:`StorylineCandidate.weeks_running`.

    ``kind`` (Story 5.1 / AC5) filters alongside the existing ``status ==
    "active"`` check: ``store.write_storylines`` persists a league's whole
    storyline set in one undivided file, so ``storylines`` can already hold
    rows of more than one ``kind`` (e.g. carried through by
    :func:`advance_storylines`'s different-kind pass-through) — without this
    filter, a different period's active storyline could surface here as a
    narration candidate for the wrong period."""
    candidates: list[StorylineCandidate] = []
    for storyline in storylines:
        if storyline.status != "active" or storyline.kind != kind:
            continue
        signal_kind, _, roster_id = storyline.id.partition(":")
        candidates.append(
            StorylineCandidate(
                id=storyline.id,
                kind=signal_kind,
                roster_ids=[roster_id] if roster_id else [],
                hook=storyline.headline,
                weeks_running=storyline.last_week - storyline.first_week + 1,
            )
        )
    return candidates
