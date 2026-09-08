"""Story 3.3 — the default ``Voice`` (``commishdesk/voices/beat_writer.py``) and the
``tests/eval/voices/`` harness.

Groups, one per Tasks & Acceptance bullet:

* the one-reference-impl guard (AC3 hook — also enforced by
  ``tests/test_extension_zones.py``);
* ``Voice`` conformance: ``isinstance`` against the ``@runtime_checkable`` protocol,
  ``voice_id == "beat-writer"``, a non-empty ``system_prompt`` that names the six
  section headings and the closed-world rule, a non-empty ``frozenset[str]``
  ``banned_topics``;
* ``_score_recap`` — the ``tests/``-local heuristic scorer — plus its unit tests
  (a clean sample passes; an invented proper noun, number, or letter grade fails
  closed-world; a length outside ±15% fails);
* the committed ``beat-writer.md`` sample scored against a freshly built demo
  narration;
* an **opt-in** live test that scores a real model generation — it makes a billed
  provider call and runs only when ``COMMISHDESK_LIVE_LLM`` is set, so a plain
  ``pytest`` run (developer laptop or CI) always skips it.

The scorer here is deliberately lighter than Story 3.4's ``narrate/safety.py``
(the real closed-world gate on every Issue). It only certifies the recorded
sample and a live smoke output.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from commishdesk import demo
from commishdesk.facts import build_draft_recap_facts
from commishdesk.facts.schema import Narration
from commishdesk.ingest import build_league_model
from commishdesk.llmconfig import load_llm_config
from commishdesk.narrate import narrate_draft_recap
from commishdesk.narrate.llm import build_client
from commishdesk.stats import (
    compute_board_metrics,
    compute_consensus_metrics,
    compute_draft_grades,
)
from commishdesk.voices import Voice, load_default_voice

REPO_ROOT = Path(__file__).resolve().parent.parent
VOICES_PKG = REPO_ROOT / "commishdesk" / "voices"
EVAL_DIR = REPO_ROOT / "tests" / "eval" / "voices"
SAMPLE = EVAL_DIR / "beat-writer.md"

#: The length target the recorded sample and a live generation are held to:
#: the raw character length of the phase-0 draft-recap reference newsletter body.
#: Documented in ``tests/eval/voices/README.md``; kept in sync with the number
#: quoted there.
REFERENCE_TARGET_CHARS = 9400

SECTION_HEADINGS = (
    "The Lead",
    "The Board — Round 1",
    "Superlatives",
    "Team Grades",
    "Positional Read",
    "The Picks We'll Be Arguing About in December",
)


# --------------------------------------------------------------------------- #
# demo narration fixture — mirrors tests/test_narrate_llm.py::narration
# --------------------------------------------------------------------------- #


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
        generated_at="2026-09-07T00:00:00Z",
        consensus_source_name=demo.DEMO_CONSENSUS_SOURCE_NAME,
        consensus_as_of=demo.DEMO_CONSENSUS_AS_OF,
    )
    return doc.narration


# --------------------------------------------------------------------------- #
# (a) exactly one reference Voice file
# --------------------------------------------------------------------------- #


def test_voices_zone_ships_exactly_one_reference_impl() -> None:
    modules = sorted(
        p.name
        for p in VOICES_PKG.glob("*.py")
        if p.name != "__init__.py" and not p.name.startswith("_")
    )
    assert modules == ["beat_writer.py"], modules
    # and no non-underscore subpackage sneaking a second impl in
    subpkgs = [
        p.name
        for p in VOICES_PKG.iterdir()
        if p.is_dir() and not p.name.startswith("_") and p.name != "__pycache__"
    ]
    assert subpkgs == [], subpkgs


# --------------------------------------------------------------------------- #
# (b) Voice conformance
# --------------------------------------------------------------------------- #


def test_default_voice_conforms_to_the_protocol() -> None:
    voice = load_default_voice()
    assert isinstance(voice, Voice)
    assert voice.voice_id == "beat-writer"
    assert isinstance(voice.system_prompt, str) and voice.system_prompt.strip()
    assert isinstance(voice.banned_topics, frozenset)
    assert voice.banned_topics  # non-empty
    assert all(isinstance(topic, str) and topic for topic in voice.banned_topics)


def test_default_voice_is_a_shared_singleton() -> None:
    assert load_default_voice() is load_default_voice()


def test_system_prompt_encodes_layer1_safety_and_structure() -> None:
    prompt = load_default_voice().system_prompt
    lowered = prompt.lower()
    # closed-world (AD-12)
    assert "only" in lowered and "json" in lowered
    assert "never invent" in lowered
    # roast the pick / approach, not the person
    assert "never the" in lowered and "person" in lowered
    # one true positive per team
    assert "positive" in lowered
    # the six section headings, verbatim
    for heading in SECTION_HEADINGS:
        assert heading in prompt, heading
    # a target length band
    assert "9,400" in prompt or "9400" in prompt


def _imported_modules(src: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_voices_zone_module_keeps_the_v0_marker_and_imports_only_the_local_protocol() -> None:
    assert "# v0" in (VOICES_PKG / "__init__.py").read_text(encoding="utf-8")
    for src_path in VOICES_PKG.glob("*.py"):
        modules = _imported_modules(src_path.read_text(encoding="utf-8"))
        roots = {name.split(".")[0] for name in modules}
        assert "anthropic" not in roots and "google" not in roots, src_path.name
        for banned in ("commishdesk.facts", "commishdesk.narrate"):
            assert not any(m == banned or m.startswith(banned + ".") for m in modules), (
                src_path.name,
                banned,
            )
        for module in modules:
            if module.startswith("commishdesk"):
                # only the zone package itself (the local Voice protocol) or a
                # sibling module inside it
                assert module == "commishdesk.voices" or module.startswith(
                    "commishdesk.voices."
                ), (src_path.name, module)


# --------------------------------------------------------------------------- #
# (c) the scorer + its unit tests
# --------------------------------------------------------------------------- #

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.’'%$+/-]*")

#: Small stop set: scaffolding and section-heading words that are capitalised or
#: numeric in prose but are not proper nouns / facts to trace (Design Notes:
#: "section headings, Round, Best, Reach, …"). Kept deliberately short.
_STOP: frozenset[str] = frozenset(
    {
        # articles / conjunctions / prepositions / pronouns / demonstratives
        "a", "an", "and", "or", "but", "nor", "so", "yet", "for", "of", "in", "on",
        "at", "to", "by", "as", "with", "from", "into", "than", "then", "that",
        "this", "these", "those", "there", "their", "them", "they", "it", "its",
        "he", "his", "him", "she", "her", "we", "we'll", "us", "our", "you",
        "your", "i", "if", "is", "was", "were", "be", "been", "being",
        "not", "no",
        # common sentence-openers / adverbs
        "the", "now", "here", "how", "when", "where", "what", "who", "whom",
        "which", "while", "after", "before", "once", "still", "also", "even",
        "just", "only", "both", "each", "every", "some", "any", "all", "most",
        "more", "less", "nobody", "everyone", "someone", "nothing", "everything",
        "because", "since", "until", "about", "over", "under", "between", "whether",
        # section-heading / scaffolding words
        "round", "board", "lead", "superlatives", "grades", "grade", "team",
        "teams", "positional", "read", "picks", "pick", "december", "arguing",
        "best", "reach", "value", "swing", "swings", "boldest", "biggest",
        "runner-up", "consensus", "draft", "recap", "season", "waiting",
        "early", "window", "later", "run", "runs",
        # position nouns not spelled out in the narration projection
        "quarterback", "quarterbacks", "receiver", "receivers", "wideout",
        "wideouts", "tight", "flex", "kicker", "defense",
        # spelled integers used as plain scaffolding (matched case-folded, so a
        # capitalised "Seventeen" at a sentence start is stopped too)
        "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
        "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
        "seventeen", "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty",
        "hundred",
    }
)

#: A letter-grade token (``A`` / ``B+`` / ``D-``). Verified as a substring of the
#: payload *before* the generic strip runs — otherwise ``A+`` / ``B+`` collapse to
#: ``a`` / ``b`` and a hallucinated grade sails through.
_GRADE = re.compile(r"^[A-F][+-]?$")


@dataclass(frozen=True)
class RecapScore:
    """The heuristic verdict on one recap against a narration payload."""

    char_count: int
    target_chars: int
    length_ok: bool
    closed_world_ok: bool
    unknown_tokens: tuple[str, ...]


def _score_recap(text: str, narration: Narration, *, target_chars: int) -> RecapScore:
    """A ``tests/``-local heuristic: every capitalised or numeric token in *text*
    (minus a small stop set) must appear, case-folded, as a substring of the
    narration payload's JSON; and the recap's length must be within ±15% of
    *target_chars*.
    """
    haystack = narration.model_dump_json().lower()
    unknown: list[str] = []
    for raw in _TOKEN.findall(text):
        # drop trailing sentence punctuation but keep a grade's +/- suffix
        trimmed = raw.strip(".,;:!?()[]\"'’")
        if _GRADE.match(trimmed):
            if trimmed.lower() not in haystack:
                unknown.append(trimmed)
            continue
        token = trimmed.strip("%$+/-")
        if not token:
            continue
        checkworthy = token[:1].isupper() or any(ch.isdigit() for ch in token)
        if not checkworthy:
            continue
        folded = token.lower()
        if folded in _STOP:
            continue
        if folded not in haystack:
            unknown.append(token)
    char_count = len(text)
    length_ok = abs(char_count - target_chars) <= round(0.15 * target_chars)
    return RecapScore(
        char_count=char_count,
        target_chars=target_chars,
        length_ok=length_ok,
        closed_world_ok=not unknown,
        unknown_tokens=tuple(dict.fromkeys(unknown)),
    )


def test_scorer_passes_a_clean_in_world_sample(narration: Narration) -> None:
    text = (
        "Trench Warfare — 2025 Draft Recap\n\n"
        "Pull-Guard Pumas opened with Ashton Jeanty at 1.01. "
        "Blitz Alpacas found Lan Larison at 6.11, a delta of 17. "
        + "Gridiron Gophers drafted six running backs. " * 40
    )
    score = _score_recap(text, narration, target_chars=len(text))
    assert score.closed_world_ok, score.unknown_tokens
    assert score.length_ok


def test_scorer_flags_an_invented_proper_noun(narration: Narration) -> None:
    text = (
        "Pull-Guard Pumas took Ashton Jeanty at 1.01, then traded up for "
        "Jamarcus Fakename, who does not appear anywhere in the board."
    )
    score = _score_recap(text, narration, target_chars=len(text))
    assert not score.closed_world_ok
    assert "Fakename" in score.unknown_tokens


def test_scorer_flags_an_invented_number(narration: Narration) -> None:
    text = "Blitz Alpacas landed Lan Larison at 6.11 with a delta of 8675309."
    score = _score_recap(text, narration, target_chars=len(text))
    assert not score.closed_world_ok
    assert "8675309" in score.unknown_tokens


def test_scorer_flags_a_hallucinated_letter_grade(narration: Narration) -> None:
    """The demo facts award no F- to anyone; a recap that does must fail
    closed-world (a naive strip would collapse ``F-`` to the stop word ``f``)."""
    awarded = {team.grade for team in narration.teams}
    assert "F-" not in awarded
    text = "Goal-Line Gazelles: F-. Pull-Guard Pumas: A+. Blitz Alpacas: A+."
    score = _score_recap(text, narration, target_chars=len(text))
    assert not score.closed_world_ok
    assert "F-" in score.unknown_tokens
    # a real grade in the same text is not flagged
    assert "A+" not in score.unknown_tokens


def test_scorer_stops_a_capitalised_spelled_integer(narration: Narration) -> None:
    text = "Seventeen slots of value went to Blitz Alpacas at 6.11."
    score = _score_recap(text, narration, target_chars=len(text))
    assert score.closed_world_ok, score.unknown_tokens


@pytest.mark.parametrize("delta", [-0.16, 0.16])
def test_scorer_rejects_a_length_outside_the_band(narration: Narration, delta: float) -> None:
    target = REFERENCE_TARGET_CHARS
    text = "x" * round(target * (1 + delta))
    score = _score_recap(text, narration, target_chars=target)
    assert not score.length_ok


def test_scorer_accepts_a_length_at_the_edge_of_the_band(narration: Narration) -> None:
    target = REFERENCE_TARGET_CHARS
    for chars in (round(target * 0.85) + 1, round(target * 1.15) - 1):
        score = _score_recap("y" * chars, narration, target_chars=target)
        assert score.length_ok, chars


# --------------------------------------------------------------------------- #
# (d) the committed sample
# --------------------------------------------------------------------------- #


def test_committed_sample_is_closed_world_and_on_length(narration: Narration) -> None:
    assert SAMPLE.is_file(), f"{SAMPLE} is missing"
    text = SAMPLE.read_text(encoding="utf-8")
    score = _score_recap(text, narration, target_chars=REFERENCE_TARGET_CHARS)
    assert score.closed_world_ok, f"out-of-world tokens: {score.unknown_tokens}"
    assert score.length_ok, f"{score.char_count} chars, target {score.target_chars}"


def test_committed_sample_carries_every_section_heading() -> None:
    text = SAMPLE.read_text(encoding="utf-8")
    for heading in SECTION_HEADINGS:
        assert f"## {heading}" in text, heading


def test_eval_dir_keeps_its_gitkeep_and_readme() -> None:
    assert (EVAL_DIR / ".gitkeep").is_file()
    readme = (EVAL_DIR / "README.md").read_text(encoding="utf-8")
    assert str(REFERENCE_TARGET_CHARS) in readme
    assert "rookie-draft.json" in readme


# --------------------------------------------------------------------------- #
# (e) opt-in live generation
# --------------------------------------------------------------------------- #

#: The single skip-reason both guard branches emit. Registered (by prefix) in
#: ``tests/conftest.py::_KNOWN_SKIP_PREFIXES``.
_LIVE_SKIP = "COMMISHDESK_LIVE_LLM is unset; the live voice eval is opt-in"


def test_live_generation_scores_in_bounds(narration: Narration) -> None:
    """Score a real model generation against the same rubric as the recorded
    sample. **Opt-in**, never run by default: it makes a billed provider call, so
    it runs only when ``COMMISHDESK_LIVE_LLM`` is set (and a provider key + the
    ``[llm]`` extra are actually usable). This is the one skip Story 3.3 adds to
    the accounted set in ``tests/conftest.py``."""
    if not os.environ.get("COMMISHDESK_LIVE_LLM"):
        pytest.skip(_LIVE_SKIP)
    result = narrate_draft_recap(
        narration,
        load_default_voice(),
        load_llm_config(),
        llm_enabled=True,
        client_factory=build_client,
    )
    if result.narrator == "template":  # secondary guard: no provider was usable
        pytest.skip(_LIVE_SKIP)
    score = _score_recap(result.text, narration, target_chars=REFERENCE_TARGET_CHARS)
    assert score.closed_world_ok, score.unknown_tokens
    assert score.length_ok, score.char_count
