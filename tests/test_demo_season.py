"""Story 8.6 -- the demo season as engine package data.

The season is fiction (``commishdesk/demo_season/``): an offline loader returns the raw
bundles the Sleeper adapter produces, so ``compute_weekly_facts`` and ``produce_weekly_issue``
take it unchanged. One test group per row of the story's I/O & Edge-Case Matrix, plus the
identity rules every committed fixture obeys.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from commishdesk import demo_season
from commishdesk.errors import CommishDeskError
from commishdesk.ingest.byes import _KNOWN_NFL_TEAMS
from commishdesk.sections import SECTION_IDS, id_for_heading, section_ids_for
from commishdesk.store import FileStore
from commishdesk.weekly import (
    WeeklyFactsBuild,
    compute_weekly_facts,
    produce_weekly_issue,
    week_not_final_reason,
)
from tests.conftest import REPO_ROOT

DATA_DIR = REPO_ROOT / "commishdesk" / "demo_season"
TOOLS = REPO_ROOT / "tools"
REPORT = REPO_ROOT / "docs" / "demo-season-name-check.txt"
LOG = logging.getLogger("tests.demo_season")

WEEK_BUNDLE_KEYS = {
    "league",
    "rosters",
    "matchups",
    "transactions",
    "winners_bracket",
    "losers_bracket",
    "next_matchups",
    "players",
    "nfl_state",
}
LEAGUE_BUNDLE_KEYS = {"league", "draft", "draft_picks", "rosters", "users", "players", "previous_league_ids"}
SIZE_BUDGET_KB = 450  # the fixtures' ceiling (tests/test_fixtures.py)
HEX32 = re.compile(r"[0-9a-f]{32}")
DIGIT_RUN_15 = re.compile(r"\d{15,}")


def _load_tool(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"demo_tool_{name}", TOOLS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _find_uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "uv"
    return str(fallback) if fallback.exists() else None


@dataclass(frozen=True)
class _Week:
    built: WeeklyFactsBuild
    issue: Any


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ANTHROPIC_API_KEY", "LLM_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="module")
def weeks(tmp_path_factory: pytest.TempPathFactory) -> dict[int, _Week]:
    """Every demo week through the real Facts stage and the template narrator, fail-closed."""
    results: dict[int, _Week] = {}
    for week in demo_season.WEEKS:
        store = FileStore(tmp_path_factory.mktemp(f"demo-week-{week}"))
        built = compute_weekly_facts(
            store,
            demo_season.DEMO_LEAGUE_ID,
            week,
            demo_season.load_week_bundle(week),
            demo_season.load_league_bundle(),
            seeding=None,
            hold_on_cross_check=True,
            logger=LOG,
        )
        issue, _ranks, _justifications, _reverted = produce_weekly_issue(
            built.doc,
            resolved=demo_season.DEMO_LEAGUE_ID,
            logger=LOG,
            allow_content_hold=False,
            suppressed=frozenset(),
        )
        results[week] = _Week(built=built, issue=issue)
    return results


# --------------------------------------------------------------------------- #
# Row: Load week
# --------------------------------------------------------------------------- #


def test_weeks_constant_is_one_to_twelve() -> None:
    assert demo_season.WEEKS == tuple(range(1, 13))
    assert demo_season.DEMO_LEAGUE_ID == "demo"


@pytest.mark.parametrize("week", [0, 13, -1, 18, True, "3", 3.0, None])
def test_week_outside_the_demo_is_a_clean_fault(week: Any) -> None:
    with pytest.raises(CommishDeskError):
        demo_season.load_week_bundle(week)


@pytest.mark.parametrize("week", demo_season.WEEKS)
def test_week_bundle_has_the_adapter_shape_and_is_cumulative(week: int) -> None:
    bundle = demo_season.load_week_bundle(week)
    assert set(bundle) == WEEK_BUNDLE_KEYS
    assert set(bundle["matchups"]) == {str(n) for n in range(1, week + 1)}
    assert set(bundle["transactions"]) == {str(n) for n in range(1, week + 1)}
    assert all(len(rows) == 12 for rows in bundle["matchups"].values())  # 12 teams, 6 games a week
    assert bundle["nfl_state"] == {"week": week + 1, "season_type": "regular"}
    assert week_not_final_reason(bundle["nfl_state"], week) is None
    assert bundle["winners_bracket"] == [] and bundle["losers_bracket"] == []
    assert len(bundle["rosters"]) == 12
    assert bundle["league"]["settings"]["playoff_week_start"] == 15
    assert bundle["league"]["season"] == "2026"
    # next week's pairings, ids only (never a score), because week + 1 is still regular season
    assert len(bundle["next_matchups"]) == 12
    assert all(set(row) == {"roster_id", "matchup_id"} for row in bundle["next_matchups"])
    # the players blob holds exactly what the bundle references
    referenced: set[str] = set()
    for roster in bundle["rosters"]:
        for key in ("players", "reserve", "taxi"):
            referenced.update(roster[key])
    for rows in bundle["matchups"].values():
        for row in rows:
            referenced.update(row["players"])
    for rows in bundle["transactions"].values():
        for txn in rows:
            referenced.update(txn["adds"])
            referenced.update(txn["drops"])
    assert referenced == set(bundle["players"])


def test_league_bundle_has_the_adapter_shape() -> None:
    bundle = demo_season.load_league_bundle()
    assert set(bundle) == LEAGUE_BUNDLE_KEYS
    assert bundle["league"]["league_id"] == demo_season.DEMO_LEAGUE_ID
    assert len(bundle["users"]) == len(bundle["rosters"]) == 12
    assert len(bundle["draft_picks"]) == 12 * 20


def test_each_call_returns_a_fresh_object() -> None:
    first = demo_season.load_week_bundle(4)
    first["matchups"]["1"].clear()
    first["league"]["name"] = "mutated"
    again = demo_season.load_week_bundle(4)
    assert again["matchups"]["1"] and again["league"]["name"] != "mutated"
    assert demo_season.load_league_bundle()["league"]["name"] != "mutated"


def test_loader_is_a_top_level_module_inside_the_fences() -> None:
    tree = ast.parse((DATA_DIR / "__init__.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module if node.module.startswith("commishdesk") else node.module.split(".")[0])
    assert not {name for name in imported if name.startswith("commishdesk")} - {"commishdesk.errors"}
    assert not imported & {"httpx", "requests", "urllib", "socket"}
    docstring = ast.get_docstring(tree) or ""
    assert "Fictional, never a League" in docstring


# --------------------------------------------------------------------------- #
# Row: Week-12 facts
# --------------------------------------------------------------------------- #


def test_every_week_passes_the_standings_cross_check(weeks: dict[int, _Week]) -> None:
    # hold_on_cross_check=True raised in the fixture if any week failed; assert the flag too.
    assert all(w.built.cross_check_passed for w in weeks.values())


@pytest.mark.parametrize("week", demo_season.WEEKS)
def test_every_week_carries_all_its_sections(weeks: dict[int, _Week], week: int) -> None:
    doc = weeks[week].built.doc
    expected = section_ids_for(doc.period.has_prior_week)
    assert doc.period.has_prior_week is (week > 1)
    assert expected == (SECTION_IDS if week > 1 else expected[:4])
    ids = [id_for_heading(section.heading) for section in weeks[week].issue.sections]
    assert ids == list(expected)
    assert len(ids) == (7 if week > 1 else 4)


def test_week_twelve_has_the_scenario_the_demo_needs(weeks: dict[int, _Week]) -> None:
    facts = weeks[12].built.facts_json
    assert facts["period"]["week"] == 12 and facts["period"]["type"] == "regular"

    teams = {team["roster_id"]: team for team in facts["teams"]}
    assert len(teams) == 12
    luck = {rid: team["season"]["luck"] for rid, team in teams.items()}
    best = max(luck.values())
    luckiest = [rid for rid, value in luck.items() if value == best]
    assert len(luckiest) == 1, f"luckiest team is not unique: {luck}"
    leader = facts["standings"]["overall"][0]
    assert luckiest == [leader]
    record = teams[leader]["season"]["record"]
    assert (record["w"], record["l"], record["t"]) == (9, 3, 0)
    assert all(team["season"]["record"]["w"] < 9 for rid, team in teams.items() if rid != leader)

    summary = facts["period"]["summary"]
    blowout = summary["biggest_blowout"]
    assert blowout is not None and summary["blowout_count"] >= 1
    scores = {rid: team["this_week"]["points"] for rid, team in teams.items()}
    winner, loser = sorted(blowout["roster_ids"], key=lambda rid: -scores[rid])
    assert scores[loser] <= 0.65 * scores[winner]
    assert summary["closest"] is not None and summary["closest"]["margin"] <= 1.0

    trades = facts["transactions"]["recent_trades"]
    assert any(trade["week"] == 12 for trade in trades)
    assert facts["transactions"]["this_week"], "week 12 has waiver or free-agent moves too"
    moves = [move for w in demo_season.WEEKS for move in weeks[w].built.facts_json["transactions"]["this_week"]]
    types = {move["type"] for move in moves}
    assert {"waiver", "free_agent"} <= types


def test_no_game_in_the_season_is_a_tie() -> None:
    for week in demo_season.WEEKS:
        rows = demo_season.load_week_bundle(week)["matchups"][str(week)]
        by_matchup: dict[int, list[float]] = {}
        for row in rows:
            by_matchup.setdefault(row["matchup_id"], []).append(row["points"])
        assert len(by_matchup) == 6
        for points in by_matchup.values():
            assert len(points) == 2 and points[0] != points[1]


def test_week_one_is_a_cold_start_and_the_rest_are_warm(weeks: dict[int, _Week]) -> None:
    assert weeks[1].built.doc.period.has_prior_week is False
    assert all(weeks[w].built.doc.period.has_prior_week for w in range(2, 13))


# --------------------------------------------------------------------------- #
# Row: Issue render (the full safety stack, named-person layer included)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("week", demo_season.WEEKS)
def test_the_template_issue_passes_the_safety_checks(weeks: dict[int, _Week], week: int) -> None:
    from commishdesk.narrate.safety import check_narration
    from commishdesk.narrate.weekly_template import weekly_issue_to_text

    built, issue = weeks[week].built, weeks[week].issue
    report = check_narration(weekly_issue_to_text(issue), built.doc.narration)
    bad = [f"{f.category}: {f.matched!r}" for f in report.findings]
    assert not bad, f"week {week} safety findings: {bad}"
    assert not report.held, f"week {week} held"


# --------------------------------------------------------------------------- #
# Offline
# --------------------------------------------------------------------------- #


def test_loading_and_facts_need_no_network(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:  # pragma: no cover - must never run
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    built = compute_weekly_facts(
        FileStore(tmp_path),
        demo_season.DEMO_LEAGUE_ID,
        12,
        demo_season.load_week_bundle(12),
        demo_season.load_league_bundle(),
        seeding=None,
        hold_on_cross_check=True,
        logger=LOG,
    )
    assert built.cross_check_passed


# --------------------------------------------------------------------------- #
# Identity rules: the committed data is invented and carries nothing real
# --------------------------------------------------------------------------- #


def _data_files() -> list[Path]:
    return [DATA_DIR / "league.json", *sorted((DATA_DIR / "weeks").glob("*.json"))]


def _walk_keyed(obj: Any, key: Any = None) -> Iterator[tuple[Any, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                yield None, k
            yield from _walk_keyed(v, k)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_keyed(v, key)
    else:
        yield key, obj


def test_there_is_one_league_file_and_twelve_week_files() -> None:
    assert [p.name for p in sorted((DATA_DIR / "weeks").glob("*.json"))] == [f"{n:02d}.json" for n in range(1, 13)]
    assert (DATA_DIR / "league.json").is_file()


def test_names_come_from_the_anonymizer_pool_and_are_distinct() -> None:
    pool = set(_load_tool("anonymize").NAME_POOL)
    bundle = demo_season.load_league_bundle()
    managers = [user["display_name"] for user in bundle["users"]]
    teams = [user["metadata"]["team_name"] for user in bundle["users"]]
    names = [bundle["league"]["name"], *managers, *teams]
    assert set(names) <= pool
    assert len(set(names)) == len(names) == 25
    assert bundle["draft"]["metadata"]["name"] == bundle["league"]["name"]


def test_timestamps_sit_on_the_synthetic_grid() -> None:
    anonymize = _load_tool("anonymize")
    base, step = anonymize.SYNTH_EPOCH_BASE_MS, anonymize.SYNTH_EPOCH_STEP_MS
    stamps: list[int] = []
    for path in _data_files():
        for key, leaf in _walk_keyed(json.loads(path.read_text(encoding="utf-8"))):
            if key in {"created", "status_updated"} and isinstance(leaf, int):
                stamps.append(leaf)
    assert stamps
    assert all(value >= base and (value - base) % step == 0 for value in stamps)
    ranks = sorted({(value - base) // step for value in stamps})
    assert ranks == list(range(len(ranks)))


def test_no_real_id_shapes_urls_or_big_numbers() -> None:
    for path in _data_files():
        text = path.read_text(encoding="utf-8")
        assert "sleepercdn.com" not in text and "http" not in text
        for key, leaf in _walk_keyed(json.loads(text)):
            if isinstance(leaf, str):
                assert not HEX32.search(leaf) and not DIGIT_RUN_15.search(leaf), f"{path.name}: {leaf!r}"
            elif isinstance(leaf, int) and not isinstance(leaf, bool) and key not in {"created", "status_updated"}:
                assert abs(leaf) < 10**14


def test_player_ids_are_invented_and_teams_are_real_nfl_codes() -> None:
    players = demo_season.load_league_bundle()["players"]
    assert len(players) > 240
    for pid, record in players.items():
        assert re.fullmatch(r"D\d{4}", pid), pid  # never a Sleeper-shaped numeric id
        assert record["team"] in _KNOWN_NFL_TEAMS
        assert record["position"] in {"QB", "RB", "WR", "TE"}
    full_names = [f"{r['first_name']} {r['last_name']}" for r in players.values()]
    assert len(set(full_names)) == len(full_names)


def test_user_and_transaction_ids_are_opaque_tokens() -> None:
    bundle = demo_season.load_league_bundle()
    assert all(re.fullmatch(r"id_[a-z0-9]{10}", u["user_id"]) for u in bundle["users"])
    for week in demo_season.WEEKS:
        for txn in demo_season.load_week_bundle(week)["transactions"][str(week)]:
            assert re.fullmatch(r"id_[a-z0-9]{10}", txn["transaction_id"])


@pytest.mark.parametrize("path", _data_files(), ids=lambda p: p.name)
def test_data_file_is_under_the_size_budget(path: Path) -> None:
    assert path.stat().st_size <= SIZE_BUDGET_KB * 1000


def test_committed_data_equals_a_re_run_of_the_generator() -> None:
    files, _summary = _load_tool("build_demo_season").build_all()
    assert set(files) == {p.relative_to(DATA_DIR).as_posix() for p in _data_files()}
    for rel, text in files.items():
        assert (DATA_DIR / rel).read_bytes() == text.encode("utf-8"), f"{rel} differs from the generator's output"


# --------------------------------------------------------------------------- #
# Row: Name check
# --------------------------------------------------------------------------- #


def test_committed_name_report_matches_the_committed_names() -> None:
    checker = _load_tool("check_demo_names")
    names = checker.demo_names(demo_season.load_league_bundle())
    lines = REPORT.read_text(encoding="utf-8").splitlines()
    header, body = lines[: lines.index("")], lines[lines.index("") + 1 :]
    assert "collisions: 0" in header
    assert any(re.fullmatch(r"real list sha256: [0-9a-f]{64}", line) for line in header)
    count = next(int(m.group(1)) for line in header if (m := re.fullmatch(r"real list records: (\d+)", line)))
    assert count > 10_000  # a real Sleeper export, not a stub
    assert f"names checked: {len(names)} " in "\n".join(header)
    assert body == [f"[{kind}] {name}" for kind, name in names]


def _fake_real_list(path: Path, *rows: tuple[str, str]) -> Path:
    data = {
        str(i): {"first_name": first, "last_name": last, "full_name": f"{first} {last}"}
        for i, (first, last) in enumerate(rows)
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_checker_writes_then_confirms_a_report_and_pins_the_list(tmp_path: Path) -> None:
    checker = _load_tool("check_demo_names")
    real = _fake_real_list(tmp_path / "real.json", ("Pat", "Mahomes"), ("Josh", "Allen"))
    report = tmp_path / "report.txt"
    argv = [str(real), "--report", str(report)]
    assert checker.main(argv) == 0
    assert checker.main([*argv, "--check"]) == 0
    text = report.read_text(encoding="utf-8")
    assert "collisions: 0" in text and "real list records: 2" in text
    # a different list is a different hash, so the committed report would be stale
    other = _fake_real_list(tmp_path / "other.json", ("Pat", "Mahomes"), ("Josh", "Allen"), ("Sam", "Darnold"))
    assert checker.main([str(other), "--report", str(report), "--check"]) == 1


def test_checker_exits_nonzero_naming_a_collision(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    checker = _load_tool("check_demo_names")
    players = demo_season.load_league_bundle()["players"]
    victim = next(iter(sorted(players.values(), key=lambda r: r["last_name"])))
    # case, punctuation and a generational suffix must not hide the match
    real = _fake_real_list(
        tmp_path / "real.json",
        (victim["first_name"].upper(), victim["last_name"].lower() + " Jr."),
        ("Someone", "Else"),
    )
    assert checker.main([str(real), "--report", str(tmp_path / "report.txt")]) == 1
    err = capsys.readouterr().err
    assert "collision" in err and f"{victim['first_name']} {victim['last_name']}" in err
    assert not (tmp_path / "report.txt").exists()


def test_checker_flags_a_manager_name_equal_to_a_real_full_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checker = _load_tool("check_demo_names")
    manager = demo_season.load_league_bundle()["users"][0]["display_name"]
    first, _, last = manager.partition(" ")
    real = _fake_real_list(tmp_path / "real.json", (first, last))
    assert checker.main([str(real), "--report", str(tmp_path / "report.txt")]) == 1
    assert manager in capsys.readouterr().err


def test_checker_flags_a_team_name_and_the_league_name_equal_to_a_real_full_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checker = _load_tool("check_demo_names")
    bundle = demo_season.load_league_bundle()
    for name in (bundle["users"][0]["metadata"]["team_name"], bundle["league"]["name"]):
        first, _, last = name.partition(" ")
        real = _fake_real_list(tmp_path / "real.json", (first, last))
        assert checker.main([str(real), "--report", str(tmp_path / "report.txt")]) == 1
        assert name in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Row: Installed wheel
# --------------------------------------------------------------------------- #


def test_wheel_ships_the_demo_season_and_the_data_files_it_depends_on(tmp_path: Path) -> None:
    uv = _find_uv()
    if uv is None:
        pytest.skip("uv not available")
    build = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert build.returncode == 0, build.stderr
    (wheel,) = tmp_path.glob("*.whl")
    extract = tmp_path / "extract"
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        archive.extractall(extract)

    expected = {"commishdesk/demo_season/__init__.py", "commishdesk/demo_season/league.json"}
    expected |= {f"commishdesk/demo_season/weeks/{n:02d}.json" for n in range(1, 13)}
    # epic-3-retro-item-39: the other packaged data files ship too.
    expected |= {"commishdesk/narrate/safety_lists.toml", "commishdesk/ingest/nfl_byes.toml"}
    assert expected <= names, sorted(expected - names)
    offenders = [n for n in names if n.startswith(("tools/", "tests/")) or "/tools/" in n or "/tests/" in n]
    assert not offenders, offenders

    # The installed copy loads and feeds the Facts stage with no repo on the path.
    program = (
        "import json, logging, sys, tempfile; "
        "from pathlib import Path; "
        "import commishdesk.demo_season as d; "
        "assert Path(d.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve()), d.__file__; "
        "from commishdesk.store import FileStore; "
        "from commishdesk.weekly import compute_weekly_facts; "
        "t = tempfile.mkdtemp(); "
        "b = compute_weekly_facts(FileStore(Path(t)), d.DEMO_LEAGUE_ID, 12, d.load_week_bundle(12), "
        "d.load_league_bundle(), seeding=None, hold_on_cross_check=True, logger=logging.getLogger('w')); "
        "print(json.dumps(b.facts_json['standings']['overall'][:1]))"
    )
    run = subprocess.run(
        [sys.executable, "-c", program, str(extract)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(extract)},
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout.strip().splitlines()[-1])
