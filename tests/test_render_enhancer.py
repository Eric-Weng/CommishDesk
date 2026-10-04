"""Story 6.0c (Part A) -- the ``WebEnhancer`` seam (AD-40).

Every row of the spec's I/O matrix with a test-only fake enhancer: a valid
fake lands its CSS at the end of the one ``<style>`` and its JS in the one
``<script>``; each pair-level rule (JS and CSS) and each page-level rule
raises :class:`EnhancerRejected` naming the rule; and a structural guard that
no ``src`` module defines an enhancer (Part B removed the built-in).
"""

from __future__ import annotations

import ast
import importlib.util
import json
from typing import Any

import pytest

from commishdesk.facts.schema import WeeklyFacts
from commishdesk.narrate.weekly_template import render_weekly_issue
from commishdesk.render import EnhancerRejected, WebEnhancer, render_weekly_web
from commishdesk.render import style as style_mod
from commishdesk.render.enhancer import validate_enhancer, validate_page
from tests.conftest import REPO_ROOT

FACTS = REPO_ROOT / "tests" / "fixtures" / "facts" / "expected-weekly-facts-week10-published.json"
PKG_ROOT = REPO_ROOT / "commishdesk"


class _Fake:
    """A test-only enhancer."""

    def __init__(self, css: str = ".fake { color: var(--ink); }", js: str = 'document.title = "fake";') -> None:
        self._css = css
        self._js = js

    def css(self) -> str:
        return self._css

    def js(self) -> str:
        return self._js


def _raw() -> dict[str, Any]:
    return json.loads(FACTS.read_text(encoding="utf-8"))


def _render(enhancer: object | None, raw: dict[str, Any] | None = None) -> str:
    facts = WeeklyFacts.model_validate(raw or _raw())
    return render_weekly_web(
        facts,
        render_weekly_issue(facts.narration),
        output_id="x",
        generated_at="2026-09-07T00:00:00Z",
        enhancer=enhancer,  # type: ignore[arg-type]
    )


def _rule(enhancer: object) -> str:
    with pytest.raises(EnhancerRejected) as caught:
        _render(enhancer)
    assert isinstance(caught.value, ValueError)
    assert caught.value.rule in str(caught.value)
    return caught.value.rule


# --------------------------------------------------------------------------- #
# I/O matrix
# --------------------------------------------------------------------------- #


def test_no_enhancer_is_static_with_zero_scripts() -> None:
    page = _render(None)
    assert "<script" not in page
    css = page.split("<style>", 1)[1].split("</style>", 1)[0]
    assert "js-reveal" not in css


def test_fake_valid_enhancer_lands_its_css_last_and_its_js_in_the_one_script() -> None:
    fake = _Fake()
    page = _render(fake)
    css = page.split("<style>\n", 1)[1].split("\n</style>", 1)[0]
    assert css == style_mod.build_weekly_style() + "\n" + fake.css()
    assert page.count("<script") == 1
    assert page.endswith("</main>\n<script>\n" + fake.js() + "\n</script>\n</body>\n</html>\n")
    assert "js-reveal" not in page  # nothing of the old built-in leaks in


def test_fake_passes_pair_validation_and_is_a_webenhancer() -> None:
    fake = _Fake()
    assert validate_enhancer(fake) == (fake.css(), fake.js())
    assert isinstance(fake, WebEnhancer)


def test_same_facts_and_enhancer_give_identical_bytes() -> None:
    assert _render(_Fake()) == _render(_Fake())


@pytest.mark.parametrize(
    ("js", "rule"),
    [
        ('var s = "</script><script>alert(1)</script>";', "js-script-tag"),
        ('var s = "<SCRIPT>";', "js-script-tag"),
        ('eval("1 + 1");', "js-eval"),
        ('var f = new Function("return 1");', "js-function-constructor"),
        ('fetch("/x");', "js-fetch"),
        ("var x = new XMLHttpRequest();", "js-xmlhttprequest"),
        ('navigator.sendBeacon("/t", "");', "js-sendbeacon"),
        ('new WebSocket("wss://x");', "js-websocket"),
        ('new EventSource("/s");', "js-eventsource"),
        ('import("./m.js");', "js-dynamic-import"),
        ('importScripts("w.js");', "js-importscripts"),
        ('var u = "https://example.com";', "js-url"),
        ('var u = "http:" + "//x";', "js-url"),
        ('img.src = "x";', "js-src-assign"),
        ('el.setAttribute("src", "x");', "js-set-src"),
        ("el.setAttribute('onclick', 'x()');", "js-set-handler"),
    ],
)
def test_forbidden_js_is_rejected_naming_the_rule(js: str, rule: str) -> None:
    assert _rule(_Fake(js=js)) == rule


@pytest.mark.parametrize(
    ("css", "rule"),
    [
        ("</style><script>x()</script>", "css-style-tag"),
        ('@import "x.css";', "css-import"),
        (".a { background: url(https://example.com/x.png); }", "css-http"),
        (".a { background: url(/x.png); }", "css-url-not-data"),
        (".a { background: url('x.png'); }", "css-url-not-data"),
    ],
)
def test_forbidden_css_is_rejected_naming_the_rule(css: str, rule: str) -> None:
    assert _rule(_Fake(css=css)) == rule


def test_data_urls_in_enhancer_css_are_allowed() -> None:
    page = _render(_Fake(css=".a { background: url(data:image/png;base64,AAAA); }"))
    assert "url(data:image/png;base64,AAAA)" in page


def test_a_non_enhancer_is_rejected() -> None:
    assert _rule(object()) == "not-a-webenhancer"


def test_non_string_halves_are_rejected() -> None:
    class _BadCss(_Fake):
        def css(self) -> str:
            return 1  # type: ignore[return-value]

    assert _rule(_BadCss()) == "css-not-str"


def test_hostile_team_name_is_escaped_text_not_rejected() -> None:
    raw = _raw()
    raw["teams"][0]["team_name"] = "<svg onload=1>"
    for enhancer in (None, _Fake()):
        page = _render(enhancer, raw)
        assert "<svg onload=1>" not in page
        assert "&lt;svg onload=1&gt;" in page


# --------------------------------------------------------------------------- #
# Page-level validator (real tags only)
# --------------------------------------------------------------------------- #


_SHELL = "<!doctype html><html><head><style>{css}</style></head><body>{body}</body></html>"


@pytest.mark.parametrize(
    ("body", "enhanced", "rule"),
    [
        ("<p>x</p>", True, "page-script-count"),
        ("<script>a()</script>", False, "page-script-count"),
        ("<script>a()</script><script>b()</script>", True, "page-script-count"),
        ('<p onclick="x()">x</p>', False, "page-inline-handler"),
        ('<svg><g onload="x()"></g></svg>', False, "page-inline-handler"),
        ('<a href="#x">x</a>', False, "page-subresource"),
        ('<svg><use xlink:href="#x"/></svg>', False, "page-subresource"),
        ('<script src="x.js"></script>', True, "page-subresource"),
        ('<link rel="stylesheet">', False, "page-subresource"),
        ("<iframe></iframe>", False, "page-subresource"),
        ('<img alt="">', False, "page-subresource"),
    ],
)
def test_page_rules_reject_real_tags(body: str, enhanced: bool, rule: str) -> None:
    with pytest.raises(EnhancerRejected) as caught:
        validate_page(_SHELL.format(css="", body=body), enhanced=enhanced)
    assert caught.value.rule == rule


def test_page_rules_ignore_escaped_text_and_script_or_style_bodies() -> None:
    body = (
        "<p>&lt;script&gt;&lt;img src=x onerror=1&gt;</p>"
        '<script>var s = "<img src=x onerror=1>";</script>'
    )
    validate_page(_SHELL.format(css='.a::after { content: "<iframe>"; }', body=body), enhanced=True)


# --------------------------------------------------------------------------- #
# Structural: the public engine defines no enhancer
# --------------------------------------------------------------------------- #


def _defines_enhancer(tree: ast.Module) -> bool:
    """A class with both ``css`` and ``js`` methods (a structural
    ``WebEnhancer``), or a class that names ``WebEnhancer`` as a base."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        bases = {b.id if isinstance(b, ast.Name) else getattr(b, "attr", "") for b in node.bases}
        if "Protocol" in bases:
            continue  # the protocol itself
        methods = {item.name for item in node.body if isinstance(item, ast.FunctionDef)}
        if {"css", "js"} <= methods or "WebEnhancer" in bases:
            return True
    return False


def test_no_src_module_defines_a_webenhancer() -> None:
    found = sorted(
        path.relative_to(PKG_ROOT).as_posix()
        for path in PKG_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts and _defines_enhancer(ast.parse(path.read_text(encoding="utf-8")))
    )
    assert found == []


def test_old_bundled_enhancer_module_is_gone() -> None:
    # The parent package is already imported at module top, so find_spec resolves cleanly.
    assert importlib.util.find_spec("commishdesk.render._built" + "in_enhancer") is None


def test_the_structural_check_sees_a_fake() -> None:
    assert _defines_enhancer(ast.parse("class X:\n    def css(self): ...\n    def js(self): ...\n"))
    assert not _defines_enhancer(
        ast.parse("class W(Protocol):\n    def css(self): ...\n    def js(self): ...\n")
    )
