"""Story 6.0b — stable section ids and per-section template fallback (AD-30)."""

from __future__ import annotations

import json
import re

import pytest

from commishdesk.facts import build_weekly_facts
from commishdesk.facts.schema import WeeklyNarration
from commishdesk.ingest import (
    build_league_model,
    build_player_names,
    build_player_snapshot,
    build_week_model,
)
from commishdesk.llmconfig import LLMConfig, LLMModelConfig
from commishdesk.narrate.llm import build_weekly_payload, narrate_weekly_issue
from commishdesk.narrate.weekly_template import (
    COLD_START_SECTION_HEADINGS,
    SECTION_HEADINGS,
    render_weekly_issue,
    section_headings_for_has_prior_week,
    weekly_issue_from_text,
    weekly_issue_to_text,
)
from commishdesk.sections import (
    COLD_START_SECTION_IDS,
    SECTION_IDS,
    effective_suppressions,
    heading_for,
)
from commishdesk.voices.beat_writer import (
    BEAT_WRITER_WEEKLY,
    BEAT_WRITER_WEEKLY_COLD_START,
    weekly_voice_for,
)
from tests.conftest import REPO_ROOT

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "week10-blowout.json"
CONFIG = LLMConfig(
    primary=LLMModelConfig("anthropic", "claude-sonnet-5"),
    fallback=LLMModelConfig("google", "gemini-3.5-flash"),
)


@pytest.fixture(scope="module")
def narration() -> WeeklyNarration:
    bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))
    doc = build_weekly_facts(
        build_week_model(bundle),
        build_league_model(bundle),
        build_player_snapshot(bundle),
        build_player_names(bundle),
        generated_at="2026-09-07T00:00:00Z",
        nfl_byes_next_week=frozenset({"IND", "NO"}),
    )
    return doc.narration


class _Client:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def generate(self, payload: str, voice: object) -> str:
        self.calls.append((payload, getattr(voice, "system_prompt", "")))
        return self.reply


def _text_without(narration: WeeklyNarration, *ids: str, has_prior_week: bool = True) -> str:
    issue = render_weekly_issue(narration, has_prior_week=has_prior_week)
    dropped = {heading_for(i, cold_start=not has_prior_week) for i in ids}
    return weekly_issue_to_text(
        issue.model_copy(update={"sections": [s for s in issue.sections if s.heading not in dropped]})
    )


# --- the id table ----------------------------------------------------------


def test_every_section_has_one_of_the_seven_ids_and_standings_is_shared(narration) -> None:
    assert SECTION_IDS == ("lead", "around_league", "standings", "power", "luck", "next_week", "transactions")
    warm = render_weekly_issue(narration, has_prior_week=True)
    cold = render_weekly_issue(narration, has_prior_week=False)
    assert tuple(s.section_id for s in warm.sections) == SECTION_IDS
    assert tuple(s.section_id for s in cold.sections) == COLD_START_SECTION_IDS
    assert heading_for("standings") != heading_for("standings", cold_start=True)
    assert warm.sections[2].section_id == cold.sections[2].section_id == "standings"


def test_heading_tuples_and_prompts_derive_from_the_table() -> None:
    assert SECTION_HEADINGS == tuple(heading_for(i) for i in SECTION_IDS)
    assert COLD_START_SECTION_HEADINGS == tuple(heading_for(i, cold_start=True) for i in COLD_START_SECTION_IDS)
    for heading in SECTION_HEADINGS:
        assert f"## {heading}\n" in BEAT_WRITER_WEEKLY.system_prompt
    for heading in COLD_START_SECTION_HEADINGS:
        assert f"## {heading}\n" in BEAT_WRITER_WEEKLY_COLD_START.system_prompt


# --- byte identity when nothing is suppressed ------------------------------


def test_nothing_suppressed_changes_nothing(narration) -> None:
    text = weekly_issue_to_text(render_weekly_issue(narration))
    base = weekly_issue_from_text(text, narration)
    assert base is not None
    for empty in (None, set(), frozenset()):
        assert weekly_issue_from_text(text, narration, suppressed=empty) == base
        assert section_headings_for_has_prior_week(True, empty) == SECTION_HEADINGS
        assert build_weekly_payload(narration, suppressed=empty) == build_weekly_payload(narration)
    assert weekly_voice_for(cold_start=False) is BEAT_WRITER_WEEKLY
    assert weekly_voice_for(cold_start=True) is BEAT_WRITER_WEEKLY_COLD_START
    # an id this Issue does not carry changes nothing either
    assert build_weekly_payload(narration, cold_start=True, suppressed={"power"}) == build_weekly_payload(
        narration, cold_start=True
    )
    assert effective_suppressions({"power"}, False) == frozenset()


# --- suppressed sections ---------------------------------------------------


def test_warm_power_suppressed_is_absent_from_the_request_and_spliced_from_the_template(narration) -> None:
    template = render_weekly_issue(narration)
    client = _Client(_text_without(narration, "power"))
    voice = weekly_voice_for(cold_start=False, suppressed=frozenset({"power"}))
    result = narrate_weekly_issue(
        narration,
        voice,
        CONFIG,
        llm_enabled=True,
        client_factory=lambda cfg: client,
        suppressed={"power"},
    )
    assert result.narrator == "llm-primary" and len(client.calls) == 1
    payload, prompt = client.calls[0]
    assert "power" not in json.loads(payload)
    assert "Power Rankings is a numbered list" not in prompt
    assert "exactly these six sections" in prompt
    for heading in SECTION_HEADINGS:
        assert (f"## {heading}\n" in prompt) == (heading != "Power Rankings")

    issue = weekly_issue_from_text(result.text, narration, suppressed={"power"})
    assert issue is not None
    assert tuple(s.heading for s in issue.sections) == SECTION_HEADINGS
    assert issue.sections[3] == template.sections[3]
    assert "points a week" in " ".join(issue.sections[3].blocks)


def test_the_validator_accepts_the_shrunken_set_and_rejects_a_missing_unsuppressed_section(narration) -> None:
    assert weekly_issue_from_text(_text_without(narration, "power"), narration) is None
    assert weekly_issue_from_text(_text_without(narration, "power"), narration, suppressed={"power"}) is not None
    assert weekly_issue_from_text(_text_without(narration, "power", "luck"), narration, suppressed={"power"}) is None


def test_a_suppressed_heading_the_model_writes_anyway_loses_to_the_template(narration) -> None:
    template = render_weekly_issue(narration)
    text = weekly_issue_to_text(template).replace("## Power Rankings\n\n", "## Power Rankings\n\nModel prose. ", 1)
    issue = weekly_issue_from_text(text, narration, suppressed={"power"})
    assert issue is not None and issue.sections[3] == template.sections[3]


def test_lead_suppressed_comes_from_the_template_rest_from_the_llm(narration) -> None:
    template = render_weekly_issue(narration)
    llm_text = _text_without(narration, "lead").replace("## Next Week\n\n", "## Next Week\n\nThe LLM wrote this. ")
    issue = weekly_issue_from_text(llm_text, narration, suppressed={"lead"})
    assert issue is not None
    assert issue.sections[0] == template.sections[0] and issue.sections[0].blocks
    assert issue.sections[5].blocks[0].startswith("The LLM wrote this.")


def test_cold_start_ignores_power_and_still_rejects_extra_sections(narration) -> None:
    cold_text = _text_without(narration, has_prior_week=False)
    cold_template = render_weekly_issue(narration, has_prior_week=False)
    base = weekly_issue_from_text(cold_text, narration, has_prior_week=False)
    assert base == weekly_issue_from_text(cold_text, narration, has_prior_week=False, suppressed={"power"})
    assert base is not None and base.sections == cold_template.sections
    warm_text = weekly_issue_to_text(render_weekly_issue(narration))
    assert weekly_issue_from_text(warm_text, narration, has_prior_week=False, suppressed={"power"}) is None


def test_cold_start_standings_suppressed_uses_the_cold_heading(narration) -> None:
    cold_template = render_weekly_issue(narration, has_prior_week=False)
    text = _text_without(narration, "standings", has_prior_week=False)
    issue = weekly_issue_from_text(text, narration, has_prior_week=False, suppressed={"standings"})
    assert issue is not None and issue.sections == cold_template.sections
    assert issue.sections[2].heading == "Standings"


def test_all_suppressed_makes_zero_llm_calls_and_is_the_template(narration) -> None:
    client = _Client("never used")
    result = narrate_weekly_issue(
        narration,
        BEAT_WRITER_WEEKLY,
        CONFIG,
        llm_enabled=True,
        client_factory=lambda cfg: client,
        suppressed=set(SECTION_IDS),
    )
    assert client.calls == []
    assert result.narrator == "template"
    assert result.text == weekly_issue_to_text(render_weekly_issue(narration))


def test_payload_drops_only_the_suppressed_sections_data(narration) -> None:
    decoded = json.loads(build_weekly_payload(narration, suppressed={"power", "luck", "transactions"}))
    full = json.loads(build_weekly_payload(narration))
    assert set(full) - set(decoded) == {"power", "luck", "transactions"}
    assert {k: v for k, v in full.items() if k in decoded} == decoded


def test_voice_prompt_for_each_single_suppression_lists_the_right_headings() -> None:
    for section_id in SECTION_IDS:
        prompt = weekly_voice_for(cold_start=False, suppressed=frozenset({section_id})).system_prompt
        listed = re.findall(r"^   ## (.+)$", prompt, flags=re.MULTILINE)
        assert listed == [h for h in SECTION_HEADINGS if h != heading_for(section_id)]
