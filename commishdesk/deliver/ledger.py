"""Story 4.4 — the Send Ledger: idempotent, channel-agnostic delivery.

:func:`send_issue` reads the existing ``Store`` Send Ledger for one
``(league_id, week)``, skips every ``(channel, recipient)`` already recorded
``confirmed``, calls a caller-supplied ``sender`` for the rest, and appends a
``confirmed`` :class:`~commishdesk.store.LedgerEntry` **only after** that send
returns. A deliberate re-issue is an explicit ``reason=`` that re-sends every
recipient and records a new entry carrying that reason.

**Confirm-on-success (AD-10).** The only durable state is "this send happened".
A crash between ``sender`` returning and ``append_ledger_entry`` completing
leaves that recipient unconfirmed, so the next run re-sends it — an
at-most-once-*more* on resume, deliberately chosen over a pending-row protocol.
There are no pending / reserved rows, no retry loop, and no locking here.

**Partial failure is a report, not an exception.** A run to N recipients where
one channel hiccups still records the N-1 successes and surfaces the one failure
in :class:`SendReport`; a per-recipient :class:`~commishdesk.errors.DeliveryError`
is caught and the loop continues (AD-9 shape). Any other exception — a
:class:`~commishdesk.errors.StoreError` from the append included — propagates.

**Pipeline fence (AD-1).** Standard library + :mod:`commishdesk.store` +
:mod:`commishdesk.errors` + :mod:`commishdesk.logconfig` only — no ``httpx``,
``render``, or ``narrate``. The caller closes over the transport in ``sender``;
the webhook URL / token never enters this module or the ledger file.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from commishdesk.errors import DeliveryError
from commishdesk.logconfig import LOGGER_NAME
from commishdesk.store import LedgerEntry, Store

__all__ = ["SendReport", "send_issue"]

_logger = logging.getLogger(f"{LOGGER_NAME}.deliver.ledger")

#: The NFL weeks a draft recap / weekly recap can be delivered for — the same
#: range ``LedgerEntry.week`` enforces (``store.py``). Checked before any send.
_MIN_WEEK = 1
_MAX_WEEK = 18


@dataclass(frozen=True, slots=True)
class SendReport:
    """The outcome of one :func:`send_issue` run.

    Purely a return value — not persisted, no ``schema_version`` (cf. the record
    models in ``store.py``). ``delivered`` and ``skipped`` are recipient ids;
    ``failed`` pairs each recipient with a one-line message.
    """

    delivered: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()


def _default_now() -> datetime:
    """The wall clock, timezone-aware UTC — the default ``now`` for ``sent_at``."""
    return datetime.now(tz=UTC)


def send_issue(
    store: Store,
    *,
    league_id: str,
    week: int,
    channel: str,
    recipients: Mapping[str, str],
    sender: Callable[[str, str], object],
    now: Callable[[], datetime] = _default_now,
    reason: str | None = None,
) -> SendReport:
    """Deliver one Issue to ``recipients`` over ``channel``, exactly once each.

    ``recipients`` maps a recipient id (a Discord webhook id, later an email
    address) to the content string to send it. For each recipient in sorted
    order:

    * if ``(channel, recipient)`` is already ``confirmed`` in the ledger for
      ``(league_id, week)`` and ``reason`` is not set, it is skipped with no
      ``sender`` call;
    * otherwise ``sender(recipient, content)`` is called, and **only if it
      returns** a ``confirmed`` :class:`~commishdesk.store.LedgerEntry` is
      appended with ``sent_at = now()`` and the given ``reason``.

    A ``sender`` raising :class:`~commishdesk.errors.DeliveryError` is caught,
    recorded in ``failed``, and the loop continues — that recipient gets no
    ledger entry. Any other exception (including a
    :class:`~commishdesk.errors.StoreError` from the append) propagates.

    ``reason`` marks a deliberate re-issue: every recipient is re-sent regardless
    of the ledger, and each new entry carries that ``reason``. A re-issue is a
    deliberate, non-idempotent operator action — running it twice sends twice.

    Raises :class:`ValueError` — before any ``sender`` call — when ``week`` is
    outside ``1..18``, when ``league_id`` could escape the store root as a path
    segment, when ``reason`` is set but blank, or when ``now()`` returns a
    datetime that is not timezone-aware. The clock is sampled once, before the
    loop, so every entry from one run shares a ``sent_at``.
    """
    if not _MIN_WEEK <= week <= _MAX_WEEK:
        raise ValueError(
            f"week {week} is outside the deliverable range {_MIN_WEEK}-{_MAX_WEEK}"
        )

    # Same rule as ``store._safe_league_id`` — inlined so this guard runs before
    # any ``sender`` call, not on the first ``append_ledger_entry`` after the
    # messages have already gone out.
    if (
        not league_id
        or league_id in (".", "..")
        or "/" in league_id
        or "\\" in league_id
        or "\x00" in league_id
    ):
        raise ValueError(f"unsafe league id: {league_id!r}")

    if reason is not None and not reason.strip():
        raise ValueError("reason for a re-issue must be a non-blank string")

    stamp = now()
    if stamp.tzinfo is None or stamp.tzinfo.utcoffset(stamp) is None:
        raise ValueError("now() must return a timezone-aware datetime")

    if reason is None:
        already = {
            entry.recipient
            for entry in store.read_ledger(league_id, week)
            if entry.channel == channel
        }
    else:
        already = set()

    delivered: list[str] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []

    for recipient, content in sorted(recipients.items()):
        if recipient in already:
            _logger.debug(
                "skipping %s on %s for week %s — already confirmed",
                recipient,
                channel,
                week,
            )
            skipped.append(recipient)
            continue

        try:
            sender(recipient, content)
        except DeliveryError as exc:
            message = str(exc) or exc.__class__.__name__
            _logger.warning(
                "delivery to %s on %s for week %s failed: %s",
                recipient,
                channel,
                week,
                message,
            )
            failed.append((recipient, message))
            continue

        store.append_ledger_entry(
            LedgerEntry(
                league_id=league_id,
                week=week,
                channel=channel,
                recipient=recipient,
                sent_at=stamp,
                reason=reason,
            )
        )
        _logger.info(
            "confirmed delivery to %s on %s for week %s%s",
            recipient,
            channel,
            week,
            f" (re-issue: {reason})" if reason is not None else "",
        )
        delivered.append(recipient)

    _logger.info(
        "send_issue for league %s week %s on %s: %d delivered, %d skipped, %d failed",
        league_id,
        week,
        channel,
        len(delivered),
        len(skipped),
        len(failed),
    )
    return SendReport(
        delivered=tuple(delivered),
        skipped=tuple(skipped),
        failed=tuple(failed),
    )
