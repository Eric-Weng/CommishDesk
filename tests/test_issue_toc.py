"""Story 8.16c: the Issue table of contents -- a "Jump to" nav on the web page and an
"In this issue" list in the email (HTML and text), both built from ``commishdesk.sections``."""

from __future__ import annotations

import re

import pytest

from commishdesk.facts.schema import WeeklyFacts
from commishdesk.narrate.weekly_template import WeeklyIssue, WeeklySection, render_weekly_issue
from commishdesk.render import render_weekly_email
from commishdesk.render import weekly_email as we
from commishdesk.sections import COLD_START_SECTION_IDS, SECTION_IDS, heading_for
from tests.test_issue_redesign import scaled_raw
from tests.test_render_weekly_web import _STAMP, _body, _page, _raw, _static, _week01_raw

#: The share pill's panel names (8.16, ``panel-luck`` / ``panel-power`` in the Worker dashboard).
_SHARE_PANELS = ("luck", "power")


def _nav(page: str) -> str:
    return re.search(r"<nav\b.*?</nav>", _body(page), flags=re.DOTALL).group(0)  # type: ignore[union-attr]


def _hrefs(page: str) -> list[str]:
    return re.findall(r'<a href="#([^"]+)"', _nav(page))


def _section_ids(page: str) -> list[str]:
    return re.findall(r'<section id="([^"]+)"', _body(page))


def _email(raw: dict, issue: WeeklyIssue | None = None):  # type: ignore[no-untyped-def]
    facts = WeeklyFacts.model_validate(raw)
    return render_weekly_email(facts, issue or render_weekly_issue(facts.narration), generated_at=_STAMP)


def test_share_panel_names_are_section_ids() -> None:
    assert set(_SHARE_PANELS) <= set(SECTION_IDS)


def test_full_issue_nav_lists_all_seven_sections_in_page_order() -> None:
    page = _static(_raw())
    assert _hrefs(page) == list(SECTION_IDS)
    ids = _section_ids(page)
    assert ids == list(SECTION_IDS)  # every link has a target, in the same order
    labels = re.findall(r'<a href="#[^"]+">([^<]+)</a>', _nav(page))
    assert labels == [heading_for(i) for i in SECTION_IDS]


def test_week_one_nav_lists_the_four_cold_start_sections_only() -> None:
    page = _static(_week01_raw())
    assert _hrefs(page) == list(COLD_START_SECTION_IDS)
    assert _section_ids(page) == list(COLD_START_SECTION_IDS)
    assert ">Standings<" in _nav(page)  # cold-start heading, not the warm one


def test_nav_is_labeled_real_links_under_the_masthead_with_no_script() -> None:
    page = _static(_raw())
    body = _body(page)
    assert '<nav class="toc" aria-label="Jump to"' in body
    assert body.index('class="masthead"') < body.index('aria-label="Jump to"') < body.index('id="lead"')
    assert "<script" not in page
    css = page.split("<style>", 1)[1].split("</style>", 1)[0]
    assert re.search(r"\.weekly_issue \[id\] \{ scroll-margin-top: \d+px; \}", css)
    assert "position: sticky" not in css.split(".toc {", 1)[1].split("}", 1)[0]


def test_a_section_that_does_not_render_is_not_listed() -> None:
    raw = _raw()
    raw["matchups"]["next_week"] = []
    page = _static(raw)
    assert "next_week" not in _section_ids(page)
    assert _hrefs(page) == _section_ids(page)
    assert "next_week" not in _hrefs(page)


def test_notice_sections_are_not_listed() -> None:
    raw = _raw()
    facts = WeeklyFacts.model_validate(raw)
    base = render_weekly_issue(facts.narration)
    issue = WeeklyIssue(
        title=base.title,
        dateline="UNVERIFIED -- check before sending",
        sections=[*base.sections, WeeklySection(heading="Correction", blocks=["Fixed a score."])],
    )
    page = _page(raw, issue)
    assert "Unverified" in _body(page) and "Correction" in _body(page)
    assert _hrefs(page) == list(SECTION_IDS)
    assert ">Correction<" not in _nav(page) and ">Unverified<" not in _nav(page)
    email = _email(raw, issue)
    assert re.findall(r'href="\{\{commishdesk:issue-url\}\}#([^"]+)"', email.html) == list(SECTION_IDS)


def test_email_html_and_text_pin_the_link_targets() -> None:
    parts = _email(_raw())
    marker = we.ISSUE_URL_MARKER
    assert marker == "{{commishdesk:issue-url}}"
    targets = [f"{marker}#{i}" for i in SECTION_IDS]
    assert re.findall(r'<a href="([^"]+)"', parts.html) == targets
    assert "In this issue" in parts.html
    assert parts.html.index("Trench Warfare") < parts.html.index("In this issue") < parts.html.index(">The Lead<")
    lines = [line for line in parts.text.split("\n") if marker in line]
    assert lines == [f"{heading_for(i)}: {marker}#{i}" for i in SECTION_IDS]
    assert "IN THIS ISSUE" in parts.text
    # no in-email anchors, no script
    assert 'href="#' not in parts.html and "<script" not in parts.html


def test_email_week_one_lists_four_entries() -> None:
    parts = _email(_week01_raw())
    ids = re.findall(r'href="\{\{commishdesk:issue-url\}\}#([^"]+)"', parts.html)
    assert ids == list(COLD_START_SECTION_IDS)
    assert parts.text.count(we.ISSUE_URL_MARKER) == 4


def test_email_list_sits_under_the_masthead_and_survives_dark_mode_inversion() -> None:
    html = _email(_raw()).html
    assert html.index(we.TOP_LINKS_MARKER) < html.index("In this issue") < html.index(">Next week</td>")
    toc = html.split("In this issue", 1)[1].split("</table>", 1)[0]
    # inline styles only (no class needed for the link colour), so the dark swap and an
    # inverting client both have a concrete colour to act on
    assert f"color:{we.EMPH_TEXT}" in toc
    assert "[style*=\";color:" + we.EMPH_TEXT + "\"]" in html  # the dark-mode rule that recolours it


def test_the_email_keeps_the_marker_in_the_stored_render() -> None:
    assert we.ISSUE_URL_MARKER in _email(_raw()).html
    assert we.ISSUE_URL_MARKER in we.__all__ or "ISSUE_URL_MARKER" in we.__all__


def test_toc_output_is_deterministic() -> None:
    assert _static(_raw()) == _static(_raw())
    assert _email(_raw()).html == _email(_raw()).html
    assert _email(_raw()).text == _email(_raw()).text


@pytest.mark.parametrize("teams", [14, 32])
def test_big_league_nav_cannot_overflow_the_phone(teams: int) -> None:
    page = _static(scaled_raw(_raw(published=True), teams))
    assert _hrefs(page) == list(SECTION_IDS)
    css = page.split("<style>", 1)[1].split("</style>", 1)[0]
    toc = css.split(".toc {", 1)[1]
    assert "flex-wrap: wrap;" in toc.split("}", 1)[0]
    assert re.search(r"\.toc a \{[^}]*max-width: 100%;[^}]*overflow-wrap: anywhere;", css)
    assert re.search(r"\.toc ul \{[^}]*flex-wrap: wrap;[^}]*min-width: 0;", css)
