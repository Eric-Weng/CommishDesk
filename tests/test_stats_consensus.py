"""Story 2.4b: ``compute_consensus_metrics`` — pure per-pick / per-team metrics.

One test per per-pick and per-team row of the spec's I/O & Edge-Case Matrix on
hand-built ``LeagueModel``s, plus both committed fixtures reconciled against the
synthetic ``expected-consensus-metrics.json`` oracle, determinism, and an
import-fence check that ``stats/consensus.py`` never reaches ``commishdesk.consensus``
or any non-``ingest`` engine module.
"""

from __future__ import annotations

import ast
import copy
import json
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
    ConsensusMetrics,
    PickConsensus,
    PickRef,
    TeamConsensus,
    compute_consensus_metrics,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
CONSENSUS_DIR = FIXTURE_DIR / "consensus"
STATS_CONSENSUS = REPO_ROOT / "commishdesk" / "stats" / "consensus.py"

FIXTURES = ("rookie-draft.json", "week10-superflex.json")

_FORBIDDEN_STATS_IMPORTS = frozenset(
    {"adapters", "store", "consensus", "facts", "narrate", "render", "deliver"}
)
_OFFLINE_BANNED = frozenset(
    {"socket", "urllib", "http", "httpx", "requests", "random", "secrets", "ssl"}
)


# --------------------------------------------------------------------------- #
# Helpers
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


def _fmt(team_count: int = 12) -> LeagueFormat:
    return LeagueFormat(
        team_count=team_count,
        roster_slots=["QB", "RB", "WR", "TE"],
        flex_eligibility={},
        scoring_label="PPR",
        is_superflex_or_2qb=False,
        te_premium=False,
    )


def _pick(pick_no: int, roster_id: str, sid: str, *, name: str | None = None) -> Pick:
    return Pick(
        pick_no=pick_no,
        round=(pick_no - 1) // 12 + 1,
        slot=(pick_no - 1) % 12 + 1,
        board_label=f"{(pick_no - 1) // 12 + 1}.{(pick_no - 1) % 12 + 1:02d}",
        roster_id=roster_id,
        manager=None,
        player=Player(sleeper_id=sid, name=name or f"Player {sid}", position="RB"),
    )


def _league(picks: list[Pick], *, team_count: int = 12) -> LeagueModel:
    roster_ids = sorted({p.roster_id for p in picks}, key=int)
    return LeagueModel(
        league_id="L1",
        name="Test",
        season=2025,
        format=_fmt(team_count),
        teams=[Team(roster_id=r, manager=f"m{r}") for r in roster_ids],
        picks=picks,
        draft=Draft(id="d1"),
    )


# --------------------------------------------------------------------------- #
# Row: delta signs  (delta = pick_no - consensus_slot; + = value, - = reach)
# --------------------------------------------------------------------------- #


def _identity_slots(pids: list[str], targets: dict[str, int]) -> dict[str, int]:
    """A ``sid -> rank`` mapping whose values are exactly ``1..len(pids)``, with
    ``targets`` pinned. Because the values are already a dense permutation, the
    function's internal dense re-rank is the identity and each pick lands on its
    pinned slot."""
    free_slots = sorted(set(range(1, len(pids) + 1)) - set(targets.values()))
    free_pids = [p for p in pids if p not in targets]
    slots = dict(targets)
    slots.update(dict(zip(free_pids, free_slots)))
    return slots


def test_delta_signs_value_reach_and_zero() -> None:
    # 58 drafted players; pin p1 -> slot 1, p16 -> slot 10, p46 -> slot 58.
    picks = [_pick(n, str((n % 12) + 1), f"p{n}") for n in range(1, 59)]
    league = _league(picks, team_count=100)
    slots = _identity_slots(
        [f"p{n}" for n in range(1, 59)], {"p1": 1, "p16": 10, "p46": 58}
    )
    by_pick = {p.pick_no: p for p in compute_consensus_metrics(league, slots).picks}

    assert by_pick[16].consensus_slot == 10 and by_pick[16].delta == 6  # value
    assert by_pick[46].consensus_slot == 58 and by_pick[46].delta == -12  # reach
    assert by_pick[1].consensus_slot == 1 and by_pick[1].delta == 0


# --------------------------------------------------------------------------- #
# Row: consensus_label from team_count
# --------------------------------------------------------------------------- #


def test_consensus_label_is_a_positional_readout() -> None:
    picks = [_pick(i, "1", f"p{i}") for i in range(1, 15)]
    league = _league(picks, team_count=12)
    slots = {f"p{i}": i for i in range(1, 15)}  # already dense 1..14
    labels = {
        p.player: p.consensus_label for p in compute_consensus_metrics(league, slots).picks
    }

    assert labels["Player p14"] == "2.02"  # slot 14, 12 teams
    assert labels["Player p1"] == "1.01"
    assert labels["Player p12"] == "1.12"
    assert labels["Player p13"] == "2.01"


# --------------------------------------------------------------------------- #
# Row: dense re-rank collapses gaps in the source ordering
# --------------------------------------------------------------------------- #


def test_dense_re_rank_over_the_drafted_set() -> None:
    picks = [_pick(1, "1", "id5"), _pick(2, "2", "id20"), _pick(3, "3", "id9")]
    league = _league(picks)
    # source ranks the drafted ids at 5, 20, 9 (with other ids in between)
    slots = {"id5": 5, "id20": 20, "id9": 9, "id_undrafted": 1}
    metrics = compute_consensus_metrics(league, slots)
    got = {p.player.replace("Player ", ""): p.consensus_slot for p in metrics.picks}

    assert got == {"id5": 1, "id9": 2, "id20": 3}


# --------------------------------------------------------------------------- #
# Row: player in neither source -> no_consensus
# --------------------------------------------------------------------------- #


def test_unranked_pick_is_flagged_no_consensus() -> None:
    picks = [_pick(1, "1", "known"), _pick(2, "1", "unknown")]
    league = _league(picks)
    metrics = compute_consensus_metrics(league, {"known": 1})
    unknown = next(p for p in metrics.picks if p.player == "Player unknown")

    assert unknown.consensus_slot is None
    assert unknown.consensus_label is None
    assert unknown.delta is None
    assert unknown.flags == ["no_consensus"]


# --------------------------------------------------------------------------- #
# Row: per-team raw extremes
# --------------------------------------------------------------------------- #


def test_per_team_best_value_and_biggest_reach() -> None:
    # roster "1" picks pinned to slots so its deltas are +6, -3, 0.
    picks = [_pick(n, "9", f"f{n}") for n in (1, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14)]
    picks += [
        _pick(2, "1", "big_reach"),  # pick 2  - slot 8 = -6
        _pick(10, "1", "even"),      # pick 10 - slot 10 = 0
        _pick(15, "1", "big_value"), # pick 15 - slot 9 = +6
    ]
    league = _league(picks, team_count=100)
    pids = [f"f{n}" for n in (1, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14)] + [
        "big_reach",
        "even",
        "big_value",
    ]
    slots = _identity_slots(pids, {"big_reach": 8, "even": 10, "big_value": 9})
    metrics = compute_consensus_metrics(league, slots)
    team = next(t for t in metrics.teams if t.roster_id == "1")

    assert team.best_value_pick == PickRef(pick_no=15, player="Player big_value", delta=6)
    assert team.biggest_reach_pick == PickRef(
        pick_no=2, player="Player big_reach", delta=-6
    )


def test_per_team_extremes_are_pick_refs_with_player_and_pick_no() -> None:
    fillers = [_pick(n, "9", f"f{n}") for n in (1, 2, 5, 6, 7)]
    picks = fillers + [
        _pick(8, "3", "a", name="Big Value"),
        _pick(3, "3", "b", name="Big Reach"),
    ]
    league = _league(picks, team_count=100)
    pids = [f"f{n}" for n in (1, 2, 5, 6, 7)] + ["a", "b"]
    slots = _identity_slots(pids, {"a": 2, "b": 7})
    team = next(
        t for t in compute_consensus_metrics(league, slots).teams if t.roster_id == "3"
    )

    assert team.best_value_pick == PickRef(pick_no=8, player="Big Value", delta=6)
    assert team.biggest_reach_pick == PickRef(pick_no=3, player="Big Reach", delta=-4)


def test_team_with_no_ranked_pick_has_none_extremes() -> None:
    picks = [_pick(1, "1", "ranked"), _pick(2, "2", "unranked_a"), _pick(3, "2", "unranked_b")]
    league = _league(picks)
    metrics = compute_consensus_metrics(league, {"ranked": 1})
    team2 = next(t for t in metrics.teams if t.roster_id == "2")

    assert team2.best_value_pick is None
    assert team2.biggest_reach_pick is None


def test_single_ranked_pick_is_both_extremes() -> None:
    picks = [_pick(5, "1", "only"), _pick(6, "1", "no")]
    league = _league(picks)
    metrics = compute_consensus_metrics(league, {"only": 1})
    team = next(t for t in metrics.teams if t.roster_id == "1")

    assert team.best_value_pick == team.biggest_reach_pick
    assert team.best_value_pick == PickRef(pick_no=5, player="Player only", delta=4)


# --------------------------------------------------------------------------- #
# Shape: picks ascend by pick_no, teams follow league.teams
# --------------------------------------------------------------------------- #


def test_output_ordering_contract() -> None:
    picks = [_pick(3, "2", "c"), _pick(1, "1", "a"), _pick(2, "2", "b")]
    league = _league(picks)
    metrics = compute_consensus_metrics(league, {"a": 1, "b": 2, "c": 3})

    assert [p.pick_no for p in metrics.picks] == [1, 2, 3]
    assert [t.roster_id for t in metrics.teams] == [t.roster_id for t in league.teams]
    assert isinstance(metrics, ConsensusMetrics)
    assert all(isinstance(p, PickConsensus) for p in metrics.picks)
    assert all(isinstance(t, TeamConsensus) for t in metrics.teams)


# --------------------------------------------------------------------------- #
# Both committed fixtures reconciled against the synthetic oracle
# --------------------------------------------------------------------------- #


def _synthetic_slots() -> dict[str, int]:
    board = json.loads(
        (CONSENSUS_DIR / "fantasycalc-values.json").read_text(encoding="utf-8")
    )
    ordered = sorted(board, key=lambda e: -e["value"])
    return {e["player"]["sleeperId"]: i for i, e in enumerate(ordered, start=1)}


@pytest.mark.parametrize("name", FIXTURES)
def test_committed_fixture_reconciles_with_expected(name: str) -> None:
    league = build_league_model(_bundle(name))
    metrics = compute_consensus_metrics(league, _synthetic_slots())

    expected = json.loads(
        (CONSENSUS_DIR / "expected-consensus-metrics.json").read_text(encoding="utf-8")
    )
    expected = {k: v for k, v in expected.items() if not k.startswith("_")}

    assert metrics.model_dump() == expected


@pytest.mark.parametrize("name", FIXTURES)
def test_committed_fixture_delta_identity_holds(name: str) -> None:
    league = build_league_model(_bundle(name))
    metrics = compute_consensus_metrics(league, _synthetic_slots())

    ranked = 0
    for pick in metrics.picks:
        if pick.consensus_slot is None:
            assert pick.consensus_label is None and pick.delta is None
            assert pick.flags == ["no_consensus"]
        else:
            assert pick.delta == pick.pick_no - pick.consensus_slot
            assert pick.flags == []
            ranked += 1
    assert ranked > 0 and ranked < len(metrics.picks)  # both paths exercised

    # every team extreme is the max / min delta among that roster's ranked picks
    picks_by_roster: dict[str, list[int]] = {}
    for pick in metrics.picks:
        if pick.delta is not None:
            picks_by_roster.setdefault(pick.roster_id, []).append(pick.delta)
    for team in metrics.teams:
        deltas = picks_by_roster.get(team.roster_id, [])
        if not deltas:
            assert team.best_value_pick is None and team.biggest_reach_pick is None
        else:
            assert team.best_value_pick.delta == max(deltas)
            assert team.biggest_reach_pick.delta == min(deltas)


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", FIXTURES)
def test_deterministic_on_fixtures(name: str) -> None:
    league = build_league_model(_bundle(name))
    slots = _synthetic_slots()
    before = league.model_dump()

    first = compute_consensus_metrics(league, slots)
    second = compute_consensus_metrics(copy.deepcopy(league), dict(slots))

    assert first.model_dump() == second.model_dump()
    assert first.model_dump_json() == second.model_dump_json()
    assert league.model_dump() == before  # purity


# --------------------------------------------------------------------------- #
# Import fence — stats/consensus.py never reaches commishdesk.consensus etc.
# --------------------------------------------------------------------------- #


def _import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module)
    return roots


def test_stats_consensus_stays_inside_the_ingest_fence() -> None:
    roots = _import_roots(STATS_CONSENSUS)
    for dotted in roots:
        parts = dotted.split(".")
        if parts[0] == "commishdesk":
            assert len(parts) > 1 and parts[1] not in _FORBIDDEN_STATS_IMPORTS, dotted
        assert parts[0] not in _OFFLINE_BANNED, dotted
