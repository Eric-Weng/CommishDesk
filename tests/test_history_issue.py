"""Story 9B.4: history callbacks in the Issue and the narration.

One wording function (``facts/history_wording.py``), printed on every weekly surface at the 9B.1 canvas
placement (G2-3); the narrator's ``history`` projection and its cap tier; the deterministic claim check
over LLM prose; the ``record_set`` lead candidate.
"""

from __future__ import annotations

import copy
import html
import json
import re
from pathlib import Path
from typing import Any

import pytest

from commishdesk.facts import history_wording as hw
from commishdesk.facts.accuracy_line import ACCURACY_LINE
from commishdesk.facts.leads import WEEKLY_LEAD_KIND_PRIORITY, build_weekly_lead_candidates
from commishdesk.facts.schema import (
    SCHEMA_VERSION,
    WeeklyFacts,
    WeeklyHistoryBlock,
    WeeklyHistoryCallback,
    WeeklyHistoryH2H,
    WeeklyHistoryMatchup,
    WeeklyHistoryRecord,
    WeeklyNarration,
)
from commishdesk.facts.weekly import _apply_weekly_narration_cap
from commishdesk.narrate.llm import build_weekly_payload
from commishdesk.narrate.verify import history_claim_findings
from commishdesk.narrate.weekly_template import render_weekly_issue, weekly_issue_to_text
from commishdesk.render import _weekly_model as wm
from commishdesk.render import discord as dmod
from commishdesk.render import (
    render_weekly_discord_post,
    render_weekly_email,
    render_weekly_text,
    render_weekly_web,
)
from tests.conftest import REPO_ROOT

FACTS_DIR = REPO_ROOT / "tests" / "fixtures" / "facts"
RAW = json.loads((FACTS_DIR / "expected-weekly-facts-week10.json").read_text(encoding="utf-8"))
RAW_WEEK01 = json.loads((FACTS_DIR / "expected-weekly-facts-week01.json").read_text(encoding="utf-8"))

# the week-10 pairings in the fixture: roster 1 (Flea Flicker Union) beat 5 (Nickel Newts), 3 beat 6, ...
LEFT, RIGHT = "1", "5"
OTHER_LEFT, OTHER_RIGHT = "3", "6"


def _cb(kind: str, wording: str, **kw: Any) -> WeeklyHistoryCallback:
    tier = {"record_set": 0, "record_tied": 0, "record_within_reach": 2, "h2h_streak": 2, "series_lead": 3}.get(kind, 1)
    return WeeklyHistoryCallback(kind=kind, tier=tier, all_time=wording != "this_season", wording=wording, **kw)


def _h2h(a_wins: int, b_wins: int, seasons: list[int], *, ties: int = 0) -> WeeklyHistoryH2H:
    return WeeklyHistoryH2H(
        a_manager_id="ma",
        b_manager_id="mb",
        a_wins=a_wins,
        b_wins=b_wins,
        ties=ties,
        playoff_a_wins=0,
        playoff_b_wins=0,
        playoff_ties=0,
        meetings=a_wins + b_wins + ties,
        seasons=seasons,
    )


def _block(
    wording: str = "all_time",
    *,
    matchup_cb: WeeklyHistoryCallback | None = None,
    league: list[WeeklyHistoryCallback] | None = None,
    seasons: list[int] | None = None,
    h2h: WeeklyHistoryH2H | None = None,
    pair: tuple[str, str] = (LEFT, RIGHT),
) -> WeeklyHistoryBlock:
    seasons = seasons or ([2025] if wording == "this_season" else [2022, 2023, 2024, 2025])
    return WeeklyHistoryBlock(
        all_time=wording != "this_season",
        wording=wording,  # type: ignore[arg-type]
        seasons_covered=seasons,
        seasons_on_record=len(seasons),
        partial=wording == "seasons_on_record",
        matchups=[
            WeeklyHistoryMatchup(
                when="this_week",
                week=10,
                a_roster_id=pair[0],
                b_roster_id=pair[1],
                a_points=229.96,
                b_points=91.77,
                winner_roster_id=LEFT,
                h2h=h2h,
                callback=matchup_cb,
            )
        ],
        league_lines=league or [],
    )


def _facts(block: WeeklyHistoryBlock | None, *, raw: dict[str, Any] | None = None) -> WeeklyFacts:
    doc = copy.deepcopy(raw or RAW)
    doc["history"] = block.model_dump(mode="json") if block is not None else None
    return WeeklyFacts.model_validate(doc)


def _series(wording: str) -> WeeklyHistoryBlock:
    return _block(
        wording,
        h2h=_h2h(5, 2, [2022, 2023, 2024, 2025]) if wording != "this_season" else _h2h(2, 1, [2025]),
        matchup_cb=_cb("series_lead", wording, roster_id=LEFT, opponent_roster_id=RIGHT, value=3.0),
        league=[
            _cb(
                "record_set",
                wording,
                roster_id=OTHER_LEFT,
                metric="highest_score",
                value=247.2,
                record=WeeklyHistoryRecord(
                    metric="highest_score", value=240.1, manager_id="m3", roster_id="3", season=2024, week=4
                ),
            )
        ],
    )


def _label(facts: WeeklyFacts):
    return wm.labeller(facts)


def _web(facts: WeeklyFacts) -> str:
    return render_weekly_web(facts, render_weekly_issue(facts.narration), output_id="x", generated_at="t")


def _email(facts: WeeklyFacts):
    return render_weekly_email(facts, render_weekly_issue(facts.narration), generated_at="t")


def _discord(facts: WeeklyFacts) -> str:
    return render_weekly_discord_post(facts, render_weekly_issue(facts.narration))


def _text(facts: WeeklyFacts) -> str:
    return render_weekly_text(facts, render_weekly_issue(facts.narration))


def _web_lines(page: str) -> list[str]:
    inner = re.findall(r'<p class="game-hist"><span class="hk">History</span>(.*?)</p>', page)
    return [html.unescape(re.sub(r"<[^>]+>", "", item)) for item in inner]


# --------------------------------------------------------------------------- #
# The wording: four states, digits only, closed world
# --------------------------------------------------------------------------- #


def test_the_schema_version_is_the_minor_bump() -> None:
    assert SCHEMA_VERSION == "0.18.0"


def test_multi_season_series_line_says_all_time() -> None:
    facts = _facts(_series("all_time"))
    lines = hw.matchup_lines(facts.history, _label(facts))
    assert lines[frozenset({LEFT, RIGHT})] == (
        "Series: Flea Flicker Union 5-2 over Nickel Newts over 4 seasons, all-time."
    )


def test_one_season_never_says_all_time_or_zero_zero() -> None:
    facts = _facts(_series("this_season"))
    lines = [*hw.matchup_lines(facts.history, _label(facts)).values(), *hw.league_lines(facts.history, _label(facts))]
    assert lines
    for line in lines:
        assert "all-time" not in line.lower() and "0-0" not in line
        assert line.endswith("this season.")


def test_partial_chain_says_seasons_on_record_and_carries_the_note() -> None:
    block = _series("seasons_on_record")
    block = block.model_copy(update={"seasons_covered": [2024, 2025], "seasons_on_record": 2})
    facts = _facts(block)
    lines = [*hw.matchup_lines(facts.history, _label(facts)).values(), *hw.league_lines(facts.history, _label(facts))]
    assert lines and all("in 2 seasons on record." in line and "all-time" not in line for line in lines)
    assert hw.partial_note(facts.history) == "History covers 2024-2025. Earlier seasons aren't linked on Sleeper."
    assert hw.partial_note(_facts(_series("all_time")).history) is None


def test_no_callback_gives_no_lines() -> None:
    facts = _facts(_block("all_time", h2h=_h2h(5, 2, [2024, 2025])))
    assert hw.matchup_lines(facts.history, _label(facts)) == {}
    assert hw.league_lines(facts.history, _label(facts)) == []
    assert wm.history_view(facts) is None
    assert hw.matchup_lines(None, _label(facts)) == {} and hw.league_lines(None, _label(facts)) == []


@pytest.mark.parametrize(
    ("callback", "expected"),
    [
        (
            _cb("record_set", "all_time", roster_id="1", metric="highest_score", value=247.2),
            "Flea Flicker Union's 247.20 is the league's highest score, all-time.",
        ),
        (
            _cb("record_tied", "this_season", roster_id="1", metric="lowest_score", value=61.5),
            "Flea Flicker Union's 61.50 ties the league's lowest score, this season.",
        ),
        (
            _cb("record_set", "all_time", roster_id="1", metric="biggest_margin", value=138.19),
            "Flea Flicker Union's 138.19-point margin is the league's biggest, all-time.",
        ),
        (
            _cb("record_set", "seasons_on_record", roster_id="11", metric="loss_streak", value=7.0),
            "Bubble-Screen Bobcats' 7-game losing streak is the league's longest, in 3 seasons on record.",
        ),
        (
            _cb(
                "record_within_reach",
                "all_time",
                roster_id="1",
                metric="win_streak",
                value=5.0,
                record=WeeklyHistoryRecord(metric="win_streak", value=6.0, manager_id="m", season=2023, week=9),
            ),
            "One more win ties Flea Flicker Union with the league's longest win streak, 6 games, all-time.",
        ),
        (
            _cb("h2h_streak", "all_time", roster_id="1", opponent_roster_id="5", value=4.0),
            "Flea Flicker Union won the last 4 meetings with Nickel Newts, all-time.",
        ),
        (
            _cb("first_win_ever", "all_time", roster_id="1", opponent_roster_id="5"),
            "Flea Flicker Union beat Nickel Newts for the first time ever.",
        ),
        (
            _cb("first_win_since", "all_time", roster_id="1", opponent_roster_id="5", since_season=2022),
            "Flea Flicker Union beat Nickel Newts for the first time since 2022.",
        ),
        (
            _cb("first_top_ever", "seasons_on_record", roster_id="2"),
            "Two Minute Drill moved into first place for the first time in 3 seasons on record.",
        ),
        (
            _cb("first_top_since", "all_time", roster_id="2", since_season=2023),
            "Two Minute Drill moved into first place for the first time since 2023.",
        ),
    ],
)
def test_each_kind_is_worded_from_facts_keys_with_digits(callback: WeeklyHistoryCallback, expected: str) -> None:
    block = _block(callback.wording, seasons=[2023, 2024, 2025] if callback.wording != "this_season" else [2025])
    facts = _facts(block)
    assert hw.callback_line(block, callback, _label(facts)) == expected
    assert not re.search(r"\b(?:three|four|five|six|seven|eight|nine|ten)\b", expected.lower())  # digits, not words


def test_a_callback_the_keys_cannot_word_is_never_guessed() -> None:
    block = _block("all_time")
    since_without_a_season = _cb("first_win_since", "all_time", roster_id="1", opponent_roster_id="5")
    assert hw.callback_line(block, since_without_a_season, str) is None
    assert hw.callback_line(block, _cb("record_set", "all_time", roster_id="1"), str) is None
    assert hw.callback_line(block, _cb("mystery", "all_time", roster_id="1"), str) is None


# --------------------------------------------------------------------------- #
# Every surface prints the same line at the canvas placement
# --------------------------------------------------------------------------- #


def test_every_surface_prints_the_same_lines() -> None:
    facts = _facts(_series("all_time"))
    game_line = hw.matchup_lines(facts.history, _label(facts))[frozenset({LEFT, RIGHT})]
    league_line = hw.league_lines(facts.history, _label(facts))[0]

    page = _web(facts)
    assert _web_lines(page) == [game_line]  # one line, under its matchup's card
    league_html = re.search(r'<div class="hist-league">(.*?)</div>', page, re.S)
    assert league_html is not None
    assert html.unescape(re.sub(r"<[^>]+>", "", league_html.group(1))) == f"League history{league_line}"

    email = _email(facts)
    assert html.escape(game_line) in email.html and html.escape(league_line) in email.html
    assert game_line in email.text and league_line in email.text
    assert "League history" in email.html and "League history" in email.text

    text = _text(facts)
    assert game_line in text and league_line in text and "League history" in text

    post = _discord(facts)
    assert f"• {game_line}" in post and f"• {league_line}" in post and "📜 **History**" in post


def test_the_game_line_sits_under_its_own_game() -> None:
    facts = _facts(_series("all_time"))
    page = _web(facts)
    card = re.search(r'<article class="card game">(?:(?!</article>).)*?game-hist.*?</article>', page, re.S)
    assert card is not None
    assert "Flea Flicker Union" in card.group(0) and "Nickel Newts" in card.group(0)
    assert page.count('class="game-hist"') == 1  # only the one game has a callback
    email = _email(facts)
    assert email.text.index("Flea Flicker Union 229.96 over Nickel Newts 91.77") < email.text.index("Series: ")
    # the lines under the games come before the league block
    assert email.text.index("Series: ") < email.text.index("League history")


def test_a_one_season_league_prints_no_all_time_and_no_zero_zero_on_any_surface() -> None:
    facts = _facts(_series("this_season"))
    outputs = [_web(facts), _email(facts).html, _email(facts).text, _text(facts), _discord(facts)]
    for out in outputs:
        assert "all-time" not in out.lower()
        assert "0-0" not in out
    assert "this season." in _text(facts)


def test_no_callback_renders_no_heading_no_box_no_gap() -> None:
    empty = _facts(_block("all_time", h2h=_h2h(5, 2, [2024, 2025])))
    none = _facts(None)
    # a block with nothing to say renders exactly like no block at all, on every surface
    assert _web(empty) == _web(none)
    assert _email(empty) == _email(none)
    # with no history lines the text is the narrated text plus the accuracy line, nothing else
    plain = weekly_issue_to_text(render_weekly_issue(none.narration))
    assert _text(empty) == _text(none) == f"{plain}\n{ACCURACY_LINE}\n"
    assert _discord(empty) == _discord(none)
    assert "game-hist" not in _web(none) and "League history" not in _web(none)
    assert "📜" not in _discord(none)


def test_week_one_prints_no_history_anywhere() -> None:
    raw = copy.deepcopy(RAW_WEEK01)
    game = raw["matchups"]["this_week"][0]
    pair = (game["home_roster_id"], game["away_roster_id"])
    callback = _cb("record_set", "all_time", roster_id=pair[0], metric="highest_score", value=200.0)
    raw["history"] = _block("all_time", matchup_cb=callback, league=[callback], pair=pair).model_dump(mode="json")
    facts = WeeklyFacts.model_validate(raw)
    assert not facts.period.has_prior_week
    assert wm.history_view(facts) is None
    assert "game-hist" not in _web(facts) and "League history" not in _web(facts)
    assert "📜" not in _discord(facts) and "League history" not in _email(facts).text


def test_a_league_line_that_repeats_a_game_line_is_printed_once() -> None:
    same = _cb("record_set", "all_time", roster_id=LEFT, metric="biggest_margin", value=138.19)
    facts = _facts(_block("all_time", matchup_cb=same, league=[same]))
    view = wm.history_view(facts)
    assert view is not None and len(view.by_pair) == 1 and view.league == []
    assert "League history" not in _text(facts)


def test_discord_escapes_names_and_trims_the_history_before_the_results() -> None:
    raw = copy.deepcopy(RAW)
    for team in raw["teams"]:
        if team["roster_id"] == LEFT:
            team["team_name"] = "*Flea_Flicker* [Union]"
    for row in raw["narration"]["standings"]:
        if row["roster_id"] == LEFT:
            row["team"] = "*Flea_Flicker* [Union]"
    block = _series("all_time")
    facts = _facts(block, raw=raw)
    post = _discord(facts)
    assert "\\*Flea\\_Flicker\\* \\[Union]" in post.split("📜 **History**")[1]  # `[` escaped, so no masked link
    assert "*Flea_Flicker*" not in post.split("📜 **History**")[1]

    # trim order: history goes right after the odds and before the awards, the optional lines and the results
    assert dmod._DROP_ODDS < dmod._DROP_HISTORY < dmod._DROP_AWARDS < dmod._DROP_RESULTS
    blocks = [dmod._weekly_results(facts), dmod._weekly_history(facts)]
    assert all(b is not None for b in blocks)
    results, history = blocks  # type: ignore[misc]
    tight = dmod.utf16_len(dmod._join([results])) + 1
    kept = dmod._trim([results, history], tight)
    assert [b.text.split("\n")[0] for b in kept] == ["🏈 **Results**"]


def test_the_static_page_carries_the_history_css_only_when_it_prints_a_line() -> None:
    with_lines = _web(_facts(_series("all_time")))
    without = _web(_facts(None))
    assert ".game-hist" in with_lines and ".game-hist" not in without


# --------------------------------------------------------------------------- #
# The narration projection and its cap tier
# --------------------------------------------------------------------------- #


def test_the_projection_is_numbers_and_labels_from_the_block() -> None:
    built =WeeklyFacts.model_validate(RAW)
    projection = built.narration.history
    assert projection is not None and projection.wording == "this_season" and projection.seasons_covered == [2025]
    first = projection.matchups[0]
    assert first.a == "Flea Flicker Union" and first.a_roster_id == "1" and first.b == "Nickel Newts"
    assert first.callback is not None and first.callback.kind == "record_set"
    assert first.callback.record_value == 104.48 and first.callback.record_season == 2025
    dumped = projection.model_dump_json()
    assert "all-time" not in dumped and "prose" not in dumped


def test_week_one_projection_and_payload_omit_history() -> None:
    narration = WeeklyFacts.model_validate(RAW_WEEK01).narration
    assert narration.history is None
    cold = json.loads(build_weekly_payload(WeeklyFacts.model_validate(RAW).narration, cold_start=True))
    assert "history" not in cold
    warm = json.loads(build_weekly_payload(WeeklyFacts.model_validate(RAW).narration))
    assert warm["history"]["matchups"][0]["callback"]["kind"] == "record_set"


def test_the_cap_drops_history_before_the_odds_and_never_touches_the_lead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import commishdesk.facts.weekly as weekly_mod
    from commishdesk.facts.schema import WeeklyNarrationOdds

    narration = WeeklyFacts.model_validate(RAW).narration
    narration = narration.model_copy(update={"playoff_odds": WeeklyNarrationOdds()})
    assert narration.history is not None
    size = len(narration.model_dump_json())
    monkeypatch.setattr(weekly_mod, "_current_cap", lambda: size - 1)
    monkeypatch.setattr(weekly_mod, "_within_cap", lambda m: len(m.model_dump_json()) <= size - 1)
    out = _apply_weekly_narration_cap(narration)
    assert out.history is None
    assert out.playoff_odds is not None  # the odds were not the first tier any more
    assert out.lead_candidates == narration.lead_candidates and out.standings == narration.standings
    assert out.games == narration.games


def test_still_over_the_cap_after_the_final_tier_is_a_schema_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import commishdesk.facts.weekly as weekly_mod
    from commishdesk.errors import SchemaValidationError

    narration = WeeklyFacts.model_validate(RAW).narration
    monkeypatch.setattr(weekly_mod, "_current_cap", lambda: 10)
    monkeypatch.setattr(weekly_mod, "_within_cap", lambda m: False)
    with pytest.raises(SchemaValidationError):
        _apply_weekly_narration_cap(narration)


def test_the_prompt_names_the_history_rule_only_when_warm() -> None:
    from commishdesk.voices.beat_writer import weekly_voice_for

    warm = weekly_voice_for(cold_start=False).system_prompt
    assert "League history." in warm and "``wording``" in warm
    assert "League history." not in weekly_voice_for(cold_start=True).system_prompt


# --------------------------------------------------------------------------- #
# The claim check over LLM prose
# --------------------------------------------------------------------------- #


def _projected(block: WeeklyHistoryBlock | None) -> WeeklyNarration:
    """The week-10 narration carrying the projection of *block* (none for ``None``)."""
    from commishdesk.facts.weekly import _history_narration

    narration = WeeklyFacts.model_validate(RAW).narration
    labels = {row.roster_id: row.team for row in narration.standings}
    return narration.model_copy(update={"history": _history_narration(block, labels)})


def _first_since_block() -> WeeklyHistoryBlock:
    return _block(
        "all_time",
        h2h=_h2h(4, 1, [2022, 2023, 2024, 2025]),
        matchup_cb=_cb("first_win_since", "all_time", roster_id=LEFT, opponent_roster_id=RIGHT, since_season=2022),
        league=[_cb("record_set", "all_time", roster_id=OTHER_LEFT, metric="highest_score", value=247.2)],
    )


def test_a_wrong_first_since_season_is_a_hallucination_finding() -> None:
    narration = _projected(_first_since_block())
    bad = history_claim_findings("Flea Flicker Union notched a first win since 2019 over Nickel Newts.", narration)
    assert bad and all(f.category == "hallucination" and f.severity == "regenerate" for f in bad)
    assert "2019" in bad[0].message
    good = history_claim_findings("Flea Flicker Union notched a first win since 2022 over Nickel Newts.", narration)
    assert good == ()


def test_supported_claims_pass_and_unsupported_ones_do_not() -> None:
    narration = _projected(_first_since_block())
    ok = (
        "Backpedal Buffalo set the league record with 247.20, the highest score all-time. "
        "Flea Flicker Union lead the series 4-1."
    )
    assert history_claim_findings(ok, narration) == ()
    assert history_claim_findings("Their 7-3 record is the best in the division.", narration) == ()  # not history
    assert history_claim_findings("They lead the series 6-0 all-time.", narration)  # no such series
    assert history_claim_findings("It is their lowest score, a league record.", narration)  # no lowest-score record
    assert history_claim_findings("A first time in 9 seasons for the Stags.", narration)  # N != seasons
    assert history_claim_findings("He went on record about the trade.", narration) == ()  # an idiom, not a claim
    assert history_claim_findings("A win streak of 3 meetings in a row for the Stags.", narration)  # no such streak
    assert history_claim_findings("Nine meetings and nine straight meetings for them.", narration)


def test_all_time_and_on_record_need_the_projection() -> None:
    one_season = _projected(_series("this_season"))
    assert history_claim_findings("Best score all-time for the Otters.", one_season)
    assert history_claim_findings("Best score in 5 seasons on record.", one_season)
    assert history_claim_findings("Best score in 5 seasons on record.", _projected(None))  # no history at all
    none = _projected(None)
    assert history_claim_findings("Their first win ever, and a league record.", none)
    assert history_claim_findings("A quiet week with no history claims at all, 7-3.", none) == ()
    allt = _projected(_series("all_time"))
    assert history_claim_findings("A new all-time high for the Buffalo.", allt) == ()


def test_a_within_reach_callback_does_not_back_a_record_set_claim() -> None:
    reach = _block(
        "all_time",
        league=[
            _cb(
                "record_within_reach",
                "all_time",
                roster_id=LEFT,
                metric="win_streak",
                value=5.0,
                record=WeeklyHistoryRecord(metric="win_streak", value=6.0, manager_id="m", season=2023, week=9),
            )
        ],
    )
    assert history_claim_findings("Their win streak broke the league record.", _projected(reach))


def test_the_cli_prints_the_history_aware_text_issue(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI's stdout and ``.txt`` come from ``render_weekly_text`` (the one that prints history lines)."""
    import commishdesk.render as render
    from tests.test_cli_weekly import _stub_adapter, app, runner

    _stub_adapter(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    real = render.render_weekly_text
    seen: list[str] = []

    def spy(doc, issue):  # type: ignore[no-untyped-def]
        seen.append(real(doc, issue) + "\nHISTORY-SPY")
        return seen[-1]

    monkeypatch.setattr(render, "render_weekly_text", spy)
    result = runner.invoke(app, ["--league", "77", "--week", "17", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert len(seen) == 1 and "HISTORY-SPY" in result.stdout
    assert any("HISTORY-SPY" in p.read_text(encoding="utf-8") for p in tmp_path.glob("*.txt"))


def test_the_check_is_deterministic_and_makes_no_model_call() -> None:
    narration = _projected(_first_since_block())
    prose = "First win since 2019 and a 9-1 series."
    assert history_claim_findings(prose, narration) == history_claim_findings(prose, narration)


def test_an_llm_claim_the_facts_do_not_back_degrades_to_the_template(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_cli_weekly import _stub_adapter, _stub_weekly_voice, app, runner

    _stub_adapter(monkeypatch)
    voice = _stub_weekly_voice(monkeypatch, invent_history=True)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    result = runner.invoke(app, ["--league", "97", "--week", "17", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert len(voice.calls) == 2  # the one permitted regeneration, then the template
    assert "first win since 2019" not in result.stdout.lower()
    assert "content-safety alert" in result.output


# --------------------------------------------------------------------------- #
# Leads: a record set this week
# --------------------------------------------------------------------------- #


def test_record_set_is_a_lead_candidate_with_a_documented_position() -> None:
    assert WEEKLY_LEAD_KIND_PRIORITY.index("lineup_loss") < WEEKLY_LEAD_KIND_PRIORITY.index("record_set")
    assert WEEKLY_LEAD_KIND_PRIORITY.index("record_set") < WEEKLY_LEAD_KIND_PRIORITY.index("biggest_blowout")
    from commishdesk.facts import leads

    assert "record_set" in Path(leads.__file__).read_text(encoding="utf-8")  # documented beside the priority tuple
    assert "record_set" in (REPO_ROOT / "docs" / "concepts" / "history-callbacks.md").read_text(encoding="utf-8")
    built = WeeklyFacts.model_validate(RAW)
    kinds = [c.kind for c in built.lead_candidates]
    assert kinds[:2] == ["lineup_loss", "record_set"]
    hook = next(c.hook for c in built.lead_candidates if c.kind == "record_set")
    assert hook == "Flea Flicker Union's 138.19-point margin is the league's biggest, this season."


def test_tied_and_within_reach_records_never_lead_and_no_history_leads_as_before() -> None:
    built = WeeklyFacts.model_validate(RAW)
    tied = _block(
        "all_time",
        matchup_cb=_cb("record_tied", "all_time", roster_id=LEFT, metric="highest_score", value=240.1),
        league=[
            _cb(
                "record_within_reach",
                "all_time",
                roster_id=LEFT,
                metric="win_streak",
                value=5.0,
                record=WeeklyHistoryRecord(metric="win_streak", value=6.0, manager_id="m", season=2023, week=9),
            )
        ],
    )
    for history in (tied, None):
        got = build_weekly_lead_candidates(built.teams, built.period, history=history)
        assert "record_set" not in [c.kind for c in got]
    assert build_weekly_lead_candidates(built.teams, built.period) == build_weekly_lead_candidates(
        built.teams, built.period, history=None
    )


def test_several_records_lead_by_the_documented_metric_order() -> None:
    built = WeeklyFacts.model_validate(RAW)
    block = _block(
        "all_time",
        league=[
            _cb("record_set", "all_time", roster_id="11", metric="loss_streak", value=7.0),
            _cb("record_set", "all_time", roster_id="3", metric="highest_score", value=247.2),
        ],
    )
    got = build_weekly_lead_candidates(built.teams, built.period, history=block)
    record = next(c for c in got if c.kind == "record_set")
    assert record.roster_ids == ["3"] and "247.20" in (record.hook or "")
    assert sum(1 for c in got if c.kind == "record_set") == 1
