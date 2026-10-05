"""The markup hooks the private app's enhancer script binds to (AD-40, retro item 64, dw:776).

``(?<![\w-])commishdesk-app(?![\w-])/batch/web/enhancer.js`` finds its targets by these selectors. The engine
ships no script, so nothing else here would notice a markup rename; this test makes the
rename fail in the engine rather than in production. Each hook is checked on the static
weekly page of a fixture that has the section.
"""

import json
import re
from typing import Any

import pytest

from commishdesk.facts.schema import WeeklyFacts
from commishdesk.narrate.weekly_template import render_weekly_issue
from commishdesk.render import render_weekly_web
from tests.conftest import REPO_ROOT

_FACTS = REPO_ROOT / "tests" / "fixtures" / "facts" / "expected-weekly-facts-week10.json"


def _cls(name: str) -> str:
    """A class token matched whole: a hyphen is a word boundary, so card-expand-state is not card-expand."""
    return rf'class="(?:[^"]*\s)?{name}(?=[\s"])'


def _attr(name: str) -> str:
    return rf"(?<![\w-]){name}(?![\w-])"


#: Selectors the app's enhancer.js uses, each as (name, regex over the page's markup).
_HOOKS: tuple[tuple[str, str], ...] = (
    (".card-expand", _cls("card-expand")),
    (".card-expand-state", _cls("card-expand-state")),
    (".hit-state", _cls("hit-state")),
    (".luck-preview", _cls("luck-preview")),
    (".standings-toggle", _cls("standings-toggle")),
    (".pw-hit[data-team]", rf'<(?=[^>]*{_cls("pw-hit")})[^>]*{_attr("data-team")}="'),
    (".bump-team[data-team]", rf'<(?=[^>]*{_cls("bump-team")})[^>]*{_attr("data-team")}="'),
    (".bump-point.bump-model-dot", rf"<(?=[^>]*{_cls('bump-point')})(?=[^>]*{_cls('bump-model-dot')})[^>]*>"),
    (".luck-row[data-luck-preview]", rf'<(?=[^>]*{_cls("luck-row")})[^>]*{_attr("data-luck-preview")}="'),
    (".rec-act", _cls("rec-act")),
    (".legend-act", _cls("legend-act")),
    (".rec-ap", _cls("rec-ap")),
    (".legend-ap", _cls("legend-ap")),
    ("button[data-view]", rf"<button\b[^>]*{_attr('data-view')}="),
    ("[data-bump-chart]", rf"<[^>]*{_attr('data-bump-chart')}"),
    ("[data-bump-detail]", rf"<[^>]*{_attr('data-bump-detail')}"),
    ("[data-draw]", rf"<[^>]*{_attr('data-draw')}"),
    ("[data-luck-section]", rf"<[^>]*{_attr('data-luck-section')}"),
    ("[data-reveal]", rf"<[^>]*{_attr('data-reveal')}"),
    ("[data-standings]", rf"<[^>]*{_attr('data-standings')}"),
)


def _raw() -> dict[str, Any]:
    return json.loads(_FACTS.read_text(encoding="utf-8"))


def _static_page(raw: dict[str, Any]) -> str:
    facts = WeeklyFacts.model_validate(raw)
    return render_weekly_web(
        facts,
        render_weekly_issue(facts.narration),
        output_id="x",
        generated_at="2026-09-07T00:00:00Z",
        enhancer=None,
    )


@pytest.fixture(scope="module")
def page() -> str:
    html = _static_page(_raw())
    assert "<script" not in html  # the static page: the hooks exist without the script
    return html.split("</style>", 1)[1]


@pytest.mark.parametrize(("hook", "pattern"), _HOOKS, ids=[h for h, _ in _HOOKS])
def test_static_page_carries_every_hook_the_app_enhancer_binds_to(page: str, hook: str, pattern: str) -> None:
    assert re.search(pattern, page), f"static weekly page lost the markup hook {hook}"


def test_a_removed_hook_is_caught_and_named(page: str) -> None:
    broken = page.replace("data-bump-detail", "data-renamed")
    missing = [h for h, p in _HOOKS if not re.search(p, broken)]
    assert missing == ["[data-bump-detail]"]


def test_a_longer_sibling_class_does_not_stand_in_for_a_renamed_hook(page: str) -> None:
    broken = re.sub(r'class="card-expand"', 'class="card-expand-renamed"', page)
    assert broken != page
    missing = [h for h, p in _HOOKS if not re.search(p, broken)]
    assert missing == [".card-expand"]
