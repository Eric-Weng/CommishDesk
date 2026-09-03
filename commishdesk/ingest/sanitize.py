"""The one league-supplied-string scrubber for the whole engine (AD-24).

``build.py`` is the only caller: every attacker-controllable string -- the league
name, a roster/user team name, a display name -- passes through :func:`sanitize`
exactly once, at the stage-1 boundary, before it enters a model field. Downstream
stages then treat model strings as trusted. Player names come from NFL data, not
the league, and are never run through here.

:func:`sanitize` is pure, deterministic and offline. In order it:

1. coerces a non-``str`` input (``None`` -> ``""``, else ``str(value)``);
2. NFKC-normalizes (a ``fi`` ligature -> ``"fi"``, full-width digits -> ASCII);
3. turns line/paragraph separators and whitespace controls into a space (so
   tokens either side don't silently merge), then drops every remaining Unicode
   control (``Cc``) and format (``Cf``) character;
4. removes URLs entirely -- ``http(s)://...`` / ``ftp://...`` and ``www....``
   unconditionally, plus a bare ``host.tld`` when it carries a path or ends in a
   common TLD;
5. collapses whitespace runs to one space and strips the ends;
6. truncates to :data:`MAX_NAME_LENGTH`.

It returns ``""`` when nothing survives. There is no semantic filtering: a
prompt-injection phrase survives as inert literal text. The output carries no
control chars, no URLs and no length blow-up -- but HTML- and prompt-structure
escaping (``<``, ``>``, ``</system>``) remains the renderer's / prompt layer's
responsibility, not this function's.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["MAX_NAME_LENGTH", "sanitize"]

#: Upper bound on any league-supplied string once it reaches a model field.
#: Team / league / display names are attacker-controlled and flow to three sinks
#: (the LLM prompt, web HTML, email HTML); a hard cap keeps a hostile rename from
#: bloating the narration payload or a rendered surface.
MAX_NAME_LENGTH = 100

# Line/paragraph separators + whitespace controls, mapped to a space (not
# deleted) so an embedded newline becomes " ", never "". U+2028 / U+2029 are
# category Zl / Zp; re interprets the backslash-u / backslash-x escapes below.
_WHITESPACE_CONTROLS = re.compile(r"[\t\n\r\f\v\x1c-\x1f\x85\u2028\u2029]")

# Control (Cc) and format (Cf) characters -- zero-width joiners, bidi overrides,
# the BEL, etc. Deleted outright once the whitespace controls above are handled.
_CONTROL_FORMAT_CATEGORIES = frozenset({"Cc", "Cf"})

# URL / bare-host removal.
#   * scheme-prefixed (http(s)://, ftp://) or "www." -> always a URL;
#   * a bare dotted host WITH a path -> a URL;
#   * a bare dotted host ending in a common TLD -> a URL.
# A bare "St.Louis" / "Run.CMC" (no path, uncommon final label) is left intact.
_HOST = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*"
_COMMON_TLD = r"(?:com|net|org|io|gg|co|app|dev|xyz|me|tv|info|biz|online|site)"
_URL_RE = re.compile(
    rf"""
    (?:(?:https?|ftp)://|www\.)\S+        # scheme:// ... or www. ...
    |
    \b{_HOST}\.[a-z]{{2,}}/\S*            # bare host + path
    |
    \b{_HOST}\.{_COMMON_TLD}\b            # bare host + common TLD
    """,
    re.IGNORECASE | re.VERBOSE,
)

_WHITESPACE_RUN = re.compile(r"\s+")


def sanitize(value: str) -> str:
    """Scrub one league-supplied string. See the module docstring for the exact,
    ordered pipeline. Pure, deterministic, offline; returns ``""`` when nothing
    survives."""
    if not isinstance(value, str):
        value = "" if value is None else str(value)
    text = unicodedata.normalize("NFKC", value)
    text = _WHITESPACE_CONTROLS.sub(" ", text)
    text = "".join(
        ch for ch in text if unicodedata.category(ch) not in _CONTROL_FORMAT_CATEGORIES
    )
    text = _URL_RE.sub(" ", text)
    text = _WHITESPACE_RUN.sub(" ", text).strip()
    return text[:MAX_NAME_LENGTH].strip()
