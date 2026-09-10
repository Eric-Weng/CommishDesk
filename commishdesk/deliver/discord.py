"""Story 4.3 — post a plain-text Issue summary to a Discord channel webhook.

:func:`post_discord_text` is one raw ``httpx`` ``POST`` to the webhook URL with a
JSON body of ``{"content": <str>, "allowed_mentions": {"parse": []}}`` only — the
``allowed_mentions`` block suppresses every ping (``@everyone`` / ``@here`` /
role / user) that a league name or a narrated line might contain, and there is
still no ``embeds``, ``files``/multipart, ``username``, or ``avatar_url``. Discord
answers a successful post with ``204``; any ``2xx`` is accepted. Every fault is
wrapped **at the call site** in :class:`~commishdesk.errors.DeliveryError` — a
webhook URL that is not a Discord webhook and a ``content`` over Discord's
2000-character limit are both rejected here with **no network call**.

The webhook URL is a secret (CLAUDE.md §1): it is read only from
``COMMISHDESK_DISCORD_WEBHOOK_URL`` by the caller, never written to a file, never
passed to ``log_context``, never logged raw. :func:`webhook_id` returns the
non-secret numeric id from the URL path — a stable value Story 4.4's Send Ledger
can use as its ``recipient`` without storing the token.

**Pipeline fence (AD-1).** Standard library + ``httpx`` +
:mod:`commishdesk.errors` + :mod:`commishdesk.logconfig` only.
"""

from __future__ import annotations

import logging
import re

import httpx

from commishdesk.errors import DeliveryError
from commishdesk.logconfig import LOGGER_NAME

__all__ = ["post_discord_text", "webhook_id"]

_logger = logging.getLogger(f"{LOGGER_NAME}.deliver.discord")

#: Discord's hard cap on a message ``content`` field.
_MAX_CONTENT = 2000

#: A channel webhook URL: the standard host, the ``ptb.`` / ``canary.`` release
#: hosts, and the legacy ``discordapp.com`` alias; a numeric id and an opaque
#: token in the path. The id is captured (non-secret); the token never is.
_WEBHOOK_RE = re.compile(
    r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/(\d+)/[\w-]+"
)

_URL_SHAPE = (
    "expected a Discord webhook URL of the shape "
    "https://discord.com/api/webhooks/<id>/<token>"
)

_TIMEOUT = 15.0


def webhook_id(url: str) -> str:
    """Return the numeric id segment of a Discord webhook ``url`` — a non-secret,
    forward-compatible value (Story 4.4's ledger ``recipient``). Raises
    :class:`~commishdesk.errors.DeliveryError` when ``url`` is not a Discord
    webhook URL."""
    match = _WEBHOOK_RE.fullmatch(url)
    if match is None:
        raise DeliveryError(f"not a Discord webhook URL — {_URL_SHAPE}")
    return match.group(1)


def post_discord_text(
    webhook_url: str, content: str, *, client: httpx.Client | None = None
) -> None:
    """POST ``{"content": content}`` to ``webhook_url`` and return ``None`` on any
    ``2xx``.

    A ``webhook_url`` that is not a Discord webhook, a ``content`` longer than
    2000 characters, and an empty / whitespace-only ``content`` are each rejected
    with a :class:`~commishdesk.errors.DeliveryError` and **no network call** (the
    URL shape is checked first). A non-2xx response, a transport failure, or an
    already-closed injected ``client`` is wrapped in a
    :class:`~commishdesk.errors.DeliveryError` chained from the underlying
    ``httpx.HTTPError`` / ``RuntimeError``. The body is
    ``{"content": <str>, "allowed_mentions": {"parse": []}}`` only —
    ``allowed_mentions`` suppresses every ping; still no ``embeds``, attachment,
    ``username``, or ``avatar_url``. ``client`` follows the
    ``commishdesk/consensus.py`` shape: an injected client is the caller's to
    close; one built here is closed in a ``finally``.
    """
    hook_id = webhook_id(webhook_url)  # URL-shape check — raises before any network call
    if len(content) > _MAX_CONTENT:
        raise DeliveryError(
            f"Discord message is {len(content)} characters; the limit is {_MAX_CONTENT}"
        )
    if not content.strip():
        raise DeliveryError("Discord message content is empty")

    owns_client = client is None
    client = client or httpx.Client()
    try:
        _logger.debug("posting a text Issue to Discord webhook %s", hook_id)
        response = client.post(
            webhook_url,
            json={"content": content, "allowed_mentions": {"parse": []}},
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
    except (httpx.HTTPError, RuntimeError) as exc:
        raise DeliveryError(_fault_message(hook_id, exc)) from exc
    finally:
        if owns_client:
            client.close()

    # Belt-and-braces: httpx's ``raise_for_status`` already flags every non-2xx
    # (4xx/5xx chain an ``HTTPStatusError`` above; 1xx/3xx do too on this httpx).
    # This guard makes "only a 2xx is a delivered post" explicit and survives a
    # client whose ``raise_for_status`` is lenient.
    if not response.is_success:
        raise DeliveryError(
            f"Discord did not accept the post (HTTP {response.status_code}) for "
            f"webhook {hook_id}"
        )


def _fault_message(hook_id: str, exc: Exception) -> str:
    """A single-line, token-free description of a webhook POST fault."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 429 or code >= 500:
            return (
                f"Discord is temporarily unavailable (HTTP {code}) posting to webhook "
                f"{hook_id} — retry shortly"
            )
        return (
            f"Discord rejected the webhook (HTTP {code}) — "
            f"the webhook {hook_id} may be deleted or the URL mistyped"
        )
    if isinstance(exc, httpx.TransportError):
        return f"could not reach Discord to post to webhook {hook_id} ({type(exc).__name__})"
    return f"posting to Discord webhook {hook_id} failed ({type(exc).__name__})"
