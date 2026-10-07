"""Story 7.3 — the spec-3-4 alias and confusable gaps as L4 fixtures.

``tests/fixtures/l4/cases.json`` holds each deferred L2 gap (a first-name / pronoun
reference, a zero-width join, a Unicode confusable) plus no-keyword mean sentences and
a benign control, with the L2 severities and the L4 score measured against the real
model named in the file (never committed, never in CI). Two tests:

* offline: L2 still behaves as recorded, so the recorded "gap" stays a real gap;
* opt-in: with ``COMMISHDESK_L4_MODEL`` pointing at a directory holding that model's
  ``model.onnx`` and ``tokenizer.json`` (and the ``l4`` extra), the live score agrees with
  the recorded outcome at the ``l4.toml`` threshold.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from commishdesk.facts import build_weekly_facts
from commishdesk.facts.schema import WeeklyNarration
from commishdesk.ingest import (
    build_league_model,
    build_player_names,
    build_player_snapshot,
    build_week_model,
)
from commishdesk.narrate.l4 import load_l4_config, load_l4_scorer
from commishdesk.narrate.safety import check_narration
from tests.conftest import REPO_ROOT

CASES = json.loads((REPO_ROOT / "tests" / "fixtures" / "l4" / "cases.json").read_text(encoding="utf-8"))
MODEL_ENV = "COMMISHDESK_L4_MODEL"


def _extra_installed() -> bool:
    try:
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.fixture(scope="module")
def narration() -> WeeklyNarration:
    bundle = json.loads((REPO_ROOT / "tests" / "fixtures" / "week10-blowout.json").read_text(encoding="utf-8"))
    doc = build_weekly_facts(
        build_week_model(bundle),
        build_league_model(bundle),
        build_player_snapshot(bundle),
        build_player_names(bundle),
        generated_at="2026-09-07T00:00:00Z",
        nfl_byes_next_week=frozenset({"IND", "NO"}),
    )
    n = doc.narration
    first = n.standings[0].model_copy(update={"team": CASES["team_label"]})
    return n.model_copy(update={"standings": [first, *n.standings[1:]]})


def test_the_recorded_model_matches_the_data_file() -> None:
    config = load_l4_config()
    assert CASES["model"]["positive_index"] == config.positive_index
    assert CASES["model"]["threshold"] == config.threshold


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: c["id"])
def test_l2_still_behaves_as_recorded(case: dict, narration: WeeklyNarration) -> None:
    report = check_narration(case["text"], narration)
    assert sorted({f.severity for f in report.findings}) == case["l2_severities"]


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: c["id"])
def test_recorded_scores_against_the_real_model(case: dict) -> None:
    model_dir = os.environ.get(MODEL_ENV)
    if not model_dir:
        pytest.skip(f"{MODEL_ENV} is unset")
    if not _extra_installed():
        pytest.skip("the l4 extra is not installed")
    scorer = load_l4_scorer(Path(model_dir))
    score = scorer.score(case["text"])
    assert (score >= load_l4_config().threshold) == case["l4_flagged"], (case["id"], score)
