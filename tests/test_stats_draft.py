"""Story 2.4a: board-only draft metrics.

One test per I/O & Edge-Case Matrix row -- both committed fixtures parametrized
through ``compute_board_metrics``, small hand-built ``LeagueModel``s for the
orphan / zero-pick / ``UNK`` / overlap / no-run / board-wide-run rows, determinism
+ purity checks, and AST tests that ``commishdesk/stats/*.py`` imports nothing
from ``adapters`` / ``store`` / a later pipeline stage (AD-1) and nothing that
would break offline determinism.

Acceptance Criterion 1 is reconciled against the committed
``tests/fixtures/board-metrics/rookie-draft-board-metrics.json`` -- values there
were derived once from ``brief/phase-0/draft-recap-facts.json`` and hand-verified
(see that file's ``_derived_from``). The reconciliation runs unconditionally, so
CI (which has no private ``brief/`` tree) still verifies it.
"""

from __future__ import annotations

import ast
import copy
import json
import subprocess
import sys
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
    MIN_RUN,
    BoardMetrics,
    PositionalRun,
    TeamBoard,
    compute_board_metrics,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
STATS_DIR = REPO_ROOT / "commishdesk" / "stats"
EXPECTED_PATH = FIXTURE_DIR / "board-metrics" / "rookie-draft-board-metrics.json"

FIXTURES = ("rookie-draft.json", "week10-superflex.json")

_STATS_PKG_PARTS = ("commishdesk", "stats")

# stats/ may not import from its own upstream (adapters), the storage port, or
# any later pipeline stage -- it consumes only commishdesk.ingest (AD-1).
_FORBIDDEN_IMPORTS = ("adapters", "store", "facts", "narrate", "render", "deliver")

# stats/ is pure/deterministic/offline: nothing that touches a socket, the
# network, or a PRNG may be imported.
_OFFLINE_BANNED = frozenset(
    {"socket", "urllib", "http", "httpx", "requests", "random", "secrets", "ssl"}
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _load_fixture(name: str) -> dict[str, Any]:
    raw = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    return {
        "league": raw["league"],
        "draft": raw["draft"],
        "draft_picks": raw["draft_picks"],
        "rosters": raw["rosters"],
        "users": raw["users"],
        "previous_league_ids": [],
    }


def _fmt() -> LeagueFormat:
    return LeagueFormat(
        team_count=2,
        roster_slots=["QB", "RB", "WR", "TE"],
        flex_eligibility={},
        scoring_label="ppr",
        is_superflex_or_2qb=False,
        te_premium=False,
    )


def _pick(
    pick_no: int, roster_id: str | int, position: str | None, *, rnd: int = 1, slot: int = 1
) -> Pick:
    return Pick(
        pick_no=pick_no,
        round=rnd,
        slot=slot,
        board_label=f"{rnd}.{slot:02d}",
        roster_id=str(roster_id),
        manager=None,
        player=Player(sleeper_id=str(pick_no), name=f"Player {pick_no}", position=position),
    )


def _league(teams: list[Team], picks: list[Pick]) -> LeagueModel:
    return LeagueModel(
        league_id="L1",
        name="Test League",
        season=2025,
        format=_fmt(),
        teams=teams,
        picks=picks,
        draft=Draft(id="d1"),
    )


def _b2b_as_lists(board: TeamBoard) -> list[list[int]]:
    return [list(pair) for pair in board.back_to_back]


# --------------------------------------------------------------------------- #
# Matrix: happy path (rookie) + shape-agnostic (superflex)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", FIXTURES)
def test_fixture_produces_valid_board_metrics(name: str) -> None:
    league = build_league_model(_load_fixture(name))
    metrics = compute_board_metrics(league)

    assert isinstance(metrics, BoardMetrics)
    # one TeamBoard per roster, in league.teams order, keyed by roster_id (str)
    assert [b.roster_id for b in metrics.teams] == [t.roster_id for t in league.teams]

    picks_by_roster: dict[str, int] = {}
    for pick in league.picks:
        picks_by_roster[pick.roster_id] = picks_by_roster.get(pick.roster_id, 0) + 1

    for board in metrics.teams:
        team = next(t for t in league.teams if t.roster_id == board.roster_id)
        assert board.manager == team.manager  # carried verbatim
        assert board.pick_count == picks_by_roster.get(board.roster_id, 0)
        assert board.pick_nos == sorted(board.pick_nos)
        assert board.pick_count == len(board.pick_nos)
        assert sum(board.positional_counts.values()) == board.pick_count
        assert list(board.positional_counts) == sorted(board.positional_counts)
        for lo, hi in board.back_to_back:
            assert hi == lo + 1

    for run in metrics.positional_runs:
        assert run.length >= MIN_RUN
        assert run.position != "UNK"
    assert [r.from_pick_no for r in metrics.positional_runs] == sorted(
        r.from_pick_no for r in metrics.positional_runs
    )


def test_rookie_draft_has_populated_runs_and_profiles() -> None:
    """Matrix row 1: rookie draft -> per-roster fields populated, runs non-empty."""
    league = build_league_model(_load_fixture("rookie-draft.json"))
    metrics = compute_board_metrics(league)

    assert metrics.positional_runs, "rookie fixture must yield >=1 positional run"
    assert any(b.back_to_back for b in metrics.teams)
    assert any(b.zero_positions for b in metrics.teams)
    assert all(b.positional_counts for b in metrics.teams)


# --------------------------------------------------------------------------- #
# Matrix: rookie-draft reconciliation (Acceptance Criterion 1)
# --------------------------------------------------------------------------- #


def test_rookie_draft_reconciles_with_committed_expected_values() -> None:
    """AC1: every per-roster field and the board-wide runs equal the committed,
    hand-verified expected-values fixture (derived from the phase-0 oracle)."""
    league = build_league_model(_load_fixture("rookie-draft.json"))
    metrics = compute_board_metrics(league)
    boards = {b.roster_id: b for b in metrics.teams}

    expected = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    exp_rosters: dict[str, dict[str, Any]] = expected["rosters"]

    assert sorted(boards, key=int) == sorted(exp_rosters, key=int), (
        f"roster id set differs: got {sorted(boards, key=int)}, "
        f"expected {sorted(exp_rosters, key=int)}"
    )

    for roster_id, want in exp_rosters.items():
        board = boards[roster_id]
        assert board.pick_count == want["pick_count"], f"roster {roster_id}: pick_count"
        assert board.pick_nos == want["pick_nos"], f"roster {roster_id}: pick_nos"
        assert board.positional_counts == want["positional_counts"], (
            f"roster {roster_id}: positional_counts got {board.positional_counts} "
            f"want {want['positional_counts']}"
        )
        assert _b2b_as_lists(board) == [list(p) for p in want["back_to_back"]], (
            f"roster {roster_id}: back_to_back"
        )
        assert board.zero_positions == want["zero_positions"], (
            f"roster {roster_id}: zero_positions got {board.zero_positions} "
            f"want {want['zero_positions']}"
        )

    got_runs = [r.model_dump() for r in metrics.positional_runs]
    assert got_runs == expected["positional_runs"], (
        f"positional_runs got {got_runs} want {expected['positional_runs']}"
    )


# --------------------------------------------------------------------------- #
# Matrix: orphan roster
# --------------------------------------------------------------------------- #


def test_orphan_roster_emits_teamboard_with_manager_none() -> None:
    league = _league(
        teams=[Team(roster_id="1", manager=None), Team(roster_id="2", manager="mgr2")],
        picks=[_pick(1, 1, "QB"), _pick(2, 2, "RB")],
    )
    metrics = compute_board_metrics(league)

    orphan = next(b for b in metrics.teams if b.roster_id == "1")
    assert orphan.manager is None
    assert orphan.pick_count == 1


# --------------------------------------------------------------------------- #
# Matrix: roster with zero picks
# --------------------------------------------------------------------------- #


def test_roster_with_zero_picks_gets_full_universe() -> None:
    league = _league(
        teams=[Team(roster_id="1", manager="a"), Team(roster_id="2", manager="b")],
        picks=[_pick(1, 1, "QB"), _pick(2, 1, "RB"), _pick(3, 1, "WR")],
    )
    metrics = compute_board_metrics(league)
    empty = next(b for b in metrics.teams if b.roster_id == "2")

    assert empty.pick_count == 0
    assert empty.pick_nos == []
    assert empty.back_to_back == []
    assert empty.positional_counts == {}
    assert empty.zero_positions == ["QB", "RB", "WR"]  # full board-wide universe


# --------------------------------------------------------------------------- #
# Matrix: unknown player position
# --------------------------------------------------------------------------- #


def test_unknown_position_counts_but_is_excluded_from_universe_and_runs() -> None:
    league = _league(
        teams=[Team(roster_id="1", manager="a"), Team(roster_id="2", manager="b")],
        picks=[
            _pick(1, 1, None),
            _pick(2, 1, None),
            _pick(3, 1, None),
            _pick(4, 2, "RB"),
        ],
    )
    metrics = compute_board_metrics(league)
    board1 = next(b for b in metrics.teams if b.roster_id == "1")

    assert board1.positional_counts == {"UNK": 3}
    assert board1.pick_count == 3
    assert "UNK" not in board1.zero_positions
    # RB is the only real position on the board -> roster 1 missed it
    assert board1.zero_positions == ["RB"]
    assert metrics.positional_runs == []  # a 3-long UNK span is not a run


# --------------------------------------------------------------------------- #
# Matrix: overlapping back-to-back
# --------------------------------------------------------------------------- #


def test_overlapping_back_to_back_pairs_are_not_merged() -> None:
    league = _league(
        teams=[Team(roster_id="1", manager="a")],
        picks=[_pick(10, 1, "RB"), _pick(11, 1, "WR"), _pick(12, 1, "TE")],
    )
    board = compute_board_metrics(league).teams[0]
    assert _b2b_as_lists(board) == [[10, 11], [11, 12]]


# --------------------------------------------------------------------------- #
# Matrix: runs
# --------------------------------------------------------------------------- #


def test_no_run_reaches_min_run() -> None:
    league = _league(
        teams=[Team(roster_id="1", manager="a")],
        picks=[
            _pick(1, 1, "RB"),
            _pick(2, 1, "WR"),
            _pick(3, 1, "RB"),
            _pick(4, 1, "WR"),
            _pick(5, 1, "QB"),
        ],
    )
    assert compute_board_metrics(league).positional_runs == []


def test_maximal_same_position_span_becomes_one_run() -> None:
    league = _league(
        teams=[Team(roster_id="1", manager="a")],
        picks=[
            _pick(1, 1, "QB"),
            _pick(2, 1, "RB", rnd=1, slot=2),
            _pick(3, 1, "RB", rnd=1, slot=3),
            _pick(4, 1, "RB", rnd=1, slot=4),
            _pick(5, 1, "RB", rnd=1, slot=5),
            _pick(6, 1, "WR", rnd=1, slot=6),
        ],
    )
    runs = compute_board_metrics(league).positional_runs
    assert len(runs) == 1
    run = runs[0]
    assert isinstance(run, PositionalRun)
    assert (run.position, run.from_pick_no, run.to_pick_no, run.length) == ("RB", 2, 5, 4)
    assert (run.from_label, run.to_label) == ("1.02", "1.05")


def test_run_scan_is_board_wide_across_roster_and_round_seams() -> None:
    """A same-position span that crosses both a roster boundary and a round seam
    is one run -- ``_scan_runs`` never resets on either."""
    league = _league(
        teams=[Team(roster_id="1", manager="a"), Team(roster_id="2", manager="b")],
        picks=[
            _pick(1, 1, "QB", rnd=1, slot=1),
            _pick(2, 2, "WR", rnd=1, slot=2),  # roster 2, round 1
            _pick(3, 1, "WR", rnd=1, slot=3),  # roster 1
            _pick(4, 2, "WR", rnd=2, slot=1),  # roster 2, ROUND SEAM 1->2
            _pick(5, 1, "WR", rnd=2, slot=2),
            _pick(6, 2, "RB", rnd=2, slot=3),
        ],
    )
    runs = compute_board_metrics(league).positional_runs
    assert len(runs) == 1
    run = runs[0]
    assert (run.position, run.from_pick_no, run.to_pick_no, run.length) == ("WR", 2, 5, 4)
    assert (run.from_label, run.to_label) == ("1.02", "2.02")


def test_run_at_the_very_first_board_pick() -> None:
    league = _league(
        teams=[Team(roster_id="1", manager="a")],
        picks=[
            _pick(1, 1, "RB", slot=1),
            _pick(2, 1, "RB", slot=2),
            _pick(3, 1, "RB", slot=3),
            _pick(4, 1, "WR", slot=4),
            _pick(5, 1, "QB", slot=5),
        ],
    )
    runs = compute_board_metrics(league).positional_runs
    assert len(runs) == 1
    assert (runs[0].position, runs[0].from_pick_no, runs[0].to_pick_no) == ("RB", 1, 3)
    assert runs[0].from_label == "1.01"


def test_run_at_the_very_last_board_pick() -> None:
    league = _league(
        teams=[Team(roster_id="1", manager="a")],
        picks=[
            _pick(1, 1, "QB", slot=1),
            _pick(2, 1, "WR", slot=2),
            _pick(3, 1, "TE", slot=3),
            _pick(4, 1, "TE", slot=4),
            _pick(5, 1, "TE", slot=5),
        ],
    )
    runs = compute_board_metrics(league).positional_runs
    assert len(runs) == 1
    assert (runs[0].position, runs[0].from_pick_no, runs[0].to_pick_no) == ("TE", 3, 5)
    assert runs[0].to_label == "1.05"


# --------------------------------------------------------------------------- #
# Matrix: determinism  (+ purity: input not mutated)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", FIXTURES)
def test_compute_is_deterministic_on_fixtures(name: str) -> None:
    league = build_league_model(_load_fixture(name))
    before = league.model_dump()

    first = compute_board_metrics(league)
    second = compute_board_metrics(copy.deepcopy(league))

    assert first.model_dump() == second.model_dump()
    # byte-level: catches a key-order regression in positional_counts / teams
    assert first.model_dump_json() == second.model_dump_json()
    # purity: the input model is untouched
    assert league.model_dump() == before


def test_compute_is_deterministic_on_hand_built_model() -> None:
    league = _league(
        teams=[Team(roster_id="1", manager="a"), Team(roster_id="2", manager=None)],
        picks=[_pick(1, 1, "QB"), _pick(2, 2, None), _pick(3, 1, "RB")],
    )
    before = league.model_dump()

    first = compute_board_metrics(league)
    second = compute_board_metrics(league)

    assert first.model_dump() == second.model_dump()
    assert first.model_dump_json() == second.model_dump_json()
    assert league.model_dump() == before


# --------------------------------------------------------------------------- #
# AD-1 — stats/ imports nothing from adapters / store / a later stage,
#        and nothing that would break offline determinism.
# --------------------------------------------------------------------------- #


def _resolve_relative(level: int, module: str | None) -> str:
    """Resolve a relative import from a module in ``commishdesk/stats/`` to its
    absolute dotted path (level 1 = ``commishdesk.stats``, level 2 =
    ``commishdesk``, ...). Mirrors ``tests/test_ingest.py``."""
    base = list(_STATS_PKG_PARTS[: len(_STATS_PKG_PARTS) - (level - 1)])
    if module:
        base += module.split(".")
    return ".".join(base)


def _is_forbidden(dotted: str) -> bool:
    parts = dotted.split(".")
    return parts[:1] == ["commishdesk"] and len(parts) > 1 and parts[1] in _FORBIDDEN_IMPORTS


def _stats_import_targets() -> list[tuple[str, str]]:
    """``(filename, dotted-target)`` for every import in ``commishdesk/stats/*.py``
    -- absolute and relative ``import`` / ``from`` plus dynamic ``import_module`` /
    ``__import__`` string args."""
    targets: list[tuple[str, str]] = []
    for path in sorted(STATS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    targets.append((path.name, alias.name))
            elif isinstance(node, ast.ImportFrom):
                dotted = (
                    node.module or ""
                    if node.level == 0
                    else _resolve_relative(node.level, node.module)
                )
                targets.append((path.name, dotted))
            elif isinstance(node, ast.Call):
                func = node.func
                fname = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else func.id
                    if isinstance(func, ast.Name)
                    else ""
                )
                if fname in {"import_module", "__import__"}:
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            targets.append((path.name, arg.value))
    return targets


def test_stats_imports_no_adapter_store_or_downstream_stage() -> None:
    offenders = [
        f"{fname}: {dotted}" for fname, dotted in _stats_import_targets() if _is_forbidden(dotted)
    ]
    assert not offenders, offenders


def test_stats_imports_nothing_that_breaks_offline_determinism() -> None:
    offenders = [
        f"{fname}: {dotted}"
        for fname, dotted in _stats_import_targets()
        if dotted.split(".")[0] in _OFFLINE_BANNED
    ]
    assert not offenders, offenders


def test_stats_package_imports_cleanly() -> None:
    import importlib

    module = importlib.import_module("commishdesk.stats")
    assert module.__doc__
    for name in ("MIN_RUN", "BoardMetrics", "PositionalRun", "TeamBoard", "compute_board_metrics"):
        assert hasattr(module, name), f"commishdesk.stats is missing re-export {name!r}"


def test_bare_stats_import_is_pydantic_free() -> None:
    """``import commishdesk.stats`` must not pull in pydantic -- the re-exports in
    ``stats/__init__.py`` are lazy so the package imports on a site-packages-free
    path (see the extension-zone wheel test)."""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import commishdesk.stats, sys; "
            "raise SystemExit(0 if 'pydantic' not in sys.modules else 1)",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
