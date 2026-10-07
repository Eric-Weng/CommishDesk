"""Story 7.4 — I7: separate transactional and bulk sending identities.

:func:`mail_identities` normalises the two configured sending identities to bare
domains and refuses a blank one or an equal pair, so a reputation hit on the bulk
subdomain cannot take down confirmations (CLAUDE.md §2, I7). Comparison ignores
case, surrounding whitespace, a display name, the local part, a port and a trailing
dot. Errors name the domain, never an address.

Pipeline fence (AD-1): standard library + :mod:`commishdesk.errors` only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from commishdesk.errors import MailIdentityError

__all__ = ["MailIdentities", "mail_identities"]

_PORT = re.compile(r":\d+$")


@dataclass(frozen=True, slots=True)
class MailIdentities:
    """The two normalised sending domains; always distinct."""

    transactional: str
    bulk: str


def _domain(label: str, value: str) -> str:
    text = value.strip()
    if "<" in text and ">" in text:
        start = text.rindex("<") + 1
        text = text[start : text.index(">", start)] if ">" in text[start:] else text
    text = text.rsplit("@", 1)[-1]
    text = text.strip().rstrip(".")
    text = _PORT.sub("", text).strip().rstrip(".").lower()
    if not text or any(ch.isspace() or ch in "<>@" for ch in text):
        raise MailIdentityError(f"the {label} sending identity has no usable domain")
    return text


def mail_identities(transactional: str, bulk: str) -> MailIdentities:
    """Resolve the transactional and bulk identities, refusing blank or equal ones."""
    first = _domain("transactional", transactional)
    second = _domain("bulk", bulk)
    if first == second:
        raise MailIdentityError(
            f"transactional and bulk mail must use separate sending identities (both resolve to {first})"
        )
    return MailIdentities(transactional=first, bulk=second)
