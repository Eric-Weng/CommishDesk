"""Story 1.5: the committed anonymized fixtures and ``tools/anonymize.py``.

One test per I/O & Edge-Case Matrix row, plus a cross-fixture anonymity scan and
an offline guard. ``tools/`` is not importable (no ``__init__.py``, not on the
path), so the module is loaded by file location the way a contributor's CI would.
"""

from __future__ import annotations

import importlib.util
import json
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "tools" / "anonymize.py"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
FIXTURES = sorted(FIXTURE_DIR.glob("*.json"))

# One budget, stated once, used by the test and quoted in the fixtures README.
SIZE_BUDGET_KB = 400


def _load_tool() -> Any:
    spec = importlib.util.spec_from_file_location("anonymize", TOOL_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


anonymize = _load_tool()

# The scoring settings and roster shape of the source league ("Back to Business",
# 0.5 PPR + TE-premium, 2-QB). These are league *config*, not anybody's personal
# data — asserting the fixtures preserve them verbatim is the point of the story,
# not a leak. (SUPER_FLEX is the one deliberate mutation in the superflex fixture.)
SOURCE_ROSTER_POSITIONS = [
    "QB", "QB", "RB", "RB", "WR", "WR", "TE",
    "FLEX", "FLEX", "FLEX", "FLEX",
    "BN", "BN", "BN", "BN", "BN", "BN", "BN", "BN", "BN", "BN", "BN", "BN",
]

HEX32 = re.compile(r"[0-9a-f]{32}")
DIGIT_RUN_15 = re.compile(r"\d{15,}")


def _walk_values(obj: Any) -> Iterator[Any]:
    """Yield every scalar *value* leaf."""
    if isinstance(obj, dict):
        for value in obj.values():
            yield from _walk_values(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_values(value)
    else:
        yield obj


def _walk_scalars(obj: Any) -> Iterator[Any]:
    """Yield every scalar — dict *keys* included (``draft_order`` is keyed by id)."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from _walk_scalars(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_scalars(value)
    else:
        yield obj


# --------------------------------------------------------------------------- #
# A tiny synthetic raw bundle — never touches the network or a real league.
# Planted values below are the things that must NOT survive anonymization.
# --------------------------------------------------------------------------- #

REAL_UID_A = "646962967174393856"
REAL_UID_B = "737516466697584640"
REAL_UID_COOWNER = "111222333444555666"  # a co-owner that never appears in `users`
REAL_LEAGUE = "1180087120844189696"

PLANTED_FREE_TEXT = {
    "league_name": "My Real League Name",
    "last_author": "someguy",
    "trade_note": "per side deal with Marcus Whitfield",
    "draft_desc": "Priya's home dynasty league, est 2019",
    "player_nick": "Uncle Rico",
    "league_note": "commissioner is Dana Kowalczyk",
    "division_label": "The Big Boy Division",
}


def synthetic_raw() -> dict[str, Any]:
    fat_player = {f"junk_field_{i}": i for i in range(53)}
    fat_player.update(
        first_name="Jordan",
        last_name="Example",
        position="WR",
        team="KC",
        years_exp=3,
        number="17",
        injury_status="",
        fantasy_positions=["WR"],
        status="Active",
        birth_date="1999-01-01",
        birth_city="Nowhere",
        high_school="Nowhere High",
    )
    return {
        "meta": {"case": "synthetic", "target_week": 1, "exercises": "unit test",
                 "note": "this extra meta key is dropped"},
        "league": {
            "name": PLANTED_FREE_TEXT["league_name"],
            "season": "2025",
            "league_id": REAL_LEAGUE,
            "draft_id": "1180087120844189697",
            "previous_league_id": "1126968544471662592",
            "avatar": "abcdef0123456789abcdef0123456789",
            "last_author_display_name": PLANTED_FREE_TEXT["last_author"],
            "last_author_id": REAL_UID_A,
            "bracket_id": 1304409136686956500,
            "roster_positions": ["QB", "RB", "WR", "FLEX", "BN"],
            "scoring_settings": {"rec": 0.5, "pass_td": 6, "rush_yd": 0.1176470588235294},
            "settings": {"playoff_teams": 6, "num_teams": 2, "divisions": 1},
            "metadata": {
                "division_1": PLANTED_FREE_TEXT["division_label"],
                "copy_from_league_id": "1048428457279062016",
                "some_note": PLANTED_FREE_TEXT["league_note"],
                "trophy_loser": "loser5",
            },
        },
        "users": [
            {
                "user_id": REAL_UID_A,
                "display_name": "commish_dave",
                "league_id": REAL_LEAGUE,
                "avatar": "0123456789abcdef0123456789abcdef",
                "metadata": {
                    "team_name": "Dave's Destroyers",
                    "avatar": "https://sleepercdn.com/uploads/abc.jpg",
                },
            },
            {
                "user_id": REAL_UID_B,
                "display_name": "sleeper_sam",
                "league_id": REAL_LEAGUE,
                "metadata": {},
            },
        ],
        "rosters": [
            {
                "roster_id": 1,
                "owner_id": REAL_UID_A,
                "co_owners": [REAL_UID_B, REAL_UID_COOWNER],
                "league_id": REAL_LEAGUE,
                "players": ["100", "200"],
                "starters": ["100"],
                "settings": {"wins": 1, "losses": 0},
                "metadata": {
                    "record": "W",
                    "streak": "1W",
                    "p_nick_100": PLANTED_FREE_TEXT["player_nick"],
                },
            },
            {
                "roster_id": 2,
                "owner_id": REAL_UID_B,
                "co_owners": None,
                "league_id": REAL_LEAGUE,
                "players": ["300"],
                "starters": ["300"],
                "settings": {"wins": 0, "losses": 1},
                "metadata": {},
            },
        ],
        "matchups": {
            "1": [
                {"roster_id": 1, "matchup_id": 1, "points": 101.5,
                 "players": ["100", "200"], "starters": ["100"],
                 "starters_points": [101.5], "players_points": {"100": 101.5, "200": 0}},
                {"roster_id": 2, "matchup_id": 1, "points": 99.9,
                 "players": ["300"], "starters": ["300"],
                 "starters_points": [99.9], "players_points": {"300": 99.9}},
            ],
            "2": None,  # a bye / not-yet-played week: must not crash
        },
        "transactions": {
            "1": [
                {
                    "type": "trade", "status": "complete", "created": 1757476352272,
                    "transaction_id": "1271351604028637184",
                    "creator": REAL_UID_A,
                    "adds": {"300": 1}, "drops": {"300": 2},
                    "consenter_ids": [1, 2], "roster_ids": [1, 2],
                    "draft_picks": [
                        {"season": "2026", "round": 1, "roster_id": 2,
                         "owner_id": 1, "previous_owner_id": 2, "league_id": REAL_LEAGUE},
                    ],
                    "metadata": {"notes": PLANTED_FREE_TEXT["trade_note"]},
                }
            ]
        },
        "draft": {
            "type": "linear", "status": "complete", "season": "2025",
            "draft_id": "1180087120844189697", "league_id": REAL_LEAGUE,
            "creators": [REAL_UID_A],
            "draft_order": {REAL_UID_A: 1, REAL_UID_B: 2},
            "slot_to_roster_id": {"1": 1, "2": 2},
            "settings": {"rounds": 6},
            "metadata": {"name": PLANTED_FREE_TEXT["league_name"],
                         "description": PLANTED_FREE_TEXT["draft_desc"],
                         "scoring_type": "dynasty_2qb"},
        },
        "draft_picks": [
            {
                "draft_id": "1180087120844189697", "picked_by": REAL_UID_A,
                "roster_id": 1, "round": 1, "draft_slot": 1, "pick_no": 1,
                "player_id": "100",
                "metadata": {"first_name": "Jordan", "last_name": "Example",
                             "position": "WR", "news_updated": "1746812424886"},
            }
        ],
        "traded_picks": [
            {"season": "2026", "round": 1, "roster_id": 1,
             "owner_id": 2, "previous_owner_id": 1},
        ],
        "winners_bracket": [{"m": 1, "r": 1, "w": 1, "l": 2, "t1": 1, "t2": 2}],
        "losers_bracket": [],
        "players": {"100": fat_player, "200": {"first_name": "Alex", "position": "RB"},
                    "300": dict(fat_player, first_name="Sam")},
    }


# --------------------------------------------------------------------------- #
# I/O & Edge-Case Matrix
# --------------------------------------------------------------------------- #


def test_anonymize_valid_bundle_same_shape_names_ids_scoring() -> None:
    raw = synthetic_raw()
    out = anonymize.anonymize_bundle(raw, seed=0)

    assert set(out) == set(raw)  # same top-level shape

    # scoring settings + roster shape preserved verbatim
    assert out["league"]["scoring_settings"] == raw["league"]["scoring_settings"]
    assert out["league"]["roster_positions"] == raw["league"]["roster_positions"]
    assert out["league"]["settings"] == raw["league"]["settings"]

    # names come from the pool; real ones are gone
    display = {u["display_name"] for u in out["users"]}
    teams = {u["metadata"]["team_name"] for u in out["users"]}
    assert display <= set(anonymize.NAME_POOL)
    assert teams <= set(anonymize.NAME_POOL)
    assert "commish_dave" not in display and "sleeper_sam" not in display

    # ids tokenised (any length) and consistent between sections
    assert out["league"]["league_id"] != REAL_LEAGUE
    assert out["league"]["league_id"].startswith("id_")
    assert out["rosters"][0]["owner_id"] == out["users"][0]["user_id"]
    assert out["rosters"][0]["co_owners"][0] == out["users"][1]["user_id"]

    blob = json.dumps(out)
    assert "sleepercdn.com" not in blob
    assert "abcdef0123456789abcdef0123456789" not in blob
    assert REAL_UID_A not in blob and REAL_UID_B not in blob
    assert REAL_UID_COOWNER not in blob
    assert "1304409136686956500" not in blob  # bracket_id int, dropped
    assert "copy_from_league_id" not in out["league"]["metadata"]


def test_meta_section_allowlisted_and_swept() -> None:
    out = anonymize.anonymize_bundle(synthetic_raw(), seed=0)
    assert set(out["meta"]) <= {"case", "target_week", "exercises"}
    assert out["meta"]["case"] == "synthetic"
    assert "note" not in out["meta"]


def test_determinism_same_seed_byte_identical() -> None:
    raw = synthetic_raw()
    a = json.dumps(anonymize.anonymize_bundle(raw, seed=0), sort_keys=True)
    b = json.dumps(anonymize.anonymize_bundle(raw, seed=0), sort_keys=True)
    assert a == b


def test_distinct_seeds_change_assignments_not_structure() -> None:
    raw = synthetic_raw()
    a = anonymize.anonymize_bundle(raw, seed=0)
    b = anonymize.anonymize_bundle(raw, seed=1)
    assert a != b
    assert a["league"]["league_id"] != b["league"]["league_id"]
    assert [u["display_name"] for u in a["users"]] != [u["display_name"] for u in b["users"]]
    assert {k: type(v).__name__ for k, v in a.items()} == {
        k: type(v).__name__ for k, v in b.items()
    }
    assert a["league"]["scoring_settings"] == b["league"]["scoring_settings"]


def test_reject_unknown_top_level_key() -> None:
    raw = synthetic_raw()
    raw["secrets"] = {"token": "xyz"}
    with pytest.raises(Exception) as exc:
        anonymize.load_bundle(raw)
    assert "secrets" in str(exc.value)


def test_reject_missing_required_section() -> None:
    raw = synthetic_raw()
    del raw["league"]
    with pytest.raises(Exception) as exc:
        anonymize.load_bundle(raw)
    assert "league" in str(exc.value)


def test_reject_wrong_typed_section() -> None:
    raw = synthetic_raw()
    raw["rosters"] = {"not": "a list"}
    with pytest.raises(Exception) as exc:
        anonymize.load_bundle(raw)
    assert "rosters" in str(exc.value)


def test_reject_happens_before_anonymization_via_cli(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"meta": {}, "users": [], "rosters": []}), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(TOOL_PATH), str(bad)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "league" in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_validation_error_is_terse_no_input_echo(tmp_path: Path) -> None:
    """Finding 10: the raw bundle's contents must not land in CI logs."""
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps({**synthetic_raw(), "surprise_secret_key": "hunter2-token-value"}),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(TOOL_PATH), str(bad)], capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "surprise_secret_key" in result.stderr  # names the offending key
    assert "hunter2-token-value" not in result.stderr  # but not the value
    assert "input_value" not in result.stderr


def test_player_record_trimmed_to_allowlist() -> None:
    raw = synthetic_raw()
    assert len(raw["players"]["100"]) > 40  # the fat record
    out = anonymize.anonymize_bundle(raw, seed=0)
    allow = set(anonymize._PLAYER_FIELDS)
    for pid, rec in out["players"].items():
        assert set(rec) <= allow, (pid, set(rec) - allow)
    assert out["players"]["100"]["first_name"] == "Jordan"  # NFL name kept
    assert "birth_date" not in out["players"]["100"]
    assert "junk_field_0" not in out["players"]["100"]


# --------------------------------------------------------------------------- #
# Free-text / metadata scrub (findings 1-3)
# --------------------------------------------------------------------------- #


def test_free_text_metadata_never_survives() -> None:
    out = anonymize.anonymize_bundle(synthetic_raw(), seed=0)
    blob = json.dumps(out)
    for label, value in PLANTED_FREE_TEXT.items():
        assert value not in blob, f"planted {label!r} survived anonymization"

    assert out["transactions"]["1"][0]["metadata"] == {}
    assert "description" not in out["draft"]["metadata"]
    assert set(out["rosters"][0]["metadata"]) <= {"record", "streak"}
    assert all(not k.startswith("p_nick") for k in out["rosters"][0]["metadata"])
    assert "some_note" not in out["league"]["metadata"]
    assert "division_1" not in out["league"]["metadata"]


def test_league_name_is_de_identified() -> None:
    out = anonymize.anonymize_bundle(synthetic_raw(), seed=0)
    assert PLANTED_FREE_TEXT["league_name"] not in json.dumps(out)
    assert out["league"]["name"] in set(anonymize.NAME_POOL)
    assert out["draft"]["metadata"]["name"] in set(anonymize.NAME_POOL)
    assert out["league"]["name"] == out["draft"]["metadata"]["name"]


def test_last_author_display_name_is_dropped() -> None:
    out = anonymize.anonymize_bundle(synthetic_raw(), seed=0)
    assert PLANTED_FREE_TEXT["last_author"] not in json.dumps(out)
    assert "last_author_display_name" not in out["league"]
    assert "last_author_id" not in out["league"]


def test_embedded_long_digit_run_in_a_string_is_tokenized() -> None:
    raw = synthetic_raw()
    raw["draft"]["settings"]["note"] = f"see league {REAL_LEAGUE} for details"
    out = anonymize.anonymize_bundle(raw, seed=0)
    assert REAL_LEAGUE not in json.dumps(out)


# --------------------------------------------------------------------------- #
# Malformed-input hardening (findings 6-9)
# --------------------------------------------------------------------------- #


def test_null_week_rows_do_not_crash() -> None:
    out = anonymize.anonymize_bundle(synthetic_raw(), seed=0)
    assert out["matchups"]["2"] == []


def test_co_owners_as_bare_string_is_not_iterated_per_character() -> None:
    raw = synthetic_raw()
    raw["rosters"][1]["co_owners"] = REAL_UID_A
    out = anonymize.anonymize_bundle(raw, seed=0)
    assert out["rosters"][1]["co_owners"] == [out["users"][0]["user_id"]]


def test_non_dict_list_item_is_a_clean_error_not_a_traceback(tmp_path: Path) -> None:
    raw = synthetic_raw()
    raw["rosters"].append("not a roster object")
    path = tmp_path / "raw.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(TOOL_PATH), str(path)], capture_output=True, text=True
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "Traceback" not in result.stderr


def test_missing_scoring_settings_is_a_clean_error() -> None:
    raw = synthetic_raw()
    del raw["league"]["scoring_settings"]
    with pytest.raises(ValueError, match="scoring_settings"):
        anonymize.anonymize_bundle(raw, seed=0)


def test_pool_too_small_is_caught_by_main(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(anonymize, "NAME_POOL", ("Only", "Three", "Names"))
    path = tmp_path / "raw.json"
    path.write_text(json.dumps(synthetic_raw()), encoding="utf-8")
    rc = anonymize.main([str(path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "cannot anonymize" in err
    assert "Traceback" not in err


# --------------------------------------------------------------------------- #
# Persona assignment (finding 4)
# --------------------------------------------------------------------------- #


def test_unknown_co_owner_gets_its_own_persona_not_the_league_name() -> None:
    out = anonymize.anonymize_bundle(synthetic_raw(), seed=0)
    co_token = out["rosters"][0]["co_owners"][1]  # REAL_UID_COOWNER, absent from users
    pool = set(anonymize.NAME_POOL)
    # it is tokenized...
    assert co_token.startswith("id_")
    # ...and every token that resolves to a persona resolves to a distinct pool name
    names = [u["display_name"] for u in out["users"]] + [out["league"]["name"]]
    assert len(names) == len(set(names)), "personas collapsed onto one name"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_each_fixture_validates(path: Path) -> None:
    anonymize.load_bundle(path.read_text(encoding="utf-8"))


def test_fixture_set_is_complete() -> None:
    names = {p.name for p in FIXTURES}
    assert names == {
        "rookie-draft.json",
        "week02-nailbiter.json",
        "week05-trade.json",
        "week10-blowout.json",
        "week10-superflex.json",
    }


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_fixture_carries_a_meta_scenario(path: Path) -> None:
    meta = json.loads(path.read_text(encoding="utf-8"))["meta"]
    assert meta.get("case")
    assert "target_week" in meta


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_fixture_contains_no_real_identity(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)

    assert "sleepercdn.com" not in text

    pool = set(anonymize.NAME_POOL)
    for user in data["users"]:
        assert user["display_name"] in pool
        team = user.get("metadata", {}).get("team_name")
        if team is not None:
            assert team in pool

    for leaf in _walk_scalars(data):
        if isinstance(leaf, str):
            assert not HEX32.search(leaf), f"{path.name}: 32-hex hash in {leaf!r}"
            assert not DIGIT_RUN_15.search(leaf), f"{path.name}: 15+-digit id in {leaf!r}"
        elif isinstance(leaf, int) and not isinstance(leaf, bool):
            assert abs(leaf) < 10**14, f"{path.name}: big int {leaf}"


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_fixture_scoring_and_roster_shape_preserved(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    blowout = json.loads((FIXTURE_DIR / "week10-blowout.json").read_text("utf-8"))

    rp = data["league"]["roster_positions"]
    if path.name == "week10-superflex.json":
        assert rp == ["QB", "SUPER_FLEX"] + SOURCE_ROSTER_POSITIONS[2:]
    else:
        assert rp == SOURCE_ROSTER_POSITIONS
        assert "SUPER_FLEX" not in rp

    # scoring is identical across every fixture (one league, one deliberate
    # roster-slot mutation that does not touch scoring)
    assert data["league"]["scoring_settings"] == blowout["league"]["scoring_settings"]
    assert data["league"]["scoring_settings"]["rec"] == 0.5  # 0.5 PPR
    assert data["league"]["scoring_settings"]["bonus_rec_yd_100"] == 2  # TE premium


def test_rookie_draft_fixture_is_pre_week_one() -> None:
    data = json.loads((FIXTURE_DIR / "rookie-draft.json").read_text("utf-8"))
    assert data["meta"]["target_week"] is None
    assert data["matchups"] == {}
    assert data["transactions"] == {}
    assert data["draft"] is not None
    assert data["draft_picks"], "the draft recap needs the picks"


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_fixture_under_size_budget(path: Path) -> None:
    kb = path.stat().st_size / 1024
    assert kb < SIZE_BUDGET_KB, f"{path.name} is {kb:.0f} KB (budget {SIZE_BUDGET_KB})"


def test_cross_fixture_id_tokens_agree() -> None:
    """All five slices of one league must tokenize a given team the same way."""
    owners_by_file = {}
    for path in FIXTURES:
        data = json.loads(path.read_text(encoding="utf-8"))
        owners_by_file[path.name] = {
            r["roster_id"]: r["owner_id"] for r in data["rosters"]
        }
    reference = owners_by_file["week10-blowout.json"]
    for name, owners in owners_by_file.items():
        assert owners == reference, f"{name} disagrees on owner tokens"


def test_already_anonymized_fixture_round_trips() -> None:
    """The Verification command: re-running the tool on a committed fixture
    still validates and stays anonymous."""
    src = json.loads((FIXTURE_DIR / "week10-blowout.json").read_text("utf-8"))
    again = anonymize.anonymize_bundle(src, seed=0)
    anonymize.load_bundle(again)
    for leaf in _walk_scalars(again):
        if isinstance(leaf, str):
            assert not DIGIT_RUN_15.search(leaf)


# --------------------------------------------------------------------------- #
# CLI success path
# --------------------------------------------------------------------------- #


def test_cli_success_path_path_and_stdin(tmp_path: Path) -> None:
    src = FIXTURE_DIR / "week10-blowout.json"

    by_path = subprocess.run(
        [sys.executable, str(TOOL_PATH), str(src), "--seed", "0"],
        capture_output=True, text=True,
    )
    assert by_path.returncode == 0
    parsed = json.loads(by_path.stdout)  # valid JSON on stdout
    anonymize.load_bundle(parsed)

    by_stdin = subprocess.run(
        [sys.executable, str(TOOL_PATH), "-", "--seed", "0"],
        input=src.read_text(encoding="utf-8"), capture_output=True, text=True,
    )
    assert by_stdin.returncode == 0
    assert by_stdin.stdout == by_path.stdout  # deterministic across input modes

    other_seed = subprocess.run(
        [sys.executable, str(TOOL_PATH), str(src), "--seed", "1"],
        capture_output=True, text=True,
    )
    assert other_seed.returncode == 0
    assert other_seed.stdout != by_path.stdout


# --------------------------------------------------------------------------- #
# Offline guarantee
# --------------------------------------------------------------------------- #


def test_fixtures_and_tool_need_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any):  # pragma: no cover - must never run
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)

    module = _load_tool()
    for path in FIXTURES:
        module.load_bundle(path.read_text(encoding="utf-8"))
    module.anonymize_bundle(synthetic_raw(), seed=3)
