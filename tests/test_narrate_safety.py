"""Story 3.4 — ``commishdesk/narrate/safety.py``: the deterministic content-safety check.

One test per I/O & Edge-Case Matrix row, plus units for the helpers
(``_manager_names`` / ``_normalize`` / the voice keyword merge), the list-file
load/compile path, the demo-recap-passes-clean assertion, and the structural
guards (import fence, eager re-export).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from commishdesk import demo
from commishdesk.errors import NarratorError
from commishdesk.facts import build_draft_recap_facts
from commishdesk.facts.schema import Narration
from commishdesk.ingest import build_league_model
from commishdesk.narrate import (
    SafetyFinding,
    SafetyReport,
    check_narration,
    recap_to_text,
    render_draft_recap,
    safety,
)
from commishdesk.stats import (
    compute_board_metrics,
    compute_consensus_metrics,
    compute_draft_grades,
)
from commishdesk.voices import load_default_voice

SAFETY_PY = Path(safety.__file__)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _fresh_lists_cache() -> None:
    """Every test starts and ends with the real packaged list file loaded."""
    safety._load_lists.cache_clear()
    yield
    safety._load_lists.cache_clear()


@pytest.fixture(scope="module")
def narration() -> Narration:
    # mirrors tests/test_narrate_llm.py::narration
    model = build_league_model(demo.load_demo_bundle())
    board = compute_board_metrics(model)
    consensus = compute_consensus_metrics(model, demo.demo_consensus_slots())
    grades = compute_draft_grades(model, consensus)
    doc = build_draft_recap_facts(
        model,
        board,
        consensus,
        grades,
        generated_at="2026-09-07T00:00:00Z",
        consensus_source_name=demo.DEMO_CONSENSUS_SOURCE_NAME,
        consensus_as_of=demo.DEMO_CONSENSUS_AS_OF,
    )
    return doc.narration


@pytest.fixture(scope="module")
def demo_recap_text(narration: Narration) -> str:
    return recap_to_text(render_draft_recap(narration))


def _with_managers(narration: Narration, *names: str) -> Narration:
    """A copy of *narration* whose first N team managers are renamed."""
    data = narration.model_dump()
    for team, name in zip(data["teams"], names, strict=False):
        team["manager"] = name
    return Narration.model_validate(data)


class _FakeVoice:
    def __init__(self, topics: frozenset[str], voice_id: str = "test-voice") -> None:
        self.system_prompt = "x"
        self.banned_topics = topics
        self.voice_id = voice_id


# --------------------------------------------------------------------------- #
# I/O & Edge-Case Matrix
# --------------------------------------------------------------------------- #


def test_clean_recap_has_no_findings(demo_recap_text: str, narration: Narration) -> None:
    report = check_narration(demo_recap_text, narration)
    assert report == SafetyReport(findings=())
    assert report.ok and not report.held


def test_legit_pick_roast_is_not_flagged(narration: Narration) -> None:
    report = check_narration("reached for a kicker in round 11", narration)
    assert report.ok, report.findings


def test_manager_plus_banned_category_holds(narration: Narration) -> None:
    n = _with_managers(narration, "Marcus")
    report = check_narration("Marcus clearly drafted hungover.", n)
    assert report.held
    hold = report.findings[0]
    assert hold.category == "named_person_proximity"
    assert hold.severity == "hold_issue"
    assert "hungover" in hold.matched


def test_manager_plus_personal_insult_holds(narration: Narration) -> None:
    n = _with_managers(narration, "Dana")
    report = check_narration("Dana is an idiot.", n)
    assert report.held
    assert report.findings[0].category == "named_person_proximity"


def test_banned_topic_without_a_name_only_warns(narration: Narration) -> None:
    report = check_narration("The betting line on this class was a joke.", narration)
    assert not report.held and not report.ok
    assert any(f.category == "banned_topic" for f in report.findings)
    assert all(f.severity != "hold_issue" for f in report.findings)


def test_hallucinated_noun_and_number_flag_regenerate(narration: Narration) -> None:
    report = check_narration(
        "Jamarcus Fakename posted a delta of 8675309.", narration
    )
    assert not report.held
    flagged = {f.matched for f in report.findings if f.category == "hallucination"}
    assert {"Fakename", "8675309"} <= flagged
    assert all(f.severity == "regenerate" for f in report.findings if f.category == "hallucination")


def test_a_real_letter_grade_is_not_flagged(narration: Narration) -> None:
    awarded = {t.grade for t in narration.teams}
    assert "A+" in awarded
    report = check_narration("Pull-Guard Pumas: A+.", narration)
    assert report.ok, report.findings


def test_unicode_evasion_is_normalized_then_still_holds(narration: Narration) -> None:
    n = _with_managers(narration, "Marcus")
    report = check_narration("M​a​rcus is an idiot.", n)
    assert report.held
    assert report.findings[0].category == "named_person_proximity"


def test_slop_warns_and_proceeds(narration: Narration) -> None:
    report = check_narration("make no mistake, buckle up", narration)
    assert not report.held
    slop = [f for f in report.findings if f.category == "slop"]
    assert {f.matched for f in slop} == {"make no mistake", "buckle up"}


def test_voice_banned_topics_merge_by_keyword(narration: Narration) -> None:
    voice = _FakeVoice(frozenset({"politics, religion, or nationality"}), voice_id="beat-writer")
    report = check_narration("He would not shut up about his politics.", narration, voice=voice)
    assert not report.held
    banned = [f for f in report.findings if f.category == "banned_topic"]
    assert any("voice:beat-writer" in f.message and f.matched == "politics" for f in banned)


def test_voice_keyword_never_reaches_hold_even_beside_a_manager_name(
    narration: Narration,
) -> None:
    """P2 — a voice-derived keyword hit in the same sentence as a manager name is
    a ``banned_topic`` (warn), never a ``hold_issue``."""
    n = _with_managers(narration, "Marcus")
    # "crypto" is only in the voice's prose, not the curated base lists
    voice = _FakeVoice(frozenset({"his crypto day-trading side hustle"}), voice_id="beat-writer")
    report = check_narration("Marcus would not stop talking about his crypto.", n, voice=voice)
    assert report.held is False
    banned = [f for f in report.findings if f.category == "banned_topic"]
    assert any("voice:beat-writer" in f.message and f.matched == "crypto" for f in banned)


def test_malformed_lists_file_raises_narrator_error(monkeypatch, narration: Narration) -> None:
    monkeypatch.setattr(safety, "_lists_toml_text", lambda: "not = valid ][ toml")
    safety._load_lists.cache_clear()
    with pytest.raises(NarratorError):
        check_narration("anything", narration)


def test_uncompilable_pattern_raises_narrator_error() -> None:
    with pytest.raises(NarratorError):
        safety._parse_lists('[banned_topics]\nlegal = ["("]\n')


def test_missing_lists_file_raises_narrator_error(monkeypatch, narration: Narration) -> None:
    """P4 — a missing / unreadable packaged list file must surface as
    ``NarratorError`` (an ``OSError`` would escape the CLI's per-league catch)."""
    def _boom() -> str:
        raise FileNotFoundError("safety_lists.toml")

    monkeypatch.setattr(safety, "_lists_toml_text", _boom)
    safety._load_lists.cache_clear()
    with pytest.raises(NarratorError):
        check_narration("anything", narration)


# --------------------------------------------------------------------------- #
# P3 — the hold tier does not fire on legitimate pick / approach roasting
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "sentence",
    [
        "Marcus reached for a kicker in round 11.",
        "Marcus took the injury-prone back and hoped for the best.",
        "Dana wasted the third-rounder on a backup tight end.",
        "A clueless approach to the position, honestly, from Marcus.",
        "Marcus prayed a running back would fall to him and one did.",
    ],
)
def test_pick_and_approach_roasting_next_to_a_name_does_not_hold(
    narration: Narration, sentence: str
) -> None:
    n = _with_managers(narration, "Marcus", "Dana")
    report = check_narration(sentence, n)
    assert not report.held, [f.message for f in report.findings]


def test_the_spec_hungover_example_still_holds(narration: Narration) -> None:
    n = _with_managers(narration, "Marcus")
    assert check_narration("Marcus clearly drafted hungover.", n).held


# --------------------------------------------------------------------------- #
# demo recap passes clean — the AC hook
# --------------------------------------------------------------------------- #


def test_demo_template_recap_passes_clean_with_the_default_voice(
    demo_recap_text: str, narration: Narration
) -> None:
    """The committed demo recap (template narrator on tests/fixtures/rookie-draft.json)
    must be finding-free, both bare and with the default voice merged in."""
    for voice in (None, load_default_voice()):
        report = check_narration(demo_recap_text, narration, voice=voice)
        assert report.ok, [f.message for f in report.findings]


# --------------------------------------------------------------------------- #
# helper units
# --------------------------------------------------------------------------- #


def test_manager_names_pulls_every_manager_and_left_waiting(narration: Narration) -> None:
    names = safety._manager_names(narration)
    assert "Pull-Guard Pumas" in names
    # left_waiting on the QB run carries manager names too
    assert "Man-Coverage Meerkats" in names
    # None / empty / single-char entries are dropped; 2-char handles are kept
    assert all(isinstance(n, str) and len(n) >= 2 for n in names)


def test_manager_names_is_case_insensitive_for_proximity(narration: Narration) -> None:
    n = _with_managers(narration, "Marcus")
    pats = safety._compile_name_patterns(safety._manager_names(n))
    assert safety._sentence_has_name("well, MARCUS did it again", pats)


def test_manager_name_matching_is_word_boundary_not_substring(narration: Narration) -> None:
    """P1 — "Sam" must not fire inside "same", "Ben" inside "benched"."""
    n = _with_managers(narration, "Sam", "Ben")
    # "same"/"benched" contain the names as substrings but not as words
    clean = check_narration("The same benched approach, drafted hungover by nobody.", n)
    assert not clean.held, clean.findings
    # a real word-boundary hit beside a banned term still holds
    held = check_narration("Sam clearly drafted hungover.", n)
    assert held.held


def test_two_char_manager_handle_holds_beside_a_banned_term(narration: Narration) -> None:
    """P1 — a 2-char handle is kept (word boundaries make it safe) and still holds."""
    n = _with_managers(narration, "AJ")
    assert check_narration("AJ showed up hungover to the draft.", n).held
    # "AJ" must not fire inside "AJAX"
    assert not check_narration("AJAX Jones drafted hungover.", n).held


def test_normalize_strips_control_and_format_chars_but_keeps_newlines() -> None:
    out = safety._normalize("a​b­\ncd")
    assert out == "ab\ncd"


def test_normalize_applies_nfkc() -> None:
    # ﬀ (U+FB00) -> "ff" under NFKC
    assert safety._normalize("oﬀ side") == "off side"


def test_voice_keyword_extraction_rules() -> None:
    voice = _FakeVoice(frozenset({"politics, religion, or nationality", "his day-to-day life"}))
    pats = safety._voice_patterns(voice)
    (category,) = pats
    assert category == "voice:test-voice"
    matched = {p.pattern for p in pats[category]}
    assert r"\bpolitics\b" in matched
    assert r"\breligion\b" in matched
    assert r"\bnationality\b" in matched
    # "or" is length < 4; "life" is in the stop-set
    assert r"\bor\b" not in matched
    assert r"\blife\b" not in matched


def test_voice_patterns_empty_or_none_gives_base_lists_only() -> None:
    assert safety._voice_patterns(None) == {}
    assert safety._voice_patterns(_FakeVoice(frozenset())) == {}


# --------------------------------------------------------------------------- #
# list file integrity
# --------------------------------------------------------------------------- #


def test_packaged_lists_load_and_compile_with_the_expected_categories() -> None:
    lists = safety._load_lists()
    assert set(lists.banned_topics) == {
        "injury_medical",
        "legal",
        "personal_life",
        "appearance",
        "politics_religion",
        "gambling",
        "substances",
    }
    assert lists.personal_insults and lists.slop
    assert all(hasattr(p, "search") for p in lists.slop)


# --------------------------------------------------------------------------- #
# P7 — closed-world noise reduction
# --------------------------------------------------------------------------- #


def test_closed_world_splits_hyphen_and_slash_tokens(narration: Narration) -> None:
    # "12-team" -> "12" (a real pick number) + "team" (a stop word); "and/or" ->
    # two stop words. No hallucination despite the capitalised compound.
    report = check_narration("This 12-team league went WR-heavy, and/or RB-heavy.", narration)
    assert not any(f.category == "hallucination" for f in report.findings), report.findings


def test_closed_world_strips_ordinal_suffixes(narration: Narration) -> None:
    report = check_narration("He grabbed his guy with the 11th pick.", narration)
    assert not any(f.category == "hallucination" for f in report.findings), report.findings


def test_closed_world_stops_capitalised_sentence_adverbs(narration: Narration) -> None:
    report = check_narration("Meanwhile, Granted, Regardless, Admittedly.", narration)
    assert not any(f.category == "hallucination" for f in report.findings), report.findings


def test_closed_world_grade_check_is_against_the_awarded_set(narration: Narration) -> None:
    awarded = {t.grade for t in narration.teams}
    assert "F-" not in awarded
    # a hallucinated suffixed grade is flagged
    bad = check_narration("Goal-Line Gazelles: F-.", narration)
    assert any(f.category == "hallucination" and f.matched == "F-" for f in bad.findings)
    # a bare scale letter in a methodology sentence is not
    scale = check_narration("The scale runs A to F with plus and minus.", narration)
    assert not any(f.category == "hallucination" for f in scale.findings), scale.findings


# --------------------------------------------------------------------------- #
# P8 — sentence splitting
# --------------------------------------------------------------------------- #


def test_sentence_split_on_missing_space_after_punctuation(narration: Narration) -> None:
    n = _with_managers(narration, "Marcus")
    # name + banned term in the *same* fragment still holds
    assert check_narration("Marcus drafted hungover.He said it was fine.", n).held
    # name and banned term in genuinely separate fragments do not
    assert not check_narration("Marcus reached early.Someone else drafted hungover.", n).held


def test_category_severity_map_shape() -> None:
    assert safety.CATEGORY_SEVERITY == {
        "named_person_proximity": "hold_issue",
        "banned_topic": "suppress_section",
        "hallucination": "regenerate",
        "slop": "suppress_section",
    }


# --------------------------------------------------------------------------- #
# determinism + structural guards
# --------------------------------------------------------------------------- #


def test_check_is_byte_identical_across_calls(narration: Narration) -> None:
    n = _with_managers(narration, "Marcus", "Dana")
    text = "Marcus drafted hungover. Dana is an idiot. make no mistake."
    a = check_narration(text, n, voice=load_default_voice())
    b = check_narration(text, n, voice=load_default_voice())
    assert a.model_dump_json() == b.model_dump_json()
    assert [type(f) for f in a.findings] == [SafetyFinding] * len(a.findings)


def test_safety_module_import_fence() -> None:
    tree = ast.parse(SAFETY_PY.read_text(encoding="utf-8"))
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
        "commishdesk.errors",
        "commishdesk.facts.schema",
        "commishdesk.voices",
    }, commishdesk_mods


def test_check_narration_is_eagerly_reexported() -> None:
    import commishdesk.narrate as narrate_pkg

    for name in ("SafetyFinding", "SafetyReport", "check_narration"):
        assert name in narrate_pkg.__all__
        assert getattr(narrate_pkg, name) is getattr(safety, name)
