"""Story 9.5 -- playoff odds in the Issue and the narration.

One test group per row of the story's I/O & Edge-Case Matrix, across the web page, the email
(HTML and plain text), the Discord post and the narrator. The base facts are the real demo
season's week 6 built through ``compute_weekly_facts`` (fresh path); variants swap the odds block
in with :func:`commishdesk.facts.weekly.with_playoff_odds`.
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any

import pytest

from commishdesk import demo_season
from commishdesk.facts.schema import WeeklyFacts, WeeklyPlayoffOdds
from commishdesk.facts.weekly import (
    PLAYOFF_ODDS_KIND,
    TRADE_LOPSIDED_KIND,
    _apply_weekly_narration_cap,
    odds_mover,
    with_playoff_odds,
)
from commishdesk.narrate import safety
from commishdesk.narrate.llm import build_weekly_payload
from commishdesk.narrate.weekly_template import render_weekly_issue
from commishdesk.render import _weekly_model as wm
from commishdesk.render.discord import render_weekly_discord_post, utf16_len
from commishdesk.render.enhancer import EnhancerRejected, validate_page
from commishdesk.render.weekly_email import ISSUE_URL_MARKER, SITE_URL_MARKER, render_weekly_email
from commishdesk.render.weekly_web import render_weekly_web
from commishdesk.store import FileStore
from commishdesk.weekly import (
    _replayed_facts,
    compute_weekly_facts,
    record_published_weekly,
    write_weekly_facts_snapshot,
)

LOG = logging.getLogger("tests.odds_cards")
_STAMP = "2026-09-07T00:00:00Z"
WEEK = 6


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Any:
    return compute_weekly_facts(
        FileStore(tmp_path_factory.mktemp("odds")),
        demo_season.DEMO_LEAGUE_ID,
        WEEK,
        demo_season.load_week_bundle(WEEK),
        demo_season.load_league_bundle(),
        seeding=None,
        hold_on_cross_check=True,
        logger=LOG,
    )


def _bare(doc: WeeklyFacts) -> WeeklyFacts:
    """*doc* as the builder made it, before any odds: no block, no projection, no candidate."""
    narration = doc.narration.model_copy(
        update={
            "playoff_odds": None,
            "storyline_candidates": [c for c in doc.narration.storyline_candidates if c.kind != PLAYOFF_ODDS_KIND],
        }
    )
    return doc.model_copy(update={"playoff_odds": None, "narration": narration})


@pytest.fixture(scope="module")
def bare(built: Any) -> WeeklyFacts:
    return _bare(built.doc)


def _block(doc: WeeklyFacts, rows: list[dict[str, Any]] | None = None, **top: Any) -> WeeklyPlayoffOdds:
    """A block over every roster: odds fall by 0.07 down the list unless *rows* says otherwise."""
    ids = [t.roster_id for t in doc.teams]
    teams = rows if rows is not None else [
        {"roster_id": rid, "make_playoffs": round(max(0.9 - 0.07 * i, 0.003), 4), "status": "in_race"}
        for i, rid in enumerate(ids)
    ]
    return WeeklyPlayoffOdds.model_validate(
        {"simulations": 10000, "module_version": "1", "through_week": doc.week, "teams": teams, **top}
    )


def _web(doc: WeeklyFacts) -> str:
    return render_weekly_web(doc, render_weekly_issue(doc.narration), output_id="x", generated_at=_STAMP)


def _email(doc: WeeklyFacts) -> Any:
    return render_weekly_email(doc, render_weekly_issue(doc.narration), generated_at=_STAMP)


def _discord(doc: WeeklyFacts) -> str:
    return render_weekly_discord_post(doc, render_weekly_issue(doc.narration))


def _surfaces(doc: WeeklyFacts) -> dict[str, str]:
    email = _email(doc)
    return {"web": _web(doc), "email": email.html, "text": email.text, "discord": _discord(doc)}


def _no_trades(doc: WeeklyFacts) -> WeeklyFacts:
    """*doc* without this week's trade cards and results, so the Discord post has room for the odds
    block (it is the first thing trimmed when the post is tight)."""
    tx = doc.transactions.model_copy(update={"this_week": [], "recent_trades": [], "trade_grades": []})
    games = doc.matchups.model_copy(update={"this_week": []})
    return doc.model_copy(update={"transactions": tx, "matchups": games})


def _with_format(doc: WeeklyFacts, *, byes: int | None = None, divisions: bool | None = None) -> WeeklyFacts:
    fmt = doc.league.format
    update: dict[str, Any] = {}
    if byes is not None and fmt.playoff is not None:
        update["playoff"] = fmt.playoff.model_copy(update={"byes": byes})
    if divisions is False:
        update["divisions"] = []
    return doc.model_copy(update={"league": doc.league.model_copy(update={"format": fmt.model_copy(update=update)})})


# --------------------------------------------------------------------------- #
# Fresh vs replay
# --------------------------------------------------------------------------- #


def test_the_fresh_build_carries_the_block_on_the_document_and_in_facts_json(built: Any) -> None:
    assert built.doc.playoff_odds is not None
    assert built.facts_json["playoff_odds"] == built.doc.playoff_odds.model_dump(mode="json")
    assert built.doc.narration.playoff_odds is not None
    assert built.facts_json["narration"]["playoff_odds"] is not None
    assert built.facts_json["schema_version"] == "0.16.0"


def test_fresh_and_replay_agree_on_the_block_the_narration_and_every_surface(
    built: Any, tmp_path_factory: pytest.TempPathFactory
) -> None:
    store = FileStore(tmp_path_factory.mktemp("replay"))
    record_published_weekly(store, demo_season.DEMO_LEAGUE_ID, WEEK, built.facts_json, {}, {})
    replayed = _replayed_facts(store, demo_season.DEMO_LEAGUE_ID, WEEK, True, LOG)
    assert replayed is not None
    assert replayed.doc.playoff_odds == built.doc.playoff_odds
    assert replayed.doc.narration == built.doc.narration
    assert replayed.facts_json == built.facts_json
    assert _surfaces(replayed.doc) == _surfaces(built.doc)


# --------------------------------------------------------------------------- #
# Change and mover
# --------------------------------------------------------------------------- #


def test_change_is_percentage_points_against_the_prior_weeks_stored_block(
    built: Any, tmp_path_factory: pytest.TempPathFactory
) -> None:
    store = FileStore(tmp_path_factory.mktemp("change"))
    prior = copy.deepcopy(built.facts_json)
    for team in prior["playoff_odds"]["teams"]:
        team["make_playoffs"] = round(team["make_playoffs"] - 0.05, 4)
    write_weekly_facts_snapshot(store, demo_season.DEMO_LEAGUE_ID, WEEK - 1, prior)
    again = compute_weekly_facts(
        store,
        demo_season.DEMO_LEAGUE_ID,
        WEEK,
        demo_season.load_week_bundle(WEEK),
        demo_season.load_league_bundle(),
        seeding=None,
        hold_on_cross_check=True,
        logger=LOG,
    )
    teams = again.doc.playoff_odds.teams  # type: ignore[union-attr]
    assert all(t.change is not None and abs(t.change - 5.0) < 0.2 for t in teams)
    assert again.facts_json["playoff_odds"]["teams"][0]["change"] is not None
    assert again.doc.narration.playoff_odds.mover_roster_id is not None  # type: ignore[union-attr]


def test_first_odds_week_has_no_change_and_no_mover(built: Any) -> None:
    assert all(t.change is None for t in built.doc.playoff_odds.teams)
    assert built.doc.narration.playoff_odds.mover_roster_id is None
    assert not any(c.kind == PLAYOFF_ODDS_KIND for c in built.doc.narration.storyline_candidates)
    view = wm.odds_view(built.doc)
    assert view is not None and view.mover is None and "mover" not in view.header


def test_an_unreadable_prior_snapshot_is_skipped_silently(
    built: Any, tmp_path_factory: pytest.TempPathFactory
) -> None:
    store = FileStore(tmp_path_factory.mktemp("bad"))
    write_weekly_facts_snapshot(store, demo_season.DEMO_LEAGUE_ID, WEEK - 1, {"playoff_odds": {"teams": [{"x": 1}]}})
    again = compute_weekly_facts(
        store,
        demo_season.DEMO_LEAGUE_ID,
        WEEK,
        demo_season.load_week_bundle(WEEK),
        demo_season.load_league_bundle(),
        seeding=None,
        hold_on_cross_check=True,
        logger=LOG,
    )
    assert all(t.change is None for t in again.doc.playoff_odds.teams)  # type: ignore[union-attr]


def test_the_mover_is_the_largest_absolute_move_ties_to_the_lower_roster_id(bare: WeeklyFacts) -> None:
    ids = [t.roster_id for t in bare.teams]
    rows = [
        {"roster_id": rid, "make_playoffs": 0.5, "status": "in_race", "change": c}
        for rid, c in zip(ids, [0.5, -7.0, 7.0, 2.0] + [None] * (len(ids) - 4), strict=False)
    ]
    block = _block(bare, rows)
    assert odds_mover(block) == min(ids[1], ids[2], key=int)
    quiet = _block(bare, [{**r, "change": 0.4} for r in rows])
    assert odds_mover(quiet) is None
    doc = with_playoff_odds(bare, block)
    assert "Biggest mover" in wm.odds_view(doc).header  # type: ignore[union-attr]
    assert "Biggest mover" not in wm.odds_view(with_playoff_odds(bare, quiet)).header  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# Web
# --------------------------------------------------------------------------- #


def test_web_shows_every_team_ranked_with_accessible_names_and_the_docs_link(bare: WeeklyFacts) -> None:
    doc = with_playoff_odds(bare, _block(bare))
    page = _web(doc)
    body = page.split("</style>", 1)[1]
    assert body.count('class="po-row"') == len(doc.teams)
    assert 'role="group" aria-label="Playoff odds"' in body
    assert body.index("Playoff odds") > body.index('class="standings"')
    assert 'href="/docs/playoff-odds/"' in body
    names = re.findall(r'class="po-row" aria-label="([^"]+)"', body)
    assert names and all("%" in n for n in names)
    assert "Top " in body and " make it." in body
    pcts = [int(m) for m in re.findall(r'class="po-pct">(\d+)%<', body)]
    assert pcts == sorted(pcts, reverse=True)
    validate_page(page, enhanced=False)  # the fixed href is inside the carve-out


def test_the_href_fence_is_exactly_four_fixed_values() -> None:
    from commishdesk.render.enhancer import ALLOWED_CARD_LINKS

    assert ALLOWED_CARD_LINKS == {
        "/docs/trade-grades/",
        "/docs/league-value/",
        "https://github.com/nflverse",
        "/docs/playoff-odds/",
    }
    for bad in ("/docs/other/", "https://example.com/docs/playoff-odds/", "/docs/playoff-odds"):
        with pytest.raises(EnhancerRejected):
            validate_page(f'<a href="{bad}">x</a>', enhanced=False)


def test_bye_and_division_columns_follow_the_league_format(bare: WeeklyFacts) -> None:
    ids = [t.roster_id for t in bare.teams]
    rows = [
        {"roster_id": rid, "make_playoffs": 0.5 - 0.01 * i, "status": "in_race", "bye": 0.2, "division": 0.1}
        for i, rid in enumerate(ids)
    ]
    full = with_playoff_odds(bare, _block(bare, rows))
    body = _web(full).split("</style>", 1)[1]
    assert "Bye 20%" in body and "Div. 10%" in body and "Make playoffs · Bye · Division" in body
    plain_rows = [{k: v for k, v in r.items() if k not in ("bye", "division")} for r in rows]
    plain = with_playoff_odds(_with_format(bare, byes=0, divisions=False), _block(bare, plain_rows))
    plain_body = _web(plain).split("</style>", 1)[1]
    odds_html = plain_body.split('class="po"', 1)[1].split("</ul>", 1)[0]
    assert "Bye " not in odds_html and "Div. " not in odds_html and "Make playoffs</p>" in odds_html
    assert "get a bye" not in wm.odds_view(plain).header  # type: ignore[union-attr]


def test_clinched_and_eliminated_are_word_pills_with_no_bar_or_percent(bare: WeeklyFacts) -> None:
    ids = [t.roster_id for t in bare.teams]
    rows = [{"roster_id": ids[0], "make_playoffs": 1.0, "status": "clinched"}]
    rows += [{"roster_id": ids[1], "make_playoffs": 0.0, "status": "eliminated"}]
    rows += [{"roster_id": rid, "make_playoffs": 0.4, "status": "in_race"} for rid in ids[2:]]
    doc = with_playoff_odds(bare, _block(bare, rows))
    body = _web(doc).split("</style>", 1)[1]
    assert "po-pill po-clinched" in body and "po-pill po-eliminated" in body
    clinched = re.search(r'<li class="po-row" aria-label="[^"]*clinched[^"]*">(.*?)</li>', body, re.S)
    assert clinched is not None and "po-bar" not in clinched.group(1) and "%" not in clinched.group(1)
    eliminated = re.search(r'<li class="po-row" aria-label="[^"]*eliminated[^"]*">(.*?)</li>', body, re.S)
    assert eliminated is not None and "po-bar" not in eliminated.group(1) and "%" not in eliminated.group(1)
    text = _email(doc).text
    assert "Clinched" in text and "Eliminated" in text


def test_a_tiny_live_chance_reads_under_one_percent_never_zero(bare: WeeklyFacts) -> None:
    ids = [t.roster_id for t in bare.teams]
    rows = [
        {"roster_id": rid, "make_playoffs": p, "status": "in_race", "bye": 0.0}
        for rid, p in zip(ids, [0.6, 0.0004, 0.0, 0.998] + [0.3] * (len(ids) - 4), strict=False)
    ]
    doc = with_playoff_odds(_no_trades(bare), _block(bare, rows))
    assert wm.odds_percent(0.0004) == "<1%" and wm.odds_percent(0.0) == "<1%" and wm.odds_percent(0.998) == ">99%"
    for name, text in _surfaces(doc).items():
        assert not re.search(r"(?<![\d.])0%", text.replace("&lt;1%", "<1%")), name
        assert "<1%" in text.replace("&lt;1%", "<1%").replace("\\<1%", "<1%"), name


# --------------------------------------------------------------------------- #
# Absent, not zero
# --------------------------------------------------------------------------- #


def test_no_block_renders_nothing_and_is_byte_identical_to_an_empty_one(bare: WeeklyFacts) -> None:
    assert bare.playoff_odds is None
    empty = bare.model_copy(update={"playoff_odds": _block(bare, [])})
    assert _surfaces(empty) == _surfaces(bare)
    for name, text in _surfaces(bare).items():
        assert "Playoff odds" not in text and "/docs/playoff-odds/" not in text, name
        assert "class=\"po-row\"" not in text, name


def test_week_one_is_a_cold_start_with_no_odds_on_any_surface(tmp_path_factory: pytest.TempPathFactory) -> None:
    week = demo_season.WEEKS[0]
    cold = compute_weekly_facts(
        FileStore(tmp_path_factory.mktemp("cold")),
        demo_season.DEMO_LEAGUE_ID,
        week,
        demo_season.load_week_bundle(week),
        demo_season.load_league_bundle(),
        seeding=None,
        hold_on_cross_check=True,
        logger=LOG,
    )
    assert cold.doc.narration.playoff_odds is None
    for name, text in _surfaces(cold.doc).items():
        assert "Playoff odds" not in text, name
    assert "playoff_odds" not in build_weekly_payload(cold.doc.narration, cold_start=True)


# --------------------------------------------------------------------------- #
# Email, text, Discord
# --------------------------------------------------------------------------- #


def test_email_shows_six_rows_plus_the_mover_four_at_phone_width_and_the_links(bare: WeeklyFacts) -> None:
    ids = [t.roster_id for t in bare.teams]
    rows = [
        {"roster_id": rid, "make_playoffs": round(0.9 - 0.07 * i, 4), "status": "in_race",
         "change": 12.0 if i == len(ids) - 1 else 0.0}
        for i, rid in enumerate(ids)
    ]
    doc = with_playoff_odds(bare, _block(bare, rows))
    html = _email(doc).html
    shown = wm.odds_email_rows(wm.odds_view(doc))  # type: ignore[arg-type]
    assert len(shown) == wm.ODDS_EMAIL_ROWS + 1 and shown[-1].roster_id == ids[-1]  # the mover rides along
    assert html.count('class="po-r5"') == wm.ODDS_EMAIL_ROWS - wm.ODDS_EMAIL_PHONE_ROWS  # the mover stays at 320
    assert "max-width:360px" in html
    assert f'href="{SITE_URL_MARKER}/docs/playoff-odds/"' in html
    assert f'href="{ISSUE_URL_MARKER}#standings"' in html
    assert "<script" not in html
    text = _email(doc).text
    assert "/docs/playoff-odds/" in text and SITE_URL_MARKER not in text


def test_text_and_discord_show_all_rows_to_twelve_teams_else_top_eight_and_a_count(bare: WeeklyFacts) -> None:
    doc = with_playoff_odds(_no_trades(bare), _block(bare))
    assert len(doc.teams) == 12
    discord = _discord(doc)
    odds_lines = discord.split("**Playoff odds**", 1)[1].split("\n\n")[0].splitlines()
    assert len([ln for ln in odds_lines if re.match(r"\d+\. ", ln)]) == 12
    assert "more" not in "".join(odds_lines)
    view = wm.odds_view(doc)
    assert view is not None and wm.odds_text_rows(view) == (view.rows, 0)
    # a 13-team block (the row count is the block's, not the format's)
    ids = [t.roster_id for t in doc.teams] + ["99"]
    rows = [
        {"roster_id": rid, "make_playoffs": round(0.9 - 0.06 * i, 4), "status": "in_race"}
        for i, rid in enumerate(ids)
    ]
    big = _no_trades(bare).model_copy(update={"playoff_odds": _block(bare, rows)})
    big_view = wm.odds_view(big)
    assert big_view is not None
    shown, left = wm.odds_text_rows(big_view)
    assert len(shown) == 8 and left == 5
    assert "and 5 more" in _discord(big) and "and 5 more" in _email(big).text
    assert len(wm.odds_email_rows(big_view)) == 6 and big_view.total == 13


def test_discord_trims_the_odds_before_anything_else_and_stays_in_the_limit(bare: WeeklyFacts) -> None:
    doc = with_playoff_odds(_no_trades(bare), _block(bare))
    post = _discord(doc)
    assert "🎯 **Playoff odds**" in post and utf16_len(post) <= 2000
    # with the trade card present the post is tight, and the odds are what gives way
    tight = _discord(with_playoff_odds(bare, _block(bare)))
    assert "Trade · week" in tight and utf16_len(tight) <= 2000
    from commishdesk.render import discord as dmod

    blocks = [b for b in (dmod._weekly_odds(doc), dmod._weekly_awards(doc), dmod._weekly_wire(doc)) if b is not None]
    assert dmod._DROP_ODDS < min(b.drop for b in blocks if b.drop is not None and b is not blocks[0])
    trimmed = dmod._trim(blocks, utf16_len(dmod._join(blocks[1:])))
    assert all("Playoff odds" not in b.text for b in trimmed)


# --------------------------------------------------------------------------- #
# Narration
# --------------------------------------------------------------------------- #


def test_the_projection_carries_integer_percents_and_the_template_line_passes_safety(bare: WeeklyFacts) -> None:
    ids = [t.roster_id for t in bare.teams]
    rows = [
        {"roster_id": rid, "make_playoffs": round(0.9 - 0.0713 * i, 4), "status": "in_race",
         "change": 8.4 if i == 3 else None}
        for i, rid in enumerate(ids)
    ]
    doc = with_playoff_odds(bare, _block(bare, rows))
    odds = doc.narration.playoff_odds
    assert odds is not None and odds.mover_roster_id == ids[3]
    assert all(isinstance(t.make_playoffs, int) for t in odds.teams)
    block_json = doc.narration.model_dump_json().split('"playoff_odds":{', 1)[1].split("}]", 1)[0]
    assert not re.search(r"\d\.\d{2,}", block_json)  # never the 4-dp floats
    issue = render_weekly_issue(doc.narration)
    text = " ".join(" ".join(s.blocks) for s in issue.sections)
    pct = next(t.make_playoffs for t in odds.teams if t.roster_id == ids[3])
    assert f"{pct} percent" in text and "8 points" in text
    assert safety.check_narration(f"{doc.narration.playoff_odds.teams[0].team} sits at "  # type: ignore[union-attr]
                                  f"{odds.teams[0].make_playoffs}% to make it.", doc.narration).findings == ()


def test_safety_accepts_a_payload_percent_and_refuses_a_made_up_one(bare: WeeklyFacts) -> None:
    ids = [t.roster_id for t in bare.teams]
    rows = [
        {"roster_id": rid, "make_playoffs": 0.43 if i == 0 else 0.2, "status": "in_race"}
        for i, rid in enumerate(ids)
    ]
    doc = with_playoff_odds(bare, _block(bare, rows))
    assert safety.closed_world_tokens("They are a 43% bet to get in.", doc.narration) == ()
    assert safety.closed_world_tokens("They are a 61% bet to get in.", doc.narration) == ("61",)


def test_the_cap_drops_the_odds_first_then_trade_grades_then_never_a_lead(
    bare: WeeklyFacts, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [t.roster_id for t in bare.teams]
    rows = [{"roster_id": rid, "make_playoffs": 0.5, "status": "in_race", "change": 3.0 if i == 0 else None}
            for i, rid in enumerate(ids)]
    doc = with_playoff_odds(bare, _block(bare, rows))
    narration = doc.narration
    assert narration.playoff_odds is not None
    assert any(c.kind == PLAYOFF_ODDS_KIND for c in narration.storyline_candidates)
    size = len(narration.model_dump_json())
    import commishdesk.facts.weekly as weekly_mod

    odds_free = narration.model_copy(update={"playoff_odds": None})
    smaller = len(odds_free.model_dump_json())
    assert smaller < size
    monkeypatch.setattr(weekly_mod, "_current_cap", lambda: size - 1)
    monkeypatch.setattr(weekly_mod, "_within_cap", lambda m: len(m.model_dump_json()) <= size - 1)
    out = _apply_weekly_narration_cap(narration)
    assert out.playoff_odds is None
    assert all(c.kind != PLAYOFF_ODDS_KIND for c in out.storyline_candidates)
    assert out.transactions.trade_grades == narration.transactions.trade_grades  # untouched: odds went first
    assert out.lead_candidates == narration.lead_candidates and out.power == narration.power
    assert TRADE_LOPSIDED_KIND  # the next tier is covered by test_trade_cards


def test_the_prompt_rule_and_payload_carry_the_odds_only_when_warm(bare: WeeklyFacts) -> None:
    from commishdesk.voices.beat_writer import weekly_voice_for

    assert "Playoff odds." in weekly_voice_for(cold_start=False).system_prompt
    assert "Playoff odds." not in weekly_voice_for(cold_start=True).system_prompt
    doc = with_playoff_odds(bare, _block(bare))
    assert '"playoff_odds":{"teams"' in build_weekly_payload(doc.narration)


def test_the_demo_season_shows_odds_on_web_email_and_text_and_discord_stays_in_the_limit(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Weeks 2-12 of the demo season: the block reaches every surface; Discord keeps odds only when
    the post has room (they are trimmed first), and never passes 2,000 UTF-16 units."""
    store = FileStore(tmp_path_factory.mktemp("demo"))
    for week in demo_season.WEEKS[1:]:
        doc = compute_weekly_facts(
            store,
            demo_season.DEMO_LEAGUE_ID,
            week,
            demo_season.load_week_bundle(week),
            demo_season.load_league_bundle(),
            seeding=None,
            hold_on_cross_check=True,
            logger=LOG,
        ).doc
        assert doc.playoff_odds is not None, week
        surfaces = _surfaces(doc)
        assert 'class="po-row"' in surfaces["web"], week
        assert "Playoff odds" in surfaces["email"] and "Playoff odds" in surfaces["text"], week
        assert utf16_len(surfaces["discord"]) <= 2000, week
