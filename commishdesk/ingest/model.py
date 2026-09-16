"""The shape-agnostic stage-1 domain model (AD-1, facts-schema design rule 2).

``build.py`` produces one :class:`LeagueModel` from a raw platform bundle. Every
league-shape fact that downstream code might otherwise branch on -- team count,
the ordered starting lineup, which positions each flex slot accepts, the scoring
label, the divisions -- is carried here as *data* on :class:`LeagueFormat`, so no
stage after ingest ever hardcodes "12 teams" or "one QB slot".

All models are ``frozen`` -- attribute reassignment is rejected; note the
collection fields (``roster_slots``, ``flex_eligibility``, ``co_owners``,
``teams``, ``picks``, ``divisions``) are not deep-frozen, so the models are not
hashable. ``extra="forbid"`` guards against a builder mistake and self-documents
the closed field set (the builder passes fixed kwargs, so an unknown *bundle*
key is simply ignored upstream). Ids that Sleeper hands us as strings stay
strings; ``season`` is an ``int`` year. Every string that originates with the
league has already been through ``sanitize()`` by the time it reaches a field
here.

Story 5.3a adds a second, parallel family: :class:`Roster`, :class:`Matchup`,
:class:`Transaction` (plus :class:`TradedPick` / :class:`FaabTransfer`) and the
:class:`WeekModel` container they build into, produced by
:func:`~commishdesk.ingest.build.build_week_model` from a weekly ``Adapter``
bundle. Same conventions -- frozen, ``extra="forbid"``, ids stay ``str``. These
carry only ids/enums/numbers -- matchups and transactions stay at
``player_id``/``roster_id`` level, never joined to a display name, so no
league-supplied free text is introduced and ``sanitize()`` has no call site
here (a future facts-building story's job).

Story 5.3b adds :class:`PlayerSnapshot`, kept deliberately separate from
:class:`WeekModel` for the same id-only reason -- see its own docstring.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Division",
    "Draft",
    "FaabTransfer",
    "LeagueFormat",
    "LeagueModel",
    "Matchup",
    "Pick",
    "Player",
    "PlayerSnapshot",
    "Roster",
    "Team",
    "TradedPick",
    "Transaction",
    "WeekModel",
]


class _Frozen(BaseModel):
    """Shared config: immutable (attribute reassignment rejected; collection
    fields are not deep-frozen) and closed to unknown keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Division(_Frozen):
    """One league division. ``name`` is ``None`` when the platform names none."""

    id: int
    name: str | None = None


class LeagueFormat(_Frozen):
    """The league's shape, as data. No downstream stage branches on a hardcoded
    league shape -- it reads these fields instead."""

    #: Must be positive (retro finding C1): a zero team count reaches a bare
    #: division in ``stats/consensus.py``'s round/column arithmetic, which
    #: would otherwise surface as an uncaught ``ZeroDivisionError`` instead of
    #: the typed ``IngestError`` this bound produces at the stage-1 boundary.
    team_count: int = Field(gt=0)
    roster_slots: list[str]
    flex_eligibility: dict[str, list[str]]
    scoring_label: str
    is_superflex_or_2qb: bool
    te_premium: bool
    divisions: list[Division] = []


class Team(_Frozen):
    """One roster, joined to its owning user. Co-owned and orphan rosters are
    tolerated: ``manager`` / ``team_name`` are ``None`` for an orphan (no owner,
    or an owner absent from the users list), and never raise."""

    roster_id: str
    owner_id: str | None = None
    manager: str | None = None
    team_name: str | None = None
    co_owners: list[str] = []
    division_id: int | None = None


class Player(_Frozen):
    """A drafted player, snapshotted from the pick record's own ``metadata`` --
    never a live ``/players/nfl`` lookup, so regenerating the recap months later
    never changes the facts. Player names are NFL data and are not sanitized."""

    sleeper_id: str
    name: str
    position: str | None = None
    nfl_team: str | None = None
    #: Real, publishable roster facts the bundle already carries. They exist so
    #: the narrator can cite them from the payload instead of reaching into its
    #: own pretraining for them -- the measured failure that produced "Ohio
    #: State wide receiver Emeka Egbuka" in copy the Facts could not support.
    #: ``college`` is joined from the bundle's ``players`` blob; the rest ride
    #: on the pick's own metadata.
    college: str | None = None
    injury_status: str | None = None
    years_exp: int | None = None


class Pick(_Frozen):
    """One draft selection. ``board_label`` is ``f"{round}.{slot:02d}"``;
    ``manager`` is the sanitized display name of whoever made the pick (or
    ``None`` if unknown)."""

    pick_no: int
    round: int
    slot: int
    board_label: str
    roster_id: str
    manager: str | None
    player: Player


class Draft(_Frozen):
    """Draft-level metadata. Epoch-ms timestamps are carried raw (Story 2.2's
    Design Notes deferred ISO 8601 conversion to Story 2.5)."""

    id: str
    type: str | None = None
    rounds: int | None = None
    started_at_ms: int | None = None
    completed_at_ms: int | None = None


class LeagueModel(_Frozen):
    """The whole stage-1 output: the league identity, its format-as-data, one
    :class:`Team` per roster, one :class:`Pick` per selection, and the draft.

    ``teams`` is ordered by ``roster_id`` (numerically where the ids parse as
    ints) and ``picks`` by ascending ``pick_no``, so two builds of one bundle
    produce equal models regardless of the bundle's list order."""

    league_id: str
    name: str
    season: int
    platform: str = "sleeper"
    format: LeagueFormat
    teams: list[Team]
    picks: list[Pick]
    draft: Draft


# --------------------------------------------------------------------------- #
# Story 5.3a: weekly ingest (rosters / matchups / transactions)
# --------------------------------------------------------------------------- #


class Roster(_Frozen):
    """One roster's season-cumulative state as of the week this model was
    built for: record, points totals, and current IR/taxi occupants. Never
    raises for a missing ``settings`` sub-object -- absent numeric fields
    default to ``0``."""

    roster_id: str
    wins: int = 0
    losses: int = 0
    ties: int = 0
    fpts: float = 0.0
    fpts_against: float = 0.0
    ppts: float = 0.0
    ir: list[str] = []
    taxi: list[str] = []


class Matchup(_Frozen):
    """One roster's participation in one week: which opponent it was paired
    against (``None`` for a bye / no-opponent week), the week's point total,
    the starting lineup, the bench, and every rostered player's points for the
    week. A roster with no opponent, an orphan roster (absent from
    :class:`Roster`), or an empty starter slot builds without raising. Player
    and roster ids only -- no display-name join (a future facts-building
    story's job)."""

    week: int
    roster_id: str
    matchup_id: int | None = None
    opponent_roster_id: str | None = None
    points: float = 0.0
    starters: list[str] = []
    starters_points: list[float] = []
    bench: list[str] = []
    players_points: dict[str, float] = {}


class TradedPick(_Frozen):
    """One draft pick that changed hands as part of a :class:`Transaction`."""

    season: str
    round: int
    roster_id: str
    owner_id: str | None = None
    previous_owner_id: str | None = None


class FaabTransfer(_Frozen):
    """One FAAB (waiver budget) transfer between rosters, part of a trade."""

    sender: str
    receiver: str
    amount: int = 0


class Transaction(_Frozen):
    """One settled (``status == "complete"``) roster move: a waiver claim, a
    free-agent add/drop, or a trade. Assets moved are carried as ids only --
    ``player_id``/``roster_id``, draft picks, FAAB -- never a player display
    name; a real transaction's ``metadata`` (where a commissioner note could
    live) is never carried into this model."""

    transaction_id: str
    type: str
    status: str
    roster_ids: list[str] = []
    adds: dict[str, str] = {}
    drops: dict[str, str] = {}
    draft_picks: list[TradedPick] = []
    faab: list[FaabTransfer] = []
    waiver_bid: int | None = None


class WeekModel(_Frozen):
    """The whole stage-1 weekly-ingest output for one league-week: every
    roster's season-cumulative state, one :class:`Matchup` per roster for
    every week ``1..week`` (Story 5.4's cumulative all-play record needs the
    full history), and ``week``'s settled :class:`Transaction` log.

    ``rosters`` is ordered by ``roster_id``, ``matchups`` by ``(week,
    roster_id)``, and ``transactions`` by ``transaction_id`` -- so two builds
    of one bundle produce equal models regardless of the bundle's list
    order."""

    week: int
    rosters: list[Roster]
    matchups: list[Matchup]
    transactions: list[Transaction]


# --------------------------------------------------------------------------- #
# Story 5.3b: NFL bye data and the player snapshot
# --------------------------------------------------------------------------- #


class PlayerSnapshot(_Frozen):
    """One player's NFL team/position as of a given league-week, persisted via
    ``Store.write_player_snapshot`` on first generation so a later
    regeneration (after a trade) reuses it verbatim instead of re-deriving it
    live (FR-5). Deliberately **not** part of :class:`WeekModel` -- matchups
    and transactions stay id-only by this family's own convention (see the
    module docstring).

    Only short enum-like codes, never free text: ``player_id`` is an NFL
    platform id, ``position`` and ``nfl_team`` are short abbreviations
    sourced from the platform's own player table, never league-supplied, so
    ``ingest/sanitize.py`` gets no new call site here -- the same precedent
    :class:`Player` already set for draft-scoped player data."""

    player_id: str
    position: str | None = None
    nfl_team: str | None = None
