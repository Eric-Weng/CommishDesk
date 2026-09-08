"""Story 3.5 — ``commishdesk/narrate/response.py``: the AD-12 Layer 3 tiered
response helpers.

Units for the four pure functions per the spec's I/O matrix:
``sanitize_completion`` (ANSI + control), ``structural_ok`` (full / missing-heading
/ empty / em-dash), ``classify`` (each tier, template vs LLM), and
``suppress_sections`` (single section dropped; lead → ``None``; unmapped finding).
Plus the import-fence + eager-re-export guards.
"""

from __future__ import annotations

import ast
from pathlib import Path

from commishdesk.narrate import (
    SECTION_HEADINGS,
    Recap,
    Section,
    TieredResponse,
    classify,
    sanitize_completion,
    structural_ok,
    suppress_sections,
)
from commishdesk.narrate import response as response_mod
from commishdesk.narrate.safety import SafetyFinding, SafetyReport

RESPONSE_PY = Path(response_mod.__file__)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _finding(category: str, severity: str, sentence: str = "s", matched: str = "m") -> SafetyFinding:
    return SafetyFinding(
        category=category,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        message=f"{category} finding",
        sentence=sentence,
        matched=matched,
    )


def _report(*findings: SafetyFinding) -> SafetyReport:
    return SafetyReport(findings=tuple(findings))


def _six_section_text(*, drop: str | None = None, dash: str = "—") -> str:
    lines = ["My Draft Recap", ""]
    for heading in SECTION_HEADINGS:
        if heading == drop:
            continue
        shown = heading.replace("—", dash)
        lines += [f"## {shown}", "", "body copy here.", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# sanitize_completion
# --------------------------------------------------------------------------- #


def test_sanitize_strips_ansi_sgr_sequences() -> None:
    dirty = "\x1b[1;31mThe Lead\x1b[0m is \x1b[4mbold\x1b[24m now"
    assert sanitize_completion(dirty) == "The Lead is bold now"


def test_sanitize_drops_control_chars_but_keeps_newlines() -> None:
    dirty = "line one\nline\x00 two\x07\nline​ three"
    out = sanitize_completion(dirty)
    assert out == "line one\nline two\nline three"
    assert "\n" in out


def test_sanitize_is_idempotent() -> None:
    dirty = "\x1b[32m## The Lead\x1b[0m\n\ntext﻿"
    once = sanitize_completion(dirty)
    assert sanitize_completion(once) == once


# --------------------------------------------------------------------------- #
# structural_ok
# --------------------------------------------------------------------------- #


def test_structural_ok_accepts_all_six_headings() -> None:
    assert structural_ok(_six_section_text()) is True


def test_structural_ok_tolerates_a_plain_hyphen_for_the_em_dash() -> None:
    assert structural_ok(_six_section_text(dash="-")) is True


def test_structural_ok_is_case_insensitive() -> None:
    assert structural_ok(_six_section_text().lower()) is True


def test_structural_ok_rejects_a_missing_heading() -> None:
    assert structural_ok(_six_section_text(drop="Superlatives")) is False


def test_structural_ok_rejects_empty_and_blank() -> None:
    assert structural_ok("") is False
    assert structural_ok("   \n\t  \n") is False


def test_structural_ok_rejects_a_bare_paragraph() -> None:
    assert structural_ok("just some prose with no headings at all") is False


# --------------------------------------------------------------------------- #
# classify
# --------------------------------------------------------------------------- #


def test_classify_clean_report_is_all_false() -> None:
    out = classify(_report(), narrator_is_template=False)
    assert out == TieredResponse(
        hold=False, hold_reasons=(), regenerate=False, suppress=False, alerts=()
    )


def test_classify_named_person_holds_and_wins_over_other_tiers() -> None:
    out = classify(
        _report(
            _finding("named_person_proximity", "hold_issue"),
            _finding("slop", "suppress_section"),
            _finding("hallucination", "regenerate"),
        ),
        narrator_is_template=False,
    )
    assert out.hold is True
    assert out.hold_reasons == ("named_person_proximity finding",)
    assert out.regenerate is False and out.suppress is False
    assert len(out.alerts) == 3


def test_classify_hallucination_regenerates_for_llm() -> None:
    out = classify(
        _report(_finding("hallucination", "regenerate")), narrator_is_template=False
    )
    assert out.regenerate is True and out.hold is False and out.suppress is False


def test_classify_hallucination_is_ignored_for_the_template_narrator() -> None:
    out = classify(
        _report(_finding("hallucination", "regenerate")), narrator_is_template=True
    )
    assert out == TieredResponse(
        hold=False, hold_reasons=(), regenerate=False, suppress=False, alerts=()
    )


def test_classify_slop_and_banned_topic_suppress() -> None:
    for category in ("slop", "banned_topic"):
        out = classify(
            _report(_finding(category, "suppress_section")), narrator_is_template=True
        )
        assert out.suppress is True and out.hold is False and out.regenerate is False


# --------------------------------------------------------------------------- #
# suppress_sections
# --------------------------------------------------------------------------- #


def _recap(*section_blocks: tuple[str, list[str]]) -> Recap:
    return Recap(
        title="t",
        dateline="d",
        sections=[Section(heading=h, blocks=b) for h, b in section_blocks],
    )


def _full_recap() -> Recap:
    return _recap(*[(h, [f"clean copy for {h.lower()}."]) for h in SECTION_HEADINGS])


def test_suppress_drops_the_single_offending_section() -> None:
    recap = _full_recap()
    recap = recap.model_copy(
        update={
            "sections": [
                s.model_copy(update={"blocks": ["the betting line was absurd."]})
                if s.heading == "Superlatives"
                else s
                for s in recap.sections
            ]
        }
    )
    report = _report(_finding("banned_topic", "suppress_section", sentence="the betting line was absurd."))
    trimmed, removed = suppress_sections(recap, report)
    assert trimmed is not None
    assert removed == ("Superlatives",)
    assert [s.heading for s in trimmed.sections] == [
        h for h in SECTION_HEADINGS if h != "Superlatives"
    ]


def test_suppress_returns_none_when_the_lead_would_be_removed() -> None:
    recap = _full_recap()
    recap = recap.model_copy(
        update={
            "sections": [
                s.model_copy(update={"blocks": ["bad sentence here."]})
                if s.heading == SECTION_HEADINGS[0]
                else s
                for s in recap.sections
            ]
        }
    )
    report = _report(_finding("slop", "suppress_section", sentence="bad sentence here."))
    trimmed, removed = suppress_sections(recap, report)
    assert trimmed is None
    assert removed == (SECTION_HEADINGS[0],)


def test_suppress_returns_none_when_fewer_than_two_sections_survive() -> None:
    recap = _recap(
        (SECTION_HEADINGS[0], ["lead copy."]),
        ("Superlatives", ["the betting line was absurd."]),
    )
    report = _report(
        _finding("banned_topic", "suppress_section", sentence="the betting line was absurd.")
    )
    trimmed, _removed = suppress_sections(recap, report)
    # only The Lead would survive -> hold
    assert trimmed is None


def test_suppress_is_a_noop_for_an_unmapped_finding() -> None:
    recap = _full_recap()
    report = _report(
        _finding("slop", "suppress_section", sentence="a sentence in no section at all.")
    )
    trimmed, removed = suppress_sections(recap, report)
    assert trimmed is recap
    assert removed == ()


def test_suppress_ignores_non_suppress_tier_findings() -> None:
    recap = _full_recap()
    report = _report(
        _finding("hallucination", "regenerate", sentence="clean copy for the lead."),
    )
    trimmed, removed = suppress_sections(recap, report)
    assert trimmed is recap and removed == ()


def test_suppress_drops_every_section_the_offending_sentence_lands_in() -> None:
    """An identical offending sentence in two sections removes BOTH (no early
    break after the first match)."""
    dupe = "the betting line was a joke."
    recap = _recap(
        (SECTION_HEADINGS[0], ["clean lead copy."]),
        ("Superlatives", [dupe]),
        ("Team Grades", ["also clean."]),
        ("Positional Read", [dupe]),
        ("The Board — Round 1", ["fine here."]),
    )
    report = _report(_finding("slop", "suppress_section", sentence=dupe))
    trimmed, removed = suppress_sections(recap, report)
    assert trimmed is not None
    assert set(removed) == {"Superlatives", "Positional Read"}
    assert [s.heading for s in trimmed.sections] == [
        SECTION_HEADINGS[0],
        "Team Grades",
        "The Board — Round 1",
    ]


# --------------------------------------------------------------------------- #
# SECTION_HEADINGS drift guard
# --------------------------------------------------------------------------- #


def test_section_headings_match_the_template_narrators_actual_output() -> None:
    """``SECTION_HEADINGS`` is a hand-kept copy of the six literals in
    ``template.py``. If a heading is edited there, every real LLM completion would
    silently fail ``structural_ok`` — this pins the tuple to the real render."""
    from datetime import UTC, datetime

    from commishdesk import demo
    from commishdesk.facts import build_draft_recap_facts
    from commishdesk.ingest import build_league_model
    from commishdesk.narrate import render_draft_recap
    from commishdesk.stats import (
        compute_board_metrics,
        compute_consensus_metrics,
        compute_draft_grades,
    )

    model = build_league_model(demo.load_demo_bundle())
    board = compute_board_metrics(model)
    consensus = compute_consensus_metrics(model, demo.demo_consensus_slots())
    grades = compute_draft_grades(model, consensus)
    doc = build_draft_recap_facts(
        model,
        board,
        consensus,
        grades,
        generated_at=datetime.now(tz=UTC),
        consensus_source_name=demo.DEMO_CONSENSUS_SOURCE_NAME,
        consensus_as_of=demo.DEMO_CONSENSUS_AS_OF,
    )
    rendered = tuple(s.heading for s in render_draft_recap(doc.narration).sections)
    assert SECTION_HEADINGS == rendered


# --------------------------------------------------------------------------- #
# guards — import fence + eager re-export
# --------------------------------------------------------------------------- #


def test_response_module_import_fence() -> None:
    tree = ast.parse(RESPONSE_PY.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    roots = {m.split(".")[0] for m in modules}
    assert not roots & {"anthropic", "google", "httpx"}
    commishdesk_mods = {m for m in modules if m.split(".")[0] == "commishdesk"}
    assert commishdesk_mods <= {
        "commishdesk.narrate.safety",
        "commishdesk.narrate.template",
        "commishdesk.facts.schema",
    }, commishdesk_mods


def test_response_helpers_are_eagerly_reexported() -> None:
    import commishdesk.narrate as narrate_pkg

    for name in (
        "SECTION_HEADINGS",
        "TieredResponse",
        "classify",
        "sanitize_completion",
        "structural_ok",
        "suppress_sections",
    ):
        assert name in narrate_pkg.__all__
        assert getattr(narrate_pkg, name) is getattr(response_mod, name)
