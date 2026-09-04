"""Stage 3 schema — the versioned, self-validated ``draft_recap`` Facts JSON (AD-2).

Pydantic v2 models for the ``draft_recap`` shape as it appears in
``brief/phase-0/draft-recap-facts.json``. Every model derives from :class:`_Doc`:
``frozen=True`` (a built document is immutable) and ``extra="ignore"`` (the
schema-tolerance invariant / facts-schema design rule 3) — a consumer written to
:data:`SCHEMA_VERSION` loads a later ``0.1.x`` payload that adds a key without
error, the unknown key silently dropped on read.

:data:`SCHEMA_VERSION` is semver: an additive key bumps the minor, a shape change
the major. The editorial prose fields (``superlatives.*.note`` /
``teams[].grade.rationale``) are the narrator's and are modelled
``str | None = None`` — the Story 2.5 builder emits ``None``. ``lead_candidates``
is populated by Story 2.6 with a deterministic factual ``hook`` per angle (still a
``0.1.x`` additive payload, not a shape change); ``storyline_candidates`` stays
``[]`` until Story 3.1. Both serialize as ``[]`` when empty, never omitted.

This module imports stdlib + pydantic only — no engine package.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

__all__ = [
    "SCHEMA_VERSION",
    "BoardPick",
    "BoldestSwing",
    "ConsensusSource",
    "DraftRecapFacts",
    "DraftRef",
    "DraftSummary",
    "FormatRef",
    "GradeMethodRef",
    "GradeRef",
    "HeadlineNumbers",
    "LeadCandidate",
    "LeagueRef",
    "ManagerCount",
    "ManagerPickCount",
    "Narration",
    "NarrationLeague",
    "NarrationTeam",
    "PickExtreme",
    "PickRow",
    "PlayerRef",
    "PositionalRunsSummary",
    "QBRunSummary",
    "RBRunSummary",
    "RoundConcentration",
    "Source",
    "StorylineCandidate",
    "Superlatives",
    "SuperlativePick",
    "TERunSummary",
    "TeamRow",
]

SCHEMA_VERSION = "0.1.0"
"""Semver contract version. Additive key -> minor bump; shape change -> major."""

_ISSUE_TYPE = "draft_recap"


class _Doc(BaseModel):
    """Shared config for every Facts JSON model: immutable, and unknown keys are
    dropped on read (facts-schema design rule 3 / schema tolerance)."""

    model_config = ConfigDict(frozen=True, extra="ignore")


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #


class Source(_Doc):
    """Where the raw league data came from. ``draft_id`` / ``fetched_at`` are
    supplied by the caller (the Story 2.7 CLI), not carried on a stage result."""

    platform: str
    league_id: str
    draft_id: str | None = None
    fetched_at: str | None = None


class ConsensusSource(_Doc):
    """The pre-draft consensus board the picks were measured against.
    ``name`` / ``as_of`` come from the caller's ``ConsensusRank``; ``engine_note``
    is a constant of the engine."""

    name: str | None = None
    as_of: str | None = None
    provisional: bool = True
    engine_note: str


# --------------------------------------------------------------------------- #
# League + draft identity
# --------------------------------------------------------------------------- #


class FormatRef(_Doc):
    """The league's shape, as data (facts-schema design rule 2)."""

    team_count: int
    roster_slots: list[str]
    flex_eligibility: dict[str, list[str]]
    scoring_label: str
    is_superflex_or_2qb: bool
    te_premium: bool


class LeagueRef(_Doc):
    """League identity. ``season`` is a string here (Facts JSON convention),
    though ``LeagueModel.season`` is an ``int`` year."""

    id: str
    name: str
    season: str
    platform: str
    format: FormatRef


class DraftRef(_Doc):
    """Draft-level metadata. ``started_at`` / ``completed_at`` are UTC ISO 8601
    (the epoch-ms -> ISO conversion Story 2.2 deferred to the builder); ``None``
    when the platform reports no timestamp."""

    id: str
    type: str | None = None
    rounds: int | None = None
    started_at: str | None = None
    completed_at: str | None = None


# --------------------------------------------------------------------------- #
# Picks
# --------------------------------------------------------------------------- #


class PlayerRef(_Doc):
    """A drafted player, snapshotted from the pick record (not a live lookup)."""

    sleeper_id: str
    name: str
    position: str | None = None
    nfl_team: str | None = None


class PickRow(_Doc):
    """One selection, joined to its consensus measurement. ``consensus_slot`` /
    ``consensus_label`` / ``delta`` are ``None`` together exactly when
    ``flags == ["no_consensus"]``."""

    pick_no: int
    round: int
    slot: int
    board_label: str
    roster_id: str
    manager: str | None = None
    player: PlayerRef
    consensus_slot: int | None = None
    consensus_label: str | None = None
    delta: int | None = None
    flags: list[str] = []


class BoardPick(_Doc):
    """A trimmed pick reference used in board projections (round-1 board, the
    round-1 QB list, a boldest-swing pair)."""

    pick_no: int
    board_label: str
    manager: str | None = None
    player: str
    position: str | None = None
    consensus_label: str | None = None
    delta: int | None = None


class PickExtreme(_Doc):
    """A per-team raw extreme — the largest / smallest ``delta`` on the roster."""

    pick_no: int
    player: str
    delta: int


# --------------------------------------------------------------------------- #
# Teams
# --------------------------------------------------------------------------- #


class GradeRef(_Doc):
    """A roster's editorial grade. ``letter`` + ``driving_picks`` come from
    ``DraftGrades``; ``rationale`` prose is the narrator's (Story 2.5 emits
    ``None``)."""

    letter: str
    driving_picks: list[int] = []
    rationale: str | None = None


class TeamRow(_Doc):
    """One roster: its board profile, its raw consensus extremes, and its grade,
    joined on ``roster_id`` in ``league.teams`` order."""

    roster_id: str
    manager: str | None = None
    pick_count: int
    pick_nos: list[int] = []
    positional_counts: dict[str, int] = {}
    back_to_back: list[tuple[int, int]] = []
    best_value_pick: PickExtreme | None = None
    biggest_reach_pick: PickExtreme | None = None
    draft_score: float
    premium_picks: int
    format_fit: int
    grade_input: float
    grade: GradeRef


# --------------------------------------------------------------------------- #
# draft_summary — board-only projections
# --------------------------------------------------------------------------- #


class ManagerPickCount(_Doc):
    """A ``{manager, pick_count}`` row in the pick-count ranking."""

    manager: str | None = None
    pick_count: int


class ManagerCount(_Doc):
    """A ``{manager, count}`` reference (the roster with the most picks of one
    position)."""

    manager: str | None = None
    count: int


class RoundConcentration(_Doc):
    """A manager who put an unusual share of their draft into one round."""

    manager: str | None = None
    round: int
    count: int


class QBRunSummary(_Doc):
    """The quarterback run. ``run_labels`` are the board labels of the QBs taken
    through the end of round 3; ``left_waiting`` are the managers who took their
    first QB after the run tailed off but before the final round."""

    total: int
    first_label: str | None = None
    by_end_round3: int
    run_labels: list[str] = []
    left_waiting: list[str] = []


class RBRunSummary(_Doc):
    """The running-back run."""

    total: int
    first_label: str | None = None
    in_round1: int
    most_by_one_manager: ManagerCount | None = None


class TERunSummary(_Doc):
    """The tight-end window: the opening cluster of TE picks, the third TE, and
    the pick gap that separated them."""

    total: int
    early_window_labels: list[str] = []
    third_te_label: str | None = None
    third_te_player: str | None = None
    gap_picks: int | None = None


class PositionalRunsSummary(_Doc):
    """The three position runs the draft recap talks about."""

    QB: QBRunSummary
    RB: RBRunSummary
    TE: TERunSummary


class DraftSummary(_Doc):
    """Board-only aggregates — no consensus source required for any field except
    the ``consensus_label`` / ``delta`` echoed inside ``round1_qbs``."""

    round1_positional: dict[str, int] = {}
    #: The size of the opening-picks window ``first_window_running_backs`` was
    #: scanned over — ``team_count - 1``, not a fixed 11 (retro finding A2: the
    #: prior ``first11_*`` names hardcoded "11", which misstated the window in
    #: any non-12-team league).
    first_window: int
    first_window_running_backs: list[str] = []
    round1_qbs: list[BoardPick] = []
    pick_count_rank: list[ManagerPickCount] = []
    round_concentration: list[RoundConcentration] = []
    positional_runs: PositionalRunsSummary


# --------------------------------------------------------------------------- #
# superlatives
# --------------------------------------------------------------------------- #


class SuperlativePick(_Doc):
    """A single highlighted pick. ``note`` prose is the narrator's (Story 2.5
    emits ``None``)."""

    pick_no: int
    board_label: str
    manager: str | None = None
    player: str
    position: str | None = None
    consensus_label: str | None = None
    delta: int | None = None
    note: str | None = None


class BoldestSwing(_Doc):
    """The roster whose draft carried the widest spread between its best value and
    its biggest reach."""

    roster_id: str
    manager: str | None = None
    picks: list[BoardPick] = []
    note: str | None = None


class Superlatives(_Doc):
    """The five recap superlatives, all derived from ``delta``. Every field is
    nullable — a draft with no ranked pick has no superlatives."""

    best_value: SuperlativePick | None = None
    best_value_runner_up: SuperlativePick | None = None
    biggest_reach: SuperlativePick | None = None
    biggest_reach_runner_up: SuperlativePick | None = None
    boldest_swing: BoldestSwing | None = None


class GradeMethodRef(_Doc):
    """The grade-method descriptor, copied verbatim from ``DraftGrades``."""

    letter_scale: str
    inputs: str
    floor_rule: str
    note: str


# --------------------------------------------------------------------------- #
# lead / storyline candidates
# --------------------------------------------------------------------------- #


class LeadCandidate(_Doc):
    """A ranked lead angle the narrator can open on (delta D7). Populated by
    Story 2.6's ``facts/leads.py``: ``kind`` is one of its
    ``LEAD_KIND_PRIORITY`` values, ``roster_ids`` attributes the angle (empty for
    a room-wide observation), and ``hook`` is a deterministic factual sentence
    (never ``None`` on a lead angle — the ``str | None`` type is shared with
    :class:`StorylineCandidate`, whose ``hook`` is still the narrator's)."""

    rank: int
    kind: str
    roster_ids: list[str] = []
    hook: str | None = None


class StorylineCandidate(_Doc):
    """A "still arguing about it in December" angle. Populated by Story 3.1;
    ``[]`` here."""

    id: str
    kind: str
    roster_ids: list[str] = []
    hook: str | None = None


# --------------------------------------------------------------------------- #
# narration — the trimmed projection both narrators read (design rules 8-9)
# --------------------------------------------------------------------------- #


class NarrationLeague(_Doc):
    """Just enough league identity for a headline."""

    name: str
    season: str
    scoring_label: str


class HeadlineNumbers(_Doc):
    """The counts a lead paragraph leans on."""

    picks_total: int
    rounds: int | None = None
    r1_positional: dict[str, int] = {}
    #: Echoes ``len(DraftSummary.first_window_running_backs)`` — see
    #: :attr:`DraftSummary.first_window` for the window size this counts over.
    first_window_rb_count: int
    pick_count_leader: ManagerPickCount | None = None
    pick_count_low: ManagerPickCount | None = None


class NarrationTeam(_Doc):
    """One roster, trimmed to what the grades section needs."""

    manager: str | None = None
    roster_id: str
    pick_count: int
    positional_counts: dict[str, int] = {}
    grade: str
    grade_driving_picks: list[int] = []
    grade_rationale: str | None = None
    best_value_pick: PickExtreme | None = None
    biggest_reach_pick: PickExtreme | None = None
    back_to_back: list[tuple[int, int]] = []


class Narration(_Doc):
    """The sanitized projection the LLM narrator sees — never a raw roster or a
    board column it does not need. No token cap yet (Story 3.1)."""

    issue_type: Literal["draft_recap"] = _ISSUE_TYPE
    league: NarrationLeague
    headline_numbers: HeadlineNumbers
    board_round1: list[BoardPick] = []
    superlatives: Superlatives
    teams: list[NarrationTeam] = []
    positional_runs: PositionalRunsSummary
    lead_candidates: list[LeadCandidate] = []
    storyline_candidates: list[StorylineCandidate] = []


# --------------------------------------------------------------------------- #
# root
# --------------------------------------------------------------------------- #


class DraftRecapFacts(_Doc):
    """The whole ``draft_recap`` Facts JSON — the one published contract every
    narrator and renderer downstream of ``facts/`` reads (AD-2). Top-level key
    order matches ``brief/phase-0/draft-recap-facts.json``."""

    schema_version: str = SCHEMA_VERSION
    generated_at: str
    provisional: bool = True
    issue_type: Literal["draft_recap"] = _ISSUE_TYPE
    source: Source
    consensus_source: ConsensusSource
    league: LeagueRef
    draft: DraftRef
    picks: list[PickRow] = []
    teams: list[TeamRow] = []
    draft_summary: DraftSummary
    superlatives: Superlatives
    grade_method: GradeMethodRef
    lead_candidates: list[LeadCandidate] = []
    storyline_candidates: list[StorylineCandidate] = []
    narration: Narration
