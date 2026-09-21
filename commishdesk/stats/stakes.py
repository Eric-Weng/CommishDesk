"""Next week's stakes, the game of the week, and bye impact (Story 5.7).

:func:`compute_next_week` turns the pairings carried on
:class:`~commishdesk.ingest.WeekModel.next_matchups` into the week-``n+1``
preview the weekly Issue leads with: one :class:`NextWeekCard` per resolved
pairing, the single :class:`GameOfWeek`, and each side's conservative
clinch/elimination outlook.

**Outlooks.** Every rule is expressed in *win equivalents* -- ``cur = W + 0.5*T``
a roster has banked, ``max = cur + remaining`` the most it can still reach, with
``remaining`` the regular-season games every roster still has -- so a tiebreak can
never flip a claim. With ``N`` the league's declared bracket size (clamped to the
roster count) and ``B`` its first-round bye count
(:func:`~commishdesk.stats.standings.bye_count`, the one shared copy of that
rule):

* ``eliminated`` -- at least ``N`` other rosters have ``cur`` **strictly**
  greater than this roster's ``max``;
* ``clinched_playoff`` -- at most ``N-1`` other rosters have ``max >= cur``;
* ``clinched_bye`` -- the same count, but ``B-1`` (and ``B > 0``);
* ``clinched_division`` -- no division rival has ``max >= cur``.

No ``league.format.playoff`` stands the three playoff flags down (all ``False``)
and emits no playoff tag, and no ``playoff_week_start`` stands every flag down; a league with no declared
divisions never emits ``division_race``.

**Stakes tags**, derived per roster and emitted per matchup as the sorted union
in this order:

* ``bye_seed`` -- ``B > 0``, the bye is not clinched, and at most ``B-1`` others
  have ``cur`` strictly above this roster's ``max`` (a bye is still reachable);
* ``wildcard_race`` -- neither clinched a playoff spot nor eliminated;
* ``division_race`` -- divisions are declared, this roster has not clinched its
  division, and no rival's ``cur`` is strictly above its ``max``;
* ``draft_position`` -- eliminated;
* ``elimination`` -- not yet eliminated, but a loss would eliminate it.

**Stand-down.** Once the previewed week is at or past ``playoff_week_start``
(there is no next regular-season game to frame), or the league declares no
``playoff_week_start`` at all, every matchup's ``stakes`` is ``[]`` -- only the
tags stand down; the clinch/elimination flags stay computed. Playoff-week
framing is a later story's (5.15 / 5.16).

**Bye impact.** For each side, the week-``n`` starters whose
:class:`~commishdesk.ingest.PlayerSnapshot` names an NFL team the caller passed
in ``nfl_byes_next_week`` are listed by ``player_id`` -- ``None`` (never ``[]``)
when the caller supplied no bye data, because "nobody on bye" and "byes unknown"
are different facts. Like ``stats/lineup.py``, this module never reads the bye
file itself.

This module stays inside the ``stats/`` fence (AD-1): it imports only stdlib,
pydantic, ``commishdesk.ingest``, and its sibling ``stats/`` modules -- never
``adapters`` / ``store`` / ``facts`` / ``narrate`` / ``render`` / a later
pipeline stage. Ids only, never a player display name (Story 5.8 joins those).
It reads no file, clock, PRNG, or network, and never raises.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from commishdesk.ingest import LeagueModel, PlayerSnapshot, WeekModel

from .power import PowerRanks
from .standings import Standings, TeamStanding, bye_count
from .weekly import _sort_key

__all__ = [
    "ByeImpact",
    "GameOfWeek",
    "NextWeek",
    "NextWeekCard",
    "NextWeekSide",
    "compute_next_week",
]

#: The stakes tags, in the order a matchup emits their union. The order is the
#: emit order (Story 5.7's own enumeration), not alphabetical.
_TAG_ORDER: tuple[str, ...] = (
    "bye_seed",
    "wildcard_race",
    "division_race",
    "draft_position",
    "elimination",
)

#: A card pairs this many rosters. A lone row (a bye, or a null ``matchup_id``
#: group) -- or any other group size -- is dropped, never emitted as a card.
_CARD_SIDES = 2

#: Stand-in for "unranked" when comparing two cards' rank sums -- the spec's
#: "``None`` counts as worst". Only ever a sort key, never a reported value.
_UNRANKED = 1 << 30


class _Frozen(BaseModel):
    """Immutable, closed to unknown keys -- matches ``stats/standings.py``."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ByeImpact(_Frozen):
    """One of a side's week-``n`` starters whose NFL team is on bye in week
    ``n+1``. ``position`` is ``None`` when the player's snapshot carries none.
    Player and roster ids only -- the display-name join is Story 5.8's, as it
    was for ``stats/lineup.py``."""

    roster_id: str
    player_id: str
    nfl_team: str
    position: str | None


class NextWeekSide(_Frozen):
    """One side of a :class:`NextWeekCard`: the roster, its model rank (``None``
    before week 1), its W-L-T from :class:`~commishdesk.stats.standings.Standings`,
    and the four conservative clinch/elimination flags this side's stakes tags
    were derived from. The three playoff flags are ``False`` when the league
    declares no playoff format, and all four are ``False`` when it declares no
    ``playoff_week_start``. The flags stay computed while the previewed week
    stands down; only the tags stop."""

    roster_id: str
    model_rank: int | None
    wins: int
    losses: int
    ties: int
    clinched_playoff: bool
    clinched_bye: bool
    eliminated: bool
    clinched_division: bool


class NextWeekCard(_Frozen):
    """One week-``n+1`` pairing. ``a`` is the lower numeric ``roster_id``.

    ``stakes`` is the sorted union of both sides' tags (see the module
    docstring for the order) -- ``[]`` while the week stands down.
    ``bye_impact`` lists the week-``n`` starters whose team is on bye next week,
    both sides together; it is ``None`` when the caller supplied no bye data and
    ``[]`` when it did and nobody is on bye. ``game_of_week`` marks the one card
    this module picked."""

    matchup_id: int | None
    a: NextWeekSide
    b: NextWeekSide
    stakes: list[str]
    bye_impact: list[ByeImpact] | None
    game_of_week: bool = False


class GameOfWeek(_Frozen):
    """The one card called the game of the week, and why.

    ``rule`` is ``"most_stakes"`` when the winning card carried at least one
    stakes tag, else ``"top_ranks"``. ``stakes`` is the winning card's own union.
    ``rank_sum`` is the two sides' model ranks summed, or ``None`` when either
    side is unranked (an unranked side sorts as worst, but is never invented)."""

    matchup_id: int | None
    rule: str
    stakes: list[str]
    rank_sum: int | None


class NextWeek(_Frozen):
    """The whole result for one league-week: the input ``week`` and the week it
    previews (``next_week = week + 1``), one :class:`NextWeekCard` per resolved
    pairing ordered by ``matchup_id``, and the single :class:`GameOfWeek`
    (``None`` when there are no cards)."""

    week: int
    next_week: int
    cards: list[NextWeekCard]
    game_of_week: GameOfWeek | None


@dataclass(frozen=True)
class _Outlook:
    """One roster's internal outlook: the flags and the tag set that produced
    its side's row. Never leaves this module -- the public surface reports the
    flags on :class:`NextWeekSide` and the union on :class:`NextWeekCard`."""

    tags: frozenset[str]
    clinched_playoff: bool
    clinched_bye: bool
    eliminated: bool
    clinched_division: bool


_NO_OUTLOOK = _Outlook(
    tags=frozenset(),
    clinched_playoff=False,
    clinched_bye=False,
    eliminated=False,
    clinched_division=False,
)


def _win_equivalent(wins: int, ties: int) -> float:
    """``cur``: the wins a roster has banked, a tie counting half."""
    return wins + 0.5 * ties


def _stand_down(week: WeekModel) -> bool:
    """Story 5.7's stand-down: no stakes once the previewed week reaches
    ``playoff_week_start`` (there is no next regular-season game to frame), or
    when the league declares no ``playoff_week_start`` at all."""
    start = week.playoff_week_start
    return start is None or week.week + 1 > start - 1


def _remaining_games(week: WeekModel, standings: Standings) -> int:
    """How many regular-season games every roster still has: ``playoff_week_start
    - 1`` is the last regular-season week, ``standings.through_week`` the last one
    folded. Never negative."""
    if week.playoff_week_start is None:
        return 0
    return max(0, week.playoff_week_start - 1 - standings.through_week)


def _outlooks(week: WeekModel, league: LeagueModel, standings: Standings) -> dict[str, _Outlook]:
    """Every roster's :class:`_Outlook`, keyed by ``roster_id``.

    All-empty when the league declares no rosters or no ``playoff_week_start``.
    Once the previewed week reaches ``playoff_week_start`` the clinch and
    elimination flags are still computed (with no games left they are the final
    regular-season picture) but no tag is emitted. A league that declares no
    ``league.format.playoff`` keeps the division picture but emits no playoff
    tag."""
    roster_ids = sorted((roster.roster_id for roster in week.rosters), key=_sort_key)
    if not roster_ids or week.playoff_week_start is None:
        return {roster_id: _NO_OUTLOOK for roster_id in roster_ids}
    emit_tags = not _stand_down(week)

    records = {team.roster_id: team for team in standings.teams}
    cur: dict[str, float] = {}
    for roster_id in roster_ids:
        record = records.get(roster_id)
        cur[roster_id] = _win_equivalent(record.wins, record.ties) if record is not None else 0.0
    remaining = _remaining_games(week, standings)
    ceiling = {roster_id: cur[roster_id] + remaining for roster_id in roster_ids}

    playoff = league.format.playoff
    bracket = min(playoff.bracket_teams, len(roster_ids)) if playoff is not None else None
    byes = bye_count(bracket) if bracket is not None else 0

    division_of = {team.roster_id: team.division_id for team in league.teams}
    declared = {division.id for division in league.format.divisions}

    outlooks: dict[str, _Outlook] = {}
    for roster_id in roster_ids:
        others = [other for other in roster_ids if other != roster_id]
        others_cur = [cur[other] for other in others]
        others_max = [ceiling[other] for other in others]

        tags: set[str] = set()
        clinched_playoff = False
        clinched_bye = False
        eliminated = False

        if bracket is not None and bracket > 0:
            ahead_cur = sum(1 for value in others_cur if value > ceiling[roster_id])
            catchers = sum(1 for value in others_max if value >= cur[roster_id])
            eliminated = ahead_cur >= bracket
            clinched_playoff = catchers <= bracket - 1
            clinched_bye = byes > 0 and catchers <= byes - 1

            if byes > 0 and not clinched_bye and ahead_cur <= byes - 1:
                tags.add("bye_seed")
            if not clinched_playoff and not eliminated:
                tags.add("wildcard_race")
            if eliminated:
                tags.add("draft_position")
            elif sum(1 for value in others_cur if value > ceiling[roster_id] - 1) >= bracket:
                tags.add("elimination")

        division_id = division_of.get(roster_id)
        clinched_division = False
        if division_id is not None and division_id in declared:
            rivals = [
                other for other in others if division_of.get(other) == division_id
            ]
            if rivals:
                rival_cur = [cur[rival] for rival in rivals]
                rival_max = [ceiling[rival] for rival in rivals]
                clinched_division = not any(value >= cur[roster_id] for value in rival_max)
                if not clinched_division and not any(value > ceiling[roster_id] for value in rival_cur):
                    tags.add("division_race")

        outlooks[roster_id] = _Outlook(
            tags=frozenset(tags) if emit_tags else frozenset(),
            clinched_playoff=clinched_playoff,
            clinched_bye=clinched_bye,
            eliminated=eliminated,
            clinched_division=clinched_division,
        )
    return outlooks


def _union_tags(outlooks: dict[str, _Outlook], roster_ids: tuple[str, ...]) -> list[str]:
    """The sorted union of the given rosters' tags, in :data:`_TAG_ORDER`."""
    tags: set[str] = set()
    for roster_id in roster_ids:
        tags |= outlooks.get(roster_id, _NO_OUTLOOK).tags
    return [tag for tag in _TAG_ORDER if tag in tags]


def _side(
    roster_id: str,
    records: Mapping[str, TeamStanding],
    ranks: Mapping[str, int | None],
    outlooks: dict[str, _Outlook],
) -> NextWeekSide:
    record = records.get(roster_id)
    outlook = outlooks.get(roster_id, _NO_OUTLOOK)
    return NextWeekSide(
        roster_id=roster_id,
        model_rank=ranks.get(roster_id),
        wins=record.wins if record else 0,
        losses=record.losses if record else 0,
        ties=record.ties if record else 0,
        clinched_playoff=outlook.clinched_playoff,
        clinched_bye=outlook.clinched_bye,
        eliminated=outlook.eliminated,
        clinched_division=outlook.clinched_division,
    )


def _bye_impact(
    roster_ids: tuple[str, ...],
    week_starters: Mapping[str, list[str]],
    players: Mapping[str, PlayerSnapshot],
    nfl_byes_next_week: frozenset[str] | None,
) -> list[ByeImpact] | None:
    """The week-``n`` starters of each side whose snapshot's NFL team is in
    ``nfl_byes_next_week``, ordered by ``(roster_id, player_id)``. ``None`` when
    the caller supplied no bye data -- never ``[]``, which means "nobody on
    bye"."""
    if nfl_byes_next_week is None:
        return None
    flagged: list[ByeImpact] = []
    seen: set[tuple[str, str]] = set()
    for roster_id in roster_ids:
        for player_id in week_starters.get(roster_id, []):
            if (roster_id, player_id) in seen:
                continue
            snapshot = players.get(player_id)
            if snapshot is None or snapshot.nfl_team is None:
                continue
            if snapshot.nfl_team in nfl_byes_next_week:
                seen.add((roster_id, player_id))
                flagged.append(
                    ByeImpact(
                        roster_id=roster_id,
                        player_id=player_id,
                        nfl_team=snapshot.nfl_team,
                        position=snapshot.position,
                    )
                )
    flagged.sort(key=lambda entry: (_sort_key(entry.roster_id), entry.player_id))
    return flagged


def _cards(
    week: WeekModel,
    standings: Standings,
    power: PowerRanks,
    players: Mapping[str, PlayerSnapshot],
    nfl_byes_next_week: frozenset[str] | None,
    outlooks: dict[str, _Outlook],
) -> list[NextWeekCard]:
    """Every resolved pairing as a card, ordered by ``matchup_id``. Only a group
    of exactly :data:`_CARD_SIDES` distinct rosters is a card; a bye, a lone row
    or a null ``matchup_id`` group is dropped."""
    records = {team.roster_id: team for team in standings.teams}
    ranks = {team.roster_id: team.model_rank for team in power.teams}
    week_starters = {matchup.roster_id: matchup.starters for matchup in week.matchups if matchup.week == week.week}

    groups: dict[int, list[str]] = {}
    for matchup in week.next_matchups:
        if matchup.matchup_id is None:
            continue
        groups.setdefault(matchup.matchup_id, []).append(matchup.roster_id)

    cards: list[NextWeekCard] = []
    for matchup_id in sorted(groups):
        unique = sorted(set(groups[matchup_id]), key=_sort_key)
        if len(unique) != _CARD_SIDES:
            continue
        a_id, b_id = unique
        cards.append(
            NextWeekCard(
                matchup_id=matchup_id,
                a=_side(a_id, records, ranks, outlooks),
                b=_side(b_id, records, ranks, outlooks),
                stakes=_union_tags(outlooks, (a_id, b_id)),
                bye_impact=_bye_impact((a_id, b_id), week_starters, players, nfl_byes_next_week),
            )
        )
    return cards


def _rank_value(rank: int | None) -> int:
    return rank if rank is not None else _UNRANKED


def _game_of_week(
    cards: list[NextWeekCard], outlooks: dict[str, _Outlook]
) -> tuple[int, GameOfWeek] | None:
    """The chosen card's index and its :class:`GameOfWeek`, or ``None`` when
    there are no cards.

    The pick is by total ``(team, tag)`` pairs across both sides (descending),
    then by the two sides' model ranks summed (ascending, an unranked side
    counting as worst), then by ``matchup_id``."""
    if not cards:
        return None

    def sort_key(card: NextWeekCard) -> tuple[int, int, int]:
        tag_count = len(outlooks.get(card.a.roster_id, _NO_OUTLOOK).tags) + len(
            outlooks.get(card.b.roster_id, _NO_OUTLOOK).tags
        )
        rank_sum = _rank_value(card.a.model_rank) + _rank_value(card.b.model_rank)
        matchup_id = card.matchup_id if card.matchup_id is not None else _UNRANKED
        return (-tag_count, rank_sum, matchup_id)

    index = min(range(len(cards)), key=lambda position: sort_key(cards[position]))
    winner = cards[index]
    stakes = list(winner.stakes)
    rule = "most_stakes" if stakes else "top_ranks"
    if winner.a.model_rank is not None and winner.b.model_rank is not None:
        rank_sum: int | None = winner.a.model_rank + winner.b.model_rank
    else:
        rank_sum = None
    return index, GameOfWeek(
        matchup_id=winner.matchup_id, rule=rule, stakes=stakes, rank_sum=rank_sum
    )


def compute_next_week(
    week: WeekModel,
    league: LeagueModel,
    standings: Standings,
    power: PowerRanks,
    players: Mapping[str, PlayerSnapshot],
    nfl_byes_next_week: frozenset[str] | None = None,
) -> NextWeek:
    """Build the week-``n+1`` preview for one league-week.

    Pure, deterministic, offline: two calls on equal inputs return an equal
    ``model_dump()``. ``standings`` and ``power`` are the caller's own
    :func:`~commishdesk.stats.standings.compute_standings` /
    :func:`~commishdesk.stats.power.compute_power_ranks` results for *week*.
    ``nfl_byes_next_week`` is the set of NFL team abbreviations on bye in week
    ``n+1``, supplied by the caller -- this module never reads the bye file.
    Never raises; a week with no ``next_matchups`` simply has no cards."""
    outlooks = _outlooks(week, league, standings)
    cards = _cards(week, standings, power, players, nfl_byes_next_week, outlooks)
    picked = _game_of_week(cards, outlooks)

    if picked is None:
        return NextWeek(week=week.week, next_week=week.week + 1, cards=cards, game_of_week=None)

    index, game_of_week = picked
    marked = [
        card.model_copy(update={"game_of_week": position == index})
        for position, card in enumerate(cards)
    ]
    return NextWeek(
        week=week.week, next_week=week.week + 1, cards=marked, game_of_week=game_of_week
    )
