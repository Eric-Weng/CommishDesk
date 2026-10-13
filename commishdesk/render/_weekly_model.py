"""Story 5.14b — the weekly Issue's one content model, shared by every surface.

The designed web page (:mod:`commishdesk.render.weekly_web`), the weekly email
(:mod:`commishdesk.render.weekly_email`) and the Discord post
(:func:`commishdesk.render.discord.render_weekly_discord_post`) all pick the
same lead, awards, game tags, power order, luck order, shared next-week stakes
and wire item from here, so the three surfaces can never drift apart.

Data only: these selectors and formatters return plain values and schema
objects, never markup, and never escape anything (each surface escapes for its
own medium). Numbers come from the Facts JSON; this module computes no stat of
its own (luck is ``season.luck`` as-is).

Story 5B.3 adds :func:`power_history` — the trailing power-rank trail the
web power-rank bump chart plots (model rank from ``WeeklyHistoryRow.power_rank``,
published rank from ``WeeklyHistoryRow.published_rank``, current-week values
resolved from the render-time :class:`PowerRow`).

Story 8.16b adds the shared team identity every surface prints the same way:
:func:`monogram_codes` (unique per league), :func:`default_team_colors` (the
render-time fallback when no stored colors are passed), the one record format
(:func:`display_record`) and the lineup-loss wording.

Story 5B.4 adds :func:`all_play_rows` — the season all-play rows the web
standings view toggle swaps in, with the same ``None``-filtering shape as
:func:`luck_rows`.

**Pipeline fence (AD-1).** Imports the standard library and
``commishdesk.facts`` schema types only.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import NamedTuple

from commishdesk.facts.schema import (
    LeadCandidate,
    WeeklyAllPlay,
    WeeklyFacts,
    WeeklyMatchup,
    WeeklyMove,
    WeeklyMovePlayer,
    WeeklyNextWeekCard,
    WeeklyTeam,
    WeeklyTrade,
)

#: The real minus sign (U+2212) for signed numerals.
MINUS = "−"

_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
)

#: Next-week stake tags (``stats/stakes.py``'s vocabulary) as chip words. An
#: unknown tag falls back to its own words rather than being dropped.
STAKE_LABELS = {
    "bye_seed": "Bye seed",
    "wildcard_race": "Wildcard race",
    "division_race": "Division race",
    "draft_position": "Draft position",
    "elimination": "Elimination",
}

#: The sub-line under the two coaching awards.
OF_BEST = "of the best possible lineup"


# --------------------------------------------------------------------------- #
# Formatters
# --------------------------------------------------------------------------- #


def spell(n: int) -> str:
    return _ONES[n] if 0 <= n < len(_ONES) else str(n)


def pts(value: float) -> str:
    """A week score / margin: two decimals (``247.20``)."""
    return f"{value:.2f}"


def whole(value: float) -> str:
    """A season total: rounded, thousands separated (``2,029``)."""
    return f"{round(value):,}"


def pct(value: float) -> str:
    """A ``0..1`` ratio as a one-decimal percentage (``0.992 -> 99.2%``)."""
    return f"{value * 100:.1f}%"


def signed(value: float) -> str:
    """A signed figure with a real minus: one decimal unless the value carries
    more (``+1.5``, ``−0.4``, ``0.0``)."""
    text = f"{abs(value):.1f}" if round(value, 1) == value else f"{abs(value):.2f}"
    if value > 0:
        return f"+{text}"
    if value < 0:
        return f"{MINUS}{text}"
    return text


def record(w: int, l: int, t: int, *, full: bool = False) -> str:  # noqa: E741 -- W-L-T
    if full or t:
        return f"{w}-{l}-{t}"
    return f"{w}-{l}"


_FULL_RECORD = re.compile(r"^(\d+)-(\d+)-(\d+)$")


def display_record(text: str) -> str:
    """A record string as the Issue prints it: ties only when nonzero (``7-4-0``
    -> ``7-4``, ``7-4-1`` stays). Anything that is not ``W-L-T`` passes through."""
    found = _FULL_RECORD.match(text.strip())
    if found is None or int(found.group(3)) != 0:
        return text
    return f"{found.group(1)}-{found.group(2)}"


#: The lineup-loss key-number label: the best possible lineup minus the one
#: started (``points_left_on_bench`` is ``optimal - actual``), never "on the bench"
#: alone, which read as a total the bench itself scored.
PERFECT_LINEUP = "A perfect lineup adds"


def lineup_loss_sub(gap: float, margin: float, bench_name: str | None, bench_points: float | None) -> str:
    """The sub-line under the lineup-loss key number: the loss it outweighed,
    then the one benched scorer."""
    parts: list[str] = []
    if gap > margin > 0:
        parts.append(f"More than the {pts(margin)}-point loss.")
    if bench_name and bench_points is not None:
        parts.append(f"{bench_name} scored {pts(bench_points)} on the bench.")
    return " ".join(parts)


def team_label(team: WeeklyTeam | None, roster_id: str | None = None) -> str:
    """The display label the narration uses: ``team_name or manager or
    "Roster <id>"``."""
    if team is None:
        return f"Roster {roster_id}" if roster_id else "An unclaimed roster"
    return team.team_name or team.manager or f"Roster {team.roster_id}"


def stake_label(tag: str) -> str:
    return STAKE_LABELS.get(tag, tag.replace("_", " ").capitalize())


def team_index(facts: WeeklyFacts) -> dict[str, WeeklyTeam]:
    return {team.roster_id: team for team in facts.teams}


def labeller(facts: WeeklyFacts) -> Callable[[str | None], str]:
    """``roster_id -> display label`` over *facts*' teams."""
    teams = team_index(facts)

    def label(roster_id: str | None) -> str:
        return team_label(teams.get(roster_id or ""), roster_id)

    return label


# --------------------------------------------------------------------------- #
# Lead
# --------------------------------------------------------------------------- #


def find_matchup(facts: WeeklyFacts, roster_ids: Sequence[str]) -> WeeklyMatchup | None:
    wanted = set(roster_ids[:2])
    for matchup in facts.matchups.this_week:
        if {matchup.home_roster_id, matchup.away_roster_id} == wanted:
            return matchup
    return None


def winner_loser(matchup: WeeklyMatchup) -> tuple[str, float, str, float]:
    """``(winner id, winner points, loser id, loser points)``; on a tie the
    higher (or home) side comes first."""
    home = (matchup.home_roster_id, matchup.home_points)
    away = (matchup.away_roster_id, matchup.away_points)
    if matchup.winner_roster_id == matchup.away_roster_id or (
        matchup.winner_roster_id is None and away[1] > home[1]
    ):
        home, away = away, home
    return home[0], home[1], away[0], away[1]


def choose_lead(facts: WeeklyFacts) -> LeadCandidate | None:
    """The best-ranked lead candidate that carries a hook, or ``None``."""
    leads = sorted(facts.lead_candidates, key=lambda c: c.rank)
    return next((candidate for candidate in leads if candidate.hook), None)


def key_number(facts: WeeklyFacts, lead: LeadCandidate) -> str:
    """The one number a type-only lead sets beside its hook, when there is one."""
    summary = facts.period.summary
    teams = team_index(facts)
    if lead.kind == "lineup_loss" and lead.roster_ids:
        team = teams.get(lead.roster_ids[0])
        if team and team.this_week and team.this_week.points_left_on_bench is not None:
            return pts(team.this_week.points_left_on_bench)
    if lead.kind in ("biggest_blowout", "closest_game"):
        matchup = find_matchup(facts, lead.roster_ids)
        if matchup is not None:
            return pts(abs(matchup.margin))
    if lead.kind == "week_high_score" and summary.high is not None:
        return pts(summary.high.points)
    return ""


class BenchSplit(NamedTuple):
    """A ``lineup_loss`` lead's numbers: started, left on the bench, opponent."""

    started: float
    bench: float
    opponent: float


def bench_split(facts: WeeklyFacts, lead: LeadCandidate) -> BenchSplit | None:
    """The started / bench / opponent triple the bench hero draws, or ``None``
    when it cannot be drawn (the same guard the web hero uses)."""
    if lead.kind != "lineup_loss" or not lead.roster_ids:
        return None
    team = team_index(facts).get(lead.roster_ids[0])
    week = team.this_week if team else None
    if week is None:
        return None
    bench = week.points_left_on_bench
    opp = week.opponent_points
    if bench is None or bench <= 0 or opp is None or week.points <= 0:
        return None
    return BenchSplit(week.points, bench, opp)


# --------------------------------------------------------------------------- #
# Awards (all from ``leaders``)
# --------------------------------------------------------------------------- #


class Award(NamedTuple):
    """One award: ``kind`` is ``coach`` / ``player`` / ``bust`` / ``goose``."""

    kind: str
    key: str
    value: str
    name: str
    sub: str
    #: The team the award belongs to (the winner, the bust, the scorer's side).
    roster_id: str | None = None


def awards(facts: WeeklyFacts) -> list[Award]:
    label = labeller(facts)
    leaders = facts.leaders
    out: list[Award] = []
    best = leaders.best_coaching
    if best is not None and best.pct is not None:
        out.append(
            Award("coach", "Coach of the week", pct(best.pct), label(best.roster_id), OF_BEST, best.roster_id)
        )
    star = leaders.week_high_player
    if star is not None:
        sub = [star.pos or "", label(star.roster_id)]
        if star.is_season_high:
            sub.append("season high")
        out.append(
            Award(
                "player",
                "Player of the week",
                pts(star.points),
                star.name,
                " · ".join(s for s in sub if s),
                star.roster_id,
            )
        )
    worst = leaders.worst_coaching
    if worst is not None and worst.pct is not None:
        out.append(
            Award("bust", "Bust of the week", pct(worst.pct), label(worst.roster_id), OF_BEST, worst.roster_id)
        )
    egg = leaders.worst_starters[0] if leaders.worst_starters else None
    if egg is not None and egg.points == 0:
        sub_text = " · ".join(s for s in (egg.pos or "", f"started by {label(egg.roster_id)}") if s)
        out.append(Award("goose", "Goose egg club", pts(egg.points), egg.name, sub_text, egg.roster_id))
    return out


# --------------------------------------------------------------------------- #
# Games
# --------------------------------------------------------------------------- #


def game_tag(facts: WeeklyFacts, matchup: WeeklyMatchup) -> str | None:
    """One tag per game: ``week_high``, else ``closest``, else ``blowout``."""
    summary = facts.period.summary
    pair = {matchup.home_roster_id, matchup.away_roster_id}
    if summary.high is not None and summary.high.roster_id in pair:
        return "week_high"
    if summary.closest is not None and set(summary.closest.roster_ids) == pair:
        return "closest"
    if matchup.is_blowout:
        return "blowout"
    return None


# --------------------------------------------------------------------------- #
# Standings
# --------------------------------------------------------------------------- #


def standings_order(facts: WeeklyFacts) -> list[WeeklyTeam]:
    teams = team_index(facts)
    order = [teams[rid] for rid in facts.standings.overall if rid in teams]
    if not facts.period.has_prior_week:
        order.sort(key=lambda team: (-team.season.points_for, team.roster_id.zfill(8)))
    return order


# --------------------------------------------------------------------------- #
# Power — by published rank (narration, then Facts, then model)
# --------------------------------------------------------------------------- #


class PowerRow(NamedTuple):
    published: int | None
    model: int | None
    team: WeeklyTeam
    reason: str | None


def power_rows(facts: WeeklyFacts) -> list[PowerRow]:
    """Every team in published-rank order; ``[]`` when no rank exists at all."""
    narrated = {row.roster_id: row for row in facts.narration.power}
    rows: list[PowerRow] = []
    for team in facts.teams:
        power = team.season.power
        row = narrated.get(team.roster_id)
        published = row.published_rank if row is not None else None
        if published is None:
            published = power.published_rank
        if published is None:
            published = power.model_rank
        reason = row.nudge_justification if row is not None else None
        rows.append(PowerRow(published, power.model_rank, team, reason))
    if not any(row.published is not None for row in rows):
        return []
    rows.sort(
        key=lambda item: (
            item.published is None,
            item.published or 0,
            item.model is None,
            item.model or 0,
            item.team.roster_id.zfill(8),
        )
    )
    return rows


class PowerHistoryPoint(NamedTuple):
    """One trailing power-rank point for the bump chart."""

    week: int
    model: int
    published: int | None
    reason: str | None


def power_history(team: WeeklyTeam, current: PowerRow, week: int | None = None) -> list[PowerHistoryPoint]:
    """The trailing <=5 ``team.history.weekly`` rows with model ranks.

    The last point is the render-time-resolved current week, so it carries
    ``current.published`` and ``current.reason``; historical points carry only
    ``WeeklyHistoryRow.published_rank`` and no prose.

    *week* is the week being rendered. A history row only carries a model rank
    inside the regular season, so in a playoff week the last usable row is an
    earlier week: the current week then becomes a point of its own instead of
    being spliced onto that stale week's number (``epic-5b-retro-item-1``). With
    no *week* the last row is taken to be the current one.
    """
    points: list[PowerHistoryPoint] = []
    for row in team.history.weekly:
        if row.power_rank is None:
            continue
        points.append(
            PowerHistoryPoint(
                week=row.week,
                model=row.power_rank,
                published=row.published_rank,
                reason=None,
            )
        )

    if not points:
        return []

    last = points[-1]
    if week is not None and last.week != week:
        if current.model is None:
            return points[-5:]  # no model rank for this week: plot what exists, never a stale relabel
        points.append(
            PowerHistoryPoint(week=week, model=current.model, published=current.published, reason=current.reason)
        )
        return points[-5:]

    points = points[-5:]
    last = points[-1]
    model = current.model if current.model is not None else last.model
    points[-1] = PowerHistoryPoint(
        week=last.week,
        model=model,
        published=current.published,
        reason=current.reason,
    )
    return points


# --------------------------------------------------------------------------- #
# Luck — ``season.luck``, most lucky first
# --------------------------------------------------------------------------- #


def luck_rows(facts: WeeklyFacts) -> list[tuple[float, WeeklyTeam]]:
    rows = [(float(team.season.luck), team) for team in facts.teams if team.season.luck is not None]
    rows.sort(key=lambda item: (-item[0], item[1].roster_id.zfill(8)))
    return rows


def all_play_rows(facts: WeeklyFacts) -> list[tuple[WeeklyAllPlay, WeeklyTeam]]:
    """Teams with a season all-play record, mirroring ``luck_rows``'s
    None-filtering shape. The standings toggle uses this list as its single
    eligibility source."""
    rows: list[tuple[WeeklyAllPlay, WeeklyTeam]] = []
    for team in facts.teams:
        all_play = team.season.all_play
        if all_play is not None:
            rows.append((all_play, team))
    rows.sort(key=lambda item: item[1].roster_id.zfill(8))
    return rows


# --------------------------------------------------------------------------- #
# Next week
# --------------------------------------------------------------------------- #


def shared_stakes(cards: Sequence[WeeklyNextWeekCard]) -> list[str]:
    """The stake tags every next-week card carries (shown once per surface)."""
    if len(cards) < 2:
        return []  # one game: its stakes are its own chips, not "every game"
    shared = [tag for tag in cards[0].stakes if all(tag in card.stakes for card in cards[1:])]
    return list(dict.fromkeys(shared))


def own_stakes(card: WeeklyNextWeekCard, shared: Sequence[str]) -> list[str]:
    """The card's stakes that differ from the shared line, de-duplicated."""
    return [tag for tag in dict.fromkeys(card.stakes) if tag not in shared]


# --------------------------------------------------------------------------- #
# Transactions + the one "wire" item
# --------------------------------------------------------------------------- #


def week_moves(facts: WeeklyFacts) -> list[WeeklyMove]:
    """The week's non-trade moves that added or dropped someone."""
    return [move for move in facts.transactions.this_week if move.type != "trade" and (move.adds or move.drops)]


def latest_trades(facts: WeeklyFacts) -> list[WeeklyTrade]:
    """Every trade from the most recent trade week, newest-first order kept."""
    trades = sorted(facts.transactions.recent_trades, key=lambda trade: trade.week, reverse=True)
    return [trade for trade in trades if trade.week == trades[0].week] if trades else []


class WirePick(NamedTuple):
    """The wire item: an add (``move`` + ``player``) or a ``trade``."""

    move: WeeklyMove | None = None
    player: WeeklyMovePlayer | None = None
    trade: WeeklyTrade | None = None


def wire_pick(facts: WeeklyFacts) -> WirePick | None:
    """The highest-FAAB add of the week (first wins a tie; no bid counts as 0),
    then the newest trade, else ``None``."""
    adds = [move for move in week_moves(facts) if move.adds]
    if adds:
        best = max(adds, key=lambda move: move.faab or 0)  # max keeps the first on a tie
        return WirePick(move=best, player=best.adds[0])
    trades = latest_trades(facts)
    if trades:
        return WirePick(trade=trades[0])
    return None


# --------------------------------------------------------------------------- #
# Team identity (Story 8.16b): one monogram and one color slot per team
# --------------------------------------------------------------------------- #

#: Palette slots in a league: 16 solid colors, then the same 16 in a second
#: (dotted) style for teams 17 to 32.
PALETTE_SIZE = 16
TEAM_COLOR_SLOTS = 32

_WORD_SPLIT = re.compile(r"[\s\-‐‑‒–—―_/]+")


def _words(label: str) -> list[str]:
    return [word for word in _WORD_SPLIT.split(label.strip()) if any(ch.isalnum() for ch in word)]


def _alnum(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum())


def monogram(label: str) -> str:
    """Two letters for a team: the first letters of the first two words
    (``Kneel-Down Koalas -> KD``), else the first two letters of a one-word name."""
    words = _words(label)
    if len(words) >= 2:
        return (_alnum(words[0])[0] + _alnum(words[1])[0]).upper()
    letters = _alnum(label)
    return letters[:2].upper() or "?"


def _monogram_candidates(label: str) -> Iterable[str]:
    """Alternate codes for a team whose first-choice code is taken, best first."""
    words = _words(label)
    first, last = (_alnum(words[0]), _alnum(words[-1])) if words else ("", "")
    if first and last:
        yield (first[0] + last[0]).upper()  # first and last word
    if len(first) >= 2:
        yield first[:2].upper()  # the first word's first two letters
    if first and len(last) >= 2:
        yield (first[0] + last[1]).upper()
    if len(last) >= 2:
        yield last[:2].upper()
    letters = _alnum(label)
    for ch in letters[1:]:
        yield (letters[0] + ch).upper()
    for digit in "123456789":
        yield (letters[:1] or "X").upper() + digit
    yield from (a + b for a in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" for b in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")


def _whitespace_derived(label: str) -> bool:
    """True when the first-choice code comes from the name's first two
    space-separated words (``Hail Mary Club -> HM``), not from splitting a
    hyphenated word (``Hash-Mark Herons -> HM``): the former keeps a contested
    code."""
    parts = label.split()
    return len(parts) >= 2 and not _WORD_SPLIT.search(parts[0])


def monogram_codes(labels: Sequence[str]) -> dict[str, str]:
    """A unique two-character code per distinct label, shared by the web page
    and the email.

    First choice is :func:`monogram`. Teams sharing a first choice are
    resolved so no two print alike: a lone name built from two real words keeps
    it (Hail Mary Club ``HM`` over Hash-Mark Herons), the rest take their first
    and last words (``HH``), then the first word's first two letters
    (Midfield Magpies ``MI``, Motion Mallards ``MO``), then further fallbacks.
    Deterministic in the order of *labels*.
    """
    ordered = list(dict.fromkeys(labels))
    first_choice = {label: monogram(label) for label in ordered}
    groups: dict[str, list[str]] = {}
    for label in ordered:
        groups.setdefault(first_choice[label], []).append(label)
    codes: dict[str, str] = {}
    pending: list[str] = []
    for code, members in groups.items():
        if len(members) == 1:
            codes[members[0]] = code
            continue
        keepers = [m for m in members if _whitespace_derived(m)]
        if len(keepers) == 1:
            codes[keepers[0]] = code
            pending.extend(m for m in members if m is not keepers[0])
        else:
            pending.extend(members)
    taken = set(codes.values())
    taken.update(first_choice[label] for label in pending)  # a contested code stays off the table
    for label in sorted(pending, key=ordered.index):
        for candidate in _monogram_candidates(label):
            if candidate not in taken:
                codes[label] = candidate
                taken.add(candidate)
                break
    return {label: codes[label] for label in ordered}


def default_team_colors(roster_ids: Iterable[str]) -> dict[str, int]:
    """The render-time fallback assignment: roster ids in numeric order take
    slots 0, 1, 2, ... Used when no stored colors are passed (and, logged, when
    the store cannot be read); the stored assignment from ``weekly`` wins."""

    def key(value: str) -> tuple[int, int | str]:
        return (0, int(value)) if value.isdigit() else (1, value)

    return {rid: slot for slot, rid in enumerate(sorted(dict.fromkeys(roster_ids), key=key))}


def color_classes(slot: int | None) -> str:
    """The CSS classes for a team's color slot: ``tc<N>`` (one of 16 palette
    colors) plus ``td`` (the dotted second style) from the 17th team on."""
    if slot is None:
        return ""
    slot = max(0, slot)
    return f"tc{slot % PALETTE_SIZE}" + (" td" if slot % TEAM_COLOR_SLOTS >= PALETTE_SIZE else "")


def resolve_team_colors(facts: WeeklyFacts, stored: Mapping[str, int] | None) -> dict[str, int]:
    """Every team's slot: the stored one, else the next free slot (the
    fallback never reuses a stored slot)."""
    ids = [team.roster_id for team in facts.teams]
    if stored is None:
        return default_team_colors(ids)
    slots = {rid: stored[rid] for rid in ids if rid in stored}
    taken = set(slots.values())
    free = (n for n in range(max(taken, default=-1) + len(ids) + 1) if n not in taken)
    for rid in ids:
        if rid not in slots:
            slots[rid] = next(free)
    return slots
