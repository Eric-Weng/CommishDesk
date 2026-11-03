"""Story 9.4: Monte Carlo playoff odds (the reference stat module)."""

from __future__ import annotations

import copy
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from commishdesk import demo_season
from commishdesk.facts.schema import WeeklyFacts, WeeklyPlayoffOdds
from commishdesk.statmods._loader import default_statmod_dir, load_stat_modules, run_stat_modules
from commishdesk.statmods.playoff_odds import SIMULATIONS, STAT_MODULE, PlayoffOddsModule, _seed
from commishdesk.store import FileStore
from commishdesk.weekly import compute_weekly_facts
from tests.conftest import REPO_ROOT

FACTS_DIR = REPO_ROOT / "tests" / "fixtures" / "facts"
GOLDEN = REPO_ROOT / "tests" / "fixtures" / "playoff_odds" / "worked-example.json"
LOG = logging.getLogger("test.playoff_odds")
FIXTURES = ["expected-weekly-facts-week01.json", "expected-weekly-facts-week10.json"]


def _facts(name: str = "expected-weekly-facts-week10.json") -> dict[str, Any]:
    return json.loads((FACTS_DIR / name).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _block(facts: dict[str, Any]) -> dict[str, Any]:
    return dict(STAT_MODULE.compute(facts)).get("playoff_odds", {})  # type: ignore[return-value]


def _card_flag(facts: dict[str, Any], roster: str, flag: str) -> None:
    for card in facts["matchups"]["next_week"]:
        for side in ("a", "b"):
            if card[f"{side}_roster_id"] == roster:
                card[f"{side}_{flag}"] = True
                return
    raise AssertionError(f"no card for roster {roster}")


def test_module_identity() -> None:
    assert STAT_MODULE.module_id == "playoff_odds" and STAT_MODULE.module_version == "1"
    assert isinstance(STAT_MODULE, PlayoffOddsModule)
    assert [m.module.module_id for m in load_stat_modules([default_statmod_dir()], LOG)] == ["playoff_odds"]


@pytest.mark.parametrize("name", FIXTURES)
def test_mid_season_rows_sum_and_validate(name: str) -> None:
    facts = _facts(name)
    block = _block(facts)
    fmt = facts["league"]["format"]
    assert block["simulations"] == SIMULATIONS == 10_000
    assert [t["roster_id"] for t in block["teams"]] == sorted((t["roster_id"] for t in facts["teams"]), key=int)
    assert abs(sum(t["make_playoffs"] for t in block["teams"]) - fmt["playoff"]["bracket_teams"]) < 1e-3
    assert abs(sum(t["bye"] for t in block["teams"]) - fmt["playoff"]["byes"]) < 1e-3
    assert abs(sum(t["division"] for t in block["teams"]) - len(fmt["divisions"])) < 1e-3
    for t in block["teams"]:
        assert 0.0 <= t["bye"] <= t["make_playoffs"] <= 1.0
    parsed = WeeklyPlayoffOdds.model_validate(block)
    assert len(parsed.teams) == len(facts["teams"])


def test_bye_and_division_only_where_declared() -> None:
    facts = _facts()
    facts["league"]["format"]["playoff"]["byes"] = 0
    facts["league"]["format"]["divisions"] = []
    for t in _block(facts)["teams"]:
        assert set(t) == {"roster_id", "make_playoffs", "status"}


def test_same_inputs_byte_identical_and_input_untouched() -> None:
    facts = _facts()
    before = copy.deepcopy(facts)
    a, b = _block(facts), _block(copy.deepcopy(facts))
    assert json.dumps(a) == json.dumps(b)
    assert facts == before


def test_golden() -> None:
    assert json.loads(GOLDEN.read_text(encoding="utf-8")) == {"playoff_odds": _block(_facts())}


def test_deterministic_across_hash_seeds() -> None:
    code = (
        "import json;from commishdesk.statmods.playoff_odds import STAT_MODULE;"
        f"d=json.load(open(r'{FACTS_DIR / FIXTURES[1]}',encoding='utf-8'));"
        "print(json.dumps(STAT_MODULE.compute(d),sort_keys=True))"
    )
    outs = []
    for seed in ("1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True)
        outs.append(run.stdout)
    assert outs[0] == outs[1]
    assert json.loads(outs[0]) == {"playoff_odds": _block(_facts())}


def test_seed_is_sha256_of_league_week_version() -> None:
    assert _seed("L", 3, "1") == _seed("L", 3, "1")
    assert len({_seed("L", 3, "1"), _seed("L", 4, "1"), _seed("M", 3, "1"), _seed("L", 3, "2")}) == 4


def test_a_seed_change_moves_the_numbers() -> None:
    facts = _facts()
    other = copy.deepcopy(facts)
    other["league"]["id"] = "another-league"
    assert _block(facts) != _block(other)


def test_clinched_and_eliminated_flags_force_one_and_zero() -> None:
    facts = _facts()
    _card_flag(facts, "4", "clinched_playoff")  # 4-6, odds well under 1
    _card_flag(facts, "2", "eliminated")  # 9-1
    _card_flag(facts, "5", "clinched_bye")
    _card_flag(facts, "12", "clinched_division")
    rows = {t["roster_id"]: t for t in _block(facts)["teams"]}
    assert rows["4"]["make_playoffs"] == 1.0 and rows["4"]["status"] == "clinched"
    assert rows["2"]["make_playoffs"] == 0.0 and rows["2"]["status"] == "eliminated"
    assert rows["2"]["bye"] == 0.0 and rows["2"]["division"] == 0.0
    assert rows["5"]["make_playoffs"] == 1.0 and rows["5"]["bye"] == 1.0
    assert rows["12"]["division"] == 1.0 and rows["12"]["status"] == "in_race"
    assert abs(sum(t["make_playoffs"] for t in rows.values()) - 6) < 1e-3


def test_commissioner_picture_decides_entry_and_byes() -> None:
    facts = _facts()
    picture = facts["standings"]["playoff_picture"]
    picture["source"] = "commissioner"
    picture["in_bracket"] = ["7", "11", "10", "12", "8", "4"]
    picture["byes"] = ["7", "11"]
    rows = {t["roster_id"]: t for t in _block(facts)["teams"]}
    for rid, row in rows.items():
        assert row["make_playoffs"] == (1.0 if rid in picture["in_bracket"] else 0.0)
        assert row["bye"] == (1.0 if rid in picture["byes"] else 0.0)


def test_absent_rules() -> None:
    no_format = _facts()
    no_format["league"]["format"]["playoff"] = None
    over = _facts()
    over["standings"]["regular_season_complete"] = True
    unplayed = _facts()
    for t in unplayed["teams"]:
        t["season"]["record"] = {"w": 0, "l": 0, "t": 0}
    incomplete = _facts()
    incomplete["matchups"]["remaining"]["complete"] = False
    modules = load_stat_modules([default_statmod_dir()], LOG)
    for facts in (no_format, over, unplayed, incomplete):
        assert dict(STAT_MODULE.compute(facts)) == {}
        assert run_stat_modules(facts, modules, LOG) == {}


def test_unpaired_roster_plays_fewer_games() -> None:
    facts = _facts()
    for wk in facts["matchups"]["remaining"]["weeks"]:
        wk["games"] = [g for g in wk["games"] if "7" not in (g["a_roster_id"], g["b_roster_id"])]
    block = _block(facts)
    assert len(block["teams"]) == 12
    assert abs(sum(t["make_playoffs"] for t in block["teams"]) - 6) < 1e-3


def test_module_raising_costs_only_itself(caplog: pytest.LogCaptureFixture) -> None:
    facts = _facts()
    del facts["standings"]
    with caplog.at_level(logging.WARNING, logger=LOG.name):
        assert run_stat_modules(facts, load_stat_modules([default_statmod_dir()], LOG), LOG) == {}
    assert "playoff_odds" in caplog.text


def test_schema_model_is_excluded_from_the_dump() -> None:
    facts = _facts()
    facts["playoff_odds"] = _block(facts)
    doc = WeeklyFacts.model_validate(facts)
    assert doc.playoff_odds is not None
    assert "playoff_odds" not in doc.model_dump(mode="json")


def test_demo_season_builds_with_the_block_and_replay_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from commishdesk.weekly import _replayed_facts, write_weekly_facts_snapshot

    monkeypatch.delenv("COMMISHDESK_STATMOD_DIRS", raising=False)
    carried = 0
    for week in demo_season.WEEKS:
        built = compute_weekly_facts(
            FileStore(tmp_path / f"w{week}"),
            demo_season.DEMO_LEAGUE_ID,
            week,
            demo_season.load_week_bundle(week),
            demo_season.load_league_bundle(),
            seeding=None,
            hold_on_cross_check=True,
            logger=LOG,
        )
        block = built.facts_json.get("playoff_odds")
        if block is None:
            continue
        carried += 1
        WeeklyPlayoffOdds.model_validate(block)
        store = FileStore(tmp_path / f"s{week}")
        write_weekly_facts_snapshot(store, demo_season.DEMO_LEAGUE_ID, week, built.facts_json)
        replay = _replayed_facts(store, demo_season.DEMO_LEAGUE_ID, week, True, LOG)
        assert replay is not None
        assert replay.facts_json["playoff_odds"] == block
    assert carried >= 1, "no demo-season week is mid-season with a complete schedule"


def test_echo_module_alone_still_loads() -> None:
    echo_dir = Path(__file__).parent / "eval" / "statmods"
    assert [m.module.module_id for m in load_stat_modules([echo_dir], LOG)] == ["echo"]
