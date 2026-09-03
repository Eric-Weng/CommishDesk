"""Stage 3 builder — merge the four frozen stage results into a validated Facts JSON.

:func:`build_draft_recap_facts` is the one place ``LeagueModel`` +
``BoardMetrics`` + ``ConsensusMetrics`` + ``DraftGrades`` become the single
published ``draft_recap`` contract (AD-2). It is pure, deterministic, and
offline: no network, no clock, no filesystem. ``generated_at`` is a caller
argument, so two builds of one input produce an equal ``model_dump()`` modulo
nothing (I4).

Data that is not carried on a stage result — ``source.draft_id`` /
``source.fetched_at`` and ``consensus_source.{name, as_of}`` (which live on the
caller's ``ConsensusRank``) — are plain keyword arguments the Story 2.7 CLI
supplies. ``commishdesk.consensus`` is **not** imported: it does network + store
I/O and sits outside the pipeline, so only its two scalars cross this boundary.

Merge keys:

* ``picks[]``  = ``Pick`` ⋈ ``PickConsensus`` on ``pick_no``
* ``teams[]``  = ``Team`` ⋈ ``TeamBoard`` ⋈ ``TeamConsensus`` ⋈ ``TeamGrade`` on
  ``roster_id``, in ``league.teams`` order

The builder validates its own output — it constructs :class:`DraftRecapFacts`,
then round-trips ``DraftRecapFacts.model_validate(doc.model_dump())`` — and any
``pydantic.ValidationError`` from either step (or a ``KeyError`` / ``AttributeError``
/ ``TypeError`` / ``ValueError`` from a malformed stage object) is re-raised as
:class:`~commishdesk.errors.SchemaValidationError`, chained, with a summary of the
underlying error and no partial document returned. No consumer downstream
re-validates.

This module imports only ``commishdesk.ingest`` + ``commishdesk.stats`` (prior
stages), stdlib, and pydantic — nothing from ``adapters`` / ``consensus`` /
``narrate`` / ``render`` / ``deliver`` / ``store`` / ``httpx``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from commishdesk.errors import SchemaValidationError
from commishdesk.ingest import LeagueModel
from commishdesk.stats import BoardMetrics, ConsensusMetrics, DraftGrades

from .leads import build_lead_candidates
from .schema import (
    BoardPick,
    BoldestSwing,
    ConsensusSource,
    DraftRecapFacts,
    DraftRef,
    DraftSummary,
    FormatRef,
    GradeMethodRef,
    GradeRef,
    HeadlineNumbers,
    LeadCandidate,
    LeagueRef,
    ManagerCount,
    ManagerPickCount,
    Narration,
    NarrationLeague,
    NarrationTeam,
    PickExtreme,
    PickRow,
    PlayerRef,
    PositionalRunsSummary,
    QBRunSummary,
    RBRunSummary,
    RoundConcentration,
    Source,
    Superlatives,
    SuperlativePick,
    TeamRow,
    TERunSummary,
)

__all__ = ["build_draft_recap_facts"]

_ENGINE_NOTE = (
    "in production, FantasyCalc /values/current fetched once at generation and "
    "cached; Sleeper search_rank offline fallback"
)
"""Mirror of the phase-0 golden's ``consensus_source.engine_note`` — a constant of
the engine, not a per-league value."""

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_CANON_POSITIONS = ("QB", "RB", "WR", "TE")
_MIN_ROUND_CONCENTRATION = 3
"""A manager's heaviest round is reported only at this many picks or more."""

_UNK = "UNK"


def build_draft_recap_facts(
    league: LeagueModel,
    board: BoardMetrics,
    consensus: ConsensusMetrics,
    grades: DraftGrades,
    *,
    generated_at: datetime | str,
    draft_id: str | None = None,
    fetched_at: str | None = None,
    consensus_source_name: str | None = None,
    consensus_as_of: str | None = None,
    provisional: bool = True,
) -> DraftRecapFacts:
    """Merge the four stage results into a validated :class:`DraftRecapFacts`.

    Pure / deterministic / offline. ``generated_at`` is a ``datetime`` (rendered
    to UTC ISO 8601, ms precision, ``Z``-suffixed — consistent with :func:`_iso`)
    or a non-empty string (passed through). Raises
    :class:`~commishdesk.errors.SchemaValidationError` (chained from the
    underlying error) if ``generated_at`` is empty / ``None`` or the merged
    document does not satisfy the schema; returns no partial document.
    """
    generated_at_str = _generated_at(generated_at)
    resolved_draft_id = draft_id if draft_id is not None else league.draft.id
    try:
        pick_rows = _merge_picks(league, consensus)
        team_rows = _merge_teams(league, board, consensus, grades)
        draft_summary = _draft_summary(league, board, pick_rows)
        superlatives = _superlatives(league, pick_rows)
        lead_candidates = build_lead_candidates(
            league, board, consensus, grades, draft_summary, superlatives
        )
        league_ref = _league_ref(league)
        narration = _narration(
            league,
            league_ref,
            draft_summary,
            superlatives,
            pick_rows,
            team_rows,
            lead_candidates,
        )
        doc = DraftRecapFacts(
            generated_at=generated_at_str,
            provisional=provisional,
            source=Source(
                platform=league.platform,
                league_id=league.league_id,
                draft_id=resolved_draft_id,
                fetched_at=fetched_at,
            ),
            consensus_source=ConsensusSource(
                name=consensus_source_name,
                as_of=consensus_as_of,
                provisional=provisional,
                engine_note=_ENGINE_NOTE,
            ),
            league=league_ref,
            draft=DraftRef(
                id=resolved_draft_id,
                type=league.draft.type,
                rounds=league.draft.rounds,
                started_at=_iso(league.draft.started_at_ms),
                completed_at=_iso(league.draft.completed_at_ms),
            ),
            picks=pick_rows,
            teams=team_rows,
            draft_summary=draft_summary,
            superlatives=superlatives,
            grade_method=GradeMethodRef(**grades.grade_method.model_dump()),
            lead_candidates=lead_candidates,
            storyline_candidates=[],
            narration=narration,
        )
        _validate(doc)
    except (ValidationError, KeyError, AttributeError, TypeError, ValueError) as exc:
        raise SchemaValidationError(_violation_message(exc)) from exc
    return doc


def _violation_message(exc: Exception) -> str:
    """A loud, self-contained summary — the reader should not need ``__cause__``."""
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        parts = [
            f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg', '')}"
            for err in errors[:3]
        ]
        detail = "; ".join(parts)
        if len(errors) > 3:
            detail += f"; (+{len(errors) - 3} more)"
    else:
        detail = f"{type(exc).__name__}: {exc}"[:200]
    return f"draft_recap Facts JSON failed schema validation — {detail}"


# --------------------------------------------------------------------------- #
# Timestamps — the Story 2.2 deferral
# --------------------------------------------------------------------------- #


def _generated_at(value: datetime | str) -> str:
    """``datetime`` -> UTC ISO 8601 (ms precision, ``Z``); non-empty ``str`` ->
    passed through. ``None`` / empty / whitespace / an unrepresentable datetime
    raises :class:`~commishdesk.errors.SchemaValidationError`."""
    if isinstance(value, datetime):
        iso = _iso_datetime(value)
        if iso is None:
            raise SchemaValidationError(
                "generated_at is not a representable datetime"
            )
        return iso
    if isinstance(value, str) and value.strip():
        return value
    raise SchemaValidationError(
        "generated_at must be a non-empty string or a datetime"
    )


def _iso_datetime(stamp: datetime) -> str | None:
    """A ``datetime`` (naive assumed UTC) -> ``"%Y-%m-%dT%H:%M:%S.%fZ"`` truncated
    to milliseconds; ``None`` if the year is not four digits or it will not
    format."""
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    else:
        stamp = stamp.astimezone(timezone.utc)
    try:
        text = stamp.strftime("%Y-%m-%dT%H:%M:%S.%f")
    except (OverflowError, OSError, ValueError):
        return None
    if len(text.split("-", 1)[0]) != 4:
        return None
    return text[:-3] + "Z"


def _iso(ms: int | None) -> str | None:
    """Epoch milliseconds -> UTC ISO 8601 truncated to milliseconds
    (``1747494304432`` -> ``"2025-05-17T15:05:04.432Z"``). ``None`` passes
    through. This is the only timestamp math in ``facts/``."""
    if ms is None:
        return None
    try:
        stamp = _EPOCH + timedelta(milliseconds=ms)
        text = stamp.strftime("%Y-%m-%dT%H:%M:%S.%f")
    except (OverflowError, OSError, ValueError):
        return None
    if len(text.split("-", 1)[0]) != 4:
        return None
    return text[:-3] + "Z"


# --------------------------------------------------------------------------- #
# Merges
# --------------------------------------------------------------------------- #


def _merge_picks(league: LeagueModel, consensus: ConsensusMetrics) -> list[PickRow]:
    by_pick_no = {row.pick_no: row for row in consensus.picks}
    rows: list[PickRow] = []
    for pick in sorted(league.picks, key=lambda p: p.pick_no):
        cons = by_pick_no.get(pick.pick_no)
        rows.append(
            PickRow(
                pick_no=pick.pick_no,
                round=pick.round,
                slot=pick.slot,
                board_label=pick.board_label,
                roster_id=pick.roster_id,
                manager=pick.manager,
                player=PlayerRef(
                    sleeper_id=pick.player.sleeper_id,
                    name=pick.player.name,
                    position=pick.player.position,
                    nfl_team=pick.player.nfl_team,
                ),
                consensus_slot=cons.consensus_slot if cons else None,
                consensus_label=cons.consensus_label if cons else None,
                delta=cons.delta if cons else None,
                flags=list(cons.flags) if cons else ["no_consensus"],
            )
        )
    return rows


def _merge_teams(
    league: LeagueModel,
    board: BoardMetrics,
    consensus: ConsensusMetrics,
    grades: DraftGrades,
) -> list[TeamRow]:
    board_by_roster = {t.roster_id: t for t in board.teams}
    cons_by_roster = {t.roster_id: t for t in consensus.teams}
    grade_by_roster = {t.roster_id: t for t in grades.teams}
    rows: list[TeamRow] = []
    for team in league.teams:
        tb = board_by_roster.get(team.roster_id)
        tc = cons_by_roster.get(team.roster_id)
        tg = grade_by_roster.get(team.roster_id)
        rows.append(
            TeamRow(
                roster_id=team.roster_id,
                manager=team.manager,
                pick_count=tb.pick_count if tb else 0,
                pick_nos=list(tb.pick_nos) if tb else [],
                positional_counts=_canon_counts(tb.positional_counts if tb else {}),
                back_to_back=[tuple(pair) for pair in tb.back_to_back] if tb else [],
                best_value_pick=_extreme(tc.best_value_pick if tc else None),
                biggest_reach_pick=_extreme(tc.biggest_reach_pick if tc else None),
                draft_score=tg.draft_score if tg else None,
                premium_picks=tg.premium_picks if tg else None,
                format_fit=tg.format_fit if tg else None,
                grade_input=tg.grade_input if tg else None,
                grade=GradeRef(
                    letter=tg.letter if tg else None,
                    driving_picks=list(tg.driving_picks) if tg else [],
                ),
            )
        )
    return rows


def _canon_counts(counts: dict[str, int]) -> dict[str, int]:
    """``{QB, RB, WR, TE}`` in that order with zero-fill, then any other position
    (e.g. ``"UNK"``) appended in sorted order — matches the phase-0 golden's
    ``teams[].positional_counts`` shape."""
    merged: dict[str, int] = {pos: 0 for pos in _CANON_POSITIONS}
    for pos, n in counts.items():
        merged[pos] = merged.get(pos, 0) + n
    extras = sorted(pos for pos in merged if pos not in _CANON_POSITIONS)
    ordered = [*_CANON_POSITIONS, *extras]
    return {pos: merged[pos] for pos in ordered}


def _extreme(ref: object) -> PickExtreme | None:
    if ref is None:
        return None
    return PickExtreme(pick_no=ref.pick_no, player=ref.player, delta=ref.delta)


# --------------------------------------------------------------------------- #
# draft_summary
# --------------------------------------------------------------------------- #


def _position(row: PickRow) -> str:
    return row.player.position or _UNK


def _board_pick(row: PickRow) -> BoardPick:
    return BoardPick(
        pick_no=row.pick_no,
        board_label=row.board_label,
        manager=row.manager,
        player=row.player.name,
        position=row.player.position,
        consensus_label=row.consensus_label,
        delta=row.delta,
    )


def _draft_summary(
    league: LeagueModel, board: BoardMetrics, pick_rows: list[PickRow]
) -> DraftSummary:
    team_count = league.format.team_count
    ordered = sorted(pick_rows, key=lambda r: r.pick_no)
    managers = {t.roster_id: t.manager for t in board.teams}
    round1 = [r for r in ordered if r.round == 1]

    round1_positional: dict[str, int] = {}
    for row in round1:
        pos = _position(row)
        round1_positional[pos] = round1_positional.get(pos, 0) + 1

    first_window = team_count - 1
    first11_rb = [
        row.player.name
        for row in ordered
        if row.pick_no <= first_window and _position(row) == "RB"
    ]

    round1_qbs = [_board_pick(row) for row in round1 if _position(row) == "QB"]

    ranked_counts = sorted(board.teams, key=lambda t: -t.pick_count)
    pick_count_rank = [
        ManagerPickCount(manager=t.manager, pick_count=t.pick_count)
        for t in ranked_counts
    ]

    per_round: dict[str, dict[int, int]] = {}
    for row in ordered:
        per_round.setdefault(row.roster_id, {})
        per_round[row.roster_id][row.round] = (
            per_round[row.roster_id].get(row.round, 0) + 1
        )
    concentration: list[tuple[str | None, int, int]] = []
    for team in board.teams:
        rounds = per_round.get(team.roster_id, {})
        if not rounds:
            continue
        heaviest = min(rounds, key=lambda rnd: (-rounds[rnd], rnd))
        if rounds[heaviest] >= _MIN_ROUND_CONCENTRATION:
            concentration.append((team.manager, heaviest, rounds[heaviest]))
    concentration.sort(key=lambda item: -item[2])
    round_concentration = [
        RoundConcentration(manager=mgr, round=rnd, count=count)
        for mgr, rnd, count in concentration
    ]

    positional_runs = PositionalRunsSummary(
        QB=_qb_run(ordered, league.draft.rounds),
        RB=_rb_run(ordered, round1, board, managers),
        TE=_te_run(ordered, team_count),
    )

    return DraftSummary(
        round1_positional=round1_positional,
        first11_running_backs=first11_rb,
        round1_qbs=round1_qbs,
        pick_count_rank=pick_count_rank,
        round_concentration=round_concentration,
        positional_runs=positional_runs,
    )


def _qb_run(ordered: list[PickRow], rounds: int | None) -> QBRunSummary:
    qbs = [r for r in ordered if _position(r) == "QB"]
    through_r3 = [r for r in qbs if r.round <= 3]

    first_qb_by_roster: dict[str, PickRow] = {}
    for row in qbs:
        first_qb_by_roster.setdefault(row.roster_id, row)
    upper = rounds if rounds is not None else float("inf")
    waiting = sorted(
        (row for row in first_qb_by_roster.values() if 3 < row.round < upper),
        key=lambda row: row.pick_no,
    )

    return QBRunSummary(
        total=len(qbs),
        first_label=qbs[0].board_label if qbs else None,
        by_end_round3=len(through_r3),
        run_labels=[r.board_label for r in through_r3],
        left_waiting=[row.manager for row in waiting if row.manager is not None],
    )


def _rb_run(
    ordered: list[PickRow],
    round1: list[PickRow],
    board: BoardMetrics,
    managers: dict[str, str | None],
) -> RBRunSummary:
    rbs = [r for r in ordered if _position(r) == "RB"]
    per_roster: dict[str, int] = {}
    for row in rbs:
        per_roster[row.roster_id] = per_roster.get(row.roster_id, 0) + 1

    most: ManagerCount | None = None
    best_roster: str | None = None
    best_count = -1
    for team in board.teams:  # league.teams order breaks ties
        count = per_roster.get(team.roster_id, 0)
        if count > best_count:
            best_count, best_roster = count, team.roster_id
    if best_roster is not None and best_count > 0:
        most = ManagerCount(manager=managers.get(best_roster), count=best_count)

    return RBRunSummary(
        total=len(rbs),
        first_label=rbs[0].board_label if rbs else None,
        in_round1=sum(1 for r in round1 if _position(r) == "RB"),
        most_by_one_manager=most,
    )


def _te_run(ordered: list[PickRow], team_count: int) -> TERunSummary:
    tes = [r for r in ordered if _position(r) == "TE"]
    gap_threshold = max(1, team_count // 2)

    early: list[PickRow] = []
    third_label = third_player = None
    gap_picks: int | None = None
    for index, row in enumerate(tes):
        if index == 0:
            early.append(row)
            continue
        gap = row.pick_no - tes[index - 1].pick_no
        # A gap only closes the early window once at least two TEs precede it —
        # otherwise the "third TE" could be the second.
        if gap >= gap_threshold and len(early) >= 2:
            third_label, third_player, gap_picks = row.board_label, row.player.name, gap
            break
        early.append(row)

    return TERunSummary(
        total=len(tes),
        early_window_labels=[r.board_label for r in early],
        third_te_label=third_label,
        third_te_player=third_player,
        gap_picks=gap_picks,
    )


# --------------------------------------------------------------------------- #
# superlatives — everything here is derived from delta
# --------------------------------------------------------------------------- #


def _superlative_pick(row: PickRow) -> SuperlativePick:
    return SuperlativePick(
        pick_no=row.pick_no,
        board_label=row.board_label,
        manager=row.manager,
        player=row.player.name,
        position=row.player.position,
        consensus_label=row.consensus_label,
        delta=row.delta,
    )


def _superlatives(league: LeagueModel, pick_rows: list[PickRow]) -> Superlatives:
    ranked = [r for r in pick_rows if r.delta is not None]
    if not ranked:
        return Superlatives()

    by_value = sorted(ranked, key=lambda r: (-r.delta, r.pick_no))
    by_reach = sorted(ranked, key=lambda r: (r.delta, r.pick_no))

    return Superlatives(
        best_value=_superlative_pick(by_value[0]),
        best_value_runner_up=(
            _superlative_pick(by_value[1]) if len(by_value) > 1 else None
        ),
        biggest_reach=_superlative_pick(by_reach[0]),
        biggest_reach_runner_up=(
            _superlative_pick(by_reach[1]) if len(by_reach) > 1 else None
        ),
        boldest_swing=_boldest_swing(league, ranked),
    )


def _boldest_swing(
    league: LeagueModel, ranked: list[PickRow]
) -> BoldestSwing | None:
    by_roster: dict[str, list[PickRow]] = {}
    for row in ranked:
        by_roster.setdefault(row.roster_id, []).append(row)

    best_roster: str | None = None
    best_spread = 0  # a spread of 0 is no swing at all
    for team in league.teams:  # league.teams order breaks ties
        rows = by_roster.get(team.roster_id, [])
        if len(rows) < 2:
            continue
        deltas = [r.delta for r in rows]
        spread = max(deltas) - min(deltas)
        if spread > best_spread:
            best_spread, best_roster = spread, team.roster_id
    if best_roster is None:
        return None

    rows = by_roster[best_roster]
    high = max(rows, key=lambda r: (r.delta, -r.pick_no))
    low = min(rows, key=lambda r: (r.delta, r.pick_no))
    if high.pick_no == low.pick_no:  # defensive: always show two distinct picks
        by_pick_no = sorted(rows, key=lambda r: r.pick_no)
        high, low = by_pick_no[0], by_pick_no[-1]
    pair = sorted({high.pick_no: high, low.pick_no: low}.values(), key=lambda r: r.pick_no)
    manager = next(
        (t.manager for t in league.teams if t.roster_id == best_roster), None
    )
    return BoldestSwing(
        roster_id=best_roster,
        manager=manager,
        picks=[_board_pick(r) for r in pair],
    )


# --------------------------------------------------------------------------- #
# league / narration projections
# --------------------------------------------------------------------------- #


def _league_ref(league: LeagueModel) -> LeagueRef:
    fmt = league.format
    return LeagueRef(
        id=league.league_id,
        name=league.name,
        season=str(league.season),
        platform=league.platform,
        format=FormatRef(
            team_count=fmt.team_count,
            roster_slots=list(fmt.roster_slots),
            flex_eligibility={k: list(v) for k, v in fmt.flex_eligibility.items()},
            scoring_label=fmt.scoring_label,
            is_superflex_or_2qb=fmt.is_superflex_or_2qb,
            te_premium=fmt.te_premium,
        ),
    )


def _narration(
    league: LeagueModel,
    league_ref: LeagueRef,
    draft_summary: DraftSummary,
    superlatives: Superlatives,
    pick_rows: list[PickRow],
    team_rows: list[TeamRow],
    lead_candidates: list[LeadCandidate],
) -> Narration:
    ordered = sorted(pick_rows, key=lambda r: r.pick_no)
    round1 = [r for r in ordered if r.round == 1]
    rank = draft_summary.pick_count_rank
    leader = rank[0] if rank else None
    low = rank[-1] if rank else None

    headline = HeadlineNumbers(
        picks_total=len(ordered),
        rounds=league.draft.rounds,
        r1_positional=dict(draft_summary.round1_positional),
        first11_rb_count=len(draft_summary.first11_running_backs),
        pick_count_leader=(
            ManagerPickCount(manager=leader.manager, pick_count=leader.pick_count)
            if leader
            else None
        ),
        pick_count_low=(
            ManagerPickCount(manager=low.manager, pick_count=low.pick_count)
            if low
            else None
        ),
    )

    teams = [
        NarrationTeam(
            manager=row.manager,
            roster_id=row.roster_id,
            pick_count=row.pick_count,
            positional_counts=dict(row.positional_counts),
            grade=row.grade.letter,
            grade_driving_picks=list(row.grade.driving_picks),
            grade_rationale=row.grade.rationale,
            best_value_pick=row.best_value_pick,
            biggest_reach_pick=row.biggest_reach_pick,
            back_to_back=list(row.back_to_back),
        )
        for row in team_rows
    ]

    return Narration(
        league=NarrationLeague(
            name=league_ref.name,
            season=league_ref.season,
            scoring_label=league_ref.format.scoring_label,
        ),
        headline_numbers=headline,
        board_round1=[_board_pick(r) for r in round1],
        superlatives=superlatives,
        teams=teams,
        positional_runs=draft_summary.positional_runs,
        lead_candidates=lead_candidates,
        storyline_candidates=[],
    )


def _validate(doc: DraftRecapFacts) -> None:
    """Self-validation: round-trip the built document through the schema."""
    DraftRecapFacts.model_validate(doc.model_dump())
