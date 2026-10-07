"""Story 7.3 — the L4 classifier extra, engine half (AD-12 L4, AD-47).

Everything here runs with no extra, no network and no model: a fake scorer stands in
for the ONNX model. The real runtime is covered by ``test_narrate_l4_runtime.py``.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from commishdesk.errors import NarratorError
from commishdesk.facts import build_weekly_facts
from commishdesk.facts.schema import WeeklyFacts
from commishdesk.ingest import (
    build_league_model,
    build_player_names,
    build_player_snapshot,
    build_week_model,
)
from commishdesk.llmconfig import LLMConfig, LLMModelConfig
from commishdesk.narrate.l4 import (
    L4UnavailableError,
    load_l4_config,
    load_l4_scorer,
    screen_weekly_issue,
)
from commishdesk.narrate.llm import NarrationResult
from commishdesk.narrate.weekly_template import (
    WeeklyIssue,
    render_weekly_issue,
    revert_sections_to_template,
    weekly_issue_from_text,
    weekly_issue_to_text,
)
from commishdesk.voices.beat_writer import weekly_voice_for
from commishdesk.weekly import produce_weekly_issue
from tests.conftest import REPO_ROOT

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "week10-blowout.json"
THRESHOLD = load_l4_config().threshold
LOGGER = logging.getLogger("test.l4")


@pytest.fixture(scope="module")
def doc() -> WeeklyFacts:
    bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return build_weekly_facts(
        build_week_model(bundle),
        build_league_model(bundle),
        build_player_snapshot(bundle),
        build_player_names(bundle),
        generated_at="2026-09-07T00:00:00Z",
        nfl_byes_next_week=frozenset({"IND", "NO"}),
    )


def _llm_text(doc: WeeklyFacts) -> str:
    """A well-formed completion whose lead and around-the-league prose differ from the template's."""
    text = weekly_issue_to_text(render_weekly_issue(doc.narration))
    for heading in ("The Lead", "Around the League"):
        text = text.replace(f"## {heading}\n\n", f"## {heading}\n\nA busy week. ", 1)
    return text


def _parsed(doc: WeeklyFacts, suppressed: frozenset[str] = frozenset()) -> WeeklyIssue:
    issue = weekly_issue_from_text(_llm_text(doc), doc.narration, suppressed=suppressed)
    assert issue is not None
    return issue


class FakeScorer:
    """Scores a section by its exact text, defaulting to 0.0; records every text asked."""

    def __init__(self, issue: WeeklyIssue, scores: dict[str, float]) -> None:
        self.by_text = {
            "\n\n".join(s.blocks): scores[s.section_id] for s in issue.sections if s.section_id in scores
        }
        self.seen: list[str] = []

    def score(self, text: str) -> float:
        self.seen.append(text)
        return self.by_text.get(text, 0.0)


class RaisingScorer:
    def score(self, text: str) -> float:
        raise RuntimeError("boom")


class NanScorer:
    def score(self, text: str) -> float:
        return float("nan")


def _section(issue: WeeklyIssue, section_id: str):
    return next(s for s in issue.sections if s.section_id == section_id)


# --- screen_weekly_issue ----------------------------------------------------


def test_clean_sections_leave_the_issue_unchanged(doc: WeeklyFacts) -> None:
    issue = _parsed(doc)
    screened, ids = screen_weekly_issue(issue, doc.narration, FakeScorer(issue, {}), has_prior_week=True)
    assert screened == issue
    assert ids == ()


def test_one_section_over_is_replaced_by_the_template_section(doc: WeeklyFacts) -> None:
    issue = _parsed(doc)
    template = render_weekly_issue(doc.narration)
    scorer = FakeScorer(issue, {"around_league": 0.99})
    screened, ids = screen_weekly_issue(issue, doc.narration, scorer, has_prior_week=True)
    assert ids == ("around_league",)
    assert _section(screened, "around_league") == _section(template, "around_league")
    assert _section(issue, "around_league") != _section(template, "around_league")
    for other in ("lead", "standings", "power", "luck", "next_week", "transactions"):
        assert _section(screened, other) == _section(issue, other)
    assert [s.heading for s in screened.sections] == [s.heading for s in issue.sections]
    assert (screened.title, screened.dateline) == (issue.title, issue.dateline)


def test_the_threshold_is_inclusive(doc: WeeklyFacts) -> None:
    issue = _parsed(doc)
    at = FakeScorer(issue, {"lead": THRESHOLD})
    assert screen_weekly_issue(issue, doc.narration, at, has_prior_week=True)[1] == ("lead",)
    just_below = FakeScorer(issue, {"lead": THRESHOLD - 1e-9})
    assert screen_weekly_issue(issue, doc.narration, just_below, has_prior_week=True)[1] == ()


def test_several_sections_are_reported_in_issue_order(doc: WeeklyFacts) -> None:
    issue = _parsed(doc)
    scorer = FakeScorer(issue, {"luck": 1.0, "lead": 1.0})
    assert screen_weekly_issue(issue, doc.narration, scorer, has_prior_week=True)[1] == ("lead", "luck")


def test_suppressed_sections_are_never_scored_or_reported(doc: WeeklyFacts) -> None:
    suppressed = frozenset({"power"})
    issue = _parsed(doc, suppressed)
    scorer = FakeScorer(issue, {"power": 1.0})
    screened, ids = screen_weekly_issue(
        issue, doc.narration, scorer, has_prior_week=True, suppressed=suppressed
    )
    assert ids == ()
    assert screened == issue
    assert "\n\n".join(_section(issue, "power").blocks) not in scorer.seen
    assert len(scorer.seen) == len(issue.sections) - 1


def test_a_suppressed_id_the_issue_does_not_carry_is_ignored(doc: WeeklyFacts) -> None:
    issue = _parsed(doc)
    scorer = FakeScorer(issue, {})
    screen_weekly_issue(issue, doc.narration, scorer, has_prior_week=True, suppressed={"nonsense"})
    assert len(scorer.seen) == len(issue.sections)


def test_a_section_outside_the_seven_is_never_scored(doc: WeeklyFacts) -> None:
    issue = _parsed(doc)
    corrected = issue.model_copy(
        update={"sections": [type(issue.sections[0])(heading="Correction", blocks=["Fixed."]), *issue.sections]}
    )
    scorer = FakeScorer(corrected, {})
    screen_weekly_issue(corrected, doc.narration, scorer, has_prior_week=True)
    assert "Fixed." not in scorer.seen
    assert len(scorer.seen) == len(issue.sections)


def test_cached_text_is_screened_identically(doc: WeeklyFacts) -> None:
    """A stored completion parsed back with ``weekly_issue_from_text`` goes through the
    same public function as a fresh one (AD-11 cache reads still pass L4)."""
    stored = _llm_text(doc)
    parsed = weekly_issue_from_text(stored, doc.narration)
    assert parsed is not None
    scorer = FakeScorer(parsed, {"luck": 0.9})
    screened, ids = screen_weekly_issue(parsed, doc.narration, scorer, has_prior_week=True)
    assert ids == ("luck",)
    assert _section(screened, "luck") == _section(render_weekly_issue(doc.narration), "luck")


def test_a_scorer_that_raises_is_l4_unavailable(doc: WeeklyFacts) -> None:
    issue = _parsed(doc)
    with pytest.raises(L4UnavailableError) as info:
        screen_weekly_issue(issue, doc.narration, RaisingScorer(), has_prior_week=True)
    assert isinstance(info.value, NarratorError)


def test_a_non_finite_score_is_l4_unavailable(doc: WeeklyFacts) -> None:
    with pytest.raises(L4UnavailableError):
        screen_weekly_issue(_parsed(doc), doc.narration, NanScorer(), has_prior_week=True)


def test_cold_start_screens_the_four_sections(doc: WeeklyFacts) -> None:
    cold = render_weekly_issue(doc.narration, has_prior_week=False)
    scorer = FakeScorer(cold, {})
    screen_weekly_issue(cold, doc.narration, scorer, has_prior_week=False)
    assert len(scorer.seen) == 4


def test_revert_helper_ignores_unknown_ids_and_returns_the_same_issue(doc: WeeklyFacts) -> None:
    issue = _parsed(doc)
    assert revert_sections_to_template(issue, doc.narration, ["nonsense"], has_prior_week=True) is issue
    assert revert_sections_to_template(issue, doc.narration, [], has_prior_week=True) is issue


# --- the L4 config + data file ---------------------------------------------


def test_threshold_positive_index_and_max_length_come_from_the_data_file() -> None:
    raw = tomllib.loads((REPO_ROOT / "commishdesk" / "narrate" / "l4.toml").read_text(encoding="utf-8"))
    config = load_l4_config()
    assert config.threshold == raw["threshold"]
    assert config.positive_index == raw["positive_index"]
    assert config.max_length == raw["max_length"]
    assert 0.0 < config.threshold <= 1.0


def test_l4_toml_is_shipped_as_package_data() -> None:
    from importlib import resources

    package = resources.files("commishdesk.narrate")
    assert package.joinpath("l4.toml").is_file()
    assert package.joinpath("safety_lists.toml").is_file()


@pytest.mark.parametrize(
    "text",
    [
        "threshold = 1.5\npositive_index = 0\nmax_length = 8\n",
        "threshold = 0.5\npositive_index = -1\nmax_length = 8\n",
        "threshold = 0.5\npositive_index = 0\nmax_length = 0\n",
        "threshold = true\npositive_index = 0\nmax_length = 8\n",
        "positive_index = 0\nmax_length = 8\n",
        "not = [valid\n",
    ],
)
def test_an_invalid_config_is_l4_unavailable(text: str) -> None:
    from commishdesk.narrate.l4 import _parse_config

    with pytest.raises(L4UnavailableError):
        _parse_config(text)


# --- load_l4_scorer failure rows --------------------------------------------


def test_no_model_path_raises() -> None:
    with pytest.raises(L4UnavailableError):
        load_l4_scorer(None)


def test_a_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(L4UnavailableError):
        load_l4_scorer(tmp_path / "nope")


def test_a_directory_without_the_model_files_raises(tmp_path: Path) -> None:
    (tmp_path / "model.onnx").write_bytes(b"x")
    with pytest.raises(L4UnavailableError):
        load_l4_scorer(tmp_path)


def test_a_missing_extra_names_the_extra(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "model.onnx").write_bytes(b"x")
    (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")
    for name in ("onnxruntime", "tokenizers"):
        monkeypatch.setitem(sys.modules, name, None)  # import raises ImportError
    with pytest.raises(L4UnavailableError, match=r"commishdesk\[l4\]"):
        load_l4_scorer(tmp_path)


# --- produce_weekly_issue ---------------------------------------------------


@pytest.fixture
def llm_path(monkeypatch: pytest.MonkeyPatch, doc: WeeklyFacts) -> None:
    """Route ``produce_weekly_issue`` down the LLM branch with a canned completion."""
    import commishdesk.narrate.llm as llm
    import commishdesk.weekly as weekly

    config = LLMConfig(
        primary=LLMModelConfig("anthropic", "claude-sonnet-5"),
        fallback=LLMModelConfig("google", "gemini-3.5-flash"),
    )
    voice = weekly_voice_for(cold_start=False, suppressed=frozenset())
    monkeypatch.setattr(weekly, "weekly_llm_selection", lambda *a, **k: (voice, config))
    monkeypatch.setattr(weekly, "weekly_estimate_within_ceiling", lambda *a, **k: True)
    monkeypatch.setattr(
        llm,
        "narrate_weekly_issue",
        lambda *a, **k: NarrationResult(text=_llm_text(doc), narrator="llm-primary"),
    )


def test_produce_without_l4_reports_no_ids(doc: WeeklyFacts, llm_path: None) -> None:
    issue, ranks, justifications, ids = produce_weekly_issue(doc, resolved="L", logger=LOGGER)
    assert ids == ()
    assert ranks  # the model-written power list published its ranks
    assert issue == _parsed(doc)


def test_produce_with_clean_l4_changes_nothing(doc: WeeklyFacts, llm_path: None) -> None:
    baseline = produce_weekly_issue(doc, resolved="L", logger=LOGGER)
    screened = produce_weekly_issue(doc, resolved="L", logger=LOGGER, l4=FakeScorer(_parsed(doc), {}))
    assert screened == baseline


def test_produce_reverts_one_section_and_logs_its_id(
    doc: WeeklyFacts, llm_path: None, caplog: pytest.LogCaptureFixture
) -> None:
    llm_issue = _parsed(doc)
    template = render_weekly_issue(doc.narration)
    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        issue, ranks, justifications, ids = produce_weekly_issue(
            doc, resolved="L", logger=LOGGER, l4=FakeScorer(llm_issue, {"around_league": 1.0})
        )
    assert ids == ("around_league",)
    assert _section(issue, "around_league") == _section(template, "around_league")
    assert _section(issue, "lead") == _section(llm_issue, "lead")
    assert ranks  # a non-Power hit keeps the published ranks
    assert any("around_league" in r.getMessage() for r in caplog.records)


def test_produce_power_hit_drops_ranks_and_justifications(doc: WeeklyFacts, llm_path: None) -> None:
    llm_issue = _parsed(doc)
    template = render_weekly_issue(doc.narration)
    issue, ranks, justifications, ids = produce_weekly_issue(
        doc, resolved="L", logger=LOGGER, l4=FakeScorer(llm_issue, {"power": 1.0})
    )
    assert ids == ("power",)
    assert ranks == {} and justifications == {}
    assert _section(issue, "power") == _section(template, "power")


def test_produce_with_a_failing_scorer_raises_and_returns_nothing(doc: WeeklyFacts, llm_path: None) -> None:
    with pytest.raises(L4UnavailableError):
        produce_weekly_issue(doc, resolved="L", logger=LOGGER, l4=RaisingScorer())


def test_produce_never_screens_a_template_issue(doc: WeeklyFacts, monkeypatch: pytest.MonkeyPatch) -> None:
    import commishdesk.weekly as weekly

    monkeypatch.setattr(weekly, "weekly_llm_selection", lambda *a, **k: None)
    issue, ranks, justifications, ids = produce_weekly_issue(doc, resolved="L", logger=LOGGER, l4=RaisingScorer())
    assert ids == ()
    assert issue == render_weekly_issue(doc.narration, has_prior_week=True)


def test_produce_never_screens_a_reissue(doc: WeeklyFacts, llm_path: None) -> None:
    _, _, _, ids = produce_weekly_issue(doc, resolved="L", logger=LOGGER, reissue=True, l4=RaisingScorer())
    assert ids == ()


def test_produce_never_screens_a_degraded_template(doc: WeeklyFacts, monkeypatch: pytest.MonkeyPatch) -> None:
    import commishdesk.narrate.llm as llm
    import commishdesk.weekly as weekly

    config = LLMConfig(
        primary=LLMModelConfig("anthropic", "claude-sonnet-5"),
        fallback=LLMModelConfig("google", "gemini-3.5-flash"),
    )
    voice = weekly_voice_for(cold_start=False, suppressed=frozenset())
    monkeypatch.setattr(weekly, "weekly_llm_selection", lambda *a, **k: (voice, config))
    monkeypatch.setattr(weekly, "weekly_estimate_within_ceiling", lambda *a, **k: True)
    monkeypatch.setattr(llm, "narrate_weekly_issue", lambda *a, **k: NarrationResult(text="", narrator="template"))
    _, _, _, ids = produce_weekly_issue(doc, resolved="L", logger=LOGGER, l4=RaisingScorer())
    assert ids == ()


# --- the import fence -------------------------------------------------------


def test_importing_the_package_loads_neither_l4_nor_its_extra() -> None:
    code = (
        "import sys, commishdesk, commishdesk.narrate, commishdesk.weekly, commishdesk.cli;"
        "bad = [m for m in ('commishdesk.narrate.l4', 'onnxruntime', 'tokenizers') if m in sys.modules];"
        "print(bad); sys.exit(1 if bad else 0)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_narrate_package_does_not_re_export_l4() -> None:
    import commishdesk.narrate as narrate

    assert "l4" not in getattr(narrate, "__all__", ())
    assert not hasattr(narrate, "load_l4_scorer")
