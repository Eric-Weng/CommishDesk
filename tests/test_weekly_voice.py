"""Story 5.12 — the weekly Voice eval: the committed sample, its scorer, and the
published-rank check.

Groups:

* ``_score_weekly_issue`` — the eval scorer (closed-world by the *production*
  entry point, plus a ``tests/``-local ±15 % length band), mirroring
  ``tests/test_voices.py::_score_recap``;
* the committed ``week10-sample.md`` sample, derived from
  ``tests/fixtures/week10-blowout.json``: closed-world, on length, carrying every
  section heading, with "Around the League" as discrete items;
* :func:`~commishdesk.narrate.published_rank.published_rank_findings` — the one
  new check (a published rank more than ``POWER_NUDGE_CAP`` from the model rank
  is a ``hallucination``-tier finding, so it routes through the existing
  regenerate-once-then-template path);
* the shape helpers the weekly LLM narrator's caller depends on,
  ``weekly_issue_from_text`` / ``parse_published_ranks``.

No live LLM call and no API key are needed anywhere in this file: the sample is
hand-authored to the target Voice, exactly as ``tests/eval/voices/beat-writer.md``
is.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from commishdesk.facts import build_weekly_facts
from commishdesk.facts.schema import (
    WeeklyNarration,
    WeeklyNarrationLeague,
    WeeklyNarrationPower,
    WeeklyNarrationTransactions,
)
from commishdesk.ingest import (
    build_league_model,
    build_player_names,
    build_player_snapshot,
    build_week_model,
)
from commishdesk.narrate import safety
from commishdesk.narrate.published_rank import published_rank_findings
from commishdesk.narrate.weekly_template import (
    SECTION_HEADINGS,
    parse_published_ranks,
    render_weekly_issue,
    weekly_issue_from_text,
    weekly_issue_to_text,
)
from commishdesk.stats.power import POWER_NUDGE_CAP
from tests.conftest import REPO_ROOT

EVAL_DIR = REPO_ROOT / "tests" / "eval" / "weekly"
SAMPLE = EVAL_DIR / "week10-sample.md"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "week10-blowout.json"

#: The length target the recorded sample and a live generation are held to.
#: Documented in ``tests/eval/weekly/README.md``; kept in sync with the number
#: quoted there.
REFERENCE_TARGET_CHARS = 3800

GENERATED_AT = "2026-09-07T00:00:00Z"
BYES_NEXT_WEEK = frozenset({"IND", "NO"})


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def narration() -> WeeklyNarration:
    """The real weekly pipeline off the committed week-10 fixture — the same
    chain ``tests/test_narrate_weekly_template.py`` runs."""
    bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))
    doc = build_weekly_facts(
        build_week_model(bundle),
        build_league_model(bundle),
        build_player_snapshot(bundle),
        build_player_names(bundle),
        generated_at=GENERATED_AT,
        nfl_byes_next_week=BYES_NEXT_WEEK,
    )
    return doc.narration


def _section(heading: str, issue):
    return next(section for section in issue.sections if section.heading == heading)


# --------------------------------------------------------------------------- #
# The scorer
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WeeklyScore:
    """The heuristic verdict on one weekly Issue against a narration payload."""

    char_count: int
    target_chars: int
    length_ok: bool
    closed_world_ok: bool
    unknown_tokens: tuple[str, ...]


def _score_weekly_issue(text: str, narration: WeeklyNarration, *, target_chars: int) -> WeeklyScore:
    """Score one weekly Issue: closed-world by **the production gate**
    (:func:`commishdesk.narrate.safety.closed_world_tokens`, which owns the whole
    normalize → league-name-mask → closed-world pipeline ``check_narration``
    runs), plus a ``tests/``-local length band of ±15% around *target_chars*.

    The closed-world half owns no logic at all — the same A3 lesson
    ``tests/test_voices.py`` records: one implementation, one entry point, called
    from both the shipping gate and this harness, so the two cannot drift apart.
    """
    unknown = safety.closed_world_tokens(text, narration)
    char_count = len(text)
    length_ok = abs(char_count - target_chars) <= round(0.15 * target_chars)
    return WeeklyScore(
        char_count=char_count,
        target_chars=target_chars,
        length_ok=length_ok,
        closed_world_ok=not unknown,
        unknown_tokens=unknown,
    )


def test_scorer_passes_a_clean_in_world_sample(narration: WeeklyNarration) -> None:
    text = weekly_issue_to_text(render_weekly_issue(narration))
    score = _score_weekly_issue(text, narration, target_chars=len(text))
    assert score.closed_world_ok, score.unknown_tokens
    assert score.length_ok


def test_scorer_flags_an_invented_number(narration: WeeklyNarration) -> None:
    text = "Blitz Alpacas scored 8675309 points in the fourth quarter."
    score = _score_weekly_issue(text, narration, target_chars=len(text))
    assert not score.closed_world_ok
    assert "8675309" in score.unknown_tokens


def test_scorer_delegates_closed_world_to_the_production_entry_point(
    narration: WeeklyNarration, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real invariant, not a name check: whatever the production entry point
    says is out of world *is* the scorer's verdict."""
    seen: dict[str, object] = {}

    def _spy(text: str, payload: WeeklyNarration) -> tuple[str, ...]:
        seen["text"] = text
        seen["narration"] = payload
        return ("SENTINEL",)

    monkeypatch.setattr(safety, "closed_world_tokens", _spy)
    score = _score_weekly_issue("The league leader won.", narration, target_chars=25)
    assert seen["text"] == "The league leader won."
    assert seen["narration"] is narration
    assert score.unknown_tokens == ("SENTINEL",)
    assert not score.closed_world_ok


@pytest.mark.parametrize("delta", [-0.16, 0.16])
def test_scorer_rejects_a_length_outside_the_band(narration: WeeklyNarration, delta: float) -> None:
    target = REFERENCE_TARGET_CHARS
    text = "x" * round(target * (1 + delta))
    score = _score_weekly_issue(text, narration, target_chars=target)
    assert not score.length_ok


def test_scorer_accepts_a_length_at_the_edge_of_the_band(narration: WeeklyNarration) -> None:
    target = REFERENCE_TARGET_CHARS
    for chars in (round(target * 0.85) + 1, round(target * 1.15) - 1):
        score = _score_weekly_issue("y" * chars, narration, target_chars=target)
        assert score.length_ok, chars


# --------------------------------------------------------------------------- #
# The committed sample
# --------------------------------------------------------------------------- #


def test_committed_sample_is_closed_world_and_on_length(narration: WeeklyNarration) -> None:
    assert SAMPLE.is_file(), f"{SAMPLE} is missing"
    text = SAMPLE.read_text(encoding="utf-8")
    score = _score_weekly_issue(text, narration, target_chars=REFERENCE_TARGET_CHARS)
    assert score.closed_world_ok, f"out-of-world tokens: {score.unknown_tokens}"
    assert score.length_ok, f"{score.char_count} chars, target {score.target_chars}"


def test_committed_sample_carries_every_section_heading() -> None:
    text = SAMPLE.read_text(encoding="utf-8")
    for heading in SECTION_HEADINGS:
        assert f"## {heading}" in text, heading


def test_committed_sample_parses_back_into_the_weekly_issue_shape(
    narration: WeeklyNarration,
) -> None:
    """The sample is also the parser's own regression: ``weekly_issue_from_text``
    must round-trip it, because that is exactly what the weekly LLM narrator's
    caller does with a live generation before any downstream surface sees it."""
    issue = weekly_issue_from_text(SAMPLE.read_text(encoding="utf-8"), narration)
    assert issue is not None
    assert [section.heading for section in issue.sections] == list(SECTION_HEADINGS)
    assert all(section.blocks for section in issue.sections)


def test_committed_sample_around_the_league_is_discrete_items(narration: WeeklyNarration) -> None:
    """The matrix's "Around the League is discrete items" row: one block per
    game, so the section reads as a list of results rather than a wall of prose."""
    issue = weekly_issue_from_text(SAMPLE.read_text(encoding="utf-8"), narration)
    assert issue is not None
    blocks = _section("Around the League", issue).blocks
    assert len(blocks) == len(narration.games)
    assert len(blocks) >= 2


def test_section_headings_match_the_template_renderer(narration: WeeklyNarration) -> None:
    """``SECTION_HEADINGS`` is the parser's contract; pin it to what the
    deterministic narrator actually emits so an edit to one cannot silently drift
    from the other."""
    rendered = [section.heading for section in render_weekly_issue(narration).sections]
    assert list(SECTION_HEADINGS) == rendered


def test_template_output_round_trips_through_the_parser(narration: WeeklyNarration) -> None:
    """The parser must accept the deterministic narrator's own output — that is
    the shape it will always be handed first."""
    text = weekly_issue_to_text(render_weekly_issue(narration))
    issue = weekly_issue_from_text(text, narration)
    assert issue is not None
    assert weekly_issue_to_text(issue) == text


def test_parser_rejects_text_without_the_seven_sections() -> None:
    bare = WeeklyNarration(
        league=WeeklyNarrationLeague(name="Test League", season="2025", week=5, team_count=2),
        week_shape="regular",
        transactions=WeeklyNarrationTransactions(),
    )
    assert weekly_issue_from_text("just one line of prose", bare) is None
    assert weekly_issue_from_text("## The Lead\n\nOnly one section.", bare) is None


def test_eval_dir_keeps_its_gitkeep_and_readme() -> None:
    assert (EVAL_DIR / ".gitkeep").is_file()
    readme = (EVAL_DIR / "README.md").read_text(encoding="utf-8")
    assert str(REFERENCE_TARGET_CHARS) in readme
    assert "week10-blowout.json" in readme


# --------------------------------------------------------------------------- #
# Published-rank deviation (Story 5.12)
# --------------------------------------------------------------------------- #


def _power_narration(model_rank: int | None) -> WeeklyNarration:
    return WeeklyNarration(
        league=WeeklyNarrationLeague(name="Test League", season="2025", week=5, team_count=2),
        week_shape="regular",
        power=[
            WeeklyNarrationPower(
                model_rank=model_rank,
                team="Alpha",
                roster_id="1",
                rec="5-0-0",
                avg_pf=120.0,
            )
        ],
        transactions=WeeklyNarrationTransactions(),
    )


def test_published_rank_within_the_cap_is_not_a_finding() -> None:
    narration = _power_narration(model_rank=4)
    assert published_rank_findings(narration, {"1": 4 + POWER_NUDGE_CAP}) == ()
    assert published_rank_findings(narration, {"1": 4 - POWER_NUDGE_CAP}) == ()


def test_published_rank_past_the_cap_is_a_hallucination_finding() -> None:
    narration = _power_narration(model_rank=4)
    findings = published_rank_findings(narration, {"1": 4 + POWER_NUDGE_CAP + 1})
    assert len(findings) == 1
    finding = findings[0]
    assert finding.category == "hallucination"
    assert finding.severity == "regenerate"
    assert "Alpha" in finding.message


def test_published_rank_findings_are_quiet_without_a_comparable_rank() -> None:
    """A cold start (no model rank) or an unranked roster is not a deviation."""
    assert published_rank_findings(_power_narration(model_rank=None), {"1": 12}) == ()
    assert published_rank_findings(_power_narration(model_rank=3), {}) == ()


def test_published_rank_findings_are_deterministic() -> None:
    narration = _power_narration(model_rank=4)
    first = published_rank_findings(narration, {"1": 9})
    second = published_rank_findings(narration, {"1": 9})
    assert first == second


# --------------------------------------------------------------------------- #
# parse_published_ranks
# --------------------------------------------------------------------------- #


def test_parse_published_ranks_reads_the_power_section(narration: WeeklyNarration) -> None:
    assert narration.power
    nudged = narration.model_copy(
        update={
            "power": [
                row.model_copy(update={"published_rank": (row.model_rank or 0) + 1})
                for row in narration.power
            ]
        }
    )
    # Rebuild the power section by hand from the model ranks, one block per team.
    rows = sorted(nudged.power, key=lambda row: (row.model_rank is None, row.model_rank or 0))
    lines = [f"{row.model_rank + 1}. {row.team} — {row.rec}, {row.avg_pf} points a week." for row in rows]
    text = "\n\n".join(
        [
            "Trench Warfare — Week 10 Recap",
            "",
            "## Power Rankings",
            "",
            *lines,
            "",
            "## The Luck Index",
            "",
            "Not enough games have been played to measure luck.",
        ]
    )
    parsed = parse_published_ranks(text, nudged)
    assert set(parsed) == {row.roster_id for row in nudged.power}
    for row in nudged.power:
        assert parsed[row.roster_id] == (row.model_rank or 0) + 1


def test_parse_published_ranks_returns_empty_without_a_power_section() -> None:
    bare = WeeklyNarration(
        league=WeeklyNarrationLeague(name="Test League", season="2025", week=5, team_count=2),
        week_shape="regular",
        transactions=WeeklyNarrationTransactions(),
    )
    assert parse_published_ranks("## The Lead\n\nNothing here.", bare) == {}


# --------------------------------------------------------------------------- #
# Loopback 1: weekly prompt, justification capture, duplicate headings
# --------------------------------------------------------------------------- #


def test_weekly_voice_prompt_names_all_seven_headings_and_draft_prompt_is_unchanged() -> None:
    from commishdesk.voices import load_default_voice

    weekly = load_default_voice("weekly")
    draft = load_default_voice()
    for heading in SECTION_HEADINGS:
        assert f"## {heading}" in weekly.system_prompt, heading
    assert "rookie draft" not in weekly.system_prompt
    assert weekly.banned_topics == draft.banned_topics
    assert "## The Board — Round 1" in draft.system_prompt
    assert "## Power Rankings" not in draft.system_prompt


def test_parse_nudge_justifications_captures_only_deviating_rows() -> None:
    from commishdesk.narrate.weekly_template import parse_nudge_justifications

    narration = WeeklyNarration(
        league=WeeklyNarrationLeague(name="L", season="2025", week=5, team_count=2),
        week_shape="regular",
        power=[
            WeeklyNarrationPower(model_rank=1, team="Alpha", roster_id="1", rec="5-0-0", avg_pf=120.0),
            WeeklyNarrationPower(model_rank=2, team="Beta", roster_id="2", rec="4-1-0", avg_pf=110.0),
        ],
        transactions=WeeklyNarrationTransactions(),
    )
    text = "## Power Rankings\n\n1. Alpha — steady as ever.\n\n3. Beta — slid on a 110.0 average.\n"
    assert parse_nudge_justifications(text, narration) == {"2": "slid on a 110.0 average."}


def test_a_repeated_heading_is_rejected_not_silently_overwritten(narration: WeeklyNarration) -> None:
    text = weekly_issue_to_text(render_weekly_issue(narration)) + "\n## The Lead\n\nA second lead.\n"
    assert weekly_issue_from_text(text, narration) is None


def test_committed_sample_power_rankings_parse_into_published_ranks(narration: WeeklyNarration) -> None:
    """The sample's Power Rankings section is a numbered list the parser can read
    a published rank from — the shape the weekly prompt instructs."""
    ranks = parse_published_ranks(SAMPLE.read_text(encoding="utf-8"), narration)
    assert ranks
    assert set(ranks) <= {row.roster_id for row in narration.power}


def test_parse_published_ranks_reads_a_tight_list_without_blank_lines(narration: WeeklyNarration) -> None:
    rows = sorted((r for r in narration.power if r.model_rank), key=lambda r: r.model_rank)
    items = [f"{r.model_rank}. {r.team} — fine." for r in rows]
    text = "\n".join(["## Power Rankings", "", *items, ""])
    assert parse_published_ranks(text, narration) == {r.roster_id: r.model_rank for r in rows}


def test_weekly_prompt_states_the_enforced_nudge_cap() -> None:
    from commishdesk.voices import load_default_voice

    assert f"at most {POWER_NUDGE_CAP} positions" in load_default_voice("weekly").system_prompt


def test_load_default_voice_rejects_an_unknown_content_type() -> None:
    from commishdesk.voices import load_default_voice

    with pytest.raises(ValueError):
        load_default_voice("weeky")  # type: ignore[arg-type]
