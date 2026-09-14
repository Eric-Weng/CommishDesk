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
    number_sentences,
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
    assert _player(narration, "Jalin Conyers").nfl_team == "MIA"
    assert _player(narration, "Trevor Etienne").nfl_team == "CAR"
    assert _player(narration, "Ashton Jeanty").nfl_team == "LV"
    assert _player(narration, "TreVeyon Henderson").college == "Ohio State"
    assert _player(narration, "Omarion Hampton").college == "North Carolina"
    assert _player(narration, "Omarion Hampton").nfl_team == "LAC"


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


def test_parses_the_compact_line_format() -> None:
    """One ``kind|sentence|fields`` line per claim: a fraction of the output
    tokens of a JSON object per claim, and output is most of the check's cost."""
    raw = "a|3|Trevor Etienne|Carolina\np|3|Trevor Etienne|3.12|36\np|7|Ashton Jeanty|1.01|\ng|12|Pull-Guard Pumas|B+"
    claims, unparsed = parse_claims(raw) or ((), -1)
    assert unparsed == 0
    assert [(c.kind, c.sentence_no) for c in claims] == [("affiliation", 3), ("pick", 3), ("pick", 7), ("grade", 12)]
    assert claims[0].affiliation == "Carolina" and claims[0].player == "Trevor Etienne"
    assert claims[1].board_label == "3.12" and claims[1].pick_no == 36
    assert claims[2].board_label == "1.01" and claims[2].pick_no is None
    assert claims[3].manager == "Pull-Guard Pumas" and claims[3].grade == "B+"


def test_a_pick_line_may_drop_its_trailing_empty_field() -> None:
    claims, unparsed = parse_claims("p|7|Ashton Jeanty|1.01") or ((), -1)
    assert unparsed == 0 and claims[0].board_label == "1.01" and claims[0].pick_no is None


def test_none_means_no_claims_and_bad_lines_are_counted_not_fatal() -> None:
    assert parse_claims("NONE") == ((), 0)
    raw = (
        "Here are the claims:\n"
        "a|2|Jalin Conyers|Miami\n"
        "x|2|Mercury|retrograde\n"
        "a|two|Jalin Conyers|Miami\n"
        "a|2|Jalin Conyers"
    )
    claims, unparsed = parse_claims(raw) or ((), -1)
    assert len(claims) == 1 and unparsed == 3


def test_numbers_are_coerced_but_a_numeric_board_label_is_dropped() -> None:
    claim = _claim(kind="consensus", sentence="x", delta="+10", pick_no="14", board_label=2.1)
    assert claim.delta == 10 and claim.pick_no == 14
    # 2.1 as a JSON number has already lost the "0" in "2.10"; never guess it back
    assert claim.board_label is None


# --------------------------------------------------------------------------- #
# adjudicate — colleges (the measured recombination failure)
# --------------------------------------------------------------------------- #


def test_a_real_school_on_the_wrong_player_is_refuted(narration: Narration) -> None:
    prose = "Boise State tight end Jalin Conyers was the steal of the final round."
    verdict = _only(narration, prose, kind="college", player="Jalin Conyers", college="Boise State")
    assert verdict.status == "refuted"
    assert "Texas Tech" in verdict.reason and "MIA" in verdict.reason
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
    prose = "Boise State tight end Jalin Conyers was the steal of the final round."
    assert _only(blind, prose, kind="college", player="Jalin Conyers", college="Boise State").status == "unverifiable"


@pytest.mark.parametrize(
    ("prose", "player", "written"),
    [
        # live validation refuted every one of these; every one is true
        ("Miami tight end Jalin Conyers was the steal of the final round.", "Jalin Conyers", "Miami"),
        ("Carolina back Trevor Etienne went at 3.12.", "Trevor Etienne", "Carolina"),
        ("Carolina-bound back Trevor Etienne went at 3.12.", "Trevor Etienne", "Carolina-bound"),
        ("Buckeye back TreVeyon Henderson went at 1.06.", "TreVeyon Henderson", "Buckeye"),
        ("Carolina back Omarion Hampton went second.", "Omarion Hampton", "Carolina"),
    ],
)
def test_an_nfl_team_a_nickname_or_a_short_school_name_is_supported(
    narration: Narration, prose: str, player: str, written: str
) -> None:
    """A place in front of a player is an affiliation: their school or their NFL team."""
    verdict = _only(narration, prose, kind="affiliation", player=player, affiliation=written)
    assert verdict.status == "supported", verdict.reason


def test_the_wrong_nfl_team_is_refuted(narration: Narration) -> None:
    prose = "Denver back Ashton Jeanty went first overall."
    verdict = _only(narration, prose, kind="affiliation", player="Ashton Jeanty", affiliation="Denver")
    assert verdict.status == "refuted" and "LV" in verdict.reason


def test_a_name_that_is_no_school_or_team_is_unverifiable(narration: Narration) -> None:
    prose = "SEC back Trevor Etienne went at 3.12."
    verdict = _only(narration, prose, kind="affiliation", player="Trevor Etienne", affiliation="SEC")
    assert verdict.status == "unverifiable"


def test_an_affiliation_the_sentence_gives_another_player_is_unverifiable(narration: Narration) -> None:
    prose = "They took Boise State back Ashton Jeanty first and Jalin Conyers much later."
    verdict = _only(narration, prose, kind="affiliation", player="Jalin Conyers", affiliation="Boise State")
    assert verdict.status == "unverifiable"


def test_an_affiliation_belongs_to_the_name_right_after_it(narration: Narration) -> None:
    """Bake-off miss: "Sanders at 3.05, Stanford receiver Tre Harris" sits exactly
    as far from each name, and a nearest-name rule could not decide."""
    prose = "Ashton Jeanty went at 1.01, Arizona receiver Jalin Conyers much later."
    conyers = _only(narration, prose, kind="affiliation", player="Jalin Conyers", affiliation="Arizona")
    assert conyers.status == "refuted", conyers.reason
    jeanty = _only(narration, prose, kind="affiliation", player="Ashton Jeanty", affiliation="Arizona")
    assert jeanty.status == "unverifiable"


def test_an_unknown_player_is_unverifiable(narration: Narration) -> None:
    prose = "Alabama wideout Jamarr Chasen went early."
    verdict = _only(narration, prose, kind="college", player="Jamarr Chasen", college="Alabama")
    assert verdict.status == "unverifiable"


def test_a_sentence_the_prose_does_not_contain_is_unverifiable(narration: Narration) -> None:
    """A refutation has to point at the prose, not only at the payload: an
    extractor that invents or paraphrases a sentence cannot cause an excision."""
    (verdict,) = adjudicate(
        [
            _claim(
                kind="college", player="Jalin Conyers", college="Boise State", sentence="Miami's Conyers was a steal."
            )
        ],
        "Texas Tech tight end Jalin Conyers was the steal of the final round.",
        narration,
    )
    assert verdict.status == "unverifiable" and verdict.sentence is None


def test_the_located_sentence_is_the_one_naming_the_player(narration: Narration) -> None:
    prose = "The Pumas went big early. Boise State tight end Jalin Conyers was the steal. Nobody saw it coming."
    (verdict,) = adjudicate(
        [_claim(kind="college", player="Jalin Conyers", college="Boise State", sentence=prose)], prose, narration
    )
    assert verdict.status == "refuted"
    assert verdict.sentence == "Boise State tight end Jalin Conyers was the steal."


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
    wrong_prose = "The Pumas opened the draft by taking running back Ashton Jeanty at 1.03."
    wrong = _only(narration, wrong_prose, kind="pick", player="Ashton Jeanty", board_label="1.03")
    assert wrong.status == "refuted"


def test_a_slot_the_sentence_gives_another_player_is_unverifiable(narration: Narration) -> None:
    """Measured: "Jeanty at 1.01 over consensus top dog Omarion Hampton" came back
    as a claim that Hampton went 1.01."""
    prose = "Pull-Guard Pumas chose Ashton Jeanty at 1.01 over consensus top dog Omarion Hampton."
    assert _only(narration, prose, kind="pick", player="Omarion Hampton", board_label="1.01").status == "unverifiable"
    assert _only(narration, prose, kind="pick", player="Ashton Jeanty", board_label="1.01").status == "supported"


@pytest.mark.parametrize(
    ("prose", "player", "label", "status"),
    [
        # a slot after a long clause and a pick number, with the next name closer (bake-off miss)
        (
            "Ashton Jeanty was a clear plus-6 value at pick 38 (1.09), while securing Omarion Hampton at pick 2.",
            "Ashton Jeanty",
            "1.09",
            "refuted",
        ),
        # a consensus mark is nobody's pick
        (
            "Omarion Hampton went at 1.02, a spot behind his 1.01 consensus mark.",
            "Omarion Hampton",
            "1.01",
            "unverifiable",
        ),
        (
            "Omarion Hampton went at 1.02, a spot behind his 1.01 consensus mark.",
            "Omarion Hampton",
            "1.02",
            "supported",
        ),
        ("Field-Goal Foxes took Omarion Hampton at 1.02 (consensus 1.01).", "Omarion Hampton", "1.01", "unverifiable"),
        ("Field-Goal Foxes took Omarion Hampton at 1.02 (consensus 1.01).", "Omarion Hampton", "1.02", "supported"),
    ],
)
def test_a_slot_belongs_to_the_name_before_it_and_never_to_a_consensus_mark(
    narration: Narration, prose: str, player: str, label: str, status: str
) -> None:
    verdict = _only(narration, prose, kind="pick", player=player, board_label=label)
    assert verdict.status == status, verdict.reason


@pytest.mark.parametrize("line", ["Pull-Guard Pumas: A+", "### Pull-Guard Pumas: A+", "Pull-Guard Pumas — Grade: A+"])
def test_a_grade_heading_with_a_colon_or_dash_is_checked(narration: Narration, line: str) -> None:
    """Measured: every "Gridiron Gophers: A" heading came back uncheckable, the
    colon read as a clause break between the team and its grade."""
    verdict = _only(narration, line, kind="grade", manager="Pull-Guard Pumas", grade="A+")
    assert verdict.status == "supported", verdict.reason


def test_a_slot_that_introduces_the_next_name_belongs_to_that_name(narration: Narration) -> None:
    """Measured: "the Ferrets shut the door at 1.12 by grabbing Luther Burden" was
    pinned on the player named earlier in the sentence."""
    prose = "The Pumas took Ashton Jeanty at 1.01, and the Foxes shut the door at 1.03 by grabbing Omarion Hampton."
    assert _only(narration, prose, kind="pick", player="Omarion Hampton", board_label="1.03").status == "refuted"
    assert _only(narration, prose, kind="pick", player="Ashton Jeanty", board_label="1.01").status == "supported"


def test_a_slot_after_consensus_at_is_still_a_slot(narration: Narration) -> None:
    prose = "Jeanty went early, and so did Tetairoa McMillan, three spots ahead of consensus at 1.04."
    verdict = _only(narration, prose, kind="pick", player="Tetairoa McMillan", board_label="1.04")
    assert verdict.status == "supported", verdict.reason
    mark = "Tetairoa McMillan went at 1.04, well ahead of his consensus mark at 1.07."
    assert _only(narration, mark, kind="pick", player="Tetairoa McMillan", board_label="1.07").status == "unverifiable"


def test_an_affiliation_line_with_its_fields_swapped_is_still_checked(narration: Narration) -> None:
    """Measured: the extractor sometimes writes ``a|n|Ohio State|Quinshon Judkins``."""
    prose = "Carolina back Trevor Etienne went at 3.12."
    verdict = _only(narration, prose, kind="affiliation", player="Carolina", affiliation="Trevor Etienne")
    assert verdict.status == "supported", verdict.reason


def test_a_spelled_out_pick_number_is_checked_but_a_spelled_margin_is_not(narration: Narration) -> None:
    """Measured: the columnist voice writes "at pick seventy-two", and every such
    claim came back uncheckable while only digits were searched for."""
    right = "Securing Tyler Warren at pick sixteen gave them an elite foundation."
    assert _only(narration, right, kind="pick", player="Tyler Warren", pick_no=16).status == "supported"
    wrong = "Securing Tyler Warren at pick seventeen gave them an elite foundation."
    assert _only(narration, wrong, kind="pick", player="Tyler Warren", pick_no=17).status == "refuted"
    margin = "Tyler Warren went three spots early."
    assert _only(narration, margin, kind="pick", player="Tyler Warren", pick_no=3).status == "unverifiable"


def test_a_pick_line_with_no_slot_or_number_is_dropped_not_counted() -> None:
    claims, unparsed = parse_claims("p|4|Tory Horton||\na|4|Tory Horton|Colorado State") or ((), -1)
    assert [c.kind for c in claims] == ["affiliation"] and unparsed == 0


def test_an_ambiguous_team_nickname_is_never_guessed(narration: Narration) -> None:
    prose = "The team took Ashton Jeanty at 1.01."
    verdict = _only(narration, prose, kind="pick", player="Ashton Jeanty", manager="team")
    assert verdict.status == "unverifiable"


def test_grade_letter_decides_and_a_modifier_alone_does_not_refute(narration: Narration) -> None:
    prose = "Pull-Guard Pumas earned an A for the whole haul."
    assert _only(narration, prose, kind="grade", manager="Pull-Guard Pumas", grade="A").status == "supported"
    c_prose = "Pull-Guard Pumas earned a C for the whole haul."
    assert _only(narration, c_prose, kind="grade", manager="Pull-Guard Pumas", grade="C").status == "refuted"


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


def test_count_and_consensus_refutations_are_logged_not_gated(narration: Narration) -> None:
    """Live extraction pins counts and consensus deltas on the wrong player of a
    multi-player sentence; a refutation there is coverage data, never an excision."""
    prose = "Pull-Guard Pumas made nine picks, more than anyone."
    (verdict,) = adjudicate(
        [_claim(kind="pick_count", manager="Pull-Guard Pumas", count=9, sentence=prose)], prose, narration
    )
    assert verdict.status == "refuted"
    assert VerificationResult(ran=True, verdicts=(verdict,)).report().findings == ()


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
    prose = "Boise State tight end Jalin Conyers was the steal. The Pumas took Ashton Jeanty at 1.01."
    claims = [
        _claim(kind="college", player="Jalin Conyers", college="Boise State", sentence=prose),
        _claim(kind="pick", player="Ashton Jeanty", board_label="1.01", sentence=prose),
    ]
    assert adjudicate(claims, prose, narration) == adjudicate(claims, prose, narration)


# --------------------------------------------------------------------------- #
# report -> repair
# --------------------------------------------------------------------------- #


def test_refutations_become_hallucination_findings_the_repair_tier_can_excise(narration: Narration) -> None:
    body = (
        "Texas Tech tight end Jalin Conyers was fine, but that was not the story. "
        "Boise State tight end Jalin Conyers was the steal of the final round. "
        "Everything else about the board held up to scrutiny all afternoon."
    )
    text = "A Title\n\n" + "".join(f"## {h}\n\n{body}\n\n" for h in _headings())
    verdicts = adjudicate(
        [
            _claim(
                kind="college",
                player="Jalin Conyers",
                college="Boise State",
                sentence="Boise State tight end Jalin Conyers was the steal of the final round.",
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
    offender = "Boise State tight end Jalin Conyers was the steal of the final round."
    filler = "The rest of this section is ordinary, careful, entirely grounded prose about the board."
    sections = []
    for i, heading in enumerate(_headings()):
        sections.append(f"## {heading}\n\n{offender + ' ' if i == 0 else ''}{filler}\n")
    text = "A Title\n\n" + "\n".join(sections)
    verdicts = adjudicate(
        [_claim(kind="college", player="Jalin Conyers", college="Boise State", sentence=offender)], text, narration
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


def test_one_call_whose_only_input_is_the_numbered_prose(narration: Narration) -> None:
    """The extractor never sees the Facts — it cannot "correct" a wrong claim into
    a right one if it has nothing to correct it against."""
    prose = "The Pumas went big early. Boise State tight end Jalin Conyers was the steal of the final round."
    reply = json.dumps(
        {"claims": [{"kind": "affiliation", "s": 2, "player": "Jalin Conyers", "affiliation": "Boise State"}]}
    )
    seen: list[tuple[str, str]] = []
    outcome = verify_narration(prose, narration, VERIFIER, client_factory=lambda _c: _FakeClient(reply, seen))
    assert outcome.ran and len(outcome.refuted) == 1
    assert outcome.refuted[0].sentence == "Boise State tight end Jalin Conyers was the steal of the final round."
    assert len(seen) == 1
    payload, system_prompt = seen[0]
    assert payload == number_sentences(prose)
    assert payload.splitlines() == [
        "[1] The Pumas went big early.",
        "[2] Boise State tight end Jalin Conyers was the steal of the final round.",
    ]
    assert "Texas Tech" not in payload + system_prompt
    assert system_prompt == EXTRACTOR_VOICE.system_prompt


def test_a_numbered_claim_needs_no_copied_sentence() -> None:
    """The reply names the sentence by number — copying it back was most of the
    output tokens, and it truncated the first live extraction at MAX_TOKENS."""
    claims, unparsed = parse_claims('{"claims": [{"kind": "grade", "s": 3, "manager": "X", "grade": "A"}]}') or ((), -1)
    assert unparsed == 0 and claims[0].sentence_no == 3 and claims[0].sentence == ""
    assert parse_claims('{"claims": [{"kind": "grade", "manager": "X", "grade": "A"}]}') == ((), 1)


@pytest.mark.parametrize("number", [0, 3, -1])
def test_a_sentence_number_outside_the_prose_is_unverifiable(narration: Narration, number: int) -> None:
    prose = "The Pumas went big early. Boise State tight end Jalin Conyers was the steal."
    (verdict,) = adjudicate(
        [_claim(kind="college", s=number, player="Jalin Conyers", college="Boise State")], prose, narration
    )
    assert verdict.status == "unverifiable" and verdict.sentence is None


def test_a_sentence_number_pointing_away_from_the_player_is_unverifiable(narration: Narration) -> None:
    """A miscounted index must never excise an innocent sentence."""
    prose = "The Pumas went big early. Boise State tight end Jalin Conyers was the steal."
    (verdict,) = adjudicate(
        [_claim(kind="college", s=1, player="Jalin Conyers", college="Boise State")], prose, narration
    )
    assert verdict.status == "unverifiable" and verdict.sentence is None


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


# --------------------------------------------------------------------------- #
# pattern claims — read with no model call, merged with the extractor's
# --------------------------------------------------------------------------- #


def test_pattern_claims_read_fixed_sentence_shapes(narration: Narration) -> None:
    prose = "Carolina back Trevor Etienne went at 3.12. The Foxes shut the door at 1.03 by grabbing Omarion Hampton."
    claims = {(c.kind, c.player, c.affiliation or c.board_label) for c in verify._pattern_claims(prose, narration)}
    assert ("affiliation", "Trevor Etienne", "Carolina") in claims
    assert ("pick", "Trevor Etienne", "3.12") in claims
    assert ("pick", "Omarion Hampton", "1.03") in claims


@pytest.mark.parametrize(
    "prose",
    [
        # measured false alarms of an earlier draft: paired slots and consensus figures
        "The Gophers paired Quinshon Judkins at 1.07 with Emeka Egbuka at 1.08.",
        "Pairing Omarion Hampton and Travis Hunter at 1.02 and 1.03 set the foundation.",
        "Ashton Jeanty went 1.01, three spots ahead of his 1.04 consensus mark.",
    ],
)
def test_pattern_claims_never_refute_true_paired_or_consensus_prose(narration: Narration, prose: str) -> None:
    verdicts = adjudicate(verify._pattern_claims(prose, narration), prose, narration)
    assert [v.reason for v in verdicts if v.status == "refuted"] == []


def test_pattern_claims_supplement_an_extractor_that_missed_the_error(narration: Narration) -> None:
    prose = "The Pumas went big early. Boise State tight end Jalin Conyers was the steal of the final round."
    seen: list[tuple[str, str]] = []
    outcome = verify_narration(prose, narration, VERIFIER, client_factory=lambda _c: _FakeClient("NONE", seen))
    assert outcome.ran and len(seen) == 1
    assert [v.claim.player for v in outcome.refuted] == ["Jalin Conyers"]


def test_a_claim_both_the_extractor_and_the_patterns_find_is_judged_once(narration: Narration) -> None:
    prose = "Boise State tight end Jalin Conyers was the steal of the final round."
    reply = "a|1|Jalin Conyers|Boise State"
    outcome = verify_narration(prose, narration, VERIFIER, client_factory=lambda _c: _FakeClient(reply, []))
    assert len([v for v in outcome.verdicts if v.claim.kind == "affiliation"]) == 1


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


def test_a_slot_paired_with_the_next_pick_is_never_the_next_players_slot(narration: Narration) -> None:
    """Measured false refutation, live 2026-09-14: a true sentence was cut because
    "with" was read as introducing the player who went at 1.05."""
    prose = (
        "You attacked the 2QB format head-on by pairing Cameron Ward at 1.05 "
        "with Colorado quarterback Shedeur Sanders at pick 29."
    )
    extracted = _only(narration, prose, kind="pick", player="Shedeur Sanders", board_label="1.05")
    assert extracted.status != "refuted", extracted.reason
    from_patterns = adjudicate(verify._pattern_claims(prose, narration), prose, narration)
    assert [v.reason for v in from_patterns if v.status == "refuted"] == []
