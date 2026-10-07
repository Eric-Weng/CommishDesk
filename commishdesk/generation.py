"""The Generation Set — the one constructor that derives a run list (AD-6 / AD-44 / I1).

:func:`build_generation_set` is the **only** function that turns plain
:class:`LeagueFacts` into the list of leagues a run will process, and
:class:`GenerationSet` is instantiated in exactly one place — here. No other code
path adds a league to a run (invariant I1): mass-registering ten thousand
harvested ids, none of them eligible, produces an empty set and zero downstream
work.

The function is pure: plain data in, plain data out, no I/O and no clock. The app
(Epic 7, stories 7.7/7.8) builds the facts from its own stores; the engine decides.

* **Eligible** — ``onboarded`` and at least one claim that is confirmed, not
  unsubscribed and not revoked. A Discord destination is never required.
* **Decay (I5)** — only confirmed *email* sends count. With at least six, a league
  is decayed when no engagement is later than the sixth-most-recent email send;
  fewer than six is never decayed; a later engagement reactivates it. Four or five
  quiet email sends mark it *approaching decay* (an operator alert, not exclusion).
* **Headroom** — already-active leagues (at least one confirmed send this season)
  always stay, even above the cap; the others fill ``max_active_leagues - active``
  slots, oldest ``created_at`` first; the rest wait.

At MVP an explicit ``--league`` argument on the CLI is the verified channel:
:func:`cli_facts` makes the one eligible fact the CLI feeds in.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

__all__ = [
    "ClaimFacts",
    "GenerationPolicy",
    "GenerationSet",
    "LeagueFacts",
    "Reason",
    "SendFacts",
    "build_generation_set",
    "cli_facts",
]

#: Confirmed email sends needed before decay can apply, and the quiet counts that
#: warn the operator beforehand (FR-46).
DECAY_SENDS = 6
APPROACHING_DECAY_SENDS = 4


class Reason(StrEnum):
    """Why a league is not in the run list."""

    INELIGIBLE = "ineligible"
    DECAYED = "decayed"
    WAITING = "waiting"
    NEW_NOT_ALLOWED = "new_not_allowed"


@dataclass(frozen=True)
class ClaimFacts:
    """One leaguemate claim, reduced to the three flags the engine decides on."""

    confirmed: bool
    unsubscribed: bool = False
    revoked: bool = False


@dataclass(frozen=True)
class SendFacts:
    """One send recorded for the league's current season."""

    sent_at: datetime
    channel: str
    confirmed: bool = True


@dataclass(frozen=True)
class LeagueFacts:
    """Everything the engine needs to decide whether a league earns an Issue."""

    league_id: str
    created_at: datetime
    onboarded: bool
    claims: Sequence[ClaimFacts] = ()
    sends: Sequence[SendFacts] = ()
    last_engagement_at: datetime | None = None


@dataclass(frozen=True)
class GenerationPolicy:
    """Operator-set limits for one run."""

    max_active_leagues: int
    allow_new: bool = True


@dataclass(frozen=True)
class GenerationSet:
    """The immutable run list: the league ids a run will process, in order, with
    no duplicates — plus why the others were left out."""

    league_ids: tuple[str, ...]
    excluded: tuple[tuple[str, Reason], ...] = field(default=())
    waiting: tuple[tuple[str, Reason], ...] = field(default=())
    approaching_decay: tuple[str, ...] = field(default=())


def _eligible(facts: LeagueFacts) -> bool:
    return facts.onboarded and any(
        c.confirmed and not c.unsubscribed and not c.revoked for c in facts.claims
    )


def _email_send_times(facts: LeagueFacts) -> list[datetime]:
    return sorted(
        (s.sent_at for s in facts.sends if s.channel == "email" and s.confirmed),
        reverse=True,
    )


def _is_decayed(emails_newest_first: list[datetime], engagement: datetime | None) -> bool:
    if len(emails_newest_first) < DECAY_SENDS:
        return False
    return engagement is None or engagement <= emails_newest_first[DECAY_SENDS - 1]


def _quiet_sends(emails: list[datetime], engagement: datetime | None) -> int:
    if engagement is None:
        return len(emails)
    return sum(1 for sent_at in emails if sent_at > engagement)


def build_generation_set(
    facts: Sequence[LeagueFacts], policy: GenerationPolicy
) -> GenerationSet:
    """Derive the :class:`GenerationSet` from ``facts`` under ``policy``.

    Duplicate ``league_id`` -> the first wins. Empty ``facts`` -> empty set. Order of
    ``league_ids``: active leagues in input order, then admitted new leagues by
    ``created_at`` (input order breaks ties).
    """
    seen: set[str] = set()
    excluded: list[tuple[str, Reason]] = []
    active: list[LeagueFacts] = []
    new: list[LeagueFacts] = []
    approaching: dict[str, None] = {}

    for league in facts:
        if league.league_id in seen:
            continue
        seen.add(league.league_id)
        if not _eligible(league):
            excluded.append((league.league_id, Reason.INELIGIBLE))
            continue
        emails = _email_send_times(league)
        if _is_decayed(emails, league.last_engagement_at):
            excluded.append((league.league_id, Reason.DECAYED))
            continue
        if any(s.confirmed for s in league.sends):
            active.append(league)
            if _quiet_sends(emails, league.last_engagement_at) >= APPROACHING_DECAY_SENDS:
                approaching[league.league_id] = None
        else:
            new.append(league)

    waiting: list[tuple[str, Reason]] = []
    admitted_new: list[LeagueFacts] = []
    if not policy.allow_new:
        waiting.extend((n.league_id, Reason.NEW_NOT_ALLOWED) for n in new)
    else:
        slots = max(0, policy.max_active_leagues - len(active))
        ordered = sorted(new, key=lambda n: n.created_at)  # stable
        admitted_new = ordered[:slots]
        waiting.extend((n.league_id, Reason.WAITING) for n in ordered[slots:])

    return GenerationSet(
        league_ids=tuple(a.league_id for a in active)
        + tuple(n.league_id for n in admitted_new),
        excluded=tuple(excluded),
        waiting=tuple(waiting),
        approaching_decay=tuple(approaching),
    )


def cli_facts(league_id: str) -> list[LeagueFacts]:
    """The CLI's own facts for an explicit ``--league``: an explicit argument *is*
    the verified channel, so the league is onboarded with one confirmed claim."""
    return [
        LeagueFacts(
            league_id=str(league_id),
            created_at=datetime(1970, 1, 1, tzinfo=UTC),
            onboarded=True,
            claims=(ClaimFacts(confirmed=True),),
        )
    ]
