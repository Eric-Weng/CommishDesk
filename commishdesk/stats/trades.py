"""Instant trade grades and trade revisit numbers (Story 9.6).

:func:`compute_trade_grades` grades each trade of the target week from the week's League Value
snapshot (Story 9.6a) and, for this-season trades three or more weeks old, emits the numbers
behind the locked "trade revisited" headline. It is core compute, like the draft grades, not a
Stat Module: public and free.

**Values come from one place.** The *snapshot* is a plain mapping (``players{pid: {pos, value,
future, est}}``, ``replacement``, ``source``, ``dynasty``, ``picks``) handed in by the caller. This
module never fetches, never imports ``league_value`` (the AD-1 fence), and never invents a number:
a player or pick the snapshot cannot price is listed ``ungraded`` with a reason. Only numbers and
Sleeper ids leave this module (AD-24).

**The scale (decided, pinned by tests).** For a horizon, a side's *received* total is ``a`` and the
rest of the trade's received total (the other side, or the other two sides of a three-team trade,
as one counter-side) is ``b``. ``share = |a - b| / (a + b)``. Within the fair band (``<= 0.05``,
``<= 0.10`` when the snapshot ``source`` is ``sleeper_only``) both sides read *Even*, letter ``B``.
Above it, ``<= 0.15`` is a *Slight edge* (leader ``B+``, trailing ``C+``, "Slightly behind"),
``<= 0.30`` a *Clear edge* (``A-`` / ``C-``, "Clearly behind"), above that *Lopsided* (``A`` /
``D``, "Far behind"). ``D`` is the floor and ``F`` is never emitted. A verdict is ``est`` when more
than half of the horizon's valued gross value comes from est. assets.

**Two horizons.** *This season* adds up the players' value in points per week over replacement;
picks never count toward it. In a dynasty league *Future* adds the players' ``future`` value and
the picks' value. The two are different units of time and are never added together.

**No roster-spot multiplier.** Values already floor at replacement, so a 2-for-1 is not
adjusted. The lineup impact (the change in the side's best starting lineup, in points per week
over replacement, ``stats/lineup.py`` rules) carries the consolidation.

**Context, not judgement.** When one side is eliminated and another is still in the race the trade
carries ``context = "contender_vs_eliminated"``. It never changes a grade.

**Revisit.** For a trade of this season that is three or more weeks old: the weeks since, each
side's received players' league ``players_points`` per week since the trade, and the leading
side. Numbers only.

Never raises: a fault inside one trade makes that trade ``ungraded``; a fault in the revisit
numbers drops them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

from commishdesk.ingest import LeagueModel, Transaction, WeekModel

from .lineup import best_lineup_total
from .stakes import roster_eliminated
from .standings import Standings
from .transactions import Trade, TradeSide, _trade
from .weekly import _sort_key

__all__ = [
    "FAIR_BAND",
    "FAIR_BAND_SLEEPER_ONLY",
    "REVISIT_MIN_WEEKS",
    "SCALE",
    "TradeAsset",
    "TradeGrade",
    "TradeGrades",
    "TradeRevisit",
    "TradeSideGrade",
    "UngradedAsset",
    "Verdict",
    "compute_trade_grades",
]

#: Share of a horizon's total inside which both sides read Even.
FAIR_BAND = 0.05
#: The wider band for a snapshot built from Sleeper data alone (the sample container).
FAIR_BAND_SLEEPER_ONLY = 0.10

#: ``(upper share bound, tier, leader letter, leader word, trailing letter, trailing word)``.
#: Above the last bound is Lopsided. ``D`` is the floor; ``F`` is never emitted.
SCALE: tuple[tuple[float, str, str, str, str, str], ...] = (
    (0.15, "slight", "B+", "Slight edge", "C+", "Slightly behind"),
    (0.30, "clear", "A-", "Clear edge", "C-", "Clearly behind"),
    (float("inf"), "lopsided", "A", "Lopsided", "D", "Far behind"),
)
_EVEN_LETTER = "B"
_EVEN_WORD = "Even"

#: A this-season trade is revisited once it is this many weeks old.
REVISIT_MIN_WEEKS = 3

_THIS_SEASON = "this_season"
_FUTURE = "future"
_TRADE_TYPE = "trade"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TradeAsset(_Frozen):
    """One valued asset a side receives. ``value`` is its This season value (points per week over
    replacement; ``None`` for a pick or when only Future counts), ``future`` its Future value
    (dynasty only), ``est`` whether it rests on an estimate."""

    kind: str
    player_id: str | None = None
    pick_season: str | None = None
    pick_round: int | None = None
    pick_from_roster_id: str | None = None
    value: float | None = None
    future: float | None = None
    est: bool = False


class UngradedAsset(_Frozen):
    """An asset that was listed but not graded, with the reason (``faab``, ``not_valued``,
    ``pick_not_priced``, ``redraft_pick``, ``no_snapshot``)."""

    kind: str
    reason: str
    player_id: str | None = None
    pick_season: str | None = None
    pick_round: int | None = None
    pick_from_roster_id: str | None = None
    faab: int | None = None


class Verdict(_Frozen):
    """One side's verdict for one horizon. ``ahead`` is ``None`` when the horizon reads Even."""

    horizon: str
    total: float
    counter_total: float
    share: float
    tier: str
    letter: str
    word: str
    ahead: bool | None
    est: bool


class TradeSideGrade(_Frozen):
    roster_id: str
    assets: list[TradeAsset]
    ungraded: list[UngradedAsset]
    this_season_total: float | None
    future_total: float | None
    lineup_impact: float | None
    verdicts: list[Verdict]


class TradeGrade(_Frozen):
    """One graded (or ungraded) trade of the target week."""

    transaction_id: str
    week: int
    status: str
    reason: str | None
    dynasty: bool
    source: str | None
    fair_band: float | None
    context: str | None
    eliminated_roster_ids: list[str]
    sides: list[TradeSideGrade]


class RevisitPlayer(_Frozen):
    player_id: str
    points_per_week: float


class RevisitSide(_Frozen):
    roster_id: str
    players: list[RevisitPlayer]
    points_per_week: float


class TradeRevisit(_Frozen):
    """Numbers for the locked "trade revisited" headline. ``leader`` is ``None`` on a tie."""

    transaction_id: str
    trade_week: int
    weeks_since: int
    leader: str | None
    sides: list[RevisitSide]


class TradeGrades(_Frozen):
    week: int
    grades: list[TradeGrade]
    revisits: list[TradeRevisit]


# --------------------------------------------------------------------------- #
# Verdicts
# --------------------------------------------------------------------------- #


def _verdict(horizon: str, total: float, counter: float, band: float, est: bool) -> Verdict:
    """The letter-and-word verdict for a side holding *total* against *counter* over a horizon."""
    gross = total + counter
    share = round(abs(total - counter) / gross, 6) if gross > 0 else 0.0
    if share <= band:
        return Verdict(
            horizon=horizon, total=round(total, 3), counter_total=round(counter, 3), share=round(share, 4),
            tier="even", letter=_EVEN_LETTER, word=_EVEN_WORD, ahead=None, est=est,
        )
    ahead = total > counter
    for bound, tier, lead_letter, lead_word, trail_letter, trail_word in SCALE:
        if share <= bound:
            return Verdict(
                horizon=horizon, total=round(total, 3), counter_total=round(counter, 3), share=round(share, 4),
                tier=tier, letter=lead_letter if ahead else trail_letter,
                word=lead_word if ahead else trail_word, ahead=ahead, est=est,
            )
    raise AssertionError("SCALE ends at infinity")  # pragma: no cover


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _pick_value(snapshot: Mapping[str, Any], season: str, rnd: int, original: str) -> float | None:
    """Future value (est.) of a pick from the snapshot's pick table, or ``None`` when it cannot be
    priced. Mirrors the method the snapshot documents: next season's pick is tiered by its
    original owner's standing, later seasons are tier-less and discounted per extra season."""
    picks = snapshot.get("picks")
    if not isinstance(picks, Mapping):
        return None
    try:
        ahead = int(season) - int(picks["next_season"])
        later = picks["later"]
        if ahead < 0 or str(rnd) not in later:
            return None
        if ahead == 0:
            tier = picks["tier_by_roster"].get(str(original), "mid")
            return _num(picks["by_tier"][tier][str(rnd)])
        discount = _num((snapshot.get("constants") or {}).get("pick_later_discount"))
        base = _num(later[str(rnd)])
        if discount is None or base is None:
            return None
        return round(base * discount**ahead, 3)
    except (KeyError, TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# One trade
# --------------------------------------------------------------------------- #


class _Priced:
    """A side's assets split into valued and ungraded, with the totals that feed the scale."""

    def __init__(self) -> None:
        self.assets: list[TradeAsset] = []
        self.ungraded: list[UngradedAsset] = []


def _price_side(side: TradeSide, snapshot: Mapping[str, Any] | None, dynasty: bool) -> _Priced:
    priced = _Priced()
    players = snapshot.get("players") if snapshot is not None else None
    for pid in side.player_ids:
        info = players.get(pid) if isinstance(players, Mapping) else None
        value = _num(info.get("value")) if isinstance(info, Mapping) else None
        if snapshot is None or value is None or not isinstance(info, Mapping):
            reason = "no_snapshot" if snapshot is None else "not_valued"
            priced.ungraded.append(UngradedAsset(kind="player", reason=reason, player_id=pid))
            continue
        future = _num(info.get("future")) if dynasty else None
        priced.assets.append(
            TradeAsset(kind="player", player_id=pid, value=round(value, 3),
                       future=round(future, 3) if future is not None else None, est=bool(info.get("est")))
        )
    for pick in side.picks:
        ref: dict[str, Any] = {
            "pick_season": pick.season,
            "pick_round": pick.round,
            "pick_from_roster_id": pick.roster_id,
        }
        if snapshot is None:
            priced.ungraded.append(UngradedAsset(kind="pick", reason="no_snapshot", **ref))
        elif not dynasty:
            priced.ungraded.append(UngradedAsset(kind="pick", reason="redraft_pick", **ref))
        else:
            value = _pick_value(snapshot, pick.season, pick.round, pick.roster_id)
            if value is None:
                priced.ungraded.append(UngradedAsset(kind="pick", reason="pick_not_priced", **ref))
            else:
                priced.assets.append(TradeAsset(kind="pick", future=round(value, 3), est=True, **ref))
    if side.faab:
        priced.ungraded.append(UngradedAsset(kind="faab", reason="faab", faab=side.faab))
    return priced


def _horizon_values(assets: list[TradeAsset], horizon: str) -> list[tuple[float, bool]]:
    out: list[tuple[float, bool]] = []
    for asset in assets:
        value = asset.value if horizon == _THIS_SEASON else asset.future
        if value is not None:
            out.append((value, asset.est))
    return out


def _roster_candidates(
    week: WeekModel, roster_id: str, snapshot: Mapping[str, Any]
) -> dict[str, tuple[str, float]] | None:
    """The week-n roster's valued players as ``{player_id: (position, value)}``, or ``None`` when
    the roster has no matchup row that week. Players the snapshot does not value are left out."""
    row = next((m for m in week.matchups if m.week == week.week and m.roster_id == roster_id), None)
    if row is None:
        return None
    roster = next((r for r in week.rosters if r.roster_id == roster_id), None)
    ir = set(roster.ir) if roster is not None else set()
    started = set(row.starters)
    players = snapshot.get("players")
    out: dict[str, tuple[str, float]] = {}
    for pid in (*row.starters, *row.bench, *row.players_points):
        if pid in out or (pid in ir and pid not in started):
            continue
        info = players.get(pid) if isinstance(players, Mapping) else None
        if not isinstance(info, Mapping):
            continue
        pos, value = info.get("pos"), _num(info.get("value"))
        if isinstance(pos, str) and value is not None:
            out[pid] = (pos, value)
    return out


def _lineup_impact(
    week: WeekModel,
    league: LeagueModel,
    snapshot: Mapping[str, Any],
    roster_id: str,
    received: list[str],
    given: list[str],
) -> float | None:
    """Change in the roster's best starting lineup (points per week over replacement): the week-n
    roster is the "after"; "before" is after minus what it received plus what it gave."""
    after = _roster_candidates(week, roster_id, snapshot)
    if after is None:
        return None
    players = snapshot.get("players")
    before = {pid: row for pid, row in after.items() if pid not in set(received)}
    for pid in given:
        info = players.get(pid) if isinstance(players, Mapping) else None
        if isinstance(info, Mapping) and isinstance(info.get("pos"), str) and _num(info.get("value")) is not None:
            before[pid] = (str(info["pos"]), float(info["value"]))
    try:
        total_after = best_lineup_total(league, [(pid, pos, val) for pid, (pos, val) in after.items()])
        total_before = best_lineup_total(league, [(pid, pos, val) for pid, (pos, val) in before.items()])
    except Exception:  # a malformed format is a lineup fault, not a failed Issue
        return None
    return round(total_after - total_before, 2)


def _given_by(
    trade: Trade, transaction: Transaction | None, side: TradeSide
) -> list[str]:
    """Player ids the side gave up: the transaction's drops by that roster, else (a two-team trade
    with no drop data) the other side's receipts."""
    if transaction is not None and transaction.drops:
        return sorted(
            (pid for pid, roster in transaction.drops.items() if roster == side.roster_id), key=_sort_key
        )
    if len(trade.sides) == 2:
        return [pid for other in trade.sides if other.roster_id != side.roster_id for pid in other.player_ids]
    return []


def _ungraded_trade(
    trade: Trade,
    reason: str,
    dynasty: bool,
    snapshot: Mapping[str, Any] | None,
    priced: Mapping[str, _Priced] | None = None,
) -> TradeGrade:
    sides = [
        TradeSideGrade(
            roster_id=side.roster_id,
            assets=[],
            ungraded=(priced[side.roster_id] if priced is not None else _price_side(side, None, dynasty)).ungraded,
            this_season_total=None,
            future_total=None,
            lineup_impact=None,
            verdicts=[],
        )
        for side in trade.sides
    ]
    source = snapshot.get("source") if snapshot is not None else None
    return TradeGrade(
        transaction_id=trade.transaction_id, week=trade.week, status="ungraded", reason=reason,
        dynasty=dynasty, source=source if isinstance(source, str) else None, fair_band=None,
        context=None, eliminated_roster_ids=[], sides=sides,
    )


def _grade_trade(
    week: WeekModel,
    league: LeagueModel,
    snapshot: Mapping[str, Any] | None,
    trade: Trade,
    transaction: Transaction | None,
    eliminated: Mapping[str, bool],
) -> TradeGrade:
    usable = isinstance(snapshot, Mapping) and bool(snapshot.get("players"))
    dynasty = bool(snapshot.get("dynasty")) if usable and snapshot is not None else False
    if not usable or snapshot is None:
        return _ungraded_trade(trade, "no_snapshot", dynasty, snapshot)

    source = snapshot.get("source")
    band = FAIR_BAND_SLEEPER_ONLY if source == "sleeper_only" else FAIR_BAND
    priced = {side.roster_id: _price_side(side, snapshot, dynasty) for side in trade.sides}

    horizons = [_THIS_SEASON, *([_FUTURE] if dynasty else [])]
    totals: dict[str, dict[str, float]] = {}
    valued: dict[str, bool] = {}
    est_gross: dict[str, float] = {}
    gross: dict[str, float] = {}
    for horizon in horizons:
        per_side = {rid: _horizon_values(p.assets, horizon) for rid, p in priced.items()}
        valued[horizon] = any(per_side.values())
        totals[horizon] = {rid: sum(v for v, _ in rows) for rid, rows in per_side.items()}
        gross[horizon] = sum(totals[horizon].values())
        est_gross[horizon] = sum(v for rows in per_side.values() for v, is_est in rows if is_est)

    if not any(valued.values()):
        return _ungraded_trade(trade, "nothing_valued", dynasty, snapshot, priced)

    sides: list[TradeSideGrade] = []
    for side in trade.sides:
        rid = side.roster_id
        verdicts = []
        for horizon in horizons:
            if not valued[horizon]:
                continue
            total = totals[horizon][rid]
            counter = gross[horizon] - total
            is_est = gross[horizon] > 0 and est_gross[horizon] * 2 > gross[horizon]
            verdicts.append(_verdict(horizon, total, counter, band, is_est))
        sides.append(
            TradeSideGrade(
                roster_id=rid,
                assets=priced[rid].assets,
                ungraded=priced[rid].ungraded,
                this_season_total=round(totals[_THIS_SEASON][rid], 3) if valued[_THIS_SEASON] else None,
                future_total=round(totals[_FUTURE][rid], 3) if dynasty and valued[_FUTURE] else None,
                lineup_impact=_lineup_impact(
                    week, league, snapshot, rid, side.player_ids, _given_by(trade, transaction, side)
                ),
                verdicts=verdicts,
            )
        )

    out_ids = sorted((s.roster_id for s in trade.sides if eliminated.get(s.roster_id)), key=_sort_key)
    alive = any(not eliminated.get(s.roster_id) for s in trade.sides)
    context = "contender_vs_eliminated" if out_ids and alive else None
    return TradeGrade(
        transaction_id=trade.transaction_id, week=trade.week, status="graded", reason=None, dynasty=dynasty,
        source=source if isinstance(source, str) else None, fair_band=band, context=context,
        eliminated_roster_ids=out_ids if context else [], sides=sides,
    )


# --------------------------------------------------------------------------- #
# Revisit
# --------------------------------------------------------------------------- #


def _revisit(week: WeekModel, trade: Trade) -> TradeRevisit | None:
    weeks = range(trade.week + 1, week.week + 1)
    points_by_week: dict[int, dict[str, float]] = {}
    for row in week.matchups:
        if row.week in weeks:
            bucket = points_by_week.setdefault(row.week, {})
            for pid, points in row.players_points.items():
                bucket.setdefault(pid, float(points))
    denominator = len(points_by_week)
    if denominator == 0 or not any(side.player_ids for side in trade.sides):
        return None
    sides: list[RevisitSide] = []
    for side in trade.sides:
        players = [
            RevisitPlayer(
                player_id=pid,
                points_per_week=round(sum(bucket.get(pid, 0.0) for bucket in points_by_week.values()) / denominator, 2),
            )
            for pid in side.player_ids
        ]
        sides.append(
            RevisitSide(
                roster_id=side.roster_id, players=players,
                points_per_week=round(sum(p.points_per_week for p in players), 2),
            )
        )
    ordered = sorted(sides, key=lambda s: -s.points_per_week)
    clear = len(ordered) > 1 and ordered[0].points_per_week > ordered[1].points_per_week
    leader = ordered[0].roster_id if clear else None
    return TradeRevisit(
        transaction_id=trade.transaction_id, trade_week=trade.week, weeks_since=week.week - trade.week,
        leader=leader, sides=sides,
    )


# --------------------------------------------------------------------------- #
# The entry
# --------------------------------------------------------------------------- #


def compute_trade_grades(
    week: WeekModel,
    league: LeagueModel,
    snapshot: Mapping[str, Any] | None,
    standings: Standings,
) -> TradeGrades:
    """Grade the target week's trades from the League Value *snapshot* and compute revisit numbers
    for this season's older trades.

    Pure, deterministic, offline: equal inputs give an equal ``model_dump()``. ``None`` or an empty
    *snapshot* leaves every trade ``ungraded`` (reason ``no_snapshot``). Never raises: a fault in
    one trade makes it ``ungraded`` (reason ``fault``)."""
    try:
        eliminated = roster_eliminated(week, league, standings)
    except Exception:  # a standings fault only drops the context flag
        eliminated = {}

    grades: list[TradeGrade] = []
    this_week = sorted(
        (txn for txn in week.transactions if txn.type == _TRADE_TYPE), key=lambda txn: txn.transaction_id
    )
    for transaction in this_week:
        try:
            trade = _trade(week.week, transaction)
            try:
                grades.append(_grade_trade(week, league, snapshot, trade, transaction, eliminated))
            except Exception:
                grades.append(_ungraded_trade(trade, "fault", False, None))
        except Exception:  # a trade that cannot even be read is skipped, never a failed Issue
            continue

    revisits: list[TradeRevisit] = []
    try:
        past_weeks = sorted(week.past_transactions, reverse=True)
    except Exception:
        past_weeks = []
    for trade_week in past_weeks:
        if week.week - trade_week < REVISIT_MIN_WEEKS:
            continue
        for transaction in sorted(week.past_transactions[trade_week], key=lambda txn: txn.transaction_id):
            if transaction.type != _TRADE_TYPE:
                continue
            try:
                revisit = _revisit(week, _trade(trade_week, transaction))
            except Exception:
                revisit = None
            if revisit is not None:
                revisits.append(revisit)
    return TradeGrades(week=week.week, grades=grades, revisits=revisits)
