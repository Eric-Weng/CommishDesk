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


def _with_league_name(narration: Narration, name: str) -> Narration:
    """A copy of *narration* under a different league name."""
    data = narration.model_dump()
    data["league"]["name"] = name
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
    """A voice's prose contributes multi-word *phrases* (never bare words) as
    warn-tier patterns under its own ``voice:<id>`` category."""
    voice = _FakeVoice(frozenset({"his crypto portfolio"}), voice_id="beat-writer")
    report = check_narration(
        "He would not shut up about his crypto portfolio.", narration, voice=voice
    )
    assert not report.held
    banned = [f for f in report.findings if f.category == "banned_topic"]
    assert any(
        "voice:beat-writer" in f.message and f.matched == "crypto portfolio"
        for f in banned
    ), [f.message for f in report.findings]


def test_voice_keyword_never_reaches_hold_even_beside_a_manager_name(
    narration: Narration,
) -> None:
    """P2 — a voice-derived phrase hit in the same sentence as a manager name is
    a ``banned_topic`` (warn), never a ``hold_issue``."""
    n = _with_managers(narration, "Marcus")
    # "crypto portfolio" is only in the voice's prose, not the curated base lists
    voice = _FakeVoice(frozenset({"his crypto portfolio"}), voice_id="beat-writer")
    report = check_narration(
        "Marcus would not stop talking about his crypto portfolio.", n, voice=voice
    )
    assert report.held is False
    banned = [f for f in report.findings if f.category == "banned_topic"]
    assert any(
        "voice:beat-writer" in f.message and f.matched == "crypto portfolio"
        for f in banned
    ), [f.message for f in report.findings]


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
# D1 — a league's own name can never brick its Issue
# --------------------------------------------------------------------------- #


def _name_trips_a_curated_pattern(name: str) -> bool:
    lists = safety._load_lists()
    return any(
        pattern.search(name)
        for patterns in lists.banned_topics.values()
        for pattern in patterns
    )


@pytest.mark.parametrize("league_name", ["The Sportsbook League", "Politics League"])
def test_a_league_named_after_a_banned_term_still_passes(
    narration: Narration, league_name: str
) -> None:
    """The whole recap for a league whose *name* matches a curated pattern is
    finding-free — the name is masked in the check's working copy — while the
    shipped prose keeps the real name."""
    assert _name_trips_a_curated_pattern(league_name), league_name
    n = _with_league_name(narration, league_name)
    text = recap_to_text(render_draft_recap(n))
    assert league_name in text  # the recap itself is untouched
    report = check_narration(text, n, voice=load_default_voice())
    assert report.ok, [f.message for f in report.findings]


def test_league_name_in_the_narrator_body_is_masked(narration: Narration) -> None:
    n = _with_league_name(narration, "The Sportsbook League")
    report = check_narration("The Sportsbook League had a wild draft.", n)
    assert report.ok, [f.message for f in report.findings]


def test_the_mask_placeholder_is_inert(narration: Narration) -> None:
    """The substitution can only ever *remove* findings: the placeholder itself
    must trip no curated pattern, no personal insult, no slop phrase, no voice
    phrase, and no closed-world check."""
    lists = safety._load_lists()
    placeholder = safety._LEAGUE_PLACEHOLDER
    for patterns in (
        *lists.banned_topics.values(),
        lists.personal_insults,
        lists.slop,
        *safety._voice_patterns(load_default_voice()).values(),
    ):
        for pattern in patterns:
            assert not pattern.search(placeholder), pattern.pattern
    assert not safety._closed_world(placeholder, narration)


def test_mask_is_case_insensitive_and_whole_token(narration: Narration) -> None:
    n = _with_league_name(narration, "Sportsbook Kings")
    masked = safety._mask_league_name("the SPORTSBOOK KINGS drafted well", n)
    assert masked == "the the league drafted well"
    # a longer token that merely contains the name is not rewritten
    assert (
        safety._mask_league_name("Sportsbook Kingsley", n) == "Sportsbook Kingsley"
    )


@pytest.mark.parametrize(
    "league_name, text, gone",
    [
        # \b cannot anchor beside a non-word character — these all silently
        # no-op'd under a bare rf"\b{escape(name)}\b" and the brick came back
        ("Sportsbook!", "The Sportsbook! crew drafted.", "Sportsbook"),
        ("(Sportsbook)", "Welcome to (Sportsbook) this year.", "Sportsbook"),
        ("🏈 Politics League", "🏈 Politics League opens the season.", "Politics"),
        # the text is normalized before the mask runs, so the pattern must be
        # built from the normalized name, not the raw one
        ("Ｓportsbook Kings", "Sportsbook Kings drafted well.", "Sportsbook"),
        # a line wrap or double space between the name's words still matches
        ("Sportsbook Kings", "Sportsbook  Kings drafted well.", "Sportsbook"),
        ("Sportsbook Kings", "Sportsbook\nKings drafted well.", "Sportsbook"),
    ],
)
def test_mask_handles_punctuation_emoji_folding_and_whitespace(
    narration: Narration, league_name: str, text: str, gone: str
) -> None:
    n = _with_league_name(narration, league_name)
    masked = safety._mask_league_name(safety._normalize(text), n)
    assert safety._LEAGUE_PLACEHOLDER in masked, masked
    assert gone not in masked, masked


def test_mask_is_skipped_when_the_league_name_carries_a_manager_name(
    narration: Narration,
) -> None:
    """P10 — masking "Marcus Memorial League" would strip the manager's name out
    of the sentence before the proximity check ran, downgrading a real hold."""
    n = _with_league_name(_with_managers(narration, "Marcus"), "Marcus Memorial League")
    text = "Marcus Memorial League saw Marcus show up hungover."
    assert safety._mask_league_name(text, n) == text
    assert check_narration(text, n).held


@pytest.mark.parametrize("league_name", ["The", "Run", "Season"])
def test_mask_is_skipped_for_a_single_ordinary_word_name(
    narration: Narration, league_name: str
) -> None:
    """P10 — a one-word league name that is an ordinary English word must not
    rewrite every occurrence of that word across the whole recap."""
    n = _with_league_name(narration, league_name)
    text = f"{league_name} is a word that appears all over the {league_name} recap."
    assert safety._mask_league_name(text, n) == text


@pytest.mark.parametrize("league_name", ["", "AA"])
def test_short_or_empty_league_name_skips_masking(
    narration: Narration, league_name: str
) -> None:
    """Below the minimum length the mask is a no-op: no crash, no spurious
    replacement of a two-letter string that appears all over ordinary prose."""
    n = _with_league_name(narration, league_name)
    text = "AA and the rest of the board went as expected."
    assert safety._mask_league_name(text, n) == text
    # the check still runs (no crash) and the placeholder never appears
    report = check_narration(text, n)
    assert not report.held
    assert all(safety._LEAGUE_PLACEHOLDER not in f.sentence for f in report.findings)


def test_findings_report_the_unmasked_sentence(narration: Narration) -> None:
    """P1 — matching happens on the masked working copy, but the finding must
    carry the sentence a reader (and ``suppress_sections``) would actually see.
    Reporting the masked sentence makes section localization miss, which holds
    the Issue — D1's own failure mode, one layer down."""
    n = _with_league_name(narration, "Trench Warfare")
    sentence = "Trench Warfare saw the betting line on this pick go absurd."
    report = check_narration(sentence, n)
    banned = [f for f in report.findings if f.category == "banned_topic"]
    assert banned, [f.message for f in report.findings]
    assert banned[0].sentence == sentence
    assert safety._LEAGUE_PLACEHOLDER not in banned[0].sentence


def test_sentence_reporting_falls_back_when_the_mask_changes_the_split(
    narration: Narration,
) -> None:
    """The alignment guard: the lowercase placeholder can merge two working
    sentences the unmasked text splits. Counts then disagree and the masked
    sentences are reported — conservative, never mismatched."""
    n = _with_league_name(narration, "Sportsbook Kings")
    normalized = safety._normalize("It was done.Sportsbook Kings won it all.")
    working = safety._mask_league_name(normalized, n)
    assert len(safety._sentences(working)) != len(safety._sentences(normalized))
    pairs = safety._sentence_pairs(working, normalized)
    assert all(masked == reported for masked, reported in pairs)


def test_a_standalone_banned_word_is_not_masked_by_a_longer_league_name(
    narration: Narration,
) -> None:
    """Accepted loss, bounded: only the *full* name string is masked, so a
    separate misuse of one of its words is still caught."""
    n = _with_league_name(_with_managers(narration, "Marcus"), "Sportsbook Kings")
    report = check_narration("Marcus basically runs a sportsbook.", n)
    assert report.held
    assert any(f.matched.lower() == "sportsbook" for f in report.findings)


# --------------------------------------------------------------------------- #
# D2 — the three retro sentences that fired on ordinary football prose
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "sentence",
    [
        "Marcus went with his gut on that pick.",
        "Marcus is gambling on upside there.",
        "Marcus is charged with rebuilding the backfield.",
    ],
)
def test_d2_idiomatic_prose_beside_a_name_does_not_hold(
    narration: Narration, sentence: str
) -> None:
    n = _with_managers(narration, "Marcus")
    report = check_narration(sentence, n)
    assert not report.held, [f.message for f in report.findings]


@pytest.mark.parametrize(
    "sentence",
    [
        "Marcus is charged with a felony.",
        "Marcus keeps posting betting advice in the chat.",
        "Marcus has a gambling problem, apparently.",
        # the noun and third-person verb are only ever about the person
        "Marcus is a degenerate gambler.",
        "Marcus gambles every single week.",
    ],
)
def test_d2_real_abuse_beside_a_name_still_holds(
    narration: Narration, sentence: str
) -> None:
    n = _with_managers(narration, "Marcus")
    assert check_narration(sentence, n).held, sentence


@pytest.mark.parametrize(
    "crime",
    ["fraud", "assault", "theft", "murder", "robbery", "two felonies"],
)
def test_charged_with_a_crime_object_still_holds(
    narration: Narration, crime: str
) -> None:
    """P3 — the narrowed ``charged with (...)`` alternation must not have dropped
    real abuse. ``fraud`` / ``assault`` / ``theft`` / ``murder`` / ``robbery``
    appear **nowhere else** in the ``legal`` list, so a hold here can only come
    from this pattern."""
    n = _with_managers(narration, "Marcus")
    report = check_narration(f"Marcus was charged with {crime} last spring.", n)
    assert report.held, [f.message for f in report.findings]
    assert any("charged with" in f.matched for f in report.findings), [
        f.matched for f in report.findings
    ]


# --------------------------------------------------------------------------- #
# A2 / D5 — closed-world is exact membership, and grades are case-folded
# --------------------------------------------------------------------------- #


def _hallucinations(report: SafetyReport) -> set[str]:
    return {f.matched for f in report.findings if f.category == "hallucination"}


def test_closed_world_rejects_a_number_that_is_only_a_substring(
    narration: Narration,
) -> None:
    """A2 — "202" sits inside the payload's "2025" but is not a token of it. The
    old substring test passed it; exact membership does not."""
    payload = safety._payload_tokens(narration)
    assert "202" not in payload
    assert any(token.startswith("202") for token in payload)  # "2025" is there
    report = check_narration("The board ran 202 picks deep.", narration)
    assert "202" in _hallucinations(report)


def test_closed_world_rejects_a_name_that_is_only_a_substring(
    narration: Narration,
) -> None:
    """A2 — "Marc" sits inside the manager name "Marcus" and used to pass."""
    n = _with_managers(narration, "Marcus")
    report = check_narration("Marc left the draft early.", n)
    assert "Marc" in _hallucinations(report)


def test_closed_world_keeps_a_real_token_in_world(narration: Narration) -> None:
    """A2 — the tightening must not flag tokens that really are in the payload."""
    n = _with_managers(narration, "Marcus")
    assert narration.headline_numbers.picks_total == 72
    report = check_narration("Marcus waited 72 picks for it.", n)
    assert not _hallucinations(report), report.findings


def test_lowercase_hallucinated_grade_is_flagged(narration: Narration) -> None:
    """D5 — a lowercase grade used to fall straight through the closed-world
    check: it did not match ``_GRADE`` and it is not capitalised."""
    awarded = {t.grade for t in narration.teams}
    assert "D+" not in awarded and "d+" not in awarded
    report = check_narration("Dana earned a d+ this year.", narration)
    assert "d+" in _hallucinations(report)


@pytest.mark.parametrize(
    "text", ["Vitamin e was the story of the draft.", "e is not a grade at all."]
)
def test_a_bare_lowercase_e_is_not_a_grade(narration: Narration, text: str) -> None:
    """P8 — ``[A-F]`` + ``IGNORECASE`` swept lowercase "e" into the grade branch,
    which ``continue``\\ s before the stop-set lookup, so an ordinary word became
    a hallucination. The class is spelled out instead."""
    assert not [t for t in safety._closed_world(text, narration) if t.lower() == "e"]


def test_lowercase_real_grade_is_not_flagged(narration: Narration) -> None:
    """D5 — the case-folded comparison cuts both ways: a grade that *was*
    awarded is in-world however it is cased."""
    assert "A+" in {t.grade for t in narration.teams}
    report = check_narration("dana earned an a+ for that haul.", narration)
    assert not _hallucinations(report), report.findings


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
    """D3 — multi-word phrases only. A phrase is split on commas / "or" / "and",
    leading determiners and possessive owners are dropped, and a fragment with
    fewer than two words contributes nothing."""
    voice = _FakeVoice(
        frozenset(
            {
                "politics, religion, or nationality",
                "a player's crypto portfolio and day-trading habit",
            }
        )
    )
    pats = safety._voice_patterns(voice)
    (category,) = pats
    assert category == "voice:test-voice"
    matched = {p.pattern for p in pats[category]}
    # the possessive owner ("a player's") is stripped, the phrase survives whole
    assert r"\bcrypto[\s-]+portfolio\b" in matched
    # hyphen or whitespace between words, either way
    assert r"\bday[\s-]+trading[\s-]+habit\b" in matched
    # single-word fragments are dropped entirely — the curated
    # ``politics_religion`` category owns those terms
    assert not any("politics" in p or "religion" in p or "nationality" in p for p in matched)


def test_voice_extraction_never_arms_a_bare_word() -> None:
    """D3 (the retro finding) — the default voice's prose must not re-arm the
    exact bare terms ``safety_lists.toml`` deliberately excluded."""
    pats = safety._voice_patterns(load_default_voice())
    (patterns,) = pats.values()
    for pattern in patterns:
        assert "[\\s-]+" in pattern.pattern, pattern.pattern
    compiled = {p.pattern for p in patterns}
    for bare in ("injury", "physical", "trouble", "personal", "family", "medical",
                 "legal", "betting", "gambling", "weight", "arrests"):
        assert rf"\b{bare}\b" not in compiled, bare


def test_voice_extraction_arms_the_head_noun_core_of_a_long_fragment() -> None:
    """P7 — a four-word fragment lifted verbatim out of the voice's prose only
    fires if the narrator quotes that prose, which never happens. The trailing
    two-word head-noun core is what actually does the work."""
    fragments = safety._voice_fragments(load_default_voice().banned_topics)
    assert "real life injury history" in fragments
    assert "injury history" in fragments  # the core of it
    assert "off field legal trouble" in fragments
    assert "legal trouble" in fragments
    assert "medical status" in fragments
    assert "betting advice" in fragments
    # still never a bare word
    assert all(len(f.split()) >= 2 for f in fragments), fragments


@pytest.mark.parametrize(
    "sentence",
    [
        "Marcus took the injury-prone back in round three.",
        "The physical tools are there, and Marcus saw them.",
        "Marcus had trouble finding a starter after that.",
    ],
)
def test_d3_scouting_talk_is_clean_with_the_default_voice_merged(
    narration: Narration, sentence: str
) -> None:
    """D3 acceptance — the retro's three scouting sentences produce **no finding**
    with the default voice merged in."""
    n = _with_managers(narration, "Marcus")
    report = check_narration(sentence, n, voice=load_default_voice())
    assert report.ok, [f.message for f in report.findings]


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
