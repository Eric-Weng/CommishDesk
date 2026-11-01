"""Story 9.7 -- trade grades in the Issue and the narration.

One test group per row of the story's I/O & Edge-Case Matrix, across the web page, the email
(HTML and plain text), the Discord post and the narrator. The base facts are the real demo
season's weeks 3 and 6 (graded offline, ``sleeper_only``); variants edit the Facts dict.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from pathlib import Path
from typing import Any

import pytest

from commishdesk import demo_season
from commishdesk.facts.schema import NARRATION_TOKEN_CAP, WeeklyFacts
from commishdesk.facts.weekly import TRADE_LOPSIDED_KIND, _apply_weekly_narration_cap
from commishdesk.narrate.weekly_template import render_weekly_issue
from commishdesk.render.discord import render_weekly_discord_post, utf16_len
from commishdesk.render.enhancer import EnhancerRejected, validate_page
from commishdesk.render.style import TEAM_PALETTE_DARK, WEEKLY_DARK_TOKENS
from commishdesk.render.weekly_email import SITE_URL_MARKER, render_weekly_email
from commishdesk.render.weekly_web import render_weekly_web
from commishdesk.store import FileStore
from commishdesk.weekly import compute_weekly_facts

LOG = logging.getLogger("tests.trade_cards")
_STAMP = "2026-09-07T00:00:00Z"


@pytest.fixture(scope="module")
def demo_raw(tmp_path_factory: pytest.TempPathFactory) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for week in (3, 6):
        built = compute_weekly_facts(
            FileStore(tmp_path_factory.mktemp(f"cards-{week}")),
            demo_season.DEMO_LEAGUE_ID,
            week,
            demo_season.load_week_bundle(week),
            demo_season.load_league_bundle(),
            seeding=None,
            hold_on_cross_check=True,
            logger=LOG,
        )
        out[week] = built.doc.model_dump(mode="json")
    return out


def _body(page: str) -> str:
    return page.split("</style>", 1)[1]


def _facts(raw: dict[str, Any]) -> WeeklyFacts:
    return WeeklyFacts.model_validate(raw)


def _web(raw: dict[str, Any], **kw: Any) -> str:
    facts = _facts(raw)
    return render_weekly_web(
        facts, render_weekly_issue(facts.narration), output_id="x", generated_at=_STAMP, **kw
    )


def _email(raw: dict[str, Any]) -> Any:
    facts = _facts(raw)
    return render_weekly_email(facts, render_weekly_issue(facts.narration), generated_at=_STAMP)


def _discord(raw: dict[str, Any]) -> str:
    facts = _facts(raw)
    return render_weekly_discord_post(facts, render_weekly_issue(facts.narration))


def _grade(raw: dict[str, Any]) -> dict[str, Any]:
    return raw["transactions"]["trade_grades"][0]


def _verdict(side: dict[str, Any]) -> dict[str, Any]:
    return side["verdicts"][0]


def _all_surfaces(raw: dict[str, Any]) -> dict[str, str]:
    email = _email(raw)
    return {"web": _web(raw), "email": email.html, "text": email.text, "discord": _discord(raw)}


# --------------------------------------------------------------------------- #
# Redraft graded / source line / links
# --------------------------------------------------------------------------- #


def test_a_graded_redraft_trade_draws_one_card_on_every_surface(demo_raw: dict[int, dict[str, Any]]) -> None:
    raw = demo_raw[3]
    grade = _grade(raw)
    assert grade["status"] == "graded"
    surfaces = _all_surfaces(raw)
    for name, text in surfaces.items():
        for side in grade["sides"]:
            v = _verdict(side)
            assert v["letter"] in text and v["word"] in text, name
        assert "Starting lineup" in text, name
        assert "Picks aren't counted in a redraft grade." in text.replace("&#x27;", "'").replace("&#39;", "'"), name
        assert "nflverse" in text and "CC BY 4.0" in text, name
        assert "Sleeper data only" in text, name
    web = _body(surfaces["web"])
    assert web.count('class="card tx tx-trade tg"') == 1
    assert 'href="/docs/trade-grades/"' in web and 'href="/docs/league-value/"' in web
    assert 'href="https://github.com/nflverse"' in web
    email = surfaces["email"]
    assert f'href="{SITE_URL_MARKER}/docs/trade-grades/"' in email
    assert f'href="{SITE_URL_MARKER}/docs/league-value/"' in email
    assert SITE_URL_MARKER not in surfaces["text"] and SITE_URL_MARKER not in surfaces["discord"]
    assert "https://github.com/nflverse" in surfaces["text"] and "/docs/trade-grades/" in surfaces["discord"]


def test_no_surface_ever_prints_an_f_grade(demo_raw: dict[int, dict[str, Any]]) -> None:
    for week in (3, 6):
        for name, text in _all_surfaces(demo_raw[week]).items():
            assert not re.search(r"\bF[+-]?\s·", text), name
            assert not re.search(r'vchip vc-\w+">F', text), name


# --------------------------------------------------------------------------- #
# Variants
# --------------------------------------------------------------------------- #


def _dynasty(raw: dict[str, Any]) -> dict[str, Any]:
    raw = copy.deepcopy(raw)
    grade = _grade(raw)
    grade["dynasty"] = True
    for side in grade["sides"]:
        first = side["verdicts"][0]
        side["verdicts"].append({**first, "horizon": "future"})
    return raw


def test_dynasty_shows_this_season_and_future_per_side(demo_raw: dict[int, dict[str, Any]]) -> None:
    raw = _dynasty(demo_raw[3])
    for name, text in _all_surfaces(raw).items():
        assert text.count("This season") >= 2 and text.count("Future") >= 2, name
        assert "Dynasty" in text, name
        assert "Picks aren't counted" not in text.replace("&#x27;", "'"), name


def test_mostly_estimates_marks_the_chip_the_banner_and_the_tag(demo_raw: dict[int, dict[str, Any]]) -> None:
    raw = copy.deepcopy(demo_raw[3])
    for side in _grade(raw)["sides"]:
        _verdict(side)["est"] = True
    surfaces = _all_surfaces(raw)
    web = _body(surfaces["web"])
    assert "vc-est" in web and "tg-banner" in web and "Mostly estimates" in web
    assert "[est.]" in surfaces["text"] and "Mostly estimates" in surfaces["text"]
    assert "border:2px dashed" in surfaces["email"]
    assert "[Mostly estimates]" in surfaces["discord"]


def test_ahead_is_filled_behind_is_outlined_and_the_letter_is_text(demo_raw: dict[int, dict[str, Any]]) -> None:
    web = _body(_web(demo_raw[3]))
    assert 'vchip vc-ahead">A-' in web and 'vchip vc-behind">C-' in web
    email = _email(demo_raw[3]).html
    assert "background-color:#4C5BD4;color:#FEFEFE" in email  # the ahead chip is filled


def test_unvalued_assets_are_tagged_and_the_verdict_says_what_it_was_graded_without(
    demo_raw: dict[int, dict[str, Any]],
) -> None:
    raw = demo_raw[6]
    assert any(
        u["kind"] == "faab" for side in _grade(raw)["sides"] for u in side["ungraded"]
    ), "demo week 6 carries a FAAB side"
    for name, text in _all_surfaces(raw).items():
        assert "Not valued" in text, name
        assert "Graded without" in text, name


def test_ungraded_shows_not_graded_with_reason_and_never_a_letter(demo_raw: dict[int, dict[str, Any]]) -> None:
    raw = copy.deepcopy(demo_raw[3])
    grade = _grade(raw)
    grade.update(status="ungraded", reason="nothing_valued", context=None, eliminated_roster_ids=[])
    for side in grade["sides"]:
        side.update(verdicts=[], assets=[], lineup_impact=None)
    for name, text in _all_surfaces(raw).items():
        assert "Not graded" in text, name
        assert "Starting lineup" not in text, name
    web = _body(_web(raw))
    assert "vc-none" in web and "vc-ahead" not in web and "vc-behind" not in web
    assert not re.search(r'class="vchip vc-(ahead|behind|even|est)', web)


def test_an_ungraded_email_card_draws_the_dotted_chip(demo_raw: dict[int, dict[str, Any]]) -> None:
    raw = copy.deepcopy(demo_raw[3])
    _grade(raw).update(status="ungraded", reason="nothing_valued", context=None, eliminated_roster_ids=[])
    for side in _grade(raw)["sides"]:
        side.update(verdicts=[], assets=[], lineup_impact=None)
    assert "border:2px dotted" in _email(raw).html


def test_a_lopsided_hook_is_stated_once_and_the_kinds_agree(demo_raw: dict[int, dict[str, Any]]) -> None:
    from commishdesk.facts.weekly import _trade_grade_narration
    from commishdesk.narrate import weekly_template

    assert weekly_template._TRADE_LOPSIDED_KIND == TRADE_LOPSIDED_KIND
    raw = copy.deepcopy(demo_raw[3])
    for side, (letter, word, ahead) in zip(
        _grade(raw)["sides"], [("A", "Lopsided", True), ("D", "Far behind", False)], strict=True
    ):
        _verdict(side).update(tier="lopsided", letter=letter, word=word, ahead=ahead, share=0.4)
    facts = _facts(raw)
    labels = {t.roster_id: t.team_name or t.roster_id for t in facts.teams}
    _, candidates = _trade_grade_narration(facts.transactions, labels)
    narration = facts.narration.model_copy(
        update={"storyline_candidates": [*facts.narration.storyline_candidates, *candidates]}
    )
    issue = render_weekly_issue(narration)
    text = " ".join(block for section in issue.sections for block in section.blocks)
    assert candidates[0].hook not in text


def test_the_context_flag_prints_one_standings_fact_and_changes_no_grade(
    demo_raw: dict[int, dict[str, Any]],
) -> None:
    raw = copy.deepcopy(demo_raw[3])
    before = [(_verdict(s)["letter"], _verdict(s)["word"]) for s in _grade(raw)["sides"]]
    _grade(raw).update(context="contender_vs_eliminated", eliminated_roster_ids=[_grade(raw)["sides"][1]["roster_id"]])
    after = [(_verdict(s)["letter"], _verdict(s)["word"]) for s in _grade(raw)["sides"]]
    assert before == after
    expected = "are out of the playoff race"
    for name, text in _all_surfaces(raw).items():
        assert text.count(expected) == 1, name
        assert "The grades are the same either way." in text, name
        assert not re.search(r"veto|collu|tank|suspicious", text, re.I), name
    plain = _all_surfaces(demo_raw[3])
    assert all(expected not in text for text in plain.values())


def test_a_three_team_trade_stacks_each_side_with_its_verdicts(demo_raw: dict[int, dict[str, Any]]) -> None:
    raw = copy.deepcopy(demo_raw[3])
    trade = next(t for t in raw["transactions"]["recent_trades"] if t["week"] == raw["week"])
    grade = _grade(raw)
    third = next(t["roster_id"] for t in raw["teams"] if t["roster_id"] not in {s["roster_id"] for s in trade["sides"]})
    trade["sides"].append({"roster_id": third, "players": [], "picks": [], "faab": 0})
    grade["sides"].append({**copy.deepcopy(grade["sides"][1]), "roster_id": third, "assets": [], "ungraded": []})
    web = _body(_web(raw))
    assert "tg-sides tg-multi" in web
    assert web.count("Receives") >= 3
    assert _discord(raw).count(" gets ") >= 3


def test_no_trades_this_week_with_an_older_trade_reads_one_line() -> None:
    raw = json.loads(
        (Path(__file__).parent / "fixtures" / "facts" / "expected-weekly-facts-week10.json").read_text(encoding="utf-8")
    )
    line = "No trades this week. The last one was in week 9."
    assert line in _web(raw)
    assert line in _email(raw).html and line in _email(raw).text
    assert "tx-trade" not in _body(_web(raw))


# --------------------------------------------------------------------------- #
# Sleeper-only wording, escaping, fences
# --------------------------------------------------------------------------- #


def test_a_nflverse_snapshot_drops_the_sleeper_only_words(demo_raw: dict[int, dict[str, Any]]) -> None:
    raw = copy.deepcopy(demo_raw[3])
    _grade(raw).update(source="nflverse", fair_band=0.05)
    for name, text in _all_surfaces(raw).items():
        assert "Sleeper data only" not in text, name


def test_a_hostile_team_name_is_escaped_in_every_card(demo_raw: dict[int, dict[str, Any]]) -> None:
    raw = copy.deepcopy(demo_raw[3])
    hostile = '<script>x</script>"><b onload=1>'
    rid = _grade(raw)["sides"][0]["roster_id"]
    for team in raw["teams"]:
        if team["roster_id"] == rid:
            team["team_name"] = hostile
    web = _body(_web(raw))
    assert hostile not in web
    assert "<script>x" not in _all_surfaces(raw)["email"]
    # Discord prints a name unescaped only inside the standings code block (inert there)
    assert "<script>x" not in _discord(raw).split("🔄 **Trade", 1)[1]


def test_the_page_fence_allows_exactly_three_card_links() -> None:
    def page(href: str) -> str:
        return f'<html><body><a href="{href}">x</a></body></html>'

    for ok in ("/docs/trade-grades/", "/docs/league-value/", "https://github.com/nflverse", "#lead"):
        validate_page(page(ok), enhanced=False)
    for bad in ("https://github.com/nflverse/other", "/docs/other/", "https://example.com", "javascript:x"):
        with pytest.raises(EnhancerRejected):
            validate_page(page(bad), enhanced=False)


def test_the_unenhanced_card_page_has_no_script_and_only_fixed_links(demo_raw: dict[int, dict[str, Any]]) -> None:
    web = _web(demo_raw[3])
    assert "<script" not in web
    hrefs = set(re.findall(r'href="([^"]*)"', web.split("</style>", 1)[1]))
    assert hrefs <= {"/docs/trade-grades/", "/docs/league-value/", "https://github.com/nflverse"} | {
        h for h in hrefs if h.startswith("#")
    }


def test_facts_without_grades_draw_the_plain_card_as_before() -> None:
    raw = json.loads(
        (Path(__file__).parent / "fixtures" / "facts" / "expected-weekly-facts-week10.json").read_text(encoding="utf-8")
    )
    raw["transactions"]["recent_trades"][0]["week"] = raw["week"]
    assert raw["transactions"]["trade_grades"] == []
    web = _body(_web(raw))
    assert 'class="card tx tx-trade"' in web and "tg-sides" not in web


# --------------------------------------------------------------------------- #
# Revisit switch
# --------------------------------------------------------------------------- #


def test_revisit_off_is_byte_identical_to_no_revisit_data(demo_raw: dict[int, dict[str, Any]]) -> None:
    raw = demo_raw[6]
    assert raw["transactions"]["trade_revisits"], "demo week 6 revisits the week-3 trade"
    stripped = copy.deepcopy(raw)
    stripped["transactions"]["trade_revisits"] = []
    assert _web(raw) == _web(stripped) == _web(raw, show_trade_revisited=False)
    assert "tg-locked" not in _body(_web(raw))


def test_revisit_on_draws_the_locked_panel_on_the_web_only(demo_raw: dict[int, dict[str, Any]]) -> None:
    raw = demo_raw[6]
    on = _body(_web(raw, show_trade_revisited=True))
    assert on.count("tg-locked") >= 1
    panel = on.split('class="card tx tg-locked"', 1)[1].split("</article>", 1)[0]
    assert "who really won" in panel and 'aria-hidden="true"' in panel and "disabled" in panel
    assert "Not available yet" in panel
    leader = raw["transactions"]["trade_revisits"][0]["leader"]
    assert f"leader {leader}" not in panel
    for name, text in _all_surfaces(raw).items():
        assert "who really won" not in text and "Not available yet" not in text, name


def test_revisit_on_without_revisit_data_draws_no_card(demo_raw: dict[int, dict[str, Any]]) -> None:
    raw = copy.deepcopy(demo_raw[6])
    raw["transactions"]["trade_revisits"] = []
    assert "tg-locked" not in _body(_web(raw, show_trade_revisited=True))


def test_the_locked_panel_is_valid_under_the_page_fence(demo_raw: dict[int, dict[str, Any]]) -> None:
    validate_page(_web(demo_raw[6], show_trade_revisited=True), enhanced=False)


# --------------------------------------------------------------------------- #
# Discord: never truncated, within the cap
# --------------------------------------------------------------------------- #


def test_the_discord_trade_block_is_never_trimmed_and_the_post_fits(demo_raw: dict[int, dict[str, Any]]) -> None:
    for week in (3, 6):
        post = _discord(demo_raw[week])
        assert utf16_len(post) <= 2000
        assert "🔄 **Trade · week" in post and "How trade grades work" in post


def test_a_graded_trade_is_not_repeated_as_a_wire_line(demo_raw: dict[int, dict[str, Any]]) -> None:
    raw = copy.deepcopy(demo_raw[3])
    raw["transactions"]["this_week"] = [m for m in raw["transactions"]["this_week"] if m["type"] == "trade"]
    assert "🔄 **Wire**" not in _discord(raw)


# --------------------------------------------------------------------------- #
# Email dark mode
# --------------------------------------------------------------------------- #


def test_the_email_chip_text_has_a_dark_mapping(demo_raw: dict[int, dict[str, Any]]) -> None:
    html = _email(demo_raw[3]).html
    assert '[style*=";color:#FEFEFE"]' in html
    assert "{{commishdesk:site-url}}" in html


# --------------------------------------------------------------------------- #
# Narration
# --------------------------------------------------------------------------- #


def test_the_narration_carries_numbers_only_and_the_template_states_the_grades(
    demo_raw: dict[int, dict[str, Any]],
) -> None:
    facts = _facts(demo_raw[3])
    grades = facts.narration.transactions.trade_grades
    assert len(grades) == 1 and grades[0].status == "graded"
    assert {v.letter for s in grades[0].sides for v in s.verdicts} == {"A-", "C-"}
    issue = render_weekly_issue(facts.narration)
    desk = next(s for s in issue.sections if s.heading == "The Transaction Desk")
    text = " ".join(desk.blocks)
    assert "trade grades" in text and "A-, clear edge" in text
    assert not re.search(r"veto|collu|rigged|tank", text, re.I)


def test_a_lopsided_trade_adds_one_transient_candidate_and_the_template_states_context(
    demo_raw: dict[int, dict[str, Any]],
) -> None:
    raw = copy.deepcopy(demo_raw[3])
    for side, (letter, word, ahead) in zip(
        _grade(raw)["sides"], [("A", "Lopsided", True), ("D", "Far behind", False)], strict=True
    ):
        v = _verdict(side)
        v.update(tier="lopsided", letter=letter, word=word, ahead=ahead, share=0.4)
    from commishdesk.facts.weekly import _trade_grade_narration

    facts = _facts(raw)
    labels = {t.roster_id: t.team_name or t.roster_id for t in facts.teams}
    grades, candidates = _trade_grade_narration(facts.transactions, labels)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.kind == TRADE_LOPSIDED_KIND
    assert cand.id == f"{TRADE_LOPSIDED_KIND}:{cand.roster_ids[0]}:{_grade(raw)['transaction_id']}"
    assert cand.weeks_running == 1 and cand.hook and "Lopsided" in cand.hook
    assert grades[0].context is None


def test_the_cap_drops_trade_grades_and_lopsided_candidates_before_any_lead(
    demo_raw: dict[int, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    facts = _facts(demo_raw[3])
    narration = facts.narration.model_copy(
        update={
            "storyline_candidates": [
                *facts.narration.storyline_candidates,
                facts.narration.storyline_candidates[0].model_copy(
                    update={"id": f"{TRADE_LOPSIDED_KIND}:1", "kind": TRADE_LOPSIDED_KIND}
                )
                if facts.narration.storyline_candidates
                else __import__("commishdesk.facts.schema", fromlist=["StorylineCandidate"]).StorylineCandidate(
                    id=f"{TRADE_LOPSIDED_KIND}:1", kind=TRADE_LOPSIDED_KIND, roster_ids=["1"], hook="x", weeks_running=1
                ),
            ]
        }
    )
    size = len(narration.model_dump_json())
    without = narration.transactions.model_copy(update={"trade_grades": []})
    trimmed_size = len(narration.model_copy(update={"transactions": without}).model_dump_json())
    assert trimmed_size < size <= NARRATION_TOKEN_CAP
    import commishdesk.facts.weekly as weekly_mod

    monkeypatch.setattr(weekly_mod, "_current_cap", lambda: size - 1)
    monkeypatch.setattr(weekly_mod, "_within_cap", lambda m: len(m.model_dump_json()) <= size - 1)
    out = _apply_weekly_narration_cap(narration)
    assert out.transactions.trade_grades == []
    assert all(c.kind != TRADE_LOPSIDED_KIND for c in out.storyline_candidates)
    assert out.lead_candidates == narration.lead_candidates and out.standings == narration.standings
    assert out.power == narration.power  # nothing past the first tier was needed


def test_the_weekly_prompt_forbids_veto_wording_and_cold_start_omits_the_rule() -> None:
    from commishdesk.voices.beat_writer import weekly_voice_for

    warm = weekly_voice_for(cold_start=False).system_prompt
    assert "trade_grades" in warm and "veto" in warm and "tanking" in warm
    assert "trade_grades" not in weekly_voice_for(cold_start=True).system_prompt
    suppressed = weekly_voice_for(cold_start=False, suppressed=frozenset({"transactions"}))
    assert "trade_grades" not in suppressed.system_prompt


def test_trade_letters_are_not_hallucinations_for_the_weekly_safety_check(demo_raw: dict[int, dict[str, Any]]) -> None:
    from commishdesk.narrate.safety import check_narration
    from commishdesk.narrate.weekly_template import weekly_issue_to_text

    facts = _facts(demo_raw[3])
    report = check_narration(weekly_issue_to_text(render_weekly_issue(facts.narration)), facts.narration)
    assert not [f for f in report.findings if f.category == "hallucination"]
    bogus = check_narration("The trade grades: Team A got a B+ on it.", facts.narration)
    assert any(f.category == "hallucination" and f.matched == "B+" for f in bogus.findings)


# --------------------------------------------------------------------------- #
# Dark team colours (contrast)
# --------------------------------------------------------------------------- #


def _lum(h: str) -> float:
    ch = [int(h.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in ch]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _ratio(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


#: The canvas's proposed lightened team colours (design-decisions.md, "Dark palette").
_CANVAS_DARK_TEAMS = ("#60A5FA", "#E879F9", "#D9A441", "#F87171", "#818CF8", "#FB923C")


@pytest.mark.parametrize("colour", _CANVAS_DARK_TEAMS)
def test_the_canvas_dark_team_colours_pass_on_the_dark_card(colour: str) -> None:
    assert _ratio(colour, WEEKLY_DARK_TOKENS["--card"]) >= 4.5
    assert _ratio(colour, "#141527") >= 4.5


@pytest.mark.parametrize("colour", TEAM_PALETTE_DARK)
def test_the_engine_dark_team_palette_passes_on_the_dark_card(colour: str) -> None:
    assert _ratio(colour, WEEKLY_DARK_TOKENS["--card"]) >= 4.5


@pytest.mark.parametrize("theme", ["dark"])
def test_card_chip_roles_pass(theme: str) -> None:
    t = WEEKLY_DARK_TOKENS
    assert _ratio(t["--on-emph"], t["--emph"]) >= 4.5  # filled chip
    assert _ratio(t["--ink"], t["--card"]) >= 4.5 and _ratio(t["--ink-2"], t["--card"]) >= 4.5
