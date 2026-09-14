"""Content-safety P1 — ``commishdesk/narrate/verify.py``: claim extraction +
deterministic adjudication.

Everything here runs offline at zero spend. The adjudicator is pure Python over
the committed facts oracle; the one paid call is replaced by a fake client, so CI
never needs a credential (I4).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from commishdesk.facts.schema import Narration
from commishdesk.llmconfig import LLMModelConfig, load_llm_config
from commishdesk.narrate import MODEL_PRICES, excise_offending_sentences, verify
from commishdesk.narrate.verify import (
    EXTRACTOR_VOICE,
    ExtractedClaim,
    VerificationResult,
    adjudicate,
    parse_claims,
    verify_narration,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FACTS = REPO_ROOT / "tests" / "fixtures" / "facts" / "expected-draft-recap-facts.json"
VERIFIER = LLMModelConfig("google", "gemini-3.8-flash", timeout=60.0)


@pytest.fixture(scope="module")
def narration() -> Narration:
    return Narration.model_validate(json.loads(FACTS.read_text(encoding="utf-8"))["narration"])


def _player(narration: Narration, name: str):
    return next(p for p in narration.players if p.name == name)


def _team(narration: Narration, manager: str):
    return next(t for t in narration.teams if t.manager == manager)


def _claim(**fields: object) -> ExtractedClaim:
    return ExtractedClaim.model_validate(fields)


def _only(narration: Narration, prose: str, **fields: object):
    fields.setdefault("sentence", prose)
    (verdict,) = adjudicate([_claim(**fields)], prose, narration)
    return verdict


def test_fixture_carries_the_facts_these_tests_rely_on(narration: Narration) -> None:
    """Every hard-coded fact below comes from the committed oracle. If it is ever
    regenerated from a different draft, fail here, loudly, first."""
    assert _player(narration, "Jalin Conyers").college == "Texas Tech"
    assert _player(narration, "Ashton Jeanty").college == "Boise State"
    assert _player(narration, "Ashton Jeanty").board_label == "1.01"
    assert _player(narration, "Ashton Jeanty").manager == "Pull-Guard Pumas"
    assert _player(narration, "Damien Martinez").college == "Miami (FL)"
    assert _player(narration, "Cam Skattebo").college == "Arizona State"
    assert _team(narration, "Pull-Guard Pumas").grade == "A+"
    assert _team(narration, "Pull-Guard Pumas").pick_count == 12


# --------------------------------------------------------------------------- #
# parse_claims
# --------------------------------------------------------------------------- #


def test_parses_a_plain_claim_list() -> None:
    parsed = parse_claims('{"claims": [{"kind": "grade", "sentence": "An A.", "manager": "X", "grade": "A"}]}')
    assert parsed is not None
    claims, unparsed = parsed
    assert unparsed == 0 and claims[0].grade == "A"


@pytest.mark.parametrize(
    "raw",
    [
        '```json\n{"claims": [{"kind": "grade", "sentence": "An A.", "grade": "A"}]}\n```',
        'Here you go:\n{"claims": [{"kind": "grade", "sentence": "An A.", "grade": "A"}]}\nDone.',
        '[{"kind": "grade", "sentence": "An A.", "grade": "A"}]',
    ],
)
def test_tolerates_fences_chatter_and_a_bare_list(raw: str) -> None:
    parsed = parse_claims(raw)
    assert parsed is not None and len(parsed[0]) == 1


def test_a_bad_claim_is_skipped_and_counted_not_fatal() -> None:
    raw = json.dumps(
        {
            "claims": [
                {"kind": "grade", "sentence": "An A.", "grade": "A"},
                {"kind": "astrology", "sentence": "Mercury was in retrograde."},
                {"kind": "grade", "sentence": "   "},
                "not an object",
            ]
        }
    )
    claims, unparsed = parse_claims(raw) or ((), -1)
    assert len(claims) == 1 and unparsed == 3


@pytest.mark.parametrize("raw", ["I cannot help with that.", '{"result": "ok"}', '{"claims": "none"}', ""])
def test_an_unusable_completion_is_none(raw: str) -> None:
    assert parse_claims(raw) is None


def test_numbers_are_coerced_but_a_numeric_board_label_is_dropped() -> None:
    claim = _claim(kind="consensus", sentence="x", delta="+10", pick_no="14", board_label=2.1)
    assert claim.delta == 10 and claim.pick_no == 14
    # 2.1 as a JSON number has already lost the "0" in "2.10"; never guess it back
    assert claim.board_label is None


# --------------------------------------------------------------------------- #
# adjudicate — colleges (the measured recombination failure)
# --------------------------------------------------------------------------- #


def test_the_measured_failure_is_refuted(narration: Narration) -> None:
    prose = "Miami tight end Jalin Conyers was the steal of the final round."
    verdict = _only(narration, prose, kind="college", player="Jalin Conyers", college="Miami")
    assert verdict.status == "refuted"
    assert "Texas Tech" in verdict.reason
    assert verdict.sentence == prose


def test_the_right_college_is_supported(narration: Narration) -> None:
    prose = "Texas Tech tight end Jalin Conyers was the steal of the final round."
    verdict = _only(narration, prose, kind="college", player="Jalin Conyers", college="Texas Tech")
    assert verdict.status == "supported"


def test_a_parenthetical_payload_college_matches_the_natural_form(narration: Narration) -> None:
    prose = "Miami back Damien Martinez was a quiet win."
    assert _only(narration, prose, kind="college", player="Damien Martinez", college="Miami").status == "supported"


def test_a_different_school_with_a_shared_word_is_still_refuted(narration: Narration) -> None:
    prose = "Arizona back Cam Skattebo went late."
    assert _only(narration, prose, kind="college", player="Cam Skattebo", college="Arizona").status == "refuted"


def test_a_college_the_payload_does_not_carry_is_unverifiable_not_refuted(narration: Narration) -> None:
    """The live path never fetches colleges. Not knowing one is never grounds to
    refute one."""
    players = [p.model_copy(update={"college": None}) if p.name == "Jalin Conyers" else p for p in narration.players]
    blind = narration.model_copy(update={"players": players})
    prose = "Miami tight end Jalin Conyers was the steal of the final round."
    assert _only(blind, prose, kind="college", player="Jalin Conyers", college="Miami").status == "unverifiable"


def test_an_unknown_player_is_unverifiable(narration: Narration) -> None:
    prose = "Alabama wideout Jamarr Chasen went early."
    verdict = _only(narration, prose, kind="college", player="Jamarr Chasen", college="Alabama")
    assert verdict.status == "unverifiable"


def test_a_sentence_the_prose_does_not_contain_is_unverifiable(narration: Narration) -> None:
    """A refutation has to point at the prose, not only at the payload: an
    extractor that invents or paraphrases a sentence cannot cause an excision."""
    (verdict,) = adjudicate(
        [_claim(kind="college", player="Jalin Conyers", college="Miami", sentence="Miami's Conyers was a steal.")],
        "Texas Tech tight end Jalin Conyers was the steal of the final round.",
        narration,
    )
    assert verdict.status == "unverifiable" and verdict.sentence is None


def test_the_located_sentence_is_the_one_naming_the_player(narration: Narration) -> None:
    prose = "The Pumas went big early. Miami tight end Jalin Conyers was the steal. Nobody saw it coming."
    (verdict,) = adjudicate(
        [_claim(kind="college", player="Jalin Conyers", college="Miami", sentence=prose)], prose, narration
    )
    assert verdict.status == "refuted"
    assert verdict.sentence == "Miami tight end Jalin Conyers was the steal."


# --------------------------------------------------------------------------- #
# adjudicate — picks, grades, counts, consensus
# --------------------------------------------------------------------------- #


def test_pick_slot_team_and_position(narration: Narration) -> None:
    prose = "The Pumas opened the draft by taking running back Ashton Jeanty at 1.01."
    right = _only(
        narration,
        prose,
        kind="pick",
        player="Ashton Jeanty",
        board_label="1.01",
        manager="Pumas",
        position="running back",
    )
    assert right.status == "supported", right.reason
    wrong = _only(narration, prose, kind="pick", player="Ashton Jeanty", board_label="1.03")
    assert wrong.status == "refuted"


def test_an_ambiguous_team_nickname_is_never_guessed(narration: Narration) -> None:
    prose = "The team took Ashton Jeanty at 1.01."
    verdict = _only(narration, prose, kind="pick", player="Ashton Jeanty", manager="team")
    assert verdict.status == "unverifiable"


def test_grade_letter_decides_and_a_modifier_alone_does_not_refute(narration: Narration) -> None:
    prose = "Pull-Guard Pumas earned an A for the whole haul."
    assert _only(narration, prose, kind="grade", manager="Pull-Guard Pumas", grade="A").status == "supported"
    assert _only(narration, prose, kind="grade", manager="Pull-Guard Pumas", grade="C").status == "refuted"


def test_pick_and_position_counts(narration: Narration) -> None:
    team = _team(narration, "Pull-Guard Pumas")
    prose = "Pull-Guard Pumas made twelve picks, more than anyone."
    assert (
        _only(narration, prose, kind="pick_count", manager="Pull-Guard Pumas", count=team.pick_count).status
        == "supported"
    )
    assert _only(narration, prose, kind="pick_count", manager="Pull-Guard Pumas", count=9).status == "refuted"
    wr = team.positional_counts["WR"]
    prose = "Pull-Guard Pumas stacked receivers all draft long."
    fields = {"kind": "position_count", "manager": "Pull-Guard Pumas", "position": "receivers"}
    assert _only(narration, prose, count=wr, **fields).status == "supported"
    assert _only(narration, prose, count=wr + 3, **fields).status == "refuted"


def test_consensus_compares_magnitude_not_sign(narration: Narration) -> None:
    """The payload says -3 for a reach; prose says "reached three slots"."""
    jeanty = next(p for p in narration.board_round1 if p.player == "Ashton Jeanty")
    assert jeanty.delta is not None and jeanty.consensus_label is not None
    prose = "Ashton Jeanty went well ahead of his consensus slot."
    assert (
        _only(narration, prose, kind="consensus", player="Ashton Jeanty", delta=abs(jeanty.delta)).status == "supported"
    )
    assert (
        _only(narration, prose, kind="consensus", player="Ashton Jeanty", delta=-abs(jeanty.delta)).status
        == "supported"
    )
    assert (
        _only(narration, prose, kind="consensus", player="Ashton Jeanty", delta=abs(jeanty.delta) + 6).status
        == "refuted"
    )
    assert (
        _only(narration, prose, kind="consensus", player="Ashton Jeanty", consensus_label=jeanty.consensus_label).status
        == "supported"
    )


def test_adjudication_is_deterministic(narration: Narration) -> None:
    prose = "Miami tight end Jalin Conyers was the steal. The Pumas took Ashton Jeanty at 1.01."
    claims = [
        _claim(kind="college", player="Jalin Conyers", college="Miami", sentence=prose),
        _claim(kind="pick", player="Ashton Jeanty", board_label="1.01", sentence=prose),
    ]
    assert adjudicate(claims, prose, narration) == adjudicate(claims, prose, narration)


# --------------------------------------------------------------------------- #
# report -> repair
# --------------------------------------------------------------------------- #


def test_refutations_become_hallucination_findings_the_repair_tier_can_excise(narration: Narration) -> None:
    body = (
        "Texas Tech tight end Jalin Conyers was fine, but that was not the story. "
        "Miami tight end Jalin Conyers was the steal of the final round. "
        "Everything else about the board held up to scrutiny all afternoon."
    )
    text = "A Title\n\n" + "".join(f"## {h}\n\n{body}\n\n" for h in _headings())
    verdicts = adjudicate(
        [
            _claim(
                kind="college",
                player="Jalin Conyers",
                college="Miami",
                sentence="Miami tight end Jalin Conyers was the steal of the final round.",
            )
        ],
        text,
        narration,
    )
    report = VerificationResult(ran=True, verdicts=verdicts).report()
    (finding,) = report.findings
    assert finding.category == "hallucination" and finding.severity == "regenerate"
    assert finding.sentence in text


def _headings() -> tuple[str, ...]:
    from commishdesk.narrate import SECTION_HEADINGS

    return SECTION_HEADINGS


def test_excision_removes_the_refuted_sentence_end_to_end(narration: Narration) -> None:
    offender = "Miami tight end Jalin Conyers was the steal of the final round."
    filler = "The rest of this section is ordinary, careful, entirely grounded prose about the board."
    sections = []
    for i, heading in enumerate(_headings()):
        sections.append(f"## {heading}\n\n{offender + ' ' if i == 0 else ''}{filler}\n")
    text = "A Title\n\n" + "\n".join(sections)
    verdicts = adjudicate(
        [_claim(kind="college", player="Jalin Conyers", college="Miami", sentence=offender)], text, narration
    )
    repaired, excised = excise_offending_sentences(text, VerificationResult(ran=True, verdicts=verdicts).report())
    assert repaired is not None and excised == (offender,)
    assert "Jalin Conyers" not in repaired


# --------------------------------------------------------------------------- #
# verify_narration — the call, and failing open
# --------------------------------------------------------------------------- #


class _FakeClient:
    def __init__(self, reply: object, seen: list[tuple[str, str]]) -> None:
        self._reply = reply
        self._seen = seen

    def generate(self, payload: str, voice: object) -> str:
        self._seen.append((payload, getattr(voice, "system_prompt", "")))
        if isinstance(self._reply, Exception):
            raise self._reply
        return str(self._reply)


def test_switched_off_makes_no_call_and_reports_no_error(narration: Narration) -> None:
    seen: list[tuple[str, str]] = []
    outcome = verify_narration("prose.", narration, None, client_factory=lambda _c: _FakeClient("{}", seen))
    assert outcome == VerificationResult(ran=False)
    assert seen == []


def test_one_call_whose_only_input_is_the_prose(narration: Narration) -> None:
    """The extractor never sees the Facts — it cannot "correct" a wrong claim into
    a right one if it has nothing to correct it against."""
    prose = "Miami tight end Jalin Conyers was the steal of the final round."
    reply = json.dumps(
        {"claims": [{"kind": "college", "sentence": prose, "player": "Jalin Conyers", "college": "Miami"}]}
    )
    seen: list[tuple[str, str]] = []
    outcome = verify_narration(prose, narration, VERIFIER, client_factory=lambda _c: _FakeClient(reply, seen))
    assert outcome.ran and len(outcome.refuted) == 1
    assert len(seen) == 1
    payload, system_prompt = seen[0]
    assert payload == prose
    assert "Texas Tech" not in payload + system_prompt
    assert system_prompt == EXTRACTOR_VOICE.system_prompt


@pytest.mark.parametrize(
    "reply",
    [RuntimeError("provider down"), "not json at all", '{"claims": 7}'],
)
def test_every_failure_fails_open(narration: Narration, reply: object) -> None:
    seen: list[tuple[str, str]] = []
    outcome = verify_narration("prose.", narration, VERIFIER, client_factory=lambda _c: _FakeClient(reply, seen))
    assert outcome.ran is False
    assert outcome.error
    assert outcome.verdicts == ()


def test_a_client_that_cannot_even_be_built_fails_open(narration: Narration) -> None:
    def _boom(_cfg: LLMModelConfig) -> _FakeClient:
        raise RuntimeError("no key")

    outcome = verify_narration("prose.", narration, VERIFIER, client_factory=_boom)
    assert outcome.ran is False and outcome.error


def test_unverifiable_share_counts_unparsed_claims_too(narration: Narration) -> None:
    prose = "Texas Tech tight end Jalin Conyers was a steal."
    verdicts = adjudicate(
        [
            _claim(kind="college", player="Jalin Conyers", college="Texas Tech", sentence=prose),
            _claim(kind="college", player="Nobody Atall", college="Texas Tech", sentence=prose),
        ],
        prose,
        narration,
    )
    result = VerificationResult(ran=True, verdicts=verdicts, unparsed=2)
    assert result.unverifiable_share == pytest.approx(3 / 4)
    assert VerificationResult(ran=True).unverifiable_share is None


# --------------------------------------------------------------------------- #
# guards
# --------------------------------------------------------------------------- #


def test_the_default_verifier_is_a_priced_model() -> None:
    """The pre-call cost estimate fails closed on an unpriced model, so an unpriced
    default would abort every LLM run rather than merely skip verification."""
    cfg = load_llm_config({}).verifier
    assert cfg is not None
    assert f"{cfg.provider}:{cfg.model_id}" in MODEL_PRICES


def test_verify_module_imports_no_provider_sdk_and_bills_nothing_itself() -> None:
    """Import fence + I3: no provider SDK or HTTP client at any level, and no
    ``.generate(`` call — the one paid call is made through ``narrate/llm.py``."""
    tree = ast.parse(Path(verify.__file__).read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert not roots & {"anthropic", "google", "httpx", "openai", "litellm", "requests"}, roots
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "generate"
    ]
    assert calls == []
