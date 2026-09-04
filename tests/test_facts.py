"""Story 2.5: ``facts.schema`` + ``build_draft_recap_facts`` — the first Facts JSON.

One test per row of the spec's I/O & Edge-Case Matrix on hand-built minimal stage
results, ISO-8601 conversion both ways, the unknown-key round-trip, determinism,
``SchemaValidationError`` chained from ``pydantic.ValidationError``, and an
import fence over ``commishdesk/facts/*.py``.

Reconciliation runs two ways. The **CI oracle** is the committed
``tests/fixtures/facts/expected-draft-recap-facts.json`` — a frozen
``model_dump(mode="json")`` snapshot of the whole document, built from the
anonymized ``rookie-draft.json`` fixture through the same ``_synthetic_slots()``
transform the consensus / grade tests use; it runs everywhere. The
**phase-0 golden** (``brief/phase-0/draft-recap-facts.json``) is a private
planning artifact that is *not* in the repo (CLAUDE.md §1); the checks that
match the built document field-by-field against it — board / structural fields,
documented consensus / grade / prose / name exclusions aside — are all
``@requires_golden`` skip-gated and only run in a workspace that has the sibling
``../brief/`` directory.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from commishdesk.errors import CommishDeskError, SchemaValidationError
from commishdesk.facts import SCHEMA_VERSION, DraftRecapFacts, build_draft_recap_facts
from commishdesk.facts.schema import Superlatives
from commishdesk.ingest import (
    Draft,
    LeagueFormat,
    LeagueModel,
    Pick,
    Player,
    Team,
    build_league_model,
)
from commishdesk.stats import (
    GRADE_METHOD,
    BoardMetrics,
    ConsensusMetrics,
    DraftGrades,
    PickConsensus,
    PickRef,
    TeamBoard,
    TeamConsensus,
    TeamGrade,
    compute_board_metrics,
    compute_consensus_metrics,
    compute_draft_grades,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
CONSENSUS_DIR = FIXTURE_DIR / "consensus"
GRADES_DIR = FIXTURE_DIR / "grades"
FACTS_DIR = FIXTURE_DIR / "facts"
FACTS_PKG = REPO_ROOT / "commishdesk" / "facts"
EXPECTED_FACTS_PATH = FACTS_DIR / "expected-draft-recap-facts.json"

# The phase-0 golden (``draft-recap-facts.json``) is a *private* planning artifact
# in the sibling ``../brief/`` directory — it is deliberately NOT in this repo
# (CLAUDE.md §1: no real ``league_id`` / manager names / brief content). Every
# check that reads it is skip-gated, exactly like ``@requires_raw`` in
# ``tests/test_fixtures.py``. The committed CI oracle is
# ``tests/fixtures/facts/expected-draft-recap-facts.json`` — a frozen
# ``model_dump()`` snapshot derived from the anonymized ``rookie-draft.json``
# fixture through the same synthetic-slots path the consensus / grade tests use.
_GOLDEN_PATH = REPO_ROOT.parent / "brief" / "phase-0" / "draft-recap-facts.json"
GOLDEN = (
    json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
    if _GOLDEN_PATH.is_file()
    else None
)
requires_golden = pytest.mark.skipif(
    GOLDEN is None,
    reason="phase-0 golden is a private planning artifact, not in the tree",
)

# The committed CI oracle — a frozen full-document snapshot (see module docstring).
_EXPECTED_FACTS = json.loads(EXPECTED_FACTS_PATH.read_text(encoding="utf-8"))

GENERATED_AT = "2026-09-03T00:00:00Z"


# --------------------------------------------------------------------------- #
# Helpers — hand-build minimal stage results
# --------------------------------------------------------------------------- #


def _fmt(**over: Any) -> LeagueFormat:
    base: dict[str, Any] = dict(
        team_count=12,
        roster_slots=["QB", "QB", "RB", "WR", "TE"],
        flex_eligibility={"FLEX": ["RB", "WR", "TE"]},
        scoring_label="0.5 PPR",
        is_superflex_or_2qb=True,
        te_premium=False,
    )
    base.update(over)
    return LeagueFormat(**base)


def _pick(pick_no: int, roster_id: str, pos: str | None, *, name: str | None = None) -> Pick:
    rnd = (pick_no - 1) // 12 + 1
    col = (pick_no - 1) % 12 + 1
    return Pick(
        pick_no=pick_no,
        round=rnd,
        slot=col,
        board_label=f"{rnd}.{col:02d}",
        roster_id=roster_id,
        manager=f"mgr{roster_id}",
        player=Player(
            sleeper_id=f"s{pick_no}",
            name=name or f"Player {pick_no}",
            position=pos,
            nfl_team="NFL",
        ),
    )


def _minimal() -> tuple[LeagueModel, BoardMetrics, ConsensusMetrics, DraftGrades]:
    """A 2-roster, 2-pick draft: roster 1 has a ranked pick, roster 2 a
    no-consensus pick and no ranked pick."""
    picks = [_pick(1, "1", "RB"), _pick(2, "2", "QB")]
    league = LeagueModel(
        league_id="L1",
        name="Mini",
        season=2025,
        format=_fmt(),
        teams=[
            Team(roster_id="1", manager="mgr1"),
            Team(roster_id="2", manager=None),
        ],
        picks=picks,
        draft=Draft(id="d1", type="linear", rounds=1),
    )
    board = BoardMetrics(
        teams=[
            TeamBoard(
                roster_id="1",
                manager="mgr1",
                pick_count=1,
                pick_nos=[1],
                positional_counts={"RB": 1},
                back_to_back=[],
                zero_positions=[],
            ),
            TeamBoard(
                roster_id="2",
                manager=None,
                pick_count=1,
                pick_nos=[2],
                positional_counts={"QB": 1},
                back_to_back=[],
                zero_positions=[],
            ),
        ],
        positional_runs=[],
    )
    consensus = ConsensusMetrics(
        picks=[
            PickConsensus(
                pick_no=1,
                roster_id="1",
                player="Player 1",
                consensus_slot=1,
                consensus_label="1.01",
                delta=0,
                flags=[],
            ),
            PickConsensus(
                pick_no=2,
                roster_id="2",
                player="Player 2",
                consensus_slot=None,
                consensus_label=None,
                delta=None,
                flags=["no_consensus"],
            ),
        ],
        teams=[
            TeamConsensus(
                roster_id="1",
                best_value_pick=PickRef(pick_no=1, player="Player 1", delta=0),
                biggest_reach_pick=PickRef(pick_no=1, player="Player 1", delta=0),
            ),
            TeamConsensus(roster_id="2", best_value_pick=None, biggest_reach_pick=None),
        ],
    )
    grades = DraftGrades(
        teams=[
            TeamGrade(
                roster_id="1",
                draft_score=0.0,
                premium_picks=1,
                format_fit=2,
                grade_input=5.0,
                letter="B",
                driving_picks=[1],
            ),
            TeamGrade(
                roster_id="2",
                draft_score=0.0,
                premium_picks=0,
                format_fit=0,
                grade_input=0.0,
                letter="C",
                driving_picks=[],
            ),
        ],
        grade_method=GRADE_METHOD,
    )
    return league, board, consensus, grades


def _build_minimal(**over: Any) -> DraftRecapFacts:
    league, board, consensus, grades = _minimal()
    return build_draft_recap_facts(
        league, board, consensus, grades, generated_at=GENERATED_AT, **over
    )


# --------------------------------------------------------------------------- #
# Rookie-draft reconciliation fixture
# --------------------------------------------------------------------------- #


def _bundle(name: str) -> dict[str, Any]:
    raw = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    return {
        "league": raw["league"],
        "draft": raw["draft"],
        "draft_picks": raw["draft_picks"],
        "rosters": raw["rosters"],
        "users": raw["users"],
        "previous_league_ids": [],
    }


def _synthetic_slots() -> dict[str, int]:
    board = json.loads(
        (CONSENSUS_DIR / "fantasycalc-values.json").read_text(encoding="utf-8")
    )
    ordered = sorted(board, key=lambda e: -e["value"])
    return {e["player"]["sleeperId"]: i for i, e in enumerate(ordered, start=1)}


def _rookie_facts() -> DraftRecapFacts:
    league = build_league_model(_bundle("rookie-draft.json"))
    slots = _synthetic_slots()
    board = compute_board_metrics(league)
    consensus = compute_consensus_metrics(league, slots)
    grades = compute_draft_grades(league, consensus)
    return build_draft_recap_facts(
        league,
        board,
        consensus,
        grades,
        generated_at=GENERATED_AT,
        fetched_at="2026-09-03",
        consensus_source_name="synthetic rookie board",
        consensus_as_of="2025-05",
    )


# --------------------------------------------------------------------------- #
# I/O & Edge-Case Matrix
# --------------------------------------------------------------------------- #


def test_happy_path_shape() -> None:
    doc = _build_minimal()
    dump = doc.model_dump()
    assert dump["schema_version"] == "0.1.0" == SCHEMA_VERSION
    assert dump["issue_type"] == "draft_recap"
    assert [p["pick_no"] for p in dump["picks"]] == [1, 2]
    assert [t["roster_id"] for t in dump["teams"]] == ["1", "2"]
    assert dump["grade_method"] == GRADE_METHOD.model_dump()
    # Story 2.6: the ``biggest_score`` floor always fires when the draft has a
    # pick, so even this 2-pick minimal draft carries one lead angle.
    assert dump["lead_candidates"] == [
        {
            "rank": 1,
            "kind": "biggest_score",
            "roster_ids": ["1"],
            "hook": "Player 1 went 1.01.",
        }
    ]
    assert dump["storyline_candidates"] == []
    assert list(dump) == [
        "schema_version",
        "generated_at",
        "provisional",
        "issue_type",
        "source",
        "consensus_source",
        "league",
        "draft",
        "picks",
        "teams",
        "draft_summary",
        "superlatives",
        "grade_method",
        "lead_candidates",
        "storyline_candidates",
        "narration",
    ]


@pytest.mark.parametrize("field", ["started_at_ms", "completed_at_ms"])
def test_timestamps_present_convert_to_iso(field: str) -> None:
    league, board, consensus, grades = _minimal()
    league = league.model_copy(
        update={"draft": Draft(id="d1", type="linear", rounds=1, **{field: 1747494304432})}
    )
    doc = build_draft_recap_facts(
        league, board, consensus, grades, generated_at=GENERATED_AT
    )
    out_field = field.removesuffix("_ms")
    other = "completed_at" if out_field == "started_at" else "started_at"
    assert getattr(doc.draft, out_field) == "2025-05-17T15:05:04.432Z"
    assert getattr(doc.draft, other) is None


def test_timestamps_absent_stay_none() -> None:
    doc = _build_minimal()
    assert doc.draft.started_at is None and doc.draft.completed_at is None


def test_no_consensus_pick_row_preserved() -> None:
    doc = _build_minimal()
    row = next(p for p in doc.picks if p.pick_no == 2)
    assert row.consensus_slot is None
    assert row.consensus_label is None
    assert row.delta is None
    assert row.flags == ["no_consensus"]


def test_team_with_no_ranked_pick() -> None:
    doc = _build_minimal()
    row = next(t for t in doc.teams if t.roster_id == "2")
    assert row.best_value_pick is None and row.biggest_reach_pick is None


def test_orphan_roster_manager_is_none() -> None:
    doc = _build_minimal()
    row = next(t for t in doc.teams if t.roster_id == "2")
    assert row.manager is None


def test_unknown_key_on_read_is_dropped() -> None:
    payload = _build_minimal().model_dump()
    payload["future_key"] = 1
    payload["narration"]["future_key"] = {"nested": True}
    doc = DraftRecapFacts.model_validate(payload)
    assert not hasattr(doc, "future_key")
    assert doc.schema_version == "0.1.0"


def test_schema_violation_raises_typed_chained_error() -> None:
    league, board, consensus, _ = _minimal()
    broken = DraftGrades(teams=[], grade_method=GRADE_METHOD)  # no grade for any roster
    with pytest.raises(SchemaValidationError) as excinfo:
        build_draft_recap_facts(
            league, board, consensus, broken, generated_at=GENERATED_AT
        )
    assert isinstance(excinfo.value, CommishDeskError)
    assert isinstance(excinfo.value.__cause__, ValidationError)
    # fails loud: the message carries a summary, not just the fixed prefix
    message = str(excinfo.value)
    assert message != "draft_recap Facts JSON failed schema validation"
    assert "letter" in message  # the first failing field path is surfaced


def test_malformed_stage_object_is_wrapped_typed(monkeypatch) -> None:
    """A non-``ValidationError`` raised while merging a malformed stage object
    (``AttributeError`` / ``TypeError`` / ``KeyError`` / ``ValueError``) is still
    caught, wrapped in ``SchemaValidationError``, and chained."""
    from commishdesk.facts import build as build_mod

    def _boom(_ref: Any) -> None:
        raise AttributeError("stage object has no 'delta'")

    monkeypatch.setattr(build_mod, "_extreme", _boom)
    league, board, consensus, grades = _minimal()
    with pytest.raises(SchemaValidationError) as excinfo:
        build_draft_recap_facts(
            league, board, consensus, grades, generated_at=GENERATED_AT
        )
    assert isinstance(excinfo.value.__cause__, AttributeError)
    assert "AttributeError" in str(excinfo.value)


def test_builder_performs_the_self_validation_round_trip(monkeypatch) -> None:
    """``_validate(doc)`` is exercised: if ``model_validate`` rejects the dump,
    the builder raises ``SchemaValidationError`` chained from it — even though
    construction succeeded."""
    from commishdesk.facts import build as build_mod

    try:
        DraftRecapFacts.model_validate({})
    except ValidationError as err:
        sample = err

    def _boom(*_a: Any, **_k: Any) -> None:
        raise sample

    monkeypatch.setattr(build_mod.DraftRecapFacts, "model_validate", _boom)
    league, board, consensus, grades = _minimal()
    with pytest.raises(SchemaValidationError) as excinfo:
        build_draft_recap_facts(
            league, board, consensus, grades, generated_at=GENERATED_AT
        )
    assert excinfo.value.__cause__ is sample


def test_generated_at_datetime_normalized_to_z_iso() -> None:
    league, board, consensus, grades = _minimal()
    aware = datetime(2026, 9, 3, 1, 2, 3, tzinfo=UTC)
    doc = build_draft_recap_facts(
        league, board, consensus, grades, generated_at=aware
    )
    assert doc.generated_at == "2026-09-03T01:02:03.000Z"
    again = build_draft_recap_facts(
        league, board, consensus, grades, generated_at=aware
    )
    assert doc.model_dump() == again.model_dump()
    # a naive datetime is read as UTC
    naive = build_draft_recap_facts(
        league, board, consensus, grades, generated_at=datetime(2026, 9, 3, 1, 2, 3)
    )
    assert naive.generated_at == "2026-09-03T01:02:03.000Z"


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_generated_at_empty_or_none_rejected(bad: Any) -> None:
    league, board, consensus, grades = _minimal()
    with pytest.raises(SchemaValidationError):
        build_draft_recap_facts(
            league, board, consensus, grades, generated_at=bad
        )


def test_determinism_equal_model_dump() -> None:
    first = _build_minimal()
    second = _build_minimal()
    assert first.model_dump() == second.model_dump()
    rookie_first = _rookie_facts().model_dump()
    rookie_second = _rookie_facts().model_dump()
    assert rookie_first == rookie_second


# --------------------------------------------------------------------------- #
# ISO 8601 — the Story 2.2 deferral, exercised both ways
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("ms", "expected"),
    [
        (1747494304432, "2025-05-17T15:05:04.432Z"),
        (1747613041680, "2025-05-19T00:04:01.680Z"),
        (0, "1970-01-01T00:00:00.000Z"),
        (1_000, "1970-01-01T00:00:01.000Z"),
    ],
)
def test_iso_conversion(ms: int, expected: str) -> None:
    from commishdesk.facts.build import _iso

    assert _iso(ms) == expected


def test_iso_none_passes_through() -> None:
    from commishdesk.facts.build import _iso

    assert _iso(None) is None


# --------------------------------------------------------------------------- #
# Self-validation contract
# --------------------------------------------------------------------------- #


def test_build_round_trips_through_model_validate() -> None:
    doc = _rookie_facts()
    again = DraftRecapFacts.model_validate(doc.model_dump())
    assert again.model_dump() == doc.model_dump()


def test_provisional_and_engine_note() -> None:
    doc = _build_minimal(provisional=False)
    assert doc.provisional is False
    assert doc.consensus_source.provisional is False
    note = doc.consensus_source.engine_note
    assert "FantasyCalc" in note and "search_rank" in note
    assert note == _EXPECTED_FACTS["consensus_source"]["engine_note"]


# --------------------------------------------------------------------------- #
# Import fence — commishdesk/facts/*.py reaches nothing downstream / no httpx
# --------------------------------------------------------------------------- #


def _imported_dotted_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module)
            elif node.level == 1:
                names.add(
                    f"commishdesk.facts.{node.module}"
                    if node.module
                    else "commishdesk.facts"
                )
        elif isinstance(node, ast.Call):
            func = node.func
            fname = getattr(func, "attr", None) or getattr(func, "id", None)
            if fname in {"import_module", "__import__"}:
                names.update(
                    a.value
                    for a in node.args
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)
                )
    return names


def test_facts_package_import_fence() -> None:
    forbidden = {"adapters", "consensus", "narrate", "render", "deliver", "store"}
    for path in sorted(FACTS_PKG.glob("*.py")):
        imported = _imported_dotted_names(path)
        assert "httpx" not in {n.split(".")[0] for n in imported}, path.name
        offenders = [
            n
            for n in imported
            if n.split(".")[:1] == ["commishdesk"]
            and len(n.split(".")) > 1
            and n.split(".")[1] in forbidden
        ]
        assert not offenders, (path.name, offenders)
        # what facts/ IS allowed to reach upstream
    build_imports = _imported_dotted_names(FACTS_PKG / "build.py")
    assert "commishdesk.ingest" in build_imports
    assert any(n.startswith("commishdesk.stats") for n in build_imports)


# --------------------------------------------------------------------------- #
# Primary reconciliation — the committed full-document snapshot (runs on CI)
# --------------------------------------------------------------------------- #


def test_reconciles_full_document_with_committed_snapshot() -> None:
    """The whole built document, byte-for-byte against the committed oracle
    ``tests/fixtures/facts/expected-draft-recap-facts.json`` — the same
    ``model_dump()`` pattern the consensus / grade fixtures use. This is the
    reconciliation that runs everywhere; regenerate the fixture from
    ``_rookie_facts()`` when the builder or an upstream stage changes.
    """
    built = _rookie_facts().model_dump(mode="json")
    expected = {k: v for k, v in _EXPECTED_FACTS.items() if not k.startswith("_")}
    assert built == expected


def test_committed_snapshot_carries_a_provenance_note() -> None:
    assert _EXPECTED_FACTS["_derived_from"].startswith("Story 2.5 oracle")
    assert "synthetic" in _EXPECTED_FACTS["_derived_from"]


# --------------------------------------------------------------------------- #
# Phase-0 reconciliation — the private golden (local-only; skip-gated on CI)
# --------------------------------------------------------------------------- #


@requires_golden
def test_reconciles_with_phase0_golden() -> None:
    """The built document matched field-by-field against the private phase-0
    golden — board / structural / board-derived projections only. Documented
    exclusions (manager names, consensus slots / deltas, grade letters,
    ``scoring_label``, ``draft.started_at``, the anonymized league / draft ids,
    delta-driven superlatives) are skipped: the fixture is manager-anonymized
    and carries a synthetic consensus board, so those cannot equal the golden.
    The committed ``expected-draft-recap-facts.json`` snapshot is the CI oracle.
    """
    assert GOLDEN is not None
    doc = _rookie_facts().model_dump(mode="json")

    # provenance + league / format shape + draft identity
    assert doc["source"]["platform"] == GOLDEN["source"]["platform"]
    assert doc["issue_type"] == GOLDEN["issue_type"]
    assert doc["consensus_source"]["engine_note"] == GOLDEN["consensus_source"]["engine_note"]
    gfmt, dfmt = GOLDEN["league"]["format"], doc["league"]["format"]
    for key in ("team_count", "roster_slots", "flex_eligibility", "is_superflex_or_2qb"):
        assert dfmt[key] == gfmt[key], key
    assert doc["league"]["season"] == GOLDEN["league"]["season"]  # "2025", a string
    assert doc["draft"]["rounds"] == GOLDEN["draft"]["rounds"]
    assert doc["draft"]["type"] == GOLDEN["draft"]["type"]

    # picks[] board columns
    golden_pick = {p["pick_no"]: p for p in GOLDEN["picks"]}
    assert len(doc["picks"]) == len(GOLDEN["picks"])
    for row in doc["picks"]:
        g = golden_pick[row["pick_no"]]
        assert (row["round"], row["slot"], row["board_label"]) == (
            g["round"],
            g["slot"],
            g["board_label"],
        )
        assert str(row["roster_id"]) == str(g["roster_id"])
        assert row["player"] == {
            k: g["player"][k]
            for k in ("sleeper_id", "name", "position", "nfl_team")
        }

    # teams[] board counts
    golden_team = {str(t["roster_id"]): t for t in GOLDEN["teams"]}
    assert {str(t["roster_id"]) for t in doc["teams"]} == set(golden_team)
    for row in doc["teams"]:
        g = golden_team[str(row["roster_id"])]
        assert row["pick_count"] == g["pick_count"]
        assert row["pick_nos"] == g["pick_nos"]
        assert row["positional_counts"] == g["positional_counts"]
        assert [list(pair) for pair in row["back_to_back"]] == g["back_to_back"]

    # draft_summary board projections
    ds, gds = doc["draft_summary"], GOLDEN["draft_summary"]
    assert ds["round1_positional"] == gds["round1_positional"]
    assert ds["first_window_running_backs"] == gds["first11_running_backs"]
    for key in ("pick_no", "player", "board_label"):
        assert [q[key] for q in ds["round1_qbs"]] == [q[key] for q in gds["round1_qbs"]]
    assert [e["pick_count"] for e in ds["pick_count_rank"]] == [
        e["pick_count"] for e in gds["pick_count_rank"]
    ]
    assert [(e["round"], e["count"]) for e in ds["round_concentration"]] == [
        (e["round"], e["count"]) for e in gds["round_concentration"]
    ]
    dpr, gpr = ds["positional_runs"], gds["positional_runs"]
    for key in ("total", "first_label", "by_end_round3", "run_labels"):
        assert dpr["QB"][key] == gpr["QB"][key], ("QB", key)
    for key in ("total", "first_label", "in_round1"):
        assert dpr["RB"][key] == gpr["RB"][key], ("RB", key)
    assert dpr["RB"]["most_by_one_manager"]["count"] == gpr["RB"]["most_by_one_manager"]["count"]
    assert dpr["TE"] == gpr["TE"]  # every TE field is board-derived

    # narration — the trimmed sections that can reconcile
    gnar = GOLDEN["narration"]
    ghn, dhn = gnar["headline_numbers"], doc["narration"]["headline_numbers"]
    assert dhn["picks_total"] == ghn["picks_total"] == 72
    assert dhn["rounds"] == ghn["rounds"] == 6
    assert dhn["r1_positional"] == ghn["r1_positional"]
    assert dhn["first_window_rb_count"] == ghn["first11_rb_count"]
    assert dhn["pick_count_leader"]["pick_count"] == ghn["pick_count_leader"]["pick_count"] == 12
    assert dhn["pick_count_low"]["pick_count"] == ghn["pick_count_low"]["pick_count"] == 3
    assert [
        (p["pick_no"], p["board_label"], p["player"], p["position"])
        for p in doc["narration"]["board_round1"]
    ] == [
        (p["pick_no"], p["board_label"], p["player"], p["position"])
        for p in gnar["board_round1"]
    ]


# --------------------------------------------------------------------------- #
# Phase-0 reconciliation — consensus / grade fields vs the synthetic oracles
# --------------------------------------------------------------------------- #


def test_reconciles_consensus_fields_with_synthetic_oracle() -> None:
    doc = _rookie_facts().model_dump()
    expected = json.loads(
        (CONSENSUS_DIR / "expected-consensus-metrics.json").read_text(encoding="utf-8")
    )
    exp_by_no = {p["pick_no"]: p for p in expected["picks"]}
    for row in doc["picks"]:
        e = exp_by_no[row["pick_no"]]
        assert row["consensus_slot"] == e["consensus_slot"]
        assert row["consensus_label"] == e["consensus_label"]
        assert row["delta"] == e["delta"]
        assert row["flags"] == e["flags"]

    exp_teams = {t["roster_id"]: t for t in expected["teams"]}
    for row in doc["teams"]:
        e = exp_teams[row["roster_id"]]
        assert row["best_value_pick"] == e["best_value_pick"]
        assert row["biggest_reach_pick"] == e["biggest_reach_pick"]


def test_reconciles_grade_fields_with_synthetic_oracle() -> None:
    doc = _rookie_facts().model_dump()
    expected = json.loads(
        (GRADES_DIR / "expected-draft-grades.json").read_text(encoding="utf-8")
    )
    exp_by_roster = {t["roster_id"]: t for t in expected["teams"]}
    for row in doc["teams"]:
        e = exp_by_roster[row["roster_id"]]
        assert row["draft_score"] == e["draft_score"]
        assert row["premium_picks"] == e["premium_picks"]
        assert row["format_fit"] == e["format_fit"]
        assert row["grade_input"] == e["grade_input"]
        assert row["grade"]["letter"] == e["letter"]
        assert row["grade"]["driving_picks"] == e["driving_picks"]
        assert row["grade"]["rationale"] is None
    assert doc["grade_method"] == expected["grade_method"]
    assert doc["grade_method"]["letter_scale"] == "A-F+/-"


# --------------------------------------------------------------------------- #
# narration — the trimmed mirror
# --------------------------------------------------------------------------- #


def test_narration_is_a_trimmed_projection() -> None:
    doc = _rookie_facts()
    nar = doc.narration
    assert nar.issue_type == "draft_recap"
    assert nar.league.name == doc.league.name
    assert nar.league.scoring_label == doc.league.format.scoring_label
    assert nar.headline_numbers.picks_total == len(doc.picks)
    assert nar.headline_numbers.first_window_rb_count == len(
        doc.draft_summary.first_window_running_backs
    )
    assert [p.pick_no for p in nar.board_round1] == list(range(1, 13))
    assert len(nar.teams) == len(doc.teams)
    assert nar.positional_runs == doc.draft_summary.positional_runs
    # Story 2.6: narration mirrors the full lead-candidate list (no trim yet —
    # the token cap is Story 3.1).
    assert nar.lead_candidates == doc.lead_candidates
    assert nar.lead_candidates != []
    assert nar.storyline_candidates == []


def test_facts_import_pulls_in_no_network_module() -> None:
    """The spec's verification command: importing ``commishdesk.facts`` and
    resolving the public names loads no ``httpx``."""
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import commishdesk.facts, sys; "
            "commishdesk.facts.build_draft_recap_facts; "
            "commishdesk.facts.FactsJSON; "
            "assert 'httpx' not in sys.modules; print('ok')",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


# --------------------------------------------------------------------------- #
# Builder keyword arguments land in the document
# --------------------------------------------------------------------------- #


def test_builder_kwargs_land_in_document() -> None:
    doc = _build_minimal(
        draft_id="draft-override",
        fetched_at="2026-09-03",
        consensus_source_name="synthetic rookie board",
        consensus_as_of="2025-05",
    )
    assert doc.source.fetched_at == "2026-09-03"
    assert doc.source.draft_id == "draft-override"
    assert doc.draft.id == "draft-override"  # resolved once, used for both
    assert doc.consensus_source.name == "synthetic rookie board"
    assert doc.consensus_source.as_of == "2025-05"


def test_draft_id_defaults_to_ingested_draft_id() -> None:
    doc = _build_minimal()
    assert doc.source.draft_id == doc.draft.id == "d1"


# --------------------------------------------------------------------------- #
# narration headline numbers — pinned literals (a rank[0]/rank[-1] swap fails)
# --------------------------------------------------------------------------- #


def test_narration_headline_numbers_pinned_values() -> None:
    hn = _rookie_facts().narration.headline_numbers
    assert hn.picks_total == 72
    assert hn.rounds == 6
    assert hn.r1_positional == {"RB": 5, "WR": 5, "QB": 2}
    assert hn.first_window_rb_count == 5
    assert hn.pick_count_leader.pick_count == 12  # most picks
    assert hn.pick_count_low.pick_count == 3  # fewest picks
    assert [p.pick_no for p in _rookie_facts().narration.board_round1] == list(
        range(1, 13)
    )


# --------------------------------------------------------------------------- #
# Degenerate inputs
# --------------------------------------------------------------------------- #


def test_empty_draft_builds_and_validates() -> None:
    league = LeagueModel(
        league_id="L0",
        name="Empty",
        season=2025,
        format=_fmt(),
        teams=[],
        picks=[],
        draft=Draft(id="d0", rounds=0),
    )
    board = BoardMetrics(teams=[], positional_runs=[])
    consensus = ConsensusMetrics(picks=[], teams=[])
    grades = DraftGrades(teams=[], grade_method=GRADE_METHOD)
    doc = build_draft_recap_facts(
        league, board, consensus, grades, generated_at=GENERATED_AT
    )
    assert doc.picks == [] and doc.teams == []
    assert doc.superlatives == Superlatives()
    assert doc.draft_summary.positional_runs.QB.total == 0
    assert doc.narration.headline_numbers.pick_count_leader is None
    assert doc.lead_candidates == []  # no pick -> not even the biggest_score floor
    DraftRecapFacts.model_validate(doc.model_dump())


def test_board_with_no_consensus_yields_empty_superlatives() -> None:
    picks = [_pick(1, "1", "RB"), _pick(2, "1", "WR")]
    league = LeagueModel(
        league_id="L1",
        name="NoConsensus",
        season=2025,
        format=_fmt(),
        teams=[Team(roster_id="1", manager="m1")],
        picks=picks,
        draft=Draft(id="d1", rounds=1),
    )
    board = compute_board_metrics(league)
    consensus = compute_consensus_metrics(league, {})  # every pick is no_consensus
    grades = compute_draft_grades(league, consensus)
    doc = build_draft_recap_facts(
        league, board, consensus, grades, generated_at=GENERATED_AT
    )
    assert doc.superlatives == Superlatives()
    assert all(p.flags == ["no_consensus"] for p in doc.picks)
    assert all(p.delta is None for p in doc.picks)
    # biggest_score still fires off pick_no 1 / board position — no crash on the
    # absent superlatives.best_value.
    assert [c.kind for c in doc.lead_candidates] == ["biggest_score"]
    assert doc.lead_candidates[0].hook == "Player 1 went 1.01."
    DraftRecapFacts.model_validate(doc.model_dump())


# --------------------------------------------------------------------------- #
# Lazy re-export targets are themselves inside the import fence
# --------------------------------------------------------------------------- #


def test_lazy_reexport_targets_are_inside_the_fence() -> None:
    import commishdesk.facts as facts_pkg

    lazy: dict[str, tuple[str, str]] = getattr(facts_pkg, "_LAZY", {})
    assert lazy, "expected facts/__init__.py to expose its lazy re-export map"
    forbidden = {"adapters", "consensus", "narrate", "render", "deliver", "store"}
    for name, (module, _attr) in lazy.items():
        parts = module.split(".")
        assert "httpx" not in parts, (name, module)
        assert not (
            parts[:1] == ["commishdesk"]
            and len(parts) > 1
            and parts[1] in forbidden
        ), (name, module)


def test_te_window_ignores_a_gap_before_the_second_te() -> None:
    """Finding #7: a big inter-TE gap does not close the early window until at
    least two TEs precede it — otherwise the 'third TE' would be the second."""
    from commishdesk.facts.build import _te_run

    rows = [
        _board_row(1, "TE"),
        _board_row(30, "TE"),  # 29-pick gap, but only one TE so far
        _board_row(31, "TE"),
        _board_row(60, "TE"),  # 29-pick gap, now two TEs precede -> boundary
        _board_row(61, "TE"),
    ]
    summary = _te_run(rows, team_count=12)
    assert summary.early_window_labels == [r.board_label for r in rows[:3]]
    assert summary.third_te_label == rows[3].board_label
    assert summary.gap_picks == 29


def _board_row(pick_no: int, pos: str):
    from commishdesk.facts.schema import PickRow, PlayerRef

    rnd = (pick_no - 1) // 12 + 1
    col = (pick_no - 1) % 12 + 1
    return PickRow(
        pick_no=pick_no,
        round=rnd,
        slot=col,
        board_label=f"{rnd}.{col:02d}",
        roster_id="1",
        player=PlayerRef(sleeper_id=f"s{pick_no}", name=f"P{pick_no}", position=pos),
    )


def test_boldest_swing_requires_a_positive_spread() -> None:
    """Finding #8: a roster whose ranked picks all share one delta is not a
    swing — ``boldest_swing`` is ``None`` rather than a zero-spread entry."""
    picks = [_pick(1, "1", "RB"), _pick(2, "1", "WR"), _pick(3, "2", "QB")]
    league = LeagueModel(
        league_id="L1",
        name="Flat",
        season=2025,
        format=_fmt(),
        teams=[Team(roster_id="1", manager="m1"), Team(roster_id="2", manager="m2")],
        picks=picks,
        draft=Draft(id="d1", rounds=1),
    )
    board = compute_board_metrics(league)
    consensus = ConsensusMetrics(
        picks=[
            PickConsensus(
                pick_no=1, roster_id="1", player="Player 1",
                consensus_slot=1, consensus_label="1.01", delta=0, flags=[],
            ),
            PickConsensus(
                pick_no=2, roster_id="1", player="Player 2",
                consensus_slot=2, consensus_label="1.02", delta=0, flags=[],
            ),
            PickConsensus(
                pick_no=3, roster_id="2", player="Player 3",
                consensus_slot=None, consensus_label=None, delta=None,
                flags=["no_consensus"],
            ),
        ],
        teams=[
            TeamConsensus(roster_id="1", best_value_pick=None, biggest_reach_pick=None),
            TeamConsensus(roster_id="2", best_value_pick=None, biggest_reach_pick=None),
        ],
    )
    grades = compute_draft_grades(league, consensus)
    doc = build_draft_recap_facts(
        league, board, consensus, grades, generated_at=GENERATED_AT
    )
    assert doc.superlatives.boldest_swing is None


# --------------------------------------------------------------------------- #
# Story 2.6 — board-only lead angles
# --------------------------------------------------------------------------- #


def test_lead_candidates_rookie_fixture_ranked_angles() -> None:
    """The rookie-draft board produces the four ranked angles, each with a
    non-empty deterministic hook, computed with no LLM."""
    cands = _rookie_facts().lead_candidates
    assert [c.kind for c in cands] == [
        "positional_run",
        "manager_approach",
        "positional_hoard",
        "biggest_score",
    ]
    assert [c.rank for c in cands] == [1, 2, 3, 4]
    assert [c.roster_ids for c in cands] == [[], ["2"], ["8"], ["2"]]
    assert all(c.hook and c.hook.endswith(".") for c in cands)
    assert "None" not in " ".join(c.hook for c in cands)


def test_lead_candidates_positional_run_outranks_the_marquee_name() -> None:
    """AC: a cross-cutting angle sits above manager-approach, above 'the 1.01'."""
    kinds = [c.kind for c in _rookie_facts().lead_candidates]
    assert kinds.index("positional_run") < kinds.index("manager_approach")
    assert kinds.index("manager_approach") < kinds.index("biggest_score")


def test_lead_candidates_tiny_draft_is_biggest_score_only() -> None:
    cands = _build_minimal().lead_candidates
    assert [(c.rank, c.kind, tuple(c.roster_ids)) for c in cands] == [
        (1, "biggest_score", ("1",))
    ]


def test_lead_candidates_are_deterministic() -> None:
    assert (
        _rookie_facts().model_dump()["lead_candidates"]
        == _rookie_facts().model_dump()["lead_candidates"]
    )


def test_lead_candidates_skip_an_orphan_roster_that_leads_a_category() -> None:
    """A manager-less roster with the unique max pick count and the biggest
    positional stack yields neither a manager_approach nor a positional_hoard
    angle — no hook is emitted with a null manager name."""
    picks = [_pick(n, "1", "RB") for n in range(1, 7)] + [
        _pick(7, "2", "WR"),
        _pick(8, "2", "WR"),
        _pick(9, "2", "WR"),
    ]
    league = LeagueModel(
        league_id="LO",
        name="Orphan",
        season=2025,
        format=_fmt(),
        teams=[Team(roster_id="1", manager=None), Team(roster_id="2", manager="m2")],
        picks=picks,
        draft=Draft(id="do", rounds=9),
    )
    board = compute_board_metrics(league)
    consensus = compute_consensus_metrics(league, {})
    grades = compute_draft_grades(league, consensus)
    doc = build_draft_recap_facts(
        league, board, consensus, grades, generated_at=GENERATED_AT
    )
    kinds = [c.kind for c in doc.lead_candidates]
    assert "manager_approach" not in kinds and "positional_hoard" not in kinds
    assert "biggest_score" in kinds
    assert "None" not in " ".join(c.hook for c in doc.lead_candidates)


@requires_golden
def test_lead_candidates_reconcile_with_phase0_golden() -> None:
    """Kind + rank + roster_ids sequence match the private phase-0 golden. Hook
    *text* is the narrator's later prose and is deliberately not compared."""
    assert GOLDEN is not None
    built = _rookie_facts().model_dump()["lead_candidates"]
    g = GOLDEN["lead_candidates"]
    assert [c["kind"] for c in built] == [c["kind"] for c in g]
    assert [c["rank"] for c in built] == [c["rank"] for c in g]
    assert [c["roster_ids"] for c in built] == [
        [str(r) for r in c["roster_ids"]] for c in g
    ]


def _facts_from_picks(
    picks: list[Pick], *, teams: list[Team] | None = None, rounds: int = 1
) -> DraftRecapFacts:
    """Build a Facts JSON from a hand-built pick list through the real compute
    stages — the lightweight path the lead-angle branch tests share."""
    roster_ids = {p.roster_id for p in picks}
    league = LeagueModel(
        league_id="LL",
        name="Lead",
        season=2025,
        format=_fmt(),
        teams=teams or [Team(roster_id=r, manager=f"m{r}") for r in sorted(roster_ids)],
        picks=picks,
        draft=Draft(id="dl", rounds=rounds),
    )
    board = compute_board_metrics(league)
    consensus = compute_consensus_metrics(league, {})
    grades = compute_draft_grades(league, consensus)
    return build_draft_recap_facts(
        league, board, consensus, grades, generated_at=GENERATED_AT
    )


def test_lead_candidates_positional_run_no_round1_qb_gets_a_terminal_hook() -> None:
    picks = [_pick(n, str(n), "RB") for n in range(1, 5)] + [
        _pick(n, str(n), "WR") for n in range(5, 13)
    ]
    run = _facts_from_picks(picks).lead_candidates[0]
    assert run.kind == "positional_run"
    assert run.hook == "Four of the first eleven picks were running backs."


def test_lead_candidates_positional_run_single_round1_qb_is_singular() -> None:
    picks = (
        [_pick(n, str(n), "RB") for n in range(1, 5)]
        + [_pick(5, "5", "QB")]
        + [_pick(n, str(n), "WR") for n in range(6, 13)]
    )
    run = _facts_from_picks(picks).lead_candidates[0]
    assert run.hook.endswith("and one manager took a quarterback in round 1.")


def test_lead_candidates_hoard_passes_an_orphan_leader_to_the_next_real_manager() -> None:
    """The biggest stack belongs to a manager-less roster; the angle still lands
    on the real manager who cleared the bar (spec: skip orphans during the scan)."""
    picks = (
        [_pick(n, "1", "RB") for n in range(1, 8)]  # orphan roster: 7 RB, most picks
        + [_pick(n, "2", "WR") for n in range(8, 13)]  # real manager: 5 WR
    )
    teams = [Team(roster_id="1", manager=None), Team(roster_id="2", manager="Deep")]
    doc = _facts_from_picks(picks, teams=teams, rounds=7)
    hoard = [c for c in doc.lead_candidates if c.kind == "positional_hoard"]
    assert hoard and hoard[0].roster_ids == ["2"]
    assert hoard[0].hook == "Deep drafted five wide receivers."


def test_lead_candidates_manager_approach_needs_a_unique_pick_count_leader() -> None:
    picks = [_pick(n, "1", "RB") for n in range(1, 5)] + [
        _pick(n, "2", "WR") for n in range(5, 9)
    ]
    doc = _facts_from_picks(picks, rounds=4)
    assert "manager_approach" not in [c.kind for c in doc.lead_candidates]


def test_lead_candidates_hoard_hook_handles_a_nonstandard_position() -> None:
    # roster 2 leads pick count (-> manager_approach); roster 1's 4-kicker stack
    # is the positional_hoard, exercising the _POSITION_PLURAL "K" entry.
    picks = [_pick(n, "1", "K") for n in range(1, 5)] + [
        _pick(n, "2", "WR") for n in range(5, 11)
    ]
    teams = [Team(roster_id="1", manager="Kicker"), Team(roster_id="2", manager="m2")]
    doc = _facts_from_picks(picks, teams=teams, rounds=1)
    hoard = [c for c in doc.lead_candidates if c.kind == "positional_hoard"]
    assert hoard and hoard[0].hook == "Kicker drafted four kickers."


def test_lead_kind_priority_covers_every_kind_a_detector_can_emit() -> None:
    from commishdesk.facts.leads import LEAD_KIND_PRIORITY

    assert len(set(LEAD_KIND_PRIORITY)) == len(LEAD_KIND_PRIORITY)
    assert {c.kind for c in _rookie_facts().lead_candidates} == set(LEAD_KIND_PRIORITY)
