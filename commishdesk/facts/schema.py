"""Stage 3 schema — the versioned, self-validated Facts JSON contracts (AD-2).

Pydantic v2 models for the ``draft_recap`` shape as it appears in
``brief/phase-0/draft-recap-facts.json`` and (Story 5.8) the standalone
``weekly`` shape. Every model derives from :class:`_Doc`: ``frozen=True`` (a
built document is immutable) and ``extra="ignore"`` (the schema-tolerance
invariant / facts-schema design rule 3) — a consumer written to
:data:`SCHEMA_VERSION` loads a later ``0.x.y`` payload that adds a key without
error, the unknown key silently dropped on read.

:data:`SCHEMA_VERSION` is semver: an additive key bumps the minor, a shape change
the major. The editorial prose fields (``superlatives.*.note`` /
``teams[].grade.rationale``) are the narrator's and are modelled
``str | None = None`` — the Story 2.5 builder emits ``None``. ``lead_candidates``
is populated by Story 2.6 with a deterministic factual ``hook`` per angle;
``storyline_candidates`` is populated by Story 3.1 from the deterministic
storyline lifecycle in ``facts/storylines.py``. Both serialize as ``[]`` when
empty, never omitted.

Story 3.1 also widened the contract additively (``0.1.0`` -> ``0.2.0``): a
schema-only ``week`` placeholder, :data:`NARRATION_TOKEN_CAP` bounding the
narrator projection, and :class:`Storyline` — the persisted narrative-memory
record — relocated here from ``store.py`` so ``facts/`` owns its shape
(``store.py`` imports it back for its read/write port).

Story 5.8 (``0.4.0`` -> ``0.5.0``) turns the empty :class:`WeeklyFacts`
placeholder into the real weekly document — a standalone root laid out like the
private ``week10-facts.json`` golden, plus :class:`WeeklyNarration`. The
``DraftRecapFacts.weekly`` field is retyped to ``None`` (resent, always null) so
a ``0.4.0`` draft-recap document still loads and renders — removing it is a
major-bump concern.

This module imports stdlib + pydantic only — no engine package.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "NARRATION_TOKEN_CAP",
    "NarrationPlayer",
    "SCHEMA_VERSION",
    "BoardPick",
    "BoldestSwing",
    "ConsensusSource",
    "DivisionRef",
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
    "Storyline",
    "StorylineCandidate",
    "Superlatives",
    "SuperlativePick",
    "TERunSummary",
    "TeamPointsRef",
    "TeamRow",
    "WeekMarginRef",
    "WeekSummaryRef",
    "WeeklyAllPlay",
    "WeeklyBenchRegret",
    "WeeklyByeImpact",
    "WeeklyByeStarter",
    "WeeklyCoachingEfficiency",
    "WeeklyFacts",
    "WeeklyFormatRef",
    "WeeklyHeadlinePlayer",
    "WeeklyHistory",
    "WeeklyHistoryRow",  # noqa: E501
    "WeeklyLeaderPlayer",
    "WeeklyLeaderRef",
    "WeeklyLeaders",
    "WeeklyLeagueRef",
    "WeeklyMarketNote",
    "WeeklyMatchup",
    "WeeklyMatchups",
    "WeeklyMove",
    "WeeklyMovePlayer",
    "WeeklyNarration",
    "WeeklyNarrationGame",
    "WeeklyNarrationLeague",
    "WeeklyNarrationLuck",
    "WeeklyNarrationNextWeek",
    "WeeklyNarrationPlayoff",
    "WeeklyNarrationPower",
    "WeeklyNarrationStanding",
    "WeeklyNarrationTransactions",
    "WeeklyNextWeekCard",
    "WeeklyPerformer",
    "WeeklyPeriod",
    "WeeklyPickRef",
    "WeeklyPlayoffPicture",
    "WeeklyPlayoffRef",
    "WeeklyPower",
    "WeeklyRecord",
    "WeeklySeason",
    "WeeklyStandings",
    "WeeklyStarter",
    "WeeklyStreak",
    "WeeklyTeam",
    "WeeklyTeamGame",
    "WeeklyTeamNextWeek",
    "WeeklyTrade",
    "WeeklyTradeSide",
    "WeeklyTransactions",
    "WeeklyWeekHighPlayer",
    "WeeklyWeekPoints",
]

SCHEMA_VERSION = "0.5.0"
"""Semver contract version. Additive key -> minor bump; shape change -> major.
``0.4.0`` -> ``0.5.0`` (Story 5.8): the empty ``WeeklyFacts`` placeholder becomes
the real weekly document and its ``WeeklyNarration``; ``DraftRecapFacts.weekly``
is retyped ``None`` (reserved, always null)."""

_ISSUE_TYPE: Literal["draft_recap"] = "draft_recap"
_IssueType = Literal["draft_recap", "weekly"]
_WEEKLY_ISSUE_TYPE: Literal["weekly"] = "weekly"

NARRATION_TOKEN_CAP: Final[int] = 40_000
#: Raised from 20,000 at ``0.3.0``, when the ``players`` block joined the
#: projection. It is a pathological-input guard, not a budget, and the payload
#: got legitimately richer on purpose: a 12-team / 15-round league now serializes
#: to ~32,600 characters against ~3,800 before. Even at the new ceiling the input
#: side of one generation is well under a cent on a cheap model, while the guard
#: still stops a malformed league from sending an unbounded payload.
"""Char-proxy ceiling on ``len(Narration.model_dump_json())`` (~5k tokens). Chosen
generous enough that no real or fixture league is ever truncated — the demo
fixture's narration serializes to well under half this — while still bounding a
synthetic oversized league. Enforced by the fixed reduction ladder in
``facts/build.py::_apply_narration_cap`` (and, from Story 5.8,
``facts/weekly.py``'s own ladder). If a document is still over the cap after the
final tier, the builder raises ``SchemaValidationError`` rather than shipping an
unbounded payload."""


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
    #: The pick's round.slot (0.4.0). With only ``pick_no`` a narrator converts
    #: the slot itself and, measured live, lands two off ("Tai Felton at 5.04"
    #: for a 5.06 pick, three Issues in ten).
    board_label: str | None = None
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
    """A "still arguing about it in December" angle — the narrator projection of an
    *active* :class:`Storyline`. Populated by Story 3.1's
    ``facts/storylines.py::project_storyline_candidates``: ``id`` is
    ``"<kind>:<roster_id>"``, ``kind`` is one of
    ``storylines.STORYLINE_KIND_PRIORITY``, ``roster_ids`` attributes the thread,
    and ``hook`` is the thread's one-sentence summary."""

    id: str
    kind: str
    roster_ids: list[str] = []
    hook: str | None = None


class Storyline(BaseModel):
    """A running narrative thread the facts builder opens, updates, and closes
    across a league's weeks (AD-14). Relocated from ``store.py`` in Story 3.1 so
    ``facts/`` owns the definition; ``commishdesk.store`` imports it back for its
    ``read_storylines`` / ``write_storylines`` port. Internal engine state,
    evolvable per story — deliberately **not** part of the versioned Facts
    contract and carries no ``schema_version``.

    ``id`` is ``"<kind>:<roster_id>"`` (the encoding
    ``project_storyline_candidates`` decodes); ``headline`` is the one-sentence
    summary the narrator may surface; ``first_week`` / ``last_week`` bound the
    thread's life (a draft recap anchors both at week 1).

    ``kind`` (Story 5.1) discriminates which *Issue category* persisted this
    row — ``draft_recap`` vs a future Epic-5 ``weekly`` — so a draft-time and a
    week-1 storyline that happen to share an ``id`` (the same firing signal)
    stay two independent rows instead of merging. This is a different axis
    from the storyline *signal* kind already embedded in ``id``'s
    ``"<kind>:<roster_id>"`` prefix (e.g. ``grade_extreme``) — the two are
    unrelated and both named ``kind`` in their own contexts. Defaults to
    ``"draft_recap"`` so every already-persisted (pre-Story-5.1) storyline
    round-trips unchanged with no migration."""

    id: str
    league_id: str
    headline: str
    status: Literal["active", "resolved"]
    first_week: int = Field(ge=1, le=18)
    last_week: int = Field(ge=1, le=18)
    notes: str = ""
    kind: _IssueType = _ISSUE_TYPE

    @model_validator(mode="after")
    def _week_span_ordered(self) -> Storyline:
        """A thread cannot close before it opened."""
        if self.last_week < self.first_week:
            raise ValueError(f"last_week ({self.last_week}) precedes first_week ({self.first_week})")
        return self


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


class NarrationPlayer(_Doc):
    """One drafted player's real, publishable roster facts.

    Added at :data:`SCHEMA_VERSION` ``0.3.0``. The measured reason: a narrator
    with no player metadata in its payload reaches into its own pretraining for
    it — a live validation run shipped "Ohio State wide receiver Emeka Egbuka"
    and "Michigan running back Donovan Edwards", true in the real world and
    unsupported by the Facts. The engine was already *fetching* every field
    below and simply not handing them over.

    Projecting them does two things at once: the narrator can cite a player's
    team or status **grounded**, and the closed-world check gains the tokens to
    *refute* a wrong one. Banning the vocabulary instead would have bought
    neither.

    Availability differs by field, and the prompt is conditional rather than
    absolute because of it: ``nfl_team`` / ``injury_status`` / ``years_exp``
    ride on the pick's own metadata and are always present; ``college`` is
    joined from the bundle's ``players`` blob, which the Sleeper adapter
    deliberately never fetches (AD: no live ``/players/nfl``, so a recap
    regenerated months later cannot change). On the live path ``college`` is
    therefore ``None`` — and a narrator told "only what the JSON gives you"
    simply says nothing about it.
    """

    name: str
    #: Where this player went, spelled both ways the copy will want them. Both
    #: are load-bearing for the closed-world check: a narrator told to cite a
    #: figure for every claim converts freely between "2.02" and "pick 14", and
    #: a payload carrying only one of the two reports every correct conversion
    #: as a hallucination (measured: 81 findings in 40 generations).
    pick_no: int | None = None
    board_label: str | None = None
    manager: str | None = None
    position: str | None = None
    nfl_team: str | None = None
    college: str | None = None
    #: Sleeper's own status string ("Questionable", "Out", "IR"). ``None`` for a
    #: healthy player — Sleeper writes "" there, normalized to None at ingest.
    injury_status: str | None = None
    #: 0 for a rookie.
    years_exp: int | None = None


class Narration(_Doc):
    """The sanitized projection the narrators see — never a raw roster or a board
    column it does not need. The builder holds this under
    :data:`NARRATION_TOKEN_CAP` via a fixed reduction ladder (Story 3.1)."""

    issue_type: _IssueType = _ISSUE_TYPE
    league: NarrationLeague
    headline_numbers: HeadlineNumbers
    #: One entry per drafted player (0.3.0). Ordered by first pick so two builds
    #: of one bundle agree byte-for-byte.
    players: list[NarrationPlayer] = []
    board_round1: list[BoardPick] = []
    superlatives: Superlatives
    teams: list[NarrationTeam] = []
    positional_runs: PositionalRunsSummary
    lead_candidates: list[LeadCandidate] = []
    storyline_candidates: list[StorylineCandidate] = []


# --------------------------------------------------------------------------- #
# weekly issue — the standalone weekly Facts document (Story 5.8)
# --------------------------------------------------------------------------- #


class DivisionRef(_Doc):
    """One declared league division, as data."""

    id: int
    name: str | None = None


class WeeklyPlayoffRef(_Doc):
    """The league's playoff shape on the weekly document: how many teams make
    the bracket, how many first-round byes that implies, and the week the
    bracket starts."""

    bracket_teams: int
    byes: int
    start_week: int


class WeeklyFormatRef(_Doc):
    """``FormatRef`` plus the weekly-only shape: the declared divisions, the
    playoff window, and the regular-season week count. All as data — no
    downstream stage branches on a hardcoded league shape."""

    team_count: int
    roster_slots: list[str]
    flex_eligibility: dict[str, list[str]]
    scoring_label: str
    is_superflex_or_2qb: bool
    te_premium: bool
    divisions: list[DivisionRef] = []
    playoff: WeeklyPlayoffRef | None = None
    regular_season_weeks: int | None = None


class WeeklyLeagueRef(_Doc):
    """League identity on the weekly document (``LeagueRef`` with the richer
    weekly ``format``)."""

    id: str
    name: str
    season: str
    platform: str
    format: WeeklyFormatRef


class TeamPointsRef(_Doc):
    """One roster's points for the week — the shape of
    :attr:`WeekSummaryRef.high` / :attr:`WeekSummaryRef.low`."""

    roster_id: str
    points: float


class WeekMarginRef(_Doc):
    """One game's margin, identified by its two participants directly (the
    weekly Facts contract carries ``roster_ids``, never a matchup index)."""

    roster_ids: list[str]
    margin: float


class WeekSummaryRef(_Doc):
    """League-wide shape of the target week, mirrored from
    ``stats/weekly.py::WeekSummary``."""

    games: int
    total_points: float
    avg_team_score: float
    high: TeamPointsRef | None = None
    low: TeamPointsRef | None = None
    closest: WeekMarginRef | None = None
    biggest_blowout: WeekMarginRef | None = None
    blowout_count: int
    blowout_threshold: float


class WeeklyPeriod(_Doc):
    """The week this document covers: its type, whether it has prior-week
    history, the NFL teams on bye this week and next (``None`` when the caller
    supplied no bye data), and the league-wide :class:`WeekSummaryRef`."""

    week: int
    type: str
    has_prior_week: bool
    nfl_byes: list[str] | None = None
    nfl_byes_next_week: list[str] | None = None
    summary: WeekSummaryRef


class WeeklyRecord(_Doc):
    """A win-loss-tie triple."""

    w: int
    l: int  # noqa: E741 -- matches the golden's record{w,l,t} key verbatim
    t: int


class WeeklyWeekPoints(_Doc):
    """One roster's points in one week."""

    week: int
    points: float


class WeeklyStreak(_Doc):
    """One roster's current run of identical results."""

    type: str
    count: int


class WeeklyAllPlay(_Doc):
    """One roster's cumulative all-play round-robin record."""

    w: int
    l: int  # noqa: E741 -- matches the golden's all_play{w,l,t} key verbatim
    t: int
    pct: float


class WeeklyCoachingEfficiency(_Doc):
    """``actual / optimal`` for one roster, either for a single week or summed
    over the season. ``pct`` is ``None`` when ``optimal <= 0``."""

    actual: float
    optimal: float
    pct: float | None = None


class WeeklyPower(_Doc):
    """One roster's model-power row. ``published_rank`` / ``nudge`` are Story
    5.9's (AD-13) and are always ``None`` here; ``prev_model_rank`` /
    ``week_delta`` are the rank's movement since the prior week (both ``None``
    when there is no prior week, or either side is unranked)."""

    model_score: float | None = None
    model_rank: int | None = None
    published_rank: int | None = None
    nudge: int | None = None
    prev_model_rank: int | None = None
    week_delta: int | None = None


class WeeklySeason(_Doc):
    """One roster's whole-season block: the head-to-head fold, the all-play
    fold, the model power rank, and the season-long coaching efficiency."""

    record: WeeklyRecord
    rank: int
    division_rank: int | None = None
    points_for: float
    points_against: float
    avg_for: float
    high_week: WeeklyWeekPoints | None = None
    low_week: WeeklyWeekPoints | None = None
    streak: WeeklyStreak | None = None
    all_play: WeeklyAllPlay | None = None
    expected_wins: float | None = None
    luck: float | None = None
    power: WeeklyPower
    coaching_efficiency: WeeklyCoachingEfficiency


class WeeklyStarter(_Doc):
    """One slot of a roster's week lineup. ``player_id`` / ``name`` / ``pos`` /
    ``nfl_team`` are ``None`` for an unfilled slot."""

    player_id: str | None = None
    name: str | None = None
    pos: str | None = None
    nfl_team: str | None = None
    slot: str
    points: float


class WeeklyPerformer(_Doc):
    """One named player with a week point total, ids alongside names."""

    player_id: str
    name: str
    pos: str | None = None
    points: float


class WeeklyBenchRegret(_Doc):
    """The manager's single worst benching decision."""

    player_id: str
    name: str
    pos: str | None = None
    points: float


class WeeklyByeStarter(_Doc):
    """One starter whose NFL team is on bye. ``points`` is only populated on the
    week-n ``started_on_bye`` shape; it defaults to ``0.0`` on the next-week
    ``starters_on_bye`` shape."""

    player_id: str
    name: str
    pos: str | None = None
    nfl_team: str | None = None
    points: float = 0.0


class WeeklyTeamGame(_Doc):
    """One roster's target-week block. ``None`` for a roster with no game;
    ``started_on_bye`` is ``None`` when the caller supplied no bye data."""

    opponent_roster_id: str | None = None
    points: float
    opponent_points: float | None = None
    result: str | None = None
    margin: float | None = None
    coaching_efficiency: WeeklyCoachingEfficiency | None = None
    points_left_on_bench: float | None = None
    starters: list[WeeklyStarter] = []
    top_performers: list[WeeklyPerformer] = []
    duds: list[WeeklyPerformer] = []
    bench_regret: WeeklyBenchRegret | None = None
    started_on_bye: list[WeeklyByeStarter] | None = None


class WeeklyHistoryRow(_Doc):
    """One week of a roster's season, with the cumulative picture as of that
    week. ``power_rank`` / ``all_play`` / ``luck`` are ``None`` before
    ``MEANINGFUL_FROM_WEEK`` and past the regular season."""

    week: int
    points: float
    opponent_roster_id: str | None = None
    result: str | None = None
    margin: float | None = None
    cum_record: WeeklyRecord
    power_rank: int | None = None
    all_play: WeeklyAllPlay | None = None
    luck: float | None = None


class WeeklyHistory(_Doc):
    """One roster's weekly history, one row per week it played."""

    weekly: list[WeeklyHistoryRow] = []


class WeeklyTeamNextWeek(_Doc):
    """One roster's next-week preview. ``None`` when there is no card for it.
    ``starters_on_bye`` is empty when the caller supplied no bye data."""

    opponent_roster_id: str
    power_rank_self: int | None = None
    power_rank_opp: int | None = None
    stakes: list[str] = []
    starters_on_bye: list[WeeklyByeStarter] = []


class WeeklyTeam(_Doc):
    """One roster's whole weekly row: identity, season block, this week, history
    and next week — in numeric roster order."""

    roster_id: str
    manager: str | None = None
    team_name: str | None = None
    division_id: int | None = None
    co_owners: list[str] = []
    season: WeeklySeason
    this_week: WeeklyTeamGame | None = None
    history: WeeklyHistory
    next_week: WeeklyTeamNextWeek | None = None


class WeeklyHeadlinePlayer(_Doc):
    """One side's top started scorer in a resolved pairing."""

    roster_id: str
    player_id: str
    name: str
    pos: str | None = None
    points: float


class WeeklyMatchup(_Doc):
    """One resolved target-week pairing. ``home_roster_id`` is the lower numeric
    roster id; ``winner_roster_id`` is ``None`` on a tie. ``headline_players``
    is each side's top started scorer."""

    matchup_id: int | None = None
    home_roster_id: str
    away_roster_id: str
    home_points: float
    away_points: float
    winner_roster_id: str | None = None
    margin: float
    is_blowout: bool = False
    headline_players: list[WeeklyHeadlinePlayer] = []


class WeeklyByeImpact(_Doc):
    """One week-n starter whose NFL team is on bye next week."""

    roster_id: str
    player_id: str
    name: str
    pos: str | None = None
    nfl_team: str | None = None


class WeeklyNextWeekCard(_Doc):
    """One week-``n+1`` pairing, flattened: each side's roster id, power rank,
    record and clinch/elimination flags, the card's stakes union, the flagged
    bye starters, and whether this is the game of the week."""

    matchup_id: int | None = None
    a_roster_id: str
    b_roster_id: str
    a_power_rank: int | None = None
    b_power_rank: int | None = None
    a_record: str
    b_record: str
    a_clinched_playoff: bool = False
    a_clinched_bye: bool = False
    a_eliminated: bool = False
    a_clinched_division: bool = False
    b_clinched_playoff: bool = False
    b_clinched_bye: bool = False
    b_eliminated: bool = False
    b_clinched_division: bool = False
    stakes: list[str] = []
    #: ``None`` -- never ``[]`` -- when the caller supplied no bye data.
    bye_impact: list[WeeklyByeImpact] | None = None
    game_of_week: bool = False


class WeeklyMatchups(_Doc):
    """The target week's resolved pairings, and the next week's cards."""

    this_week: list[WeeklyMatchup] = []
    next_week: list[WeeklyNextWeekCard] = []


class WeeklyPlayoffPicture(_Doc):
    """The derived playoff picture, mirrored from
    ``stats/standings.py::PlayoffPicture``."""

    source: str
    in_bracket: list[str] = []
    byes: list[str] = []
    first_out: str | None = None
    bubble: list[str] = []
    cut_line_after_rank: int
    consolation: list[str] = []


class WeeklyStandings(_Doc):
    """The standings block: the overall order, the per-division order, the
    derived playoff picture, and how far the fold reached."""

    overall: list[str] = []
    divisions: dict[int, list[str]] = {}
    playoff_picture: WeeklyPlayoffPicture | None = None
    through_week: int
    regular_season_complete: bool = False


class WeeklyMovePlayer(_Doc):
    """One player moved in a transaction, with the side it moved for."""

    player_id: str
    name: str
    pos: str | None = None
    roster_id: str


class WeeklyMove(_Doc):
    """One settled transaction from the target week, with names."""

    transaction_id: str
    type: str
    roster_ids: list[str] = []
    adds: list[WeeklyMovePlayer] = []
    drops: list[WeeklyMovePlayer] = []
    faab: int | None = None


class WeeklyPickRef(_Doc):
    """One draft pick that moved in a trade."""

    season: str
    round: int
    from_roster_id: str


class WeeklyTradeSide(_Doc):
    """One participating roster's receipts from a trade, with names."""

    roster_id: str
    players: list[WeeklyMovePlayer] = []
    picks: list[WeeklyPickRef] = []
    faab: int = 0


class WeeklyTrade(_Doc):
    """One settled trade with its sides."""

    week: int
    transaction_id: str
    sides: list[WeeklyTradeSide] = []


class WeeklyMarketNote(_Doc):
    """When the league last traded. ``complete`` ``False`` (with both week
    fields ``None``) means a partial history could not tell."""

    last_trade_week: int | None = None
    weeks_since_last_trade: int | None = None
    complete: bool = False


class WeeklyTransactions(_Doc):
    """The transactions desk, with names."""

    this_week: list[WeeklyMove] = []
    recent_trades: list[WeeklyTrade] = []
    market_note: WeeklyMarketNote


class WeeklyLeaderPlayer(_Doc):
    """One named player in a league-wide leaders list."""

    roster_id: str
    player_id: str
    name: str
    pos: str | None = None
    points: float


class WeeklyWeekHighPlayer(WeeklyLeaderPlayer):
    """``week_high_player`` only: whether this week's high score is also a new
    season high (``points >= every prior week's best``, ties count)."""

    is_season_high: bool


class WeeklyLeaderRef(_Doc):
    """A ``{roster_id, pct}`` coaching-efficiency leader."""

    roster_id: str
    pct: float | None = None


class WeeklyLeaders(_Doc):
    """The week's league-wide leaders: the high player, the top players, the
    worst starters, and the best / worst coaching weeks."""

    week_high_player: WeeklyWeekHighPlayer | None = None
    top_players: list[WeeklyLeaderPlayer] = []
    worst_starters: list[WeeklyLeaderPlayer] = []
    best_coaching: WeeklyLeaderRef | None = None
    worst_coaching: WeeklyLeaderRef | None = None


class WeeklyNarrationLeague(_Doc):
    """Just enough league identity for a weekly headline."""

    name: str
    season: str
    week: int
    team_count: int
    divisions: list[str] = []


class WeeklyNarrationGame(_Doc):
    """One narrated game: the winner / loser labels, their points, the margin,
    and the game's top scorer label."""

    matchup_id: int | None = None
    winner: str | None = None
    winner_pts: float
    loser: str | None = None
    loser_pts: float
    margin: float
    top: str | None = None


class WeeklyNarrationStanding(_Doc):
    """One narrated standings row."""

    rank: int
    team: str
    roster_id: str
    rec: str
    pf: float
    model_rank: int | None = None


class WeeklyNarrationPlayoff(_Doc):
    """The narrated playoff picture, teams resolved to labels."""

    format: str
    in_bracket: list[str] = []
    byes: list[str] = []
    first_out: str | None = None
    bubble: list[str] = []


class WeeklyNarrationPower(_Doc):
    """One narrated power-rank row. ``all_play`` is the first field the
    narration reduction ladder drops."""

    model_rank: int | None = None
    team: str
    roster_id: str
    rec: str
    avg_pf: float
    all_play: str | None = None
    luck: float | None = None
    week_delta: int | None = None


class WeeklyNarrationLuck(_Doc):
    """One narrated luck row."""

    team: str
    roster_id: str
    rec: str
    all_play: str | None = None
    earned_wins: float | None = None
    luck: float | None = None


class WeeklyNarrationNextWeek(_Doc):
    """One narrated next-week card, both sides resolved to labels."""

    matchup_id: int | None = None
    a: str
    a_roster_id: str
    a_rec: str
    a_model_rank: int | None = None
    b: str
    b_roster_id: str
    b_rec: str
    b_model_rank: int | None = None
    stakes: list[str] = []
    game_of_week: bool = False


class WeeklyNarrationTransactions(_Doc):
    """The narrated transactions glance."""

    last_trade_week: int | None = None
    weeks_since_last_trade: int | None = None
    complete: bool = False
    this_week_count: int = 0
    recent_trade_count: int = 0


class WeeklyNarration(_Doc):
    """The sanitized weekly projection the narrator sees — ids resolved to a
    ``team`` label (``team_name or manager or "Roster <id>"``), with the
    ``roster_id`` always alongside. No free prose. Held under
    :data:`NARRATION_TOKEN_CAP` by ``facts/weekly.py``'s reduction ladder."""

    league: WeeklyNarrationLeague
    week_shape: str
    games: list[WeeklyNarrationGame] = []
    standings: list[WeeklyNarrationStanding] = []
    playoff_picture: WeeklyNarrationPlayoff | None = None
    power: list[WeeklyNarrationPower] = []
    luck: list[WeeklyNarrationLuck] = []
    next_week: list[WeeklyNarrationNextWeek] = []
    next_week_nfl_byes: list[str] | None = None
    transactions: WeeklyNarrationTransactions
    lead_candidates: list[LeadCandidate] = []
    storyline_candidates: list[StorylineCandidate] = []


# --------------------------------------------------------------------------- #
# roots
# --------------------------------------------------------------------------- #


class DraftRecapFacts(_Doc):
    """The whole draft-recap Facts JSON — the contract every narrator and
    renderer downstream of ``facts/`` reads for a ``draft_recap`` Issue (AD-2).
    Top-level key order matches ``brief/phase-0/draft-recap-facts.json``.

    ``weekly`` is reserved and always ``None`` (Story 5.8): the weekly document
    is its own standalone root (:class:`WeeklyFacts`), and this field is kept —
    never removed — so a ``0.4.0`` draft-recap document still loads and renders."""

    schema_version: str = SCHEMA_VERSION
    generated_at: str
    provisional: bool = True
    issue_type: _IssueType = _ISSUE_TYPE
    #: The NFL week for an ``issue_type == "weekly"`` document; ``None`` for a
    #: draft recap. Bounded ``1..18`` to match :attr:`Storyline.first_week`.
    week: int | None = Field(default=None, ge=1, le=18)
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
    #: Reserved, always ``None`` — the weekly document is ``WeeklyFacts``.
    weekly: None = None

    @model_validator(mode="after")
    def _issue_type_consistency(self) -> DraftRecapFacts:
        """A ``draft_recap`` carries no ``week`` and no ``weekly`` block; a
        ``weekly`` issue must name its week."""
        if self.issue_type == "draft_recap":
            if self.week is not None or self.weekly is not None:
                raise ValueError("draft_recap document must have week=None and weekly=None")
        elif self.issue_type == "weekly" and self.week is None:
            raise ValueError("weekly document must have a week (1..18)")
        return self


class WeeklyFacts(_Doc):
    """The whole weekly Facts JSON — a standalone root laid out like the private
    ``week10-facts.json`` golden, built by ``facts/weekly.py::build_weekly_facts``
    (Story 5.8). Every computed number comes from ``commishdesk.stats``; names
    are joined from the bundle's ``players`` blob."""

    schema_version: str = SCHEMA_VERSION
    generated_at: str
    provisional: bool = True
    issue_type: Literal["weekly"] = _WEEKLY_ISSUE_TYPE
    week: int = Field(ge=1, le=18)
    source: Source
    league: WeeklyLeagueRef
    period: WeeklyPeriod
    teams: list[WeeklyTeam] = []
    matchups: WeeklyMatchups
    standings: WeeklyStandings
    transactions: WeeklyTransactions
    leaders: WeeklyLeaders
    #: Both always ``[]`` here — Story 5.9 fills them.
    lead_candidates: list[LeadCandidate] = []
    storyline_candidates: list[StorylineCandidate] = []
    narration: WeeklyNarration
