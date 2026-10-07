"""Story 7.3 — the L4 classifier against the real ``onnxruntime`` + ``tokenizers``.

Uses the few-hundred-byte committed models under ``tests/fixtures/l4_tiny*`` (authored by
``tools/make_l4_fixture.py``): a bag-of-words plumbing fixture, not a classifier. Skipped
with a registered reason when the ``l4`` extra is absent; the ``l4`` CI job runs it with
``uv run --frozen --extra l4 pytest tests/test_narrate_l4_runtime.py``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from commishdesk.narrate.l4 import L4UnavailableError, load_l4_config, load_l4_scorer
from tests.conftest import REPO_ROOT

try:
    import onnxruntime  # noqa: F401
    import tokenizers  # noqa: F401
except ImportError:
    pytest.skip("the l4 extra is not installed", allow_module_level=True)

FIXTURES = REPO_ROOT / "tests" / "fixtures"
TINY = FIXTURES / "l4_tiny"
TINY_SIGMOID = FIXTURES / "l4_tiny_sigmoid"
THRESHOLD = load_l4_config().threshold


def test_the_fixture_matches_the_data_file_positive_index() -> None:
    """The softmax fixture pushes the class at ``l4.toml``'s ``positive_index``; if that
    index changes, re-run ``tools/make_l4_fixture.py``."""
    assert load_l4_config().positive_index in (0, 1)
    scorer = load_l4_scorer(TINY)
    assert scorer.score("toxic") > 0.99
    assert scorer.score("nice") < 0.5


def test_softmax_scores_unsafe_text_high_and_safe_text_low() -> None:
    scorer = load_l4_scorer(TINY)
    assert scorer.score("this is toxic") >= THRESHOLD
    assert scorer.score("what an idiot") >= THRESHOLD
    assert scorer.score("nice nice nice") < THRESHOLD
    assert scorer.score("") < THRESHOLD
    assert scorer.score("some words nobody trained on") < THRESHOLD


def test_scores_are_probabilities_and_deterministic() -> None:
    scorer = load_l4_scorer(TINY)
    first = scorer.score("nice and toxic")
    assert 0.0 <= first <= 1.0
    assert scorer.score("nice and toxic") == first


def test_a_single_logit_model_is_a_sigmoid() -> None:
    scorer = load_l4_scorer(TINY_SIGMOID)
    assert scorer.score("toxic") > 0.99
    assert scorer.score("nice") < 0.5
    assert scorer.score("") < THRESHOLD


def test_the_tokenizer_is_case_insensitive_in_the_fixture() -> None:
    assert load_l4_scorer(TINY).score("TOXIC") > 0.99


def test_long_text_is_truncated_to_max_length() -> None:
    """A toxic word beyond ``max_length`` tokens is cut before scoring; the same word
    inside the window is seen."""
    max_length = load_l4_config().max_length
    scorer = load_l4_scorer(TINY)
    beyond = " ".join(["nice"] * (max_length + 20) + ["toxic"])
    inside = " ".join(["toxic"] + ["nice"] * (max_length + 20))
    assert scorer.score(beyond) < THRESHOLD
    assert scorer.score(inside) >= THRESHOLD


def test_a_corrupt_model_is_l4_unavailable(tmp_path: Path) -> None:
    shutil.copy(TINY / "tokenizer.json", tmp_path / "tokenizer.json")
    (tmp_path / "model.onnx").write_bytes(b"this is not an onnx model")
    with pytest.raises(L4UnavailableError):
        load_l4_scorer(tmp_path)


def test_a_corrupt_tokenizer_is_l4_unavailable(tmp_path: Path) -> None:
    shutil.copy(TINY / "model.onnx", tmp_path / "model.onnx")
    (tmp_path / "tokenizer.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(L4UnavailableError):
        load_l4_scorer(tmp_path)


def test_a_positive_index_beyond_the_outputs_fails_closed() -> None:
    """A positive_index beyond the model's outputs must not score as 0.0 (fail open)."""
    scorer = load_l4_scorer(TINY_SIGMOID)
    scorer._config = type(scorer._config)(threshold=0.3, positive_index=5, max_length=8)  # type: ignore[attr-defined]
    # one logit -> sigmoid, positive_index is not consulted
    assert scorer.score("toxic") > 0.99
    two = load_l4_scorer(TINY)
    two._config = type(two._config)(threshold=0.3, positive_index=5, max_length=8)  # type: ignore[attr-defined]
    with pytest.raises(L4UnavailableError):
        two.score("toxic")


def test_screening_with_the_real_scorer_reverts_a_toxic_section() -> None:
    import json

    from commishdesk.facts import build_weekly_facts
    from commishdesk.ingest import (
        build_league_model,
        build_player_names,
        build_player_snapshot,
        build_week_model,
    )
    from commishdesk.narrate.l4 import screen_weekly_issue
    from commishdesk.narrate.weekly_template import render_weekly_issue

    bundle = json.loads((FIXTURES / "week10-blowout.json").read_text(encoding="utf-8"))
    doc = build_weekly_facts(
        build_week_model(bundle),
        build_league_model(bundle),
        build_player_snapshot(bundle),
        build_player_names(bundle),
        generated_at="2026-09-07T00:00:00Z",
        nfl_byes_next_week=frozenset({"IND", "NO"}),
    )
    template = render_weekly_issue(doc.narration)
    llm = template.model_copy(
        update={
            "sections": [
                s.model_copy(update={"blocks": ["That was toxic."]}) if s.section_id == "luck" else s
                for s in template.sections
            ]
        }
    )
    screened, ids = screen_weekly_issue(llm, doc.narration, load_l4_scorer(TINY), has_prior_week=True)
    assert ids == ("luck",)
    assert screened == template
