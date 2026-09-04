"""Story 2.7 — the template narrator writes every reference section from ``narration`` alone.

The oracle is ``narration`` built through the real pipeline off the committed
``demo`` fixture (the same chain ``commishdesk --league demo --draft-recap``
runs). One test per matrix row that concerns the narrator: section completeness,
names / numbers vs the narration, determinism, orphan-safety, and the "December"
section deriving from ``superlatives`` + ``lead_candidates``.
"""

from __future__ import annotations

import pytest

from commishdesk import demo
from commishdesk.facts import build_draft_recap_facts
from commishdesk.facts.schema import (
    BoardPick,
    HeadlineNumbers,
    Narration,
    NarrationLeague,
    NarrationTeam,
    PositionalRunsSummary,
    QBRunSummary,
    RBRunSummary,
    Superlatives,
    TERunSummary,
)
from commishdesk.ingest import build_league_model
from commishdesk.narrate import Recap, recap_to_text, render_draft_recap
from commishdesk.narrate import template
from commishdesk.stats import (
    compute_board_metrics,
    compute_consensus_metrics,
    compute_draft_grades,
)
from tests.test_facts import _bundle, _synthetic_slots

GENERATED_AT = "2026-09-03T00:00:00Z"

SECTION_HEADINGS = [
    "The Lead",
    "The Board — Round 1",
    "Superlatives",
    "Team Grades",
    "Positional Read",
    "The Picks We'll Be Arguing About in December",
]


@pytest.fixture(scope="module")
def narration() -> Narration:
    model = build_league_model(demo.load_demo_bundle())
    board = compute_board_metrics(model)
    consensus = compute_consensus_metrics(model, demo.demo_consensus_slots())
    grades = compute_draft_grades(model, consensus)
    doc = build_draft_recap_facts(
        model,
        board,
        consensus,
        grades,
        generated_at=GENERATED_AT,
        consensus_source_name="synthetic rookie board",
        consensus_as_of="2025-05",
    )
    return doc.narration


def _section(recap: Recap, heading: str):
    return next(s for s in recap.sections if s.heading == heading)


def test_emits_all_six_reference_sections(narration: Narration) -> None:
    recap = render_draft_recap(narration)
    assert isinstance(recap, Recap)
    assert [s.heading for s in recap.sections] == SECTION_HEADINGS
    assert all(section.blocks for section in recap.sections)
    assert narration.league.name in recap.title


def test_lead_carries_headline_numbers_and_the_ranked_hooks(narration: Narration) -> None:
    text = recap_to_text(render_draft_recap(narration))
    assert "Five of the first eleven picks were running backs" in text
    assert "Ashton Jeanty went 1.01." in text
    leader = narration.headline_numbers.pick_count_leader
    assert leader is not None and leader.manager in text


def test_board_round1_names_and_numbers_match_the_narration(narration: Narration) -> None:
    board = _section(render_draft_recap(narration), "The Board — Round 1")
    assert len(board.blocks) == len(narration.board_round1)
    for block, pick in zip(board.blocks, narration.board_round1):
        assert block.startswith(f"{pick.board_label} — ")
        assert pick.player in block
        if pick.manager:
            assert pick.manager in block
        if pick.consensus_label is not None:
            assert pick.consensus_label in block


def test_superlatives_name_the_right_picks(narration: Narration) -> None:
    section = _section(render_draft_recap(narration), "Superlatives")
    blob = " ".join(section.blocks)
    sup = narration.superlatives
    assert sup.best_value is not None and sup.best_value.player in blob
    assert sup.biggest_reach is not None and sup.biggest_reach.player in blob
    assert sup.boldest_swing is not None and sup.boldest_swing.manager in blob


def test_every_manager_grade_letter_is_present(narration: Narration) -> None:
    section = _section(render_draft_recap(narration), "Team Grades")
    blob = "\n".join(section.blocks)
    for team in narration.teams:
        if team.manager:
            assert f"{team.manager}: {team.grade}" in blob


def test_positional_read_uses_the_run_summaries(narration: Narration) -> None:
    section = _section(render_draft_recap(narration), "Positional Read")
    blob = " ".join(section.blocks)
    runs = narration.positional_runs
    assert runs.RB.most_by_one_manager is not None
    assert runs.RB.most_by_one_manager.manager in blob
    for manager in runs.QB.left_waiting:
        assert manager in blob


def test_december_section_derives_from_superlatives(narration: Narration) -> None:
    section = _section(
        render_draft_recap(narration), "The Picks We'll Be Arguing About in December"
    )
    blob = " ".join(section.blocks)
    swing = narration.superlatives.boldest_swing
    assert swing is not None
    assert swing.picks[0].player in blob and swing.picks[1].player in blob
    reach = narration.superlatives.biggest_reach
    assert reach is not None and reach.player in blob
    # the lead hook is not repeated here — it already opens the recap
    assert narration.lead_candidates[0].hook not in section.blocks


def test_render_is_deterministic(narration: Narration) -> None:
    assert (
        render_draft_recap(narration).model_dump()
        == render_draft_recap(narration).model_dump()
    )


def test_no_literal_none_in_the_prose(narration: Narration) -> None:
    assert "None" not in recap_to_text(render_draft_recap(narration))


def test_orphan_roster_is_skipped_never_rendered_as_none() -> None:
    runs = PositionalRunsSummary(
        QB=QBRunSummary(total=0, by_end_round3=0),
        RB=RBRunSummary(total=0, in_round1=0),
        TE=TERunSummary(total=0),
    )
    orphan_narration = Narration(
        league=NarrationLeague(name="Orphan League", season="2025", scoring_label="PPR"),
        headline_numbers=HeadlineNumbers(
            picks_total=1, rounds=1, r1_positional={"RB": 1}, first_window_rb_count=1
        ),
        board_round1=[
            BoardPick(
                pick_no=1,
                board_label="1.01",
                manager=None,
                player="Unclaimed Selection",
                position="RB",
                consensus_label=None,
                delta=None,
            )
        ],
        superlatives=Superlatives(),
        teams=[
            NarrationTeam(
                manager=None,
                roster_id="1",
                pick_count=1,
                positional_counts={"RB": 1},
                grade="C",
            )
        ],
        positional_runs=runs,
        lead_candidates=[],
    )
    recap = render_draft_recap(orphan_narration)
    text = recap_to_text(recap)
    assert "None" not in text
    assert "Unclaimed Selection" in text  # board line still renders, without a name
    grades = _section(recap, "Team Grades")
    assert grades.blocks == [
        template._GRADE_METHOD_BLOCK,
        "No roster carried a manager name to grade.",
    ]


def test_grade_method_block_anchors_the_letters(narration: Narration) -> None:
    grades = _section(render_draft_recap(narration), "Team Grades")
    assert grades.blocks[0] == template._GRADE_METHOD_BLOCK
    assert "A to F" in grades.blocks[0]


# --------------------------------------------------------------------------- #
# Drift guards — commishdesk.demo must match the tests/test_facts helpers
# --------------------------------------------------------------------------- #


def test_demo_bundle_matches_the_test_facts_reshape() -> None:
    assert demo.load_demo_bundle() == _bundle("rookie-draft.json")


def test_demo_consensus_slots_match_the_synthetic_slots() -> None:
    assert demo.demo_consensus_slots() == _synthetic_slots()


def test_demo_consensus_source_constants() -> None:
    assert demo.DEMO_CONSENSUS_SOURCE_NAME == "synthetic rookie board"
    assert demo.DEMO_CONSENSUS_AS_OF == "2025-05"


def test_spell_helper_has_not_drifted_from_facts_leads() -> None:
    import commishdesk.facts.leads as leads

    assert template._ONES == leads._ONES
    for n in range(0, 40):
        assert template._spell(n) == leads._spell(n), n
