"""Story 2.4c: ``compute_draft_grades`` -- the editorial draft-grade algorithm.

One test per row of the spec's I/O & Edge-Case Matrix on hand-built
``LeagueModel`` + ``ConsensusMetrics`` pairs, a table-driven walk of all 13
letters on the ``"A-F+/-"`` scale, the premium-picks floor across every "below C"
band, determinism, no ``-0.0`` leak, the inclusive driving-pick cutoff, format_fit
caps and composition, degenerate league shapes, import-fence checks that
``stats/grades.py`` reaches no network / clock / PRNG / filesystem module, and both
committed fixtures reconciled against ``expected-draft-grades.json`` via the Story
2.4b synthetic-slots path.
"""

from __future__ import annotations

import ast
import copy
import json
import math
from pathlib import Path
from typing import Any

import pytest

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
    THIRTEEN_POINT_SCALE,
    ConsensusMetrics,
    DraftGrades,
    GradeMethod,
    PickConsensus,
    TeamGrade,
    compute_consensus_metrics,
    compute_draft_grades,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
CONSENSUS_DIR = FIXTURE_DIR / "consensus"
GRADES_DIR = FIXTURE_DIR / "grades"
STATS_GRADES = REPO_ROOT / "commishdesk" / "stats" / "grades.py"

FIXTURES = ("rookie-draft.json", "week10-superflex.json")

_THIRTEEN = frozenset(
    {"A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "F"}
)


# --------------------------------------------------------------------------- #
# Helpers -- hand-build a LeagueModel + its ConsensusMetrics from compact rows
# --------------------------------------------------------------------------- #


def _fmt(*, superflex: bool = False, te_premium: bool = False, team_count: int = 12) -> LeagueFormat:
    return LeagueFormat(
        team_count=team_count,
        roster_slots=["QB", "RB", "WR", "TE"],
        flex_eligibility={},
        scoring_label="PPR",
        is_superflex_or_2qb=superflex,
        te_premium=te_premium,
    )


def _pick(pick_no: int, roster_id: str, pos: str | None) -> Pick:
    rnd = (pick_no - 1) // 12 + 1
    col = (pick_no - 1) % 12 + 1
    return Pick(
        pick_no=pick_no,
        round=rnd,
        slot=col,
        board_label=f"{rnd}.{col:02d}",
        roster_id=roster_id,
        manager=None,
        player=Player(sleeper_id=f"s{pick_no}", name=f"P{pick_no}", position=pos),
    )


def _consensus(rows: list[dict[str, Any]]) -> ConsensusMetrics:
    picks = [
        PickConsensus(
            pick_no=r["pick_no"],
            roster_id=r["roster_id"],
            player=f"P{r['pick_no']}",
            consensus_slot=r.get("slot"),
            consensus_label=None,
            delta=r.get("delta"),
            flags=[] if r.get("slot") is not None else ["no_consensus"],
        )
        for r in rows
    ]
    picks.sort(key=lambda p: p.pick_no)
    return ConsensusMetrics(picks=picks, teams=[])


def _scenario(
    rows: list[dict[str, Any]],
    *,
    superflex: bool = False,
    te_premium: bool = False,
    team_count: int = 12,
    extra_rosters: tuple[str, ...] = (),
) -> tuple[LeagueModel, ConsensusMetrics]:
    """``rows``: dicts with ``pick_no``, ``roster_id``, ``pos`` and optional
    ``slot`` / ``delta`` (both absent -> a ``no_consensus`` pick). ``slot`` and
    ``delta`` are pinned independently, as the consensus tests do -- the algorithm
    consumes them as given and never re-derives ``delta``."""
    picks = [_pick(r["pick_no"], r["roster_id"], r.get("pos", "RB")) for r in rows]
    roster_ids = sorted({r["roster_id"] for r in rows} | set(extra_rosters), key=int)
    league = LeagueModel(
        league_id="L1",
        name="Test",
        season=2025,
        format=_fmt(superflex=superflex, te_premium=te_premium, team_count=team_count),
        teams=[Team(roster_id=r, manager=f"m{r}") for r in roster_ids],
        picks=picks,
        draft=Draft(id="d1"),
    )
    return league, _consensus(rows)


def _grade(scenario: tuple[LeagueModel, ConsensusMetrics], roster_id: str) -> TeamGrade:
    league, consensus = scenario
    grades = compute_draft_grades(league, consensus)
    return next(t for t in grades.teams if t.roster_id == roster_id)


# --------------------------------------------------------------------------- #
# Row: tier weighting
# --------------------------------------------------------------------------- #


def test_tier_weighting_draft_score() -> None:
    # (delta, slot) = (+6, 1), (-12, 58), (0, 1)
    #   +6 * (59/60) = +5.9 ; -12 * max(0.15, 2/60) = -1.8 ; 0
    scenario = _scenario(
        [
            {"pick_no": 1, "roster_id": "1", "slot": 1, "delta": 6},
            {"pick_no": 2, "roster_id": "1", "slot": 58, "delta": -12},
            {"pick_no": 3, "roster_id": "1", "slot": 1, "delta": 0},
        ],
        team_count=100,
    )
    assert _grade(scenario, "1").draft_score == 4.1


# --------------------------------------------------------------------------- #
# Row: premium_picks boundary (consensus_slot <= 10)
# --------------------------------------------------------------------------- #


def test_premium_picks_boundary_is_inclusive_at_10() -> None:
    scenario = _scenario(
        [
            {"pick_no": 1, "roster_id": "1", "slot": 9, "delta": 0},
            {"pick_no": 2, "roster_id": "1", "slot": 10, "delta": 0},
            {"pick_no": 3, "roster_id": "1", "slot": 11, "delta": 0},
        ]
    )
    assert _grade(scenario, "1").premium_picks == 2


# --------------------------------------------------------------------------- #
# Row: format_fit -- 2QB, non-2QB, premium TE
# --------------------------------------------------------------------------- #


def test_format_fit_superflex_two_qb_is_four() -> None:
    scenario = _scenario(
        [
            {"pick_no": 1, "roster_id": "1", "pos": "QB", "slot": 20, "delta": 0},
            {"pick_no": 2, "roster_id": "1", "pos": "QB", "slot": 21, "delta": 0},
            {"pick_no": 3, "roster_id": "1", "pos": "RB", "slot": 22, "delta": 0},
        ],
        superflex=True,
    )
    assert _grade(scenario, "1").format_fit == 4


def test_format_fit_two_qb_but_not_superflex_is_zero() -> None:
    scenario = _scenario(
        [
            {"pick_no": 1, "roster_id": "1", "pos": "QB", "slot": 20, "delta": 0},
            {"pick_no": 2, "roster_id": "1", "pos": "QB", "slot": 21, "delta": 0},
        ],
        superflex=False,
    )
    assert _grade(scenario, "1").format_fit == 0


def test_format_fit_premium_te_counts_only_slot_le_12() -> None:
    scenario = _scenario(
        [
            {"pick_no": 1, "roster_id": "1", "pos": "TE", "slot": 8, "delta": 0},
            {"pick_no": 2, "roster_id": "1", "pos": "TE", "slot": 20, "delta": 0},
        ],
        te_premium=True,
    )
    assert _grade(scenario, "1").format_fit == 1


# --------------------------------------------------------------------------- #
# Row: grade_input + letter  (the golden tdamoney roster)
# --------------------------------------------------------------------------- #


def test_grade_input_and_letter_d_plus() -> None:
    # draft_score -11.6 (= -11.0 + -0.6), premium_picks 1, format_fit 2 (superflex + 1 QB)
    scenario = _scenario(
        [
            {"pick_no": 1, "roster_id": "1", "pos": "QB", "slot": 5, "delta": -12},
            {"pick_no": 2, "roster_id": "1", "pos": "RB", "slot": 55, "delta": -4},
        ],
        superflex=True,
    )
    grade = _grade(scenario, "1")
    assert grade.draft_score == -11.6
    assert grade.premium_picks == 1
    assert grade.format_fit == 2
    assert grade.grade_input == -6.6
    assert grade.letter == "D+"


# --------------------------------------------------------------------------- #
# Row: letter cut table  (12.8 / 3.9 / 0.4 / -7.0 -> A / B- / C / D+)
# --------------------------------------------------------------------------- #


def test_letter_cut_table_sample_row() -> None:
    scenario = _scenario(
        [
            {"pick_no": 1, "roster_id": "1", "slot": 12, "delta": 16},  # 0.8 * 16 = 12.8
            {"pick_no": 2, "roster_id": "2", "slot": 42, "delta": 13},  # 0.3 * 13 = 3.9
            {"pick_no": 3, "roster_id": "3", "slot": 36, "delta": 1},   # 0.4 * 1 = 0.4
            {"pick_no": 4, "roster_id": "4", "slot": 30, "delta": -14}, # 0.5 * -14 = -7.0
        ]
    )
    league, consensus = scenario
    got = {t.roster_id: (t.grade_input, t.letter) for t in compute_draft_grades(league, consensus).teams}
    assert got["1"] == (12.8, "A")
    assert got["2"] == (3.9, "B-")
    assert got["3"] == (0.4, "C")
    assert got["4"] == (-7.0, "D+")


# --------------------------------------------------------------------------- #
# Row: premium floor / no floor  (grade_input -4.0, premium_picks 2 / 1)
# --------------------------------------------------------------------------- #


def test_premium_floor_raises_below_c_to_c() -> None:
    # two premium picks, draft_score -10.0 -> grade_input -10.0 + 6 = -4.0 -> C- -> floored to C
    scenario = _scenario(
        [
            {"pick_no": 1, "roster_id": "1", "slot": 10, "delta": -6},
            {"pick_no": 2, "roster_id": "1", "slot": 10, "delta": -6},
        ]
    )
    grade = _grade(scenario, "1")
    assert grade.premium_picks == 2
    assert grade.grade_input == -4.0
    assert grade.letter == "C"


def test_no_floor_below_two_premium_picks() -> None:
    # one premium pick, draft_score -7.0 -> grade_input -7.0 + 3 = -4.0 -> C-, not floored
    scenario = _scenario(
        [
            {"pick_no": 1, "roster_id": "1", "slot": 10, "delta": -6},
            {"pick_no": 2, "roster_id": "1", "slot": 30, "delta": -4},
        ]
    )
    grade = _grade(scenario, "1")
    assert grade.premium_picks == 1
    assert grade.grade_input == -4.0
    assert grade.letter == "C-"


# --------------------------------------------------------------------------- #
# Row: driving_picks  (contributions [-3.9, -2.1, -0.3, +1.8] -> [11, 23, 47])
# --------------------------------------------------------------------------- #


def test_driving_picks_magnitude_and_order() -> None:
    scenario = _scenario(
        [
            {"pick_no": 23, "roster_id": "1", "slot": 42, "delta": -13},  # 0.3 * -13 = -3.9
            {"pick_no": 11, "roster_id": "1", "slot": 42, "delta": -7},   # -2.1
            {"pick_no": 5, "roster_id": "1", "slot": 42, "delta": -1},    # -0.3  (below cutoff)
            {"pick_no": 47, "roster_id": "1", "slot": 42, "delta": 6},    # +1.8
        ]
    )
    assert _grade(scenario, "1").driving_picks == [11, 23, 47]


def test_driving_picks_capped_at_four_keeps_largest_ties_to_earlier() -> None:
    # six picks over the 1.5 cutoff; keep the four largest, tie -> earlier pick_no
    scenario = _scenario(
        [
            {"pick_no": 10, "roster_id": "1", "slot": 30, "delta": 20},  # 10.0
            {"pick_no": 20, "roster_id": "1", "slot": 30, "delta": 16},  # 8.0
            {"pick_no": 30, "roster_id": "1", "slot": 30, "delta": 12},  # 6.0
            {"pick_no": 40, "roster_id": "1", "slot": 30, "delta": -8},  # -4.0  (magnitude 4.0)
            {"pick_no": 5, "roster_id": "1", "slot": 30, "delta": 8},    # 4.0   (tie with pick 40 -> earlier wins)
            {"pick_no": 50, "roster_id": "1", "slot": 30, "delta": 4},   # 2.0   (smallest, dropped)
        ]
    )
    assert _grade(scenario, "1").driving_picks == [5, 10, 20, 30]


# --------------------------------------------------------------------------- #
# Row: all-no-consensus roster
# --------------------------------------------------------------------------- #


def test_all_no_consensus_roster_still_grades_from_positions() -> None:
    scenario = _scenario(
        [
            {"pick_no": 1, "roster_id": "1", "pos": "QB"},
            {"pick_no": 2, "roster_id": "1", "pos": "QB"},
        ],
        superflex=True,
    )
    grade = _grade(scenario, "1")
    assert grade.draft_score == 0.0
    assert grade.premium_picks == 0
    assert grade.driving_picks == []
    assert grade.format_fit == 4  # positions are consensus-independent
    assert grade.grade_input == 4.0
    assert grade.letter in THIRTEEN_POINT_SCALE


def test_roster_with_no_picks_appears_with_zeroed_grade() -> None:
    scenario = _scenario(
        [{"pick_no": 1, "roster_id": "1", "slot": 5, "delta": 2}],
        extra_rosters=("2",),
    )
    grade = _grade(scenario, "2")
    assert grade.draft_score == 0.0
    assert grade.premium_picks == 0
    assert grade.format_fit == 0
    assert grade.driving_picks == []


# --------------------------------------------------------------------------- #
# Table-driven: every one of the 13 letters, and the band boundaries
# --------------------------------------------------------------------------- #


_LETTER_CASES = [
    (20.0, "A+"),
    (16.0, "A+"),
    (15.5, "A"),
    (12.0, "A"),
    (11.5, "A-"),
    (9.0, "A-"),
    (8.5, "B+"),
    (6.0, "B+"),
    (5.5, "B"),
    (4.5, "B"),
    (4.0, "B-"),
    (3.0, "B-"),
    (2.5, "C+"),
    (1.0, "C+"),
    (0.5, "C"),
    (0.0, "C"),
    (-1.0, "C"),
    (-1.5, "C-"),
    (-4.0, "C-"),
    (-4.5, "D+"),
    (-8.0, "D+"),
    (-8.5, "D"),
    (-13.0, "D"),
    (-13.5, "D-"),
    (-18.0, "D-"),
    (-18.5, "F"),
    (-25.0, "F"),
]


@pytest.mark.parametrize("grade_input, expected", _LETTER_CASES)
def test_letter_scale_covers_all_thirteen_letters(grade_input: float, expected: str) -> None:
    # one non-premium pick at slot 30 (weight 0.5) -> draft_score == grade_input == 0.5 * delta
    delta = round(grade_input / 0.5)
    assert delta * 0.5 == grade_input, "pick a grade_input reachable as 0.5 * int"
    scenario = _scenario([{"pick_no": 1, "roster_id": "1", "slot": 30, "delta": delta}])
    grade = _grade(scenario, "1")
    assert grade.grade_input == grade_input
    assert grade.letter == expected


def test_every_letter_in_the_scale_is_exercised() -> None:
    assert {letter for _, letter in _LETTER_CASES} == _THIRTEEN
    assert THIRTEEN_POINT_SCALE == _THIRTEEN


@pytest.mark.parametrize(
    "letter_input_delta, premium, expected",
    [
        # slot 5 premium picks; extra non-premium filler tunes grade_input.
        ("C stays C", 2, "C"),
        ("above C untouched", 2, "C+"),
        ("one premium no floor", 1, "D"),
    ],
)
def test_floor_only_fires_at_two_premium_and_only_below_c(
    letter_input_delta: str, premium: int, expected: str
) -> None:
    if letter_input_delta == "C stays C":
        rows = [
            {"pick_no": 1, "roster_id": "1", "slot": 5, "delta": -3},
            {"pick_no": 2, "roster_id": "1", "slot": 5, "delta": -3},
        ]
        # -3 * (55/60) * 2 = -5.5 ; + 6 premium bonus -> grade_input ~ 0.5 -> C (no change)
    elif letter_input_delta == "above C untouched":
        rows = [
            {"pick_no": 1, "roster_id": "1", "slot": 5, "delta": -2},
            {"pick_no": 2, "roster_id": "1", "slot": 5, "delta": -2},
        ]
        # -2 * (55/60) * 2 = -3.7 ; + 6 premium bonus -> grade_input 2.3 -> C+ (above C, untouched)
    else:
        rows = [
            {"pick_no": 1, "roster_id": "1", "slot": 5, "delta": -14},
            {"pick_no": 2, "roster_id": "1", "slot": 40, "delta": 0},
        ]
        # -14 * (55/60) = -12.8 ; + 3 (one premium pick) -> grade_input -9.8 -> D, floor inactive
    grade = _grade(_scenario(rows), "1")
    assert grade.premium_picks == premium
    if letter_input_delta == "one premium no floor":
        assert grade.letter == expected
        assert grade.letter in {"D+", "D", "D-", "F", "C-"}  # genuinely below C
    else:
        # floor never lowers a grade and never fires below 2 premium picks
        assert grade.letter == expected


# --------------------------------------------------------------------------- #
# Ordering + determinism
# --------------------------------------------------------------------------- #


def test_teams_follow_league_order_and_grade_method_is_the_constant() -> None:
    scenario = _scenario(
        [
            {"pick_no": 1, "roster_id": "3", "slot": 5, "delta": 1},
            {"pick_no": 2, "roster_id": "1", "slot": 6, "delta": 1},
            {"pick_no": 3, "roster_id": "2", "slot": 7, "delta": 1},
        ]
    )
    league, consensus = scenario
    grades = compute_draft_grades(league, consensus)
    assert [t.roster_id for t in grades.teams] == [t.roster_id for t in league.teams]
    assert isinstance(grades, DraftGrades)
    assert all(isinstance(t, TeamGrade) for t in grades.teams)
    assert grades.grade_method == GRADE_METHOD
    assert isinstance(GRADE_METHOD, GradeMethod)
    assert GRADE_METHOD.letter_scale == "A-F+/-"
    assert GRADE_METHOD.floor_rule == ">=2 premium picks -> minimum C"


def test_deterministic_on_hand_built_input() -> None:
    scenario = _scenario(
        [
            {"pick_no": 1, "roster_id": "1", "pos": "QB", "slot": 3, "delta": 4},
            {"pick_no": 2, "roster_id": "1", "pos": "RB", "slot": 40, "delta": -6},
            {"pick_no": 3, "roster_id": "2", "pos": "TE", "slot": 12, "delta": 2},
            {"pick_no": 4, "roster_id": "2", "pos": "WR"},
        ],
        superflex=True,
        te_premium=True,
    )
    league, consensus = scenario
    first = compute_draft_grades(league, consensus)
    second = compute_draft_grades(copy.deepcopy(league), copy.deepcopy(consensus))
    assert first.model_dump() == second.model_dump()
    assert first.model_dump_json() == second.model_dump_json()


# --------------------------------------------------------------------------- #
# Import fence: grades.py never names commishdesk.consensus
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
                names.add(f"commishdesk.stats.{node.module}" if node.module else "commishdesk.stats")
            elif node.level == 2:
                names.add(node.module or "commishdesk")
        elif isinstance(node, ast.Call):
            func = node.func
            fname = getattr(func, "attr", None) or getattr(func, "id", None)
            if fname in {"import_module", "__import__"}:
                names.update(
                    a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)
                )
    return names


def test_grades_never_reaches_the_network_consensus_module() -> None:
    imported = _imported_dotted_names(STATS_GRADES)
    assert "commishdesk.consensus" not in imported
    forbidden_roots = {"adapters", "store", "facts", "narrate", "render", "deliver"}
    offenders = [
        name
        for name in imported
        if name.split(".")[:1] == ["commishdesk"]
        and len(name.split(".")) > 1
        and name.split(".")[1] in forbidden_roots
    ]
    assert not offenders, offenders
    # what it *is* allowed to reach
    assert "commishdesk.ingest" in imported
    assert "commishdesk.stats.consensus" in imported


# --------------------------------------------------------------------------- #
# Both committed fixtures reconciled against the synthetic oracle
# --------------------------------------------------------------------------- #


def _synthetic_slots() -> dict[str, int]:
    board = json.loads((CONSENSUS_DIR / "fantasycalc-values.json").read_text(encoding="utf-8"))
    ordered = sorted(board, key=lambda e: -e["value"])
    return {e["player"]["sleeperId"]: i for i, e in enumerate(ordered, start=1)}


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


@pytest.mark.parametrize("name", FIXTURES)
def test_committed_fixture_reconciles_with_expected(name: str) -> None:
    league = build_league_model(_bundle(name))
    consensus = compute_consensus_metrics(league, _synthetic_slots())
    grades = compute_draft_grades(league, consensus)

    expected = json.loads((GRADES_DIR / "expected-draft-grades.json").read_text(encoding="utf-8"))
    expected = {k: v for k, v in expected.items() if not k.startswith("_")}

    assert grades.model_dump() == expected
    assert all(t["letter"] in _THIRTEEN for t in expected["teams"])


@pytest.mark.parametrize("name", FIXTURES)
def test_committed_fixture_is_deterministic(name: str) -> None:
    league = build_league_model(_bundle(name))
    slots = _synthetic_slots()
    first = compute_draft_grades(league, compute_consensus_metrics(league, slots))
    second = compute_draft_grades(league, compute_consensus_metrics(league, dict(slots)))
    assert first.model_dump() == second.model_dump()


# --------------------------------------------------------------------------- #
# Floor: every "below C" band is raised to exactly "C" at >= 2 premium picks
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "filler_delta, raw_band, expected_grade_input",
    [
        (-24, "D+", -6.0),   # -24 * 0.5 = -12.0 draft_score ; + 6 premium bonus -> -6.0
        (-32, "D", -10.0),
        (-42, "D-", -15.0),
        (-52, "F", -20.0),
    ],
)
def test_premium_floor_raises_every_below_c_band_to_c(
    filler_delta: int, raw_band: str, expected_grade_input: float
) -> None:
    # Two premium picks (slot 5, delta 0 -> no draft_score contribution) + one
    # non-premium filler at slot 30 (weight 0.5) that drives grade_input into the
    # named band. A regression narrowing _FLOOR_LETTERS to {"C-"} fails here.
    rows = [
        {"pick_no": 1, "roster_id": "1", "slot": 5, "delta": 0},
        {"pick_no": 2, "roster_id": "1", "slot": 5, "delta": 0},
        {"pick_no": 3, "roster_id": "1", "slot": 30, "delta": filler_delta},
    ]
    grade = _grade(_scenario(rows), "1")
    assert grade.premium_picks == 2
    assert grade.grade_input == expected_grade_input  # lands in the raw_band pre-floor
    assert grade.letter == "C"


# --------------------------------------------------------------------------- #
# No negative-zero leaks into draft_score / grade_input
# --------------------------------------------------------------------------- #


def test_draft_score_and_grade_input_are_positive_zero_not_negative_zero() -> None:
    # slot 11 delta -2 -> -1.6333... ; slot 12 delta +2 -> +1.6 ; net -0.0333
    # rounds to -0.0, which the ``or 0.0`` guard must normalise to +0.0.
    scenario = _scenario(
        [
            {"pick_no": 1, "roster_id": "1", "slot": 11, "delta": -2},
            {"pick_no": 2, "roster_id": "1", "slot": 12, "delta": 2},
        ]
    )
    grade = _grade(scenario, "1")
    assert grade.premium_picks == 0
    assert grade.draft_score == 0.0
    assert math.copysign(1.0, grade.draft_score) == 1.0
    assert grade.grade_input == 0.0
    assert math.copysign(1.0, grade.grade_input) == 1.0


# --------------------------------------------------------------------------- #
# driving_picks: the 1.5 cutoff is inclusive
# --------------------------------------------------------------------------- #


def test_driving_pick_exactly_on_cutoff_is_included() -> None:
    # slot 30 -> weight 0.5 ; delta 3 -> contribution exactly 1.5 (>= cutoff, kept)
    # slot 30, delta 2 -> 1.0 (< cutoff, dropped)
    scenario = _scenario(
        [
            {"pick_no": 7, "roster_id": "1", "slot": 30, "delta": 3},
            {"pick_no": 8, "roster_id": "1", "slot": 30, "delta": 2},
        ]
    )
    assert _grade(scenario, "1").driving_picks == [7]


# --------------------------------------------------------------------------- #
# Import fence: no clock, no PRNG, no filesystem
# --------------------------------------------------------------------------- #


def test_grades_imports_no_clock_prng_or_filesystem_module() -> None:
    imported = _imported_dotted_names(STATS_GRADES)
    banned = {"datetime", "time", "os", "pathlib", "random", "secrets"}
    hits = {name for name in imported if name.split(".")[0] in banned}
    assert not hits, hits


# --------------------------------------------------------------------------- #
# format_fit: 2QB cap, and superflex + te_premium composing
# --------------------------------------------------------------------------- #


def test_format_fit_three_qb_in_superflex_still_caps_at_four() -> None:
    scenario = _scenario(
        [
            {"pick_no": 1, "roster_id": "1", "pos": "QB", "slot": 20, "delta": 0},
            {"pick_no": 2, "roster_id": "1", "pos": "QB", "slot": 21, "delta": 0},
            {"pick_no": 3, "roster_id": "1", "pos": "QB", "slot": 22, "delta": 0},
        ],
        superflex=True,
    )
    assert _grade(scenario, "1").format_fit == 4


def test_format_fit_superflex_and_te_premium_compose_additively() -> None:
    # 2 QB -> +4 ; two premium TE at consensus_slot <= 12 -> +2 ; a slot-30 TE excluded
    scenario = _scenario(
        [
            {"pick_no": 1, "roster_id": "1", "pos": "QB", "slot": 20, "delta": 0},
            {"pick_no": 2, "roster_id": "1", "pos": "QB", "slot": 21, "delta": 0},
            {"pick_no": 3, "roster_id": "1", "pos": "TE", "slot": 6, "delta": 0},
            {"pick_no": 4, "roster_id": "1", "pos": "TE", "slot": 12, "delta": 0},
            {"pick_no": 5, "roster_id": "1", "pos": "TE", "slot": 30, "delta": 0},
        ],
        superflex=True,
        te_premium=True,
    )
    assert _grade(scenario, "1").format_fit == 6


# --------------------------------------------------------------------------- #
# Degenerate league shapes never raise
# --------------------------------------------------------------------------- #


def test_never_raises_on_empty_league_teams() -> None:
    league = LeagueModel(
        league_id="L1",
        name="Test",
        season=2025,
        format=_fmt(),
        teams=[],
        picks=[],
        draft=Draft(id="d1"),
    )
    grades = compute_draft_grades(league, ConsensusMetrics(picks=[], teams=[]))
    assert grades.teams == []
    assert grades.grade_method == GRADE_METHOD


def test_thirteen_point_scale_is_reachable_via_the_package_re_export() -> None:
    import commishdesk.stats as stats

    assert stats.THIRTEEN_POINT_SCALE == THIRTEEN_POINT_SCALE
    assert "THIRTEEN_POINT_SCALE" in stats.__all__
