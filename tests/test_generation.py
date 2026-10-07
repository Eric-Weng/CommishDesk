"""Stories 2.7 / 7.1 — the single Generation Set constructor (AD-6 / AD-44 / I1).

Eligibility, decay, headroom and ordering over ``LeagueFacts``, and an AST sweep of
the package asserting ``GenerationSet(...)`` is constructed in exactly one module.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

from commishdesk import generation
from commishdesk.generation import (
    ClaimFacts,
    GenerationPolicy,
    GenerationSet,
    LeagueFacts,
    Reason,
    SendFacts,
    build_generation_set,
)

PKG_ROOT = Path(generation.__file__).resolve().parent
T0 = datetime(2026, 9, 1, tzinfo=UTC)
GOOD = (ClaimFacts(confirmed=True),)
POLICY = GenerationPolicy(max_active_leagues=3, allow_new=True)


def emails(n: int, channel: str = "email") -> tuple[SendFacts, ...]:
    return tuple(SendFacts(T0 + timedelta(days=7 * i), channel) for i in range(n))


def league(
    league_id: str,
    *,
    created: int = 0,
    onboarded: bool = True,
    claims: tuple[ClaimFacts, ...] = GOOD,
    sends: tuple[SendFacts, ...] = (),
    engaged: datetime | None = None,
) -> LeagueFacts:
    return LeagueFacts(league_id, T0 + timedelta(days=created), onboarded, claims, sends, engaged)


def test_empty_facts_yield_an_empty_set() -> None:
    result = build_generation_set([], POLICY)
    assert isinstance(result, GenerationSet)
    assert result == GenerationSet(())
    assert result.league_ids == ()


def test_generation_set_defaults_keep_the_one_arg_form() -> None:
    assert GenerationSet(()).excluded == ()
    assert GenerationSet(()).waiting == ()
    assert GenerationSet(()).approaching_decay == ()


def test_ineligible_leagues_are_excluded_with_a_reason() -> None:
    cases = [
        league("a", onboarded=False),
        league("b", claims=()),
        league("c", claims=(ClaimFacts(confirmed=False),)),
        league("d", claims=(ClaimFacts(confirmed=True, unsubscribed=True),)),
        league("e", claims=(ClaimFacts(confirmed=True, revoked=True),)),
    ]
    result = build_generation_set(cases, POLICY)
    assert result.league_ids == ()
    assert result.excluded == tuple((c.league_id, Reason.INELIGIBLE) for c in cases)


def test_one_good_claim_among_bad_ones_is_eligible() -> None:
    claims = (ClaimFacts(confirmed=True, revoked=True), *GOOD)
    assert build_generation_set([league("a", claims=claims)], POLICY).league_ids == ("a",)


def test_ten_thousand_ineligible_ids_yield_an_empty_set() -> None:
    facts = [league(str(n), onboarded=False) for n in range(10_000)]
    assert build_generation_set(facts, POLICY).league_ids == ()


def test_six_quiet_email_sends_decay_the_league() -> None:
    result = build_generation_set([league("a", sends=emails(6))], POLICY)
    assert result.league_ids == ()
    assert result.excluded == (("a", Reason.DECAYED),)


def test_engagement_at_the_sixth_send_time_is_a_tie_and_still_decays() -> None:
    sends = emails(8)
    sixth = sorted((s.sent_at for s in sends), reverse=True)[5]
    tied = build_generation_set([league("a", sends=sends, engaged=sixth)], POLICY)
    assert tied.excluded == (("a", Reason.DECAYED),)
    later = sixth + timedelta(seconds=1)
    result = build_generation_set([league("a", sends=sends, engaged=later)], POLICY)
    assert result.league_ids == ("a",)


def test_a_later_click_reactivates_a_decayed_league() -> None:
    sends = emails(6)
    assert build_generation_set([league("a", sends=sends)], POLICY).league_ids == ()
    click = sends[-1].sent_at + timedelta(hours=1)
    result = build_generation_set([league("a", sends=sends, engaged=click)], POLICY)
    assert result.league_ids == ("a",)


def test_only_confirmed_email_sends_count_toward_decay() -> None:
    discord = emails(6, channel="discord")
    unconfirmed = tuple(SendFacts(s.sent_at, "email", confirmed=False) for s in emails(6))
    assert build_generation_set([league("a", sends=discord)], POLICY).league_ids == ("a",)
    assert build_generation_set([league("b", sends=unconfirmed)], POLICY).league_ids == ("b",)


def test_quiet_count_4_and_5_approach_decay_6_decays_3_does_not_warn() -> None:
    for n, approaching, admitted in [
        (3, (), True),
        (4, ("a",), True),
        (5, ("a",), True),
        (6, (), False),
    ]:
        result = build_generation_set([league("a", sends=emails(n))], POLICY)
        assert result.approaching_decay == approaching, n
        assert (result.league_ids == ("a",)) is admitted, n


def test_engagement_resets_the_quiet_count() -> None:
    sends = emails(5)
    engaged = sends[3].sent_at + timedelta(hours=1)  # only the 5th send is quiet
    result = build_generation_set([league("a", sends=sends, engaged=engaged)], POLICY)
    assert result.league_ids == ("a",)
    assert result.approaching_decay == ()


def test_headroom_keeps_active_and_admits_oldest_new_first() -> None:
    facts = [
        league("n1", created=5),
        league("act1", created=9, sends=emails(1)),
        league("n2", created=1),
        league("act2", created=8, sends=emails(2)),
        league("n3", created=3),
    ]
    result = build_generation_set(facts, POLICY)  # cap 3, 2 active -> 1 slot
    assert result.league_ids == ("act1", "act2", "n2")
    assert result.waiting == (("n3", Reason.WAITING), ("n1", Reason.WAITING))


def test_active_leagues_stay_above_the_cap() -> None:
    facts = [league(f"a{i}", sends=emails(1)) for i in range(4)] + [league("new")]
    result = build_generation_set(facts, GenerationPolicy(max_active_leagues=2))
    assert result.league_ids == ("a0", "a1", "a2", "a3")
    assert result.waiting == (("new", Reason.WAITING),)


def test_allow_new_false_admits_only_active_leagues() -> None:
    facts = [league("new1"), league("act", sends=emails(1)), league("new2")]
    result = build_generation_set(facts, GenerationPolicy(max_active_leagues=9, allow_new=False))
    assert result.league_ids == ("act",)
    assert result.waiting == (
        ("new1", Reason.NEW_NOT_ALLOWED),
        ("new2", Reason.NEW_NOT_ALLOWED),
    )


def test_unconfirmed_only_sends_are_new_not_active() -> None:
    unconfirmed = (SendFacts(T0, "email", confirmed=False),)
    facts = [league("u", created=0, sends=unconfirmed), league("act", created=5, sends=emails(1))]
    blocked = build_generation_set(facts, GenerationPolicy(max_active_leagues=9, allow_new=False))
    assert blocked.league_ids == ("act",)
    assert blocked.waiting == (("u", Reason.NEW_NOT_ALLOWED),)
    capped = build_generation_set(
        [league("u", created=0, sends=unconfirmed), league("n", created=1)],
        GenerationPolicy(max_active_leagues=1),
    )
    assert capped.league_ids == ("u",)
    assert capped.waiting == (("n", Reason.WAITING),)


def test_season_rollover_incumbents_keep_slots_and_sendless_leagues_are_new() -> None:
    facts = [league("rolled", created=0), league("incumbent", created=50, sends=emails(1))]
    result = build_generation_set(facts, GenerationPolicy(max_active_leagues=1))
    assert result.league_ids == ("incumbent",)
    assert result.waiting == (("rolled", Reason.WAITING),)
    alone = build_generation_set([league("rolled")], GenerationPolicy(max_active_leagues=1))
    assert alone.league_ids == ("rolled",)


def test_duplicate_league_id_first_wins() -> None:
    facts = [league("x", onboarded=False), league("x"), league("y"), league("y")]
    result = build_generation_set(facts, POLICY)
    assert result.league_ids == ("y",)
    assert result.excluded == (("x", Reason.INELIGIBLE),)


def test_ordering_is_stable_for_equal_created_at() -> None:
    facts = [league(i, created=1) for i in "cab"]
    result = build_generation_set(facts, GenerationPolicy(max_active_leagues=2))
    assert result.league_ids == ("c", "a")
    assert result.waiting == (("b", Reason.WAITING),)


def test_cli_facts_make_one_eligible_new_league() -> None:
    result = build_generation_set(generation.cli_facts("9"), GenerationPolicy(max_active_leagues=1))
    assert result.league_ids == ("9",)


def test_generation_set_is_constructed_in_exactly_one_module() -> None:
    constructors: list[str] = []
    for path in sorted(PKG_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "GenerationSet":
                constructors.append(path.relative_to(PKG_ROOT).as_posix())
    assert constructors == ["generation.py"], constructors
