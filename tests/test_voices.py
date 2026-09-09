"""Story 3.3 — the default ``Voice`` (``commishdesk/voices/beat_writer.py``) and the
``tests/eval/voices/`` harness.

Groups, one per Tasks & Acceptance bullet:

* the one-reference-impl guard (AC3 hook — also enforced by
  ``tests/test_extension_zones.py``);
* ``Voice`` conformance: ``isinstance`` against the ``@runtime_checkable`` protocol,
  ``voice_id == "beat-writer"``, a non-empty ``system_prompt`` that names the six
  section headings and the closed-world rule, a non-empty ``frozenset[str]``
  ``banned_topics``;
* ``_score_recap`` — the eval scorer — plus its unit tests (a clean sample
  passes; an invented proper noun, number, or letter grade fails closed-world; a
  length outside ±15% fails);
* the committed ``beat-writer.md`` sample scored against a freshly built demo
  narration;
* an **opt-in** live test that scores a real model generation — it makes a billed
  provider call and runs only when ``COMMISHDESK_LIVE_LLM`` is set, so a plain
  ``pytest`` run (developer laptop or CI) always skips it.

The scorer's closed-world verdict is **not** a second implementation: it calls
``commishdesk.narrate.safety._closed_world`` — the same gate every Issue passes
through in production — so the eval harness and the shipping gate cannot drift
apart (the pre-4.1 calibration gate, retro action item A3; the two copies had
already diverged). Only the ±15 % length band is scored here.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from commishdesk import demo
from commishdesk.facts import build_draft_recap_facts
from commishdesk.facts.schema import Narration
from commishdesk.ingest import build_league_model
from commishdesk.llmconfig import load_llm_config
from commishdesk.narrate import narrate_draft_recap, safety
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

@dataclass(frozen=True)
class RecapScore:
    """The heuristic verdict on one recap against a narration payload."""

    char_count: int
    target_chars: int
    length_ok: bool
    closed_world_ok: bool
    unknown_tokens: tuple[str, ...]


def _score_recap(text: str, narration: Narration, *, target_chars: int) -> RecapScore:
    """Score one recap: closed-world by **the production gate**
    (:func:`commishdesk.narrate.safety.closed_world_tokens`, which owns the whole
    normalize → league-name-mask → closed-world pipeline ``check_narration``
    runs), plus a ``tests/``-local length band of ±15% around *target_chars*.

    The closed-world half owns no logic at all — not a regex, not a stop set, not
    even the choice of which preprocessing steps to apply. This module used to
    carry a copy of that logic; the copy drifted (it matched a grade as a bare
    JSON substring, and never learned the token-set tightening or the ordinal /
    hyphen splitting), so the eval harness certified samples the shipping gate
    would have flagged. Retro action item A3: one implementation, one entry
    point, called from both places.
    """
    unknown = safety.closed_world_tokens(text, narration)
    char_count = len(text)
    length_ok = abs(char_count - target_chars) <= round(0.15 * target_chars)
    return RecapScore(
        char_count=char_count,
        target_chars=target_chars,
        length_ok=length_ok,
        closed_world_ok=not unknown,
        unknown_tokens=unknown,
    )


def test_scorer_delegates_closed_world_to_the_production_entry_point(
    narration: Narration, monkeypatch
) -> None:
    """A3 — the real invariant, not a name check: whatever the production entry
    point says is out of world *is* the scorer's verdict. A reintroduced local
    copy (under any name) would fail here."""
    seen: dict[str, object] = {}

    def _spy(text: str, payload: Narration) -> tuple[str, ...]:
        seen["text"] = text
        seen["narration"] = payload
        return ("SENTINEL",)

    monkeypatch.setattr(safety, "closed_world_tokens", _spy)
    score = _score_recap("Pull-Guard Pumas took Ashton Jeanty.", narration, target_chars=35)
    assert seen["text"] == "Pull-Guard Pumas took Ashton Jeanty."
    assert seen["narration"] is narration
    assert score.unknown_tokens == ("SENTINEL",)
    assert not score.closed_world_ok


def test_the_production_entry_point_masks_the_league_name_too(
    narration: Narration,
) -> None:
    """The scorer sees exactly the text the gate sees — league-name mask
    included. Reaching past ``closed_world_tokens`` into ``_closed_world`` would
    silently skip that step and score a different string."""
    data = narration.model_dump()
    data["league"]["name"] = "Fakename Kings"
    renamed = Narration.model_validate(data)
    # "Fakename" is out of world for the *original* payload...
    assert "Fakename" in safety.closed_world_tokens("Fakename Kings drafted.", narration)
    # ...and masked away for the league that is actually called that
    assert not safety.closed_world_tokens("Fakename Kings drafted.", renamed)


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
