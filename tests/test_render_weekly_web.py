"""Story 5.14a — ``render_weekly_web``, the designed weekly page.

Every row of the spec's I/O matrix (week 10, the published-rank overlay, a
nudge with no reason, stood-down sections, a hostile team name), plus the
page's self-containment, the embedded fonts, the ``<title>`` on every chart,
WCAG AA contrast for the Tuesday Morning text roles in both themes, the shared
next-week stakes line, and luck bars that plot ``season.luck`` as-is.

Story 5B.4 adds the luck index's focus / tap preview, its grow-from-zero bar
animation, and the standings actual-vs-all-play toggle (including its stand-down
when no team carries an all-play record).

Inputs are the committed weekly Facts fixtures; the narrated Issue is the
template narrator's.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import subprocess
import zipfile
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from commishdesk.facts.schema import WeeklyFacts
from commishdesk.narrate.weekly_template import SECTION_HEADINGS, WeeklyIssue, WeeklySection, render_weekly_issue
from commishdesk.render import _weekly_model as wm
from commishdesk.render import render_weekly_web
from commishdesk.render import style as style_mod
from tests.conftest import REPO_ROOT

FACTS_DIR = REPO_ROOT / "tests" / "fixtures" / "facts"
_STAMP = "2026-09-07T00:00:00Z"

#: The section markers, in the required page order.
_SECTION_ORDER = (
    'class="masthead"',
    'aria-label="The lead"',
    'class="awards"',
    'aria-label="Around the league"',
    'aria-label="Standings"',
    'aria-label="Power rankings"',
    'aria-label="The luck index"',
    'aria-label="Next week"',
    'aria-label="The transaction desk"',
)


def _raw(published: bool = False) -> dict[str, Any]:
    name = "expected-weekly-facts-week10-published.json" if published else "expected-weekly-facts-week10.json"
    return json.loads((FACTS_DIR / name).read_text(encoding="utf-8"))


def _week01_raw() -> dict[str, Any]:
    return json.loads(
        (FACTS_DIR / "expected-weekly-facts-week01.json").read_text(encoding="utf-8")
    )


def _page(raw: dict[str, Any], issue: WeeklyIssue | None = None) -> str:
    facts = WeeklyFacts.model_validate(raw)
    return render_weekly_web(
        facts, issue or render_weekly_issue(facts.narration), output_id="x", generated_at=_STAMP
    )


def _body(page: str) -> str:
    """The page after its stylesheet (the embedded font data is not markup)."""
    return page.split("</style>", 1)[1]


def _power_rows(page: str) -> list[tuple[str, str, str]]:
    """``(rank, team, following nudge html or "")`` per power row, in page order."""
    power = _body(page).split('aria-label="Power rankings"', 1)[1].split("</section>", 1)[0]
    items = re.findall(r'<div class="pw-item">(.*?)</div>(<p class="nudge">.*?</p>)?</div>', power)
    out = []
    for row, nudge in items:
        rank = re.search(r'<span class="rk">(.*?)</span>', row).group(1)  # type: ignore[union-attr]
        team = re.search(r'<span class="tn">(.*?)</span>', row).group(1)  # type: ignore[union-attr]
        out.append((rank, team, nudge))
    return out


def _power_section(page: str) -> str:
    """The whole ``aria-label="Power rankings"`` section, bump chart and all."""
    return _body(page).split('aria-label="Power rankings"', 1)[1].split("</section>", 1)[0]


def _bump_team_blocks(power_html: str) -> dict[str, str]:
    """``{roster_id: inner html}`` for every ``<g class="bump-team">`` group."""
    return dict(
        re.findall(r'<g class="bump-team" data-team="([^"]+)">(.*?)</g>', power_html, flags=re.DOTALL)
    )


# --------------------------------------------------------------------------- #
# I/O matrix
# --------------------------------------------------------------------------- #


def test_week10_renders_every_section_in_order_with_no_nudge() -> None:
    page = _page(_raw())
    body = _body(page)
    positions = [body.index(marker) for marker in _SECTION_ORDER]
    assert positions == sorted(positions)
    assert 'class="paper weekly_issue"' in body
    assert "Nudged" not in body
    ranks = [rank for rank, _team, _nudge in _power_rows(page)]
    assert ranks == [str(n) for n in range(1, 13)]


def test_published_overlay_swaps_7_and_8_with_both_chips_and_reasons() -> None:
    raw = _raw(published=True)
    rows = _power_rows(_page(raw))
    by_rank = {rank: (team, nudge) for rank, team, nudge in rows}
    assert by_rank["7"][0] == "Nickel Newts"
    assert by_rank["8"][0] == "Chip-Block Chinchillas"
    assert "Nudged up · model #8" in by_rank["7"][1]
    assert "Nudged down · model #7" in by_rank["8"][1]
    reasons = {row["team"]: row["nudge_justification"] for row in raw["narration"]["power"]}
    assert "the record settles the tie the model left open" in by_rank["7"][1]
    assert _esc_text(reasons["Nickel Newts"]) in by_rank["7"][1]
    assert _esc_text(reasons["Chip-Block Chinchillas"]) in by_rank["8"][1]
    assert sum(1 for _r, _t, nudge in rows if nudge) == 2


def _esc_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("'", "&#x27;").replace('"', "&quot;")


def test_a_nudge_with_no_reason_shows_the_chip_alone() -> None:
    raw = _raw(published=True)
    for row in raw["narration"]["power"]:
        row["nudge_justification"] = None
    by_rank = {rank: nudge for rank, _team, nudge in _power_rows(_page(raw))}
    for rank in ("7", "8"):
        assert "Nudged" in by_rank[rank]
        assert 'class="why"' not in by_rank[rank]


def test_rank_falls_back_to_the_facts_published_rank_when_narration_has_none() -> None:
    raw = _raw(published=True)
    for row in raw["narration"]["power"]:
        row["published_rank"] = None
        row["nudge_justification"] = None
    by_rank = {rank: (team, nudge) for rank, team, nudge in _power_rows(_page(raw))}
    assert by_rank["7"][0] == "Nickel Newts" and "Nudged up" in by_rank["7"][1]
    assert 'class="why"' not in by_rank["7"][1]


def test_stood_down_sections_render_no_heading_and_no_frame() -> None:
    raw = _raw()
    for team in raw["teams"]:
        team["season"]["luck"] = None
    raw["transactions"]["this_week"] = []
    raw["transactions"]["recent_trades"] = []
    raw["matchups"]["next_week"] = []
    raw["standings"]["playoff_picture"] = None
    body = _body(_page(raw))
    for gone in ("The luck index", "Who the schedule favoured", "The transaction desk", "What moved on the wire",
                 "Next week", "stakes-shared", "Playoff line", "chip-emph\">Bye"):
        assert gone not in body, gone
    # the rest still renders
    for kept in ('aria-label="Standings"', 'aria-label="Power rankings"', 'aria-label="Around the league"'):
        assert kept in body


def test_week01_cold_start_omits_power_luck_transactions_and_playoff_picture() -> None:
    """Story 5.16: at Week 1 the page renders only the four cold-start sections
    (lead, results, standings by points, next week) -- no power rankings, no
    luck index, no transaction desk, and no playoff picture."""
    body = _body(_page(_week01_raw()))

    for gone in (
        'aria-label="Power rankings"',
        'aria-label="The luck index"',
        'aria-label="The transaction desk"',
        "Standings and the Playoff Picture",
        "Playoff line",
        "Seeding unconfirmed",
    ):
        assert gone not in body, gone

    assert "Standings by points" in body
    assert "Around the league" in body
    assert "Next week" in body


def test_a_lead_hero_that_cannot_draw_falls_back_to_type_only() -> None:
    raw = _raw()
    lead = raw["lead_candidates"][0]
    assert lead["kind"] == "lineup_loss"
    team = next(t for t in raw["teams"] if t["roster_id"] == lead["roster_ids"][0])
    team["this_week"]["points_left_on_bench"] = None
    body = _body(_page(raw))
    lead_html = body.split('aria-label="The lead"', 1)[1].split('class="awards"', 1)[0]
    assert "<svg" not in lead_html
    assert 'class="lead-type"' in lead_html
    assert _esc_text(lead["hook"]) in lead_html


@pytest.mark.parametrize("kind", ["lineup_loss", "biggest_blowout", "closest_game", "week_high_score"])
def test_every_lead_kind_draws_its_hero(kind: str) -> None:
    raw = _raw()
    raw["lead_candidates"] = [dict(c, rank=1) for c in raw["lead_candidates"] if c["kind"] == kind]
    lead_html = _body(_page(raw)).split('aria-label="The lead"', 1)[1].split('class="awards"', 1)[0]
    assert "<svg" in lead_html and 'class="lead-type"' not in lead_html


def test_a_hostile_team_name_appears_only_escaped() -> None:
    hostile = '<script>x</script>"><svg onload=1>'
    raw = _raw(published=True)
    for team in raw["teams"]:
        if team["roster_id"] in ("7", "5"):  # the lead team and a nudged team
            team["team_name"] = hostile
    for row in raw["narration"]["power"]:
        if row["roster_id"] == "5":
            row["nudge_justification"] = hostile
    page = _page(raw)
    assert hostile not in page
    assert "<script>x</script>" not in page
    assert "onload=1>" not in page
    escaped = "&lt;script&gt;x&lt;/script&gt;&quot;&gt;&lt;svg onload=1&gt;"
    body = _body(page)
    assert escaped in body
    # in SVG text / attribute values too (the lead hero's aria-label and <title>)
    assert f'aria-label="{escaped} started' in body
    assert f"<title>{escaped} started" in body


def test_correction_and_unverified_stamps_reach_the_page() -> None:
    raw = _raw()
    facts = WeeklyFacts.model_validate(raw)
    issue = render_weekly_issue(facts.narration)
    issue = issue.model_copy(
        update={
            "dateline": f"UNVERIFIED — {issue.dateline}",
            "sections": [WeeklySection(heading="Correction", blocks=["Correction — fixed a score"]), *issue.sections],
        }
    )
    body = _body(_page(raw, issue))
    assert "UNVERIFIED — " in body
    assert "Correction — fixed a score" in body
    assert body.index("Correction") < body.index('aria-label="The lead"')


def test_narrated_prose_comes_from_the_issue_sections() -> None:
    raw = _raw()
    facts = WeeklyFacts.model_validate(raw)
    issue = render_weekly_issue(facts.narration)
    marker = {heading: f"Narrated line for {heading}." for heading in SECTION_HEADINGS}
    issue = issue.model_copy(
        update={"sections": [WeeklySection(heading=h, blocks=[marker[h]]) for h in SECTION_HEADINGS]}
    )
    body = _body(_page(raw, issue))
    for heading, line in marker.items():
        assert line in body, heading


# --------------------------------------------------------------------------- #
# Self-containment, fonts, charts
# --------------------------------------------------------------------------- #


def test_page_is_self_contained() -> None:
    page = _page(_raw(published=True))
    assert page.startswith("<!doctype html>") and page.endswith("\n") and "\r" not in page
    assert page.count("<style>") == 1
    assert page.count("<script") == 1
    assert page.count("<script>") == 1
    lowered = page.lower()
    for banned in ("<link", "@import", "src=", "http"):
        assert banned not in lowered, banned
    assert "script src=" not in lowered
    urls = re.findall(r"url\(([^)]*)\)", page)
    assert urls and all(url.startswith("data:font/woff2;base64,") for url in urls)
    assert len(urls) == len(style_mod.WEEKLY_FONT_FILES)


def test_fonts_resolve_from_the_installed_package() -> None:
    fonts = resources.files("commishdesk.render").joinpath("fonts")
    for _family, _weight, filename in style_mod.WEEKLY_FONT_FILES:
        data = fonts.joinpath(filename).read_bytes()
        assert data[:4] == b"wOF2", filename
    for licence in ("OFL-BricolageGrotesque.txt", "OFL-DMSans.txt", "OFL-IBMPlexMono.txt"):
        assert "SIL Open Font License" in fonts.joinpath(licence).read_text(encoding="utf-8")


def test_fonts_and_licences_ship_in_the_wheel(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not available")
    result = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(tmp_path)], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    (wheel,) = tmp_path.glob("*.whl")
    names = set(zipfile.ZipFile(wheel).namelist())
    for _family, _weight, filename in style_mod.WEEKLY_FONT_FILES:
        assert f"commishdesk/render/fonts/{filename}" in names
    assert {f"commishdesk/render/fonts/OFL-{n}.txt" for n in ("BricolageGrotesque", "DMSans", "IBMPlexMono")} <= names


def test_every_svg_has_a_title() -> None:
    svgs = re.findall(r"<svg\b.*?</svg>", _body(_page(_raw())), flags=re.DOTALL)
    assert len(svgs) >= 2  # the lead hero and the luck index
    for svg in svgs:
        assert re.match(r'<svg\b[^>]*role="img"[^>]*><title>[^<]+</title>', svg), svg[:120]
        assert ' data-draw' in svg.split('>', 1)[0]


def test_render_is_deterministic() -> None:
    assert _page(_raw(published=True)) == _page(_raw(published=True))


#: SHA-256 of the Story 5.14a page (commit 779de47) for the two committed
#: fixtures — Story 5B.2 added the interaction layer's inline ``<script>`` and
#: the reveal/draw-in CSS, which moved the hash. Story 5B.3 adds the
#: power-rank bump chart (SVG, per-mark draw-in, hit targets, hover/pin CSS
#: and JS), which moved it again. Story 5B.4 adds the luck focus / tap preview
#: and grow-in bars plus the all-play standings toggle, which moved it again.
#: Change these only with a deliberate design change to the page, and regenerate
#: them by running this test once and copying the printed digests.
_GOLDEN_SHA256 = {
    False: "86c84e576db8cc37735d238a19bd03683f91185c8475b4f9c2fa7fd5e68160ee",
    True: "bf4bf5440a7ff927646d36ed6f33b3996d461d7ff7a92050e280c9fc6386b47f",
}


@pytest.mark.parametrize("published", [False, True])
def test_page_is_byte_identical_to_the_5_14a_golden(published: bool) -> None:
    page = _page(_raw(published=published))
    actual = hashlib.sha256(page.encode("utf-8")).hexdigest()
    expected = _GOLDEN_SHA256[published]
    assert actual == expected, (
        f"rendered page hash changed (published={published}).\n"
        f"  actual   = {actual}\n"
        f"  expected = {expected}\n"
        f"If this is a deliberate design change, set _GOLDEN_SHA256[{published}] "
        f"to the actual value above."
    )


def test_interaction_script_is_csp_safe() -> None:
    page = _page(_raw())
    assert page.count("<script") == 1
    script = page.split("<script>", 1)[1].split("</script>", 1)[0]
    assert "eval(" not in script
    assert "innerHTML" not in script.lower()
    assert "document.write" not in script.lower()
    assert not re.search(r"\son[a-z]+\s*=", script, flags=re.IGNORECASE)


def test_reduced_motion_query_zeroes_interaction_durations() -> None:
    page = _page(_raw())
    css = page.split("<style>", 1)[1].split("</style>", 1)[0]
    assert "@media (prefers-reduced-motion: reduce)" in css
    block = css.split("@media (prefers-reduced-motion: reduce)", 1)[1].split("}", 1)[0]
    assert "animation-duration: 0s" in block
    assert "animation-delay: 0s" in block
    assert "transition-duration: 0s" in block


def test_js_off_is_finished_and_visible_by_default() -> None:
    page = _page(_raw())
    css = page.split("<style>", 1)[1].split("</style>", 1)[0]
    body_without_script = _body(page).split("<script>", 1)[0]
    assert "data-reveal" in body_without_script
    assert 'style="opacity: 0' not in body_without_script
    assert "is-revealed" not in body_without_script
    assert not re.search(r'\[data-reveal\][^{}]*\{[^}]*opacity\s*:\s*0', css)
    assert not re.search(r'\[data-reveal\][^{}]*\{[^}]*visibility\s*:\s*hidden', css)


def test_normal_scroll_js_and_motion_on_reveals_once_and_draws_in() -> None:
    """I/O matrix row: "Normal scroll, JS+motion on" — each section reveals on
    first entry only (never re-fires scrolling back up), and each chart's
    marks draw in with the approved stagger/easing."""
    page = _page(_raw())
    script = page.split("<script>", 1)[1].split("</script>", 1)[0]
    css = page.split("<style>", 1)[1].split("</style>", 1)[0]

    # Fires once, never re-triggers scrolling back up: an IntersectionObserver
    # that unobserves the element as soon as it has been revealed.
    assert "IntersectionObserver" in script
    observer_body = script.split("new IntersectionObserver(", 1)[1]
    assert "unobserve(" in observer_body

    # Each chart's own marks draw in within the approved 350-450ms window,
    # with the approved easing curve.
    match = re.search(r"animation:\s*draw-in\s+(\d+)ms\s+cubic-bezier\(([^)]+)\)", css)
    assert match, "expected a draw-in animation rule in the interaction CSS"
    duration_ms = int(match.group(1))
    assert 350 <= duration_ms <= 450
    assert match.group(2).replace(" ", "") == ".16,1,.3,1"

    # The stagger: a per-element index drives a ~25ms-multiplied delay via a
    # CSS custom property, set from the child's index in the script.
    assert re.search(r"animation-delay:\s*calc\(var\(--draw-i,\s*0\)\s*\*\s*25ms\)", css)
    assert 'setProperty("--draw-i"' in script


def test_keyboard_only_nav_shows_a_visible_focus_outline() -> None:
    """I/O matrix row: "Keyboard-only nav" — tabbing through the page must show
    a visible focus outline on every focusable element, via a real (non-zero,
    non-``none``) ``:focus-visible`` outline."""
    page = _page(_raw())
    css = page.split("<style>", 1)[1].split("</style>", 1)[0]
    match = re.search(r":focus-visible\s*\{([^}]*)\}", css)
    assert match, "expected a :focus-visible rule in the stylesheet"
    block = match.group(1)
    outline_match = re.search(r"outline:\s*([^;]+);", block)
    assert outline_match, block
    outline_value = outline_match.group(1).strip().lower()
    assert outline_value not in ("none", "0")


# --------------------------------------------------------------------------- #
# Power-ranking bump chart (Story 5B.3) -- I/O matrix
# --------------------------------------------------------------------------- #


def test_bump_chart_draws_model_line_and_diamonds_only_where_nudged() -> None:
    """Matrix row: enough history (>=2 usable weeks) draws the model line for
    every team, with a published-rank diamond only for the nudged teams."""
    raw = _raw(published=True)
    power = _power_section(_page(raw))
    assert "data-bump-chart" in power
    teams = _bump_team_blocks(power)
    assert len(teams) == 12
    nudged = {"4", "5"}  # Chip-Block Chinchillas, Nickel Newts (week 10 overrule)
    for roster_id, block in teams.items():
        assert 'class="bump-hit"' in block
        assert 'class="bump-model" style="--len:' in block
        has_diamond = 'class="bump-point bump-pub"' in block
        assert has_diamond == (roster_id in nudged), roster_id


def test_bump_chart_stands_down_under_two_usable_weeks() -> None:
    """Matrix row: fewer than 2 usable weeks -- no chart, today's static
    ranked list renders exactly as now."""
    raw = _raw(published=True)
    for team in raw["teams"]:
        team["history"]["weekly"] = [row for row in team["history"]["weekly"] if row["week"] == 10]
    page = _page(raw)
    power = _power_section(page)
    assert "data-bump-chart" not in power
    assert "bump-team" not in power
    assert 'class="pw-item"' in power
    ranks = [rank for rank, _team, _nudge in _power_rows(page)]
    assert ranks == [str(n) for n in range(1, 13)]


def test_a_held_week_breaks_the_published_line_never_interpolated() -> None:
    """Matrix row: a held/skipped published week -- that team's published-rank
    line has a visible break at the gap (two separate polylines), never one
    polyline interpolating across it; the model line is unaffected."""
    raw = _raw(published=True)
    team4 = next(t for t in raw["teams"] if t["roster_id"] == "4")
    by_week = {row["week"]: row for row in team4["history"]["weekly"]}
    by_week[6]["published_rank"] = 9
    by_week[7]["published_rank"] = 9
    by_week[8]["published_rank"] = None  # held/skipped week -- the gap
    by_week[9]["published_rank"] = 9
    # week 10 stays the render-time-resolved current nudge (published 8)
    block = _bump_team_blocks(_power_section(_page(raw)))["4"]
    pub_segments = re.findall(r'<polyline class="bump-pub-line"[^>]*points="([^"]*)"', block)
    assert len(pub_segments) == 2, pub_segments
    for points in pub_segments:
        assert len(points.split()) == 2  # each broken run is exactly 2 points
    model_pts = re.search(r'<polyline class="bump-model"[^>]*points="([^"]*)"', block)
    assert model_pts and len(model_pts.group(1).split()) == 5  # model line unbroken


def test_bump_chart_shows_current_week_diamond_and_justification_callout() -> None:
    """Matrix row: the current week's nudge shows a diamond for that team +
    week, and the justification callout (existing ``PowerRow.reason``)
    beside the chart."""
    raw = _raw(published=True)
    power = _power_section(_page(raw))
    blocks = _bump_team_blocks(power)
    assert 'class="bump-point bump-pub"' in blocks["4"]
    assert 'data-week="10"' in blocks["4"]
    assert 'class="bump-point bump-pub"' in blocks["5"]
    assert 'data-week="10"' in blocks["5"]
    assert "data-bump-callout" in power
    assert _esc_text("Chip-Block Chinchillas slide behind Nickel Newts on record, 4-6 against 6-4.") in power
    assert (
        _esc_text(
            "Nickel Newts sit 6-4 to Chip-Block Chinchillas' 4-6, "
            "and the record settles the tie the model left open."
        )
        in power
    )


def test_bump_chart_hostile_team_name_is_escaped_everywhere() -> None:
    """Matrix row: a hostile team name is escaped via ``_esc``/``textContent``
    only, everywhere it appears in the new chart markup -- never ``innerHTML``."""
    hostile = '<script>x</script>"><svg onload=1>'
    raw = _raw(published=True)
    for team in raw["teams"]:
        if team["roster_id"] == "4":
            team["team_name"] = hostile
    page = _page(raw)
    assert hostile not in page
    assert "<script>x</script>" not in page
    power = _power_section(page)
    assert "data-bump-chart" in power
    escaped = "&lt;script&gt;x&lt;/script&gt;&quot;&gt;&lt;svg onload=1&gt;"
    assert escaped in power
    assert f'data-name="{escaped}"' in power  # legend chip, read via getAttribute + textContent only
    assert f"<title>{escaped} week 10" in power  # dot/diamond tooltip
    script = page.split("<script>", 1)[1].split("</script>", 1)[0]
    assert "innerHTML" not in script.lower()


def test_bump_chart_keyboard_pin_is_button_driven_with_a_distinct_pinned_label() -> None:
    """Matrix row: keyboard-only nav -- Tab to a legend chip (a native
    <button>, so Enter/Space fires a click for free) pins that team with a
    "Pinned open" label and its own ``.is-pinned`` state, distinct from the
    hover-only ``.is-active`` state (interaction-decisions.md: the two must
    never share their only signal)."""
    page = _page(_raw(published=True))
    assert '<button type="button" class="hit pw-hit"' in page
    script = page.split("<script>", 1)[1].split("</script>", 1)[0]
    assert 'addEventListener("click"' in script
    assert '"Pinned open"' in script
    assert 'classList.toggle("is-pinned"' in script
    assert 'classList.toggle("is-active"' in script
    css = page.split("<style>", 1)[1].split("</style>", 1)[0]
    assert ".hit.is-pinned" in css
    assert ".hit.is-active" in css
    assert not re.search(r":hover\s*\.is-pinned|\.is-pinned[^{}]*:hover", css)


def test_bump_chart_hit_targets_are_wide_and_layered_under_each_mark() -> None:
    """interaction-decisions.md: real hover-tracking on thin marks needs a
    generous invisible hit target layered under the visible stroke."""
    page = _page(_raw(published=True))
    body = _body(page)
    css = page.split("<style>", 1)[1].split("</style>", 1)[0]
    stroke_width = re.search(r"\.bump-team polyline\.bump-hit\s*\{[^}]*stroke-width:\s*(\d+)", css)
    assert stroke_width and int(stroke_width.group(1)) >= 10
    assert re.search(r"\.bump-team circle\.bump-hit\s*\{[^}]*fill:\s*transparent", css)
    assert re.search(r'<polyline class="bump-hit"[^>]*/><polyline class="bump-model"', body)
    assert re.search(r'<circle class="bump-hit"[^>]*/><circle class="bump-point bump-model-dot"', body)


def test_bump_chart_lines_draw_in_via_their_own_traced_path_length() -> None:
    """epic-5B-context.md: draw-in dash length is computed from each line's
    own traced path, not a shared constant; 350-450ms, ~25ms/team stagger,
    the approved easing -- matching 5B.2's other charts' timing."""
    page = _page(_raw(published=True))
    body = _body(page)
    css = page.split("<style>", 1)[1].split("</style>", 1)[0]

    lens = {m for m in re.findall(r'class="bump-model" style="--len:([\d.]+)"', body)}
    assert len(lens) > 1  # every team's line has its own length, not one shared value

    match = re.search(r"animation:\s*bump-draw-line\s+(\d+)ms\s+cubic-bezier\(([^)]+)\)", css)
    assert match, "expected a bump-draw-line animation rule"
    assert 350 <= int(match.group(1)) <= 450
    assert match.group(2).replace(" ", "") == ".16,1,.3,1"
    assert re.search(r"animation-delay:\s*calc\(var\(--draw-i,\s*0\)\s*\*\s*25ms\)", css)
    assert re.search(r"@keyframes bump-draw-line\s*\{[^}]*stroke-dashoffset:\s*var\(--len", css)


def test_bump_chart_marks_are_fully_drawn_by_default_not_stuck_mid_dash() -> None:
    """JS-off / not-yet-revealed degrades to fully finished marks, never a
    stuck dashed-in state -- ``stroke-dashoffset`` only ever appears inside
    the ``@keyframes`` it animates, never as a static/base property."""
    page = _page(_raw(published=True))
    css = page.split("<style>", 1)[1].split("</style>", 1)[0]
    assert css.count("stroke-dashoffset") == 2  # the keyframe's from/to only
    body_without_script = _body(page).split("<script>", 1)[0]
    assert "stroke-dashoffset" not in body_without_script


# --------------------------------------------------------------------------- #
# Theme: WCAG AA text contrast in light and dark
# --------------------------------------------------------------------------- #


def _luminance(hex_colour: str) -> float:
    value = hex_colour.lstrip("#")
    channels = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_the_contrast_function_matches_known_values() -> None:
    assert _contrast("#000000", "#FFFFFF") == pytest.approx(21.0)
    assert _contrast("#1E9E6A", "#FFFFFF") == pytest.approx(3.41, abs=0.01)  # why light good needed a text variant
    assert _contrast("#E5484D", "#FFFFFF") == pytest.approx(3.91, abs=0.01)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("fg", ["--emph", "--good-text", "--bad-text", "--ink"])
@pytest.mark.parametrize("bg", ["--card", "--paper"])
def test_text_roles_meet_wcag_aa(theme: str, fg: str, bg: str) -> None:
    tokens = style_mod.WEEKLY_LIGHT_TOKENS if theme == "light" else style_mod.WEEKLY_DARK_TOKENS
    assert _contrast(tokens[fg], tokens[bg]) >= 4.5


def _over(wash: str, base: str) -> str:
    """An ``#rrggbbaa`` wash composited over an opaque ``#rrggbb`` base."""
    alpha = int(wash[7:9], 16) / 255
    mixed = (
        round(alpha * int(wash[i : i + 2], 16) + (1 - alpha) * int(base[i : i + 2], 16)) for i in (1, 3, 5)
    )
    return "#" + "".join(f"{channel:02X}" for channel in mixed)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize(
    ("fg", "wash"), [("--good-text", "--good-wash"), ("--bad-text", "--bad-wash"), ("--emph", "--emph-wash")]
)
def test_status_text_on_its_wash_meets_wcag_aa(theme: str, fg: str, wash: str) -> None:
    tokens = style_mod.WEEKLY_LIGHT_TOKENS if theme == "light" else style_mod.WEEKLY_DARK_TOKENS
    assert _contrast(tokens[fg], _over(tokens[wash], tokens["--card"])) >= 4.5


def test_light_text_variants_are_the_recorded_values() -> None:
    assert style_mod.WEEKLY_LIGHT_TOKENS["--good-text"] == "#157F55"
    assert style_mod.WEEKLY_LIGHT_TOKENS["--bad-text"] == "#C93338"


def test_the_stylesheet_ships_light_and_a_dark_variant() -> None:
    css = style_mod.build_weekly_style()
    assert "@media (prefers-color-scheme: dark)" in css
    assert f"--paper: {style_mod.WEEKLY_DARK_TOKENS['--paper']};" in css
    assert f"--paper: {style_mod.WEEKLY_LIGHT_TOKENS['--paper']};" in css


# --------------------------------------------------------------------------- #
# Next week and luck
# --------------------------------------------------------------------------- #


def test_shared_stakes_render_once_and_cards_carry_no_empty_chip_row() -> None:
    raw = _raw()
    assert all(
        card["stakes"] == ["bye_seed", "wildcard_race", "division_race"] for card in raw["matchups"]["next_week"]
    )
    body = _body(_page(raw))
    assert body.count('class="stakes-shared"') == 1
    next_week = body.split('aria-label="Next week"', 1)[1].split("</section>", 1)[0]
    cards = re.findall(r'<article class="card nw[^"]*">(.*?)</article>', next_week)
    assert len(cards) == len(raw["matchups"]["next_week"])
    for card, data in zip(cards, raw["matchups"]["next_week"], strict=True):
        if data["game_of_week"]:
            assert "Game of the week" in card and "Bye seed" not in card
        else:
            assert 'class="nw-chips"' not in card


def test_a_differing_stake_shows_as_a_chip_on_its_card_only() -> None:
    raw = _raw()
    raw["matchups"]["next_week"][0]["stakes"] = ["bye_seed", "wildcard_race", "division_race", "elimination"]
    body = _body(_page(raw))
    next_week = body.split('aria-label="Next week"', 1)[1].split("</section>", 1)[0]
    cards = re.findall(r'<article class="card nw[^"]*">(.*?)</article>', next_week)
    assert "Elimination" in cards[0]
    assert all("Elimination" not in card for card in cards[1:])
    assert body.count('class="stakes-shared"') == 1


def test_a_single_next_week_card_keeps_its_stakes_as_chips() -> None:
    raw = _raw()
    raw["matchups"]["next_week"] = [card for card in raw["matchups"]["next_week"] if not card["game_of_week"]][:1]
    body = _body(_page(raw))
    assert 'class="stakes-shared"' not in body
    next_week = body.split('aria-label="Next week"', 1)[1].split("</section>", 1)[0]
    assert "Bye seed" in next_week and 'class="nw-chips"' in next_week


# --------------------------------------------------------------------------- #
# Standings, results and the transaction desk
# --------------------------------------------------------------------------- #


def _team_names(raw: dict[str, Any]) -> dict[str, str]:
    from commishdesk.render.weekly_web import _team_label

    return {team.roster_id: _team_label(team) for team in WeeklyFacts.model_validate(raw).teams}


def test_standings_draw_the_playoff_line_and_bye_and_bubble_chips() -> None:
    raw = _raw()
    picture = raw["standings"]["playoff_picture"]
    names = _team_names(raw)
    body = _body(_page(raw))
    standings = body.split('aria-label="Standings"', 1)[1].split("</section>", 1)[0]
    assert standings.count("Playoff line") == 1
    before, after = standings.split("Playoff line", 1)
    assert before.count('<div class="st-row') == picture["cut_line_after_rank"]
    rows = re.findall(r'<div class="(st-row[^"]*)">(.*?)</div>', standings)
    assert len(rows) == len(raw["standings"]["overall"])
    for (cls, row), roster_id in zip(rows, raw["standings"]["overall"], strict=True):
        assert _esc_text(names[roster_id]) in row
        below = raw["standings"]["overall"].index(roster_id) >= picture["cut_line_after_rank"]
        assert ("below" in cls) == below, roster_id
        if roster_id in picture["byes"]:
            assert 'chip-emph">Bye' in row
        elif roster_id in picture["bubble"]:
            assert 'chip-notable">Bubble' in row
        else:
            assert 'class="chip' not in row
    assert after.count('<div class="st-row') == len(rows) - picture["cut_line_after_rank"]


def test_a_tied_game_shows_no_winner() -> None:
    raw = _raw()
    game = raw["matchups"]["this_week"][0]
    game.update(away_points=game["home_points"], winner_roster_id=None, margin=0.0)
    body = _body(_page(raw))
    around = body.split('aria-label="Around the league"', 1)[1].split("</section>", 1)[0]
    card = re.findall(r'<article class="card game">(.*?)</article>', around)[0]
    assert '<span class="by">Tied</span>' in card
    assert "Won by" not in card and 'class="side win"' not in card


def test_every_trade_from_the_latest_trade_week_gets_a_card() -> None:
    raw = _raw()
    latest = raw["transactions"]["recent_trades"][0]
    second = json.loads(json.dumps(latest))
    second["transaction_id"] = "id_second"
    older = json.loads(json.dumps(latest))
    older.update(transaction_id="id_older", week=latest["week"] - 1)
    raw["transactions"]["recent_trades"] = [older, latest, second]
    body = _body(_page(raw))
    assert body.count(f"Trade · week {latest['week']}") == 2
    assert f"Trade · week {latest['week'] - 1}" not in body


def test_luck_bars_plot_season_luck_most_lucky_first() -> None:
    raw = _raw()
    body = _body(_page(raw))
    plotted = [float(value) for value in re.findall(r'class="luck-bar" data-luck="([^"]+)"', body)]
    expected = sorted((t["season"]["luck"] for t in raw["teams"]), reverse=True)
    assert plotted == expected
    # the labels carry the same values, with a real minus
    assert "−1.8" in body and "+2.5" in body


def test_luck_rows_are_focusable_and_carry_the_template_voice_preview() -> None:
    raw = _raw()
    facts = WeeklyFacts.model_validate(raw)
    body = _body(_page(raw))
    luck = body.split('aria-label="The luck index"', 1)[1].split("</section>", 1)[0]

    previews = re.findall(r'<g class="luck-row"[^>]*data-luck-preview="([^"]*)"', luck)
    assert previews
    assert 'tabindex="0"' in luck
    assert 'class="luck-hit"' in luck
    assert 'class="luck-preview" data-luck-preview hidden' in luck

    lucky, team = wm.luck_rows(facts)[0]
    expected = f"{wm.team_label(team)} — " + wm.record(
        team.season.record.w, team.season.record.l, team.season.record.t
    )
    if team.season.expected_wins is not None:
        expected += f", {team.season.expected_wins} wins earned"
    expected += f", luck {wm.signed(lucky)}."
    assert html.unescape(previews[0]) == expected


def test_luck_bar_reveal_is_a_single_grow_from_the_zero_line() -> None:
    raw = _raw()
    page = _page(raw)
    css = page.split("<style>", 1)[1].split("</style>", 1)[0]
    body = _body(page)

    assert "@keyframes luck-grow" in css
    # The whole keyframes rule: its closing brace sits at the start of a line.
    block = css.split("@keyframes luck-grow", 1)[1].split("\n}", 1)[0]
    assert "infinite" not in block
    assert "transform: scaleX(0)" in block
    assert "transform: scaleX(var(--luck-len, 1))" in block
    assert 'class="luck-bar"' in body
    assert "--luck-len:1" in body
    assert "transform-origin: var(--luck-origin, left center)" in css

    # Grow direction is per-row, not a fixed constant: a positive-luck bar
    # grows from its left (axis) edge, a negative-luck bar from its right —
    # the fixture's own known-sign rows ("−1.8" / "+2.5") pin each down.
    luck = body.split('aria-label="The luck index"', 1)[1].split("</section>", 1)[0]
    positive_row = luck.split(">+2.5<", 1)[0].rsplit('<g class="luck-row"', 1)[1]
    negative_row = luck.split(">−1.8<", 1)[0].rsplit('<g class="luck-row"', 1)[1]
    assert "--luck-origin:left center" in positive_row
    assert "--luck-origin:right center" in negative_row


def test_all_play_toggle_renders_and_carries_both_record_and_bar_values() -> None:
    raw = _raw()
    facts = WeeklyFacts.model_validate(raw)
    body = _body(_page(raw))
    standings = body.split('aria-label="Standings"', 1)[1].split("</section>", 1)[0]

    assert 'class="standings-toggle"' in standings
    assert 'data-view="actual"' in standings
    assert 'data-view="allplay"' in standings
    assert 'aria-pressed="true"' in standings

    eligible = [team for team in facts.teams if team.season.all_play is not None]
    assert eligible
    assert standings.count('class="rec rec-swap"') == len(eligible)
    assert standings.count('class="bar bar-swap"') == len(eligible)
    assert "Bar: points for" in standings
    assert "Bar: all-play win pct" in standings

    # Content, not just count: one eligible team's all-play record text and
    # bar width must be the value computed from its own facts, not a
    # swapped or mis-sourced one.
    all_play = eligible[0].season.all_play
    expected_rec = wm.record(all_play.w, all_play.l, all_play.t)
    expected_width = (all_play.pct or 0.0) * 100
    assert f'<span class="rec-ap" aria-hidden="true">{expected_rec}</span>' in standings
    assert f'class="fill-allplay" style="width:{expected_width:.1f}%"' in standings


def test_standings_toggle_hidden_by_default_css_only() -> None:
    """I/O matrix row: "JavaScript off" — no toggle control renders. The
    mechanism is CSS-only: ``.standings-toggle`` is ``display: none`` in a
    rule that is not itself gated behind the reduced-motion media query (or
    otherwise inert), and the only override that makes it visible is scoped
    under the script-added ``html.js-reveal`` ancestor class, which the page
    never gets when JS doesn't run."""
    raw = _raw()
    facts = WeeklyFacts.model_validate(raw)
    assert any(team.season.all_play is not None for team in facts.teams)  # eligible fixture

    page = _page(raw)
    body = _body(page)
    assert 'class="standings-toggle"' in body

    css = page.split("<style>", 1)[1].split("</style>", 1)[0]
    css_before_reduced_motion = css.split("@media (prefers-reduced-motion: reduce)", 1)[0]

    assert ".standings-toggle {" in css_before_reduced_motion
    toggle_block = css_before_reduced_motion.split(".standings-toggle {", 1)[1].split("}", 1)[0]
    assert "display: none" in toggle_block

    assert "html.js-reveal .standings-toggle { display: flex; }" in css_before_reduced_motion


def test_all_play_toggle_stands_down_when_no_team_has_all_play() -> None:
    raw = _raw()
    for team in raw["teams"]:
        team["season"]["all_play"] = None

    body = _body(_page(raw))
    standings = body.split('aria-label="Standings"', 1)[1].split("</section>", 1)[0]

    assert "standings-toggle" not in standings
    assert "rec-swap" not in standings
    assert "bar-swap" not in standings
    assert "Bar: all-play win pct" not in standings
    assert "Bar: points for" in standings


def test_single_division_league_omits_legend_when_toggle_absent() -> None:
    """Pre-5B.4, a single-/no-division league's standings never rendered a
    ``<p class="legend">`` at all (the whole legend, "Bar: points for"
    included, lived inside ``if dot_of:``). The all-play toggle must not
    change that for leagues where it doesn't itself apply."""
    raw = _raw()
    for team in raw["teams"]:
        team["division_id"] = 1
        team["season"]["all_play"] = None
    raw["league"]["format"]["divisions"] = [{"id": 1, "name": None}]

    standings = _body(_page(raw)).split('aria-label="Standings"', 1)[1].split("</section>", 1)[0]
    assert 'class="legend"' not in standings
    assert "Bar: points for" not in standings


def test_single_division_league_still_shows_toggle_legend_when_eligible() -> None:
    """A single-/no-division league with eligible all-play data still needs
    the cross-fading "Bar: ..." legend, since the toggle can change what the
    bar means even without a division-dot legend to sit beside."""
    raw = _raw()
    for team in raw["teams"]:
        team["division_id"] = 1
    raw["league"]["format"]["divisions"] = [{"id": 1, "name": None}]

    standings = _body(_page(raw)).split('aria-label="Standings"', 1)[1].split("</section>", 1)[0]
    assert 'class="legend"' in standings
    assert "Bar: points for" in standings
    assert "Bar: all-play win pct" in standings


def test_all_play_toggle_keeps_ineligible_rows_on_actual_only() -> None:
    raw = _raw()
    raw["teams"][0]["season"]["all_play"] = None
    facts = WeeklyFacts.model_validate(raw)
    body = _body(_page(raw))
    standings = body.split('aria-label="Standings"', 1)[1].split("</section>", 1)[0]

    eligible = sum(1 for team in facts.teams if team.season.all_play is not None)
    assert standings.count('class="rec rec-swap"') == eligible
    assert standings.count('<span class="rec">') == len(facts.teams) - eligible


def test_hostile_team_name_is_escaped_in_luck_preview() -> None:
    hostile = "<script>x</script>"
    raw = _raw()
    for team in raw["teams"]:
        if team["season"]["luck"] is not None:
            team["team_name"] = hostile
            break

    body = _body(_page(raw))
    assert hostile not in body
    assert "<script>x</script>" not in body

    escaped = "&lt;script&gt;x&lt;/script&gt;"
    assert escaped in body

    luck = body.split('aria-label="The luck index"', 1)[1].split("</section>", 1)[0]
    assert escaped in luck
    assert 'data-luck-preview="' in luck


def test_the_unconfirmed_seeding_note_follows_the_facts_flag_alone() -> None:
    raw = _raw()
    assert raw["standings"]["playoff_picture"]["seeding_unconfirmed"] is False
    base_issue = render_weekly_issue(WeeklyFacts.model_validate(raw).narration)
    off = _page(raw, base_issue)
    assert "Seeding unconfirmed" not in off and "seeding-note" not in off

    flagged = json.loads(json.dumps(raw))
    flagged["standings"]["playoff_picture"]["seeding_unconfirmed"] = True
    on = _page(flagged, base_issue)
    assert '<p class="seeding-note">Seeding unconfirmed — derived from the standings.</p>' in on
    assert on.replace('<p class="seeding-note">Seeding unconfirmed — derived from the standings.</p>', "") == off


def _cold_start_order(raw: dict[str, Any]) -> list[str]:
    """Team labels by points-for descending, the order Week-1 standings show."""
    teams = sorted(raw["teams"], key=lambda t: -t["season"]["points_for"])
    return [t["team_name"] for t in teams]


def _in_order(haystack: str, names: list[str]) -> bool:
    positions = [haystack.index(name) for name in names]
    return positions == sorted(positions)


def test_week01_standings_rows_are_ordered_by_points_for() -> None:
    raw = _week01_raw()
    record_order = [
        t["team_name"]
        for rid in raw["standings"]["overall"]
        for t in raw["teams"]
        if t["roster_id"] == rid
    ]
    expected = _cold_start_order(raw)
    assert expected != record_order  # the oracle really separates the two orders
    section = _body(_page(raw)).split('aria-label="Standings"', 1)[1].split("</section>", 1)[0]
    assert _in_order(section, expected)


def test_week01_prose_sections_match_the_cold_start_set() -> None:
    """The cold-start headings match the renderer's set: no notice box, and the
    "Standings" prose lands inside the standings card."""
    raw = _week01_raw()
    facts = WeeklyFacts.model_validate(raw)
    issue = render_weekly_issue(facts.narration)
    marker = "MARKERSTANDINGSPROSE"
    sections = [
        s.model_copy(update={"blocks": [marker]}) if s.heading == "Standings" else s
        for s in issue.sections
    ]
    body = _body(_page(raw, issue.model_copy(update={"sections": sections})))
    assert 'class="notice"' not in body
    standings = body.split('aria-label="Standings"', 1)[1].split("</section>", 1)[0]
    assert marker in standings
    assert body.count(marker) == 1


def test_warm_week_keeps_its_seven_section_matching() -> None:
    body = _body(_page(_raw()))
    assert 'class="notice"' not in body
    assert 'aria-label="Power rankings"' in body


def test_week01_standings_ranks_are_positional() -> None:
    section = _body(_page(_week01_raw())).split('aria-label="Standings"', 1)[1].split("</section>", 1)[0]
    ranks = re.findall(r'<span class="rk">(\d+)</span>', section)
    assert ranks == [str(n) for n in range(1, 13)]
