"""Extension seam: the weekly web page's interaction layer (AD-40). Protocol
only, one reference impl max.

:func:`~commishdesk.render.weekly_web.render_weekly_web` takes an optional
:class:`WebEnhancer` — one CSS string and one JS string. The engine keeps the
markup, the static fallbacks and every escape; an enhancer only adds motion and
interaction on top of a page that is already complete without it. With no
enhancer the page carries zero ``<script>`` elements.

Whatever an enhancer supplies is validated at render time, twice:

* **Pair level** (:func:`validate_enhancer`) — the CSS and JS strings
  themselves: no way out of their element, no network, no code-from-string.
* **Page level** (:func:`validate_page`) — the finished page, parsed with the
  standard library's :mod:`html.parser` (real tags only; escaped text is never
  a tag): exactly one ``<script>`` with an enhancer and zero without, no
  ``on*`` attribute on any tag, and no ``src`` / ``href`` / ``<link>`` /
  ``<iframe>`` / ``<img>`` sub-resource.

Every refusal raises :class:`EnhancerRejected` naming the rule it broke.

The public engine ships no enhancer of its own apart from the private
``_builtin_enhancer`` kept for one tag (Story 6.0c Part A, removed in Part B).

**Pipeline fence (AD-1).** Standard library only.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Protocol, runtime_checkable

__all__ = ["EnhancerRejected", "WebEnhancer", "validate_enhancer", "validate_page"]


@runtime_checkable
class WebEnhancer(Protocol):
    # AD-40: the hosted app supplies the one implementation; the engine ships none
    # (Story 6.0c Part A keeps a private built-in for one tag).
    def css(self) -> str: ...

    def js(self) -> str: ...


class EnhancerRejected(ValueError):
    """An enhancer (or the page it produced) broke a named AD-40 rule."""

    def __init__(self, rule: str, detail: str = "") -> None:
        self.rule = rule
        message = f"enhancer rejected: {rule}"
        if detail:
            message += f" ({detail})"
        super().__init__(message)


# (rule name, pattern) — checked against the enhancer's JS string.
_JS_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("js-script-tag", re.compile(r"<\s*/?\s*script", re.IGNORECASE)),
    ("js-eval", re.compile(r"\beval\s*\(")),
    ("js-function-constructor", re.compile(r"\bFunction\s*\(")),
    ("js-fetch", re.compile(r"\bfetch\s*\(")),
    ("js-xmlhttprequest", re.compile(r"XMLHttpRequest")),
    ("js-sendbeacon", re.compile(r"sendBeacon")),
    ("js-websocket", re.compile(r"WebSocket")),
    ("js-eventsource", re.compile(r"EventSource")),
    ("js-dynamic-import", re.compile(r"\bimport\s*\(")),
    ("js-importscripts", re.compile(r"importScripts")),
    ("js-url", re.compile(r"https?:", re.IGNORECASE)),
    ("js-src-assign", re.compile(r"\.src\s*=")),
    ("js-set-src", re.compile(r"""setAttribute\s*\(\s*["'`]src""", re.IGNORECASE)),
    ("js-set-handler", re.compile(r"""setAttribute\s*\(\s*["'`]on""", re.IGNORECASE)),
)

# (rule name, pattern) — checked against the enhancer's CSS string.
_CSS_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("css-style-tag", re.compile(r"<\s*/\s*style", re.IGNORECASE)),
    ("css-import", re.compile(r"@import", re.IGNORECASE)),
    ("css-http", re.compile(r"http", re.IGNORECASE)),
    ("css-url-not-data", re.compile(r"""url\(\s*(?!["']?\s*data:)""", re.IGNORECASE)),
)

_SUBRESOURCE_TAGS = frozenset({"link", "iframe", "img"})
_SUBRESOURCE_ATTRS = frozenset({"src", "href", "srcset"})


def validate_enhancer(enhancer: object) -> tuple[str, str]:
    """Return the enhancer's ``(css, js)`` after the pair-level rules, or raise
    :class:`EnhancerRejected` naming the first rule broken."""
    if not isinstance(enhancer, WebEnhancer):
        raise EnhancerRejected("not-a-webenhancer", type(enhancer).__name__)
    css = enhancer.css()
    js = enhancer.js()
    if not isinstance(css, str):
        raise EnhancerRejected("css-not-str", type(css).__name__)
    if not isinstance(js, str):
        raise EnhancerRejected("js-not-str", type(js).__name__)
    for rule, pattern in _JS_RULES:
        if pattern.search(js):
            raise EnhancerRejected(rule)
    for rule, pattern in _CSS_RULES:
        if pattern.search(css):
            raise EnhancerRejected(rule)
    return css, js


class _PageScan(HTMLParser):
    """Collects the page-level facts the AD-40 rules read. ``<script>`` and
    ``<style>`` bodies are CDATA to :class:`HTMLParser`, so nothing inside them
    — and no escaped text anywhere — is ever reported as a tag."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts = 0
        self.violation: tuple[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.scripts += 1
        if self.violation is not None:
            return
        if tag in _SUBRESOURCE_TAGS:
            self.violation = ("page-subresource", f"<{tag}>")
            return
        for name, _value in attrs:
            if name.startswith("on"):
                self.violation = ("page-inline-handler", f"<{tag} {name}>")
                return
            if name in _SUBRESOURCE_ATTRS or name.rsplit(":", 1)[-1] == "href":
                self.violation = ("page-subresource", f"<{tag} {name}>")
                return


def validate_page(page: str, *, enhanced: bool) -> None:
    """Raise :class:`EnhancerRejected` unless *page* meets the page-level rules:
    exactly one ``<script>`` element when *enhanced*, zero otherwise; no
    ``on*`` attribute; no ``src`` / ``href`` / ``<link>`` / ``<iframe>`` /
    ``<img>`` sub-resource."""
    scan = _PageScan()
    scan.feed(page)
    scan.close()
    if scan.violation is not None:
        raise EnhancerRejected(*scan.violation)
    expected = 1 if enhanced else 0
    if scan.scripts != expected:
        raise EnhancerRejected("page-script-count", f"{scan.scripts} <script>, expected {expected}")
