"""Story 9B.5: the accuracy line is defined once and printed on every Issue surface."""

from __future__ import annotations

import json
from typing import Any

import pytest

from commishdesk.facts import accuracy_line as al
from commishdesk.facts.schema import DraftRecapFacts, WeeklyFacts
from commishdesk.narrate import render_draft_recap
from commishdesk.narrate.weekly_template import render_weekly_issue, weekly_issue_to_text
from commishdesk.render import (
    render_discord_summary,
    render_email,
    render_weekly_discord_post,
    render_weekly_email,
    render_weekly_text,
    render_weekly_web,
)
from commishdesk.render.web import render_web
from tests.conftest import REPO_ROOT
from tests.test_render_weekly_web import _STAMP, _raw, _week01_raw

LINE = "Real numbers from your league, every week."
_RECAP = REPO_ROOT / "tests" / "fixtures" / "facts" / "expected-draft-recap-facts.json"


def _weekly(raw: dict[str, Any]) -> tuple[WeeklyFacts, Any]:
    facts = WeeklyFacts.model_validate(raw)
    return facts, render_weekly_issue(facts.narration)


def _recap_facts(credit: bool) -> DraftRecapFacts:
    facts = DraftRecapFacts.model_validate(json.loads(_RECAP.read_text(encoding="utf-8")))
    source = facts.consensus_source.model_copy(
        update={"name": "league_value", "attribution": "stats: nflverse, CC BY 4.0"}
        if credit
        else {"attribution": None}
    )
    return facts.model_copy(update={"consensus_source": source})


def _surfaces(raw: dict[str, Any]) -> dict[str, str]:
    facts, issue = _weekly(raw)
    parts = render_weekly_email(facts, issue, generated_at=_STAMP)
    web = render_weekly_web(facts, issue, output_id="x", generated_at=_STAMP, enhancer=None)
    return {
        "web": web,
        "email_html": parts.html,
        "email_text": parts.text,
        "text": render_weekly_text(facts, issue),
        "discord": render_weekly_discord_post(facts, issue, issue_url="https://x/y"),
    }


def test_the_line_is_pinned_exactly() -> None:
    assert al.ACCURACY_LINE == LINE
    assert al.ACCURACY_LINK_TEXT == "How we compute"
    assert al.DOCS_INDEX == "/docs/"
    lowered = al.ACCURACY_LINE.lower()
    for banned in ("sleeper data only", "real data", "sleeper", "espn", "yahoo", "fantasypros", "nfl.com"):
        assert banned not in lowered


@pytest.mark.parametrize("raw_fn", [_raw, _week01_raw], ids=["week10", "cold-start"])
def test_every_weekly_surface_carries_the_line_once(raw_fn: Any) -> None:
    for name, out in _surfaces(raw_fn()).items():
        assert out.count(LINE) == 1, name


def test_placement_and_links() -> None:
    out = _surfaces(_raw())
    assert out["text"].endswith(f"\n{LINE}\n")
    assert out["email_text"].rstrip().endswith(LINE)
    assert out["discord"].endswith(f"\n\n-# {LINE}\n\nFull Issue → https://x/y")
    assert f'{LINE} <a href="/docs/">How we compute</a></p></footer>' in out["web"]
    assert f'{LINE} <a href="{{{{commishdesk:site-url}}}}/docs/"' in out["email_html"]
    assert ">How we compute</a>" in out["email_html"]
    assert "How we compute" not in out["text"] + out["email_text"] + out["discord"]
    for gone in ("computed from the league", "Sleeper record"):
        assert gone not in out["web"] and gone not in out["email_html"] and gone not in out["email_text"]
    assert "The prose is the narrator" in out["web"] and "The prose is the narrator" in out["email_html"]


def test_the_line_stays_out_of_the_narration() -> None:
    facts, issue = _weekly(_raw())
    assert LINE not in weekly_issue_to_text(issue)
    assert LINE not in json.dumps(facts.narration.model_dump(mode="json"))
    assert LINE not in json.dumps(issue.model_dump(mode="json"))


def test_discord_keeps_the_line_with_a_long_league_name() -> None:
    raw = _raw()
    raw["league"]["name"] = "Long League Name " * 25
    facts, issue = _weekly(raw)
    url = "https://example.com/i/" + "x" * 80
    post = render_weekly_discord_post(facts, issue, issue_url=url)
    assert len(post.encode("utf-16-le")) // 2 <= 2000
    assert post.endswith(f"\n\n-# {LINE}\n\nFull Issue → {url}")


@pytest.mark.parametrize("credit", [True, False])
def test_draft_recap_surfaces_carry_the_line_above_the_credit(credit: bool) -> None:
    facts = _recap_facts(credit)
    recap = render_draft_recap(facts.narration)
    web = render_web(facts, recap=recap, output_id="x", generated_at=_STAMP)
    email = render_email(facts, recap=recap, generated_at=_STAMP)
    discord = render_discord_summary(facts, recap=recap)
    assert web.count(LINE) == 1 and email.html.count(LINE) == 1 and email.text.count(LINE) == 1
    assert discord.endswith(f"\n\n{LINE}") and len(discord) < 2000
    for page in (web, email.html):
        assert "How we compute" not in page
        if credit:
            assert page.index(LINE) < page.index("stats: nflverse")
        else:
            assert "nflverse" not in page.split(LINE)[1].split("</footer>")[0].lower()
