"""Story 1.5: the committed anonymized fixtures and ``tools/anonymize.py``.

One test per I/O & Edge-Case Matrix row, plus a cross-fixture anonymity scan and
an offline guard. ``tools/`` is not importable (no ``__init__.py``, not on the
path), so the module is loaded by file location the way a contributor's CI would.

Story 5.7 extends the fixture set's contract: every weekly fixture now carries a
``next_matchups`` key (week ``n+1``'s pairings, ``{roster_id, matchup_id}`` only,
``[]`` at or past the playoff cutoff) -- see the story's own fixtures rows below.
"""

from __future__ import annotations

import importlib.util
import json
import re
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import REPO_ROOT

TOOL_PATH = REPO_ROOT / "tools" / "anonymize.py"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
FIXTURES = sorted(FIXTURE_DIR.glob("*.json"))

# One budget, stated once, used by the test and quoted in the fixtures README.
# Raised from 400 when college / age / rookie_year on the player allowlist pushed
# the two week-10 fixtures to ~413 KB.
SIZE_BUDGET_KB = 450


def _load_tool() -> Any:
    spec = importlib.util.spec_from_file_location("anonymize", TOOL_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


anonymize = _load_tool()

# The synthetic timestamp grid every committed fixture's created / status_updated
# value must sit on (see anonymize._remap_timestamps).
SYNTH_BASE = anonymize.SYNTH_EPOCH_BASE_MS
SYNTH_STEP = anonymize.SYNTH_EPOCH_STEP_MS
TIMESTAMP_KEYS = {"created", "status_updated"}


def _walk_keyed(obj: Any, key: Any = None) -> Iterator[tuple[Any, Any]]:
    """Yield ``(parent_key, scalar)`` for every scalar leaf **and every dict key**
    (``draft_order`` is keyed by id, so keys need scanning too). A key is yielded
    with ``parent_key=None`` so it is never mistaken for a timestamp value."""
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
        college="Nowhere State",
        age=27,
        birth_date="1999-01-01",
        birth_city="Nowhere",
        high_school="Nowhere High",
        metadata={"rookie_year": "2020", "channel_id": "9" * 19},
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
        "next_matchups": [
            {"roster_id": 1, "matchup_id": 7, "points": 999.9, "starters": ["100"]},
            {"roster_id": 2, "matchup_id": 7},
        ],
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

    # per-player weekly points survive verbatim (_MATCHUP_FIELDS)
    raw_row = raw["matchups"]["1"][0]
    out_row = out["matchups"]["1"][0]
    assert out_row["players_points"] == raw_row["players_points"]
    assert out_row["starters_points"] == raw_row["starters_points"]

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


def test_player_record_keeps_public_fact_fields() -> None:
    """college / age / rookie_year survive; other metadata does not."""
    out = anonymize.anonymize_bundle(synthetic_raw(), seed=0)
    rec = out["players"]["100"]
    assert rec["college"] == "Nowhere State"
    assert rec["age"] == 27
    assert rec["rookie_year"] == "2020"  # lifted out of raw player.metadata
    assert "metadata" not in rec
    assert "channel_id" not in rec
    # a record with no metadata / no new fields simply omits them
    assert set(out["players"]["200"]) <= set(anonymize._PLAYER_FIELDS)
    assert "rookie_year" not in out["players"]["200"]


# --------------------------------------------------------------------------- #
# Story 5.7: the next-week schedule key
# --------------------------------------------------------------------------- #


def test_next_matchups_is_projected_to_pairings_only() -> None:
    """A row's score / lineup / player list is dropped, not passed through --
    a fixture must never leak a future outcome."""
    raw = synthetic_raw()
    assert raw["next_matchups"][0]["points"] == 999.9  # planted, must not survive
    out = anonymize.anonymize_bundle(raw, seed=0)
    assert out["next_matchups"] == [
        {"roster_id": 1, "matchup_id": 7},
        {"roster_id": 2, "matchup_id": 7},
    ]
    assert "999.9" not in json.dumps(out["next_matchups"])


def test_absent_next_matchups_stays_absent() -> None:
    """A bundle that predates the key keeps its shape -- the anonymizer emits
    no phantom empty section."""
    raw = synthetic_raw()
    del raw["next_matchups"]
    out = anonymize.anonymize_bundle(raw, seed=0)
    assert "next_matchups" not in out


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


def test_timestamps_remapped_onto_the_grid_order_preserving() -> None:
    raw = synthetic_raw()
    # three settled transactions, timestamps deliberately out of row order with
    # one exact tie (created == status_updated on the middle row).
    raw["transactions"]["1"] = [
        {"type": "waiver", "status": "complete", "transaction_id": "1" * 18,
         "created": 1_700_000_003_000, "status_updated": 1_700_000_009_000},
        {"type": "waiver", "status": "complete", "transaction_id": "2" * 18,
         "created": 1_700_000_001_000, "status_updated": 1_700_000_001_000},
        {"type": "waiver", "status": "complete", "transaction_id": "3" * 18,
         "created": 1_700_000_009_000, "status_updated": 1_700_000_020_000},
    ]
    out = anonymize.anonymize_bundle(raw, seed=0)
    rows = out["transactions"]["1"]

    base = anonymize.SYNTH_EPOCH_BASE_MS
    step = anonymize.SYNTH_EPOCH_STEP_MS
    # distinct real values sorted: 1,3,9,20 (×1e9-ish) -> ranks 0,1,2,3
    assert rows[0]["created"] == base + 1 * step
    assert rows[0]["status_updated"] == base + 2 * step
    assert rows[1]["created"] == rows[1]["status_updated"] == base + 0 * step
    assert rows[2]["created"] == base + 2 * step  # same real value as row0 status
    assert rows[2]["status_updated"] == base + 3 * step
    # ordering within and across rows is preserved
    flat = [r["created"] for r in rows] + [r["status_updated"] for r in rows]
    real = [1_700_000_003_000, 1_700_000_001_000, 1_700_000_009_000,
            1_700_000_009_000, 1_700_000_001_000, 1_700_000_020_000]
    assert [a < b for a, b in zip(real, real[1:], strict=False)] == [
        a < b for a, b in zip(flat, flat[1:], strict=False)
    ]


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
        "week01-openers.json",
        "week02-nailbiter.json",
        "week05-trade.json",
        "week08-median.json",
        "week10-blowout.json",
        "week10-superflex.json",
        "week17-playoffs.json",
    }


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_fixture_carries_a_meta_scenario(path: Path) -> None:
    meta = json.loads(path.read_text(encoding="utf-8"))["meta"]
    assert meta.get("case")
    assert "target_week" in meta


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_fixture_next_matchups_is_a_pairing_or_empty(path: Path) -> None:
    """Story 5.7: every fixture carries the key; every row is projected to
    ``{roster_id, matchup_id}``; each ``matchup_id`` groups exactly two
    distinct roster ids (a real pairing, never a bye row)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("next_matchups")
    assert isinstance(rows, list), f"{path.name} is missing next_matchups"

    rosters = {r["roster_id"] for r in data["rosters"]}
    by_matchup: dict[Any, list[Any]] = {}
    for row in rows:
        assert set(row) == {"roster_id", "matchup_id"}, (path.name, row)
        assert row["roster_id"] in rosters, f"{path.name}: unknown roster {row['roster_id']}"
        by_matchup.setdefault(row["matchup_id"], []).append(row["roster_id"])
    for matchup_id, roster_ids in by_matchup.items():
        assert matchup_id is not None, path.name
        assert len(roster_ids) == 2 and len(set(roster_ids)) == 2, (path.name, matchup_id)


def test_next_matchups_present_only_before_the_playoff_cutoff() -> None:
    """Mid-season fixtures carry week ``n+1``'s pairings; a fixture whose
    ``n + 1`` reaches the playoff period does not (and the pre-week-1 fixture
    has no schedule at all)."""
    mid = json.loads((FIXTURE_DIR / "week08-median.json").read_text("utf-8"))
    assert mid["next_matchups"]

    blowout = json.loads((FIXTURE_DIR / "week10-blowout.json").read_text("utf-8"))
    assert blowout["next_matchups"]

    playoff = json.loads((FIXTURE_DIR / "week17-playoffs.json").read_text("utf-8"))
    assert playoff["next_matchups"] == []


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

    # The de-identified league name reaches two more places.
    assert data["league"]["name"] in pool, f"{path.name}: league.name not from pool"
    draft = data.get("draft")
    if draft is not None:
        assert draft["metadata"]["name"] in pool, (
            f"{path.name}: draft.metadata.name not from pool"
        )

    timestamps: list[int] = []
    for key, leaf in _walk_keyed(data):
        if isinstance(leaf, str):
            assert not HEX32.search(leaf), f"{path.name}: 32-hex hash in {leaf!r}"
            assert not DIGIT_RUN_15.search(leaf), f"{path.name}: 15+-digit id in {leaf!r}"
        elif isinstance(leaf, bool):
            continue
        elif isinstance(leaf, int):
            if key in TIMESTAMP_KEYS:
                timestamps.append(leaf)
            else:
                assert abs(leaf) < 10**14, f"{path.name}: big int {leaf}"

    # Every timestamp sits exactly on the synthetic grid, and the distinct values
    # form a contiguous rank sequence from 0 — no real epoch-ms leaked through.
    for value in timestamps:
        assert value >= SYNTH_BASE and (value - SYNTH_BASE) % SYNTH_STEP == 0, (
            f"{path.name}: timestamp {value} is off the synthetic grid"
        )
    if timestamps:
        ranks = sorted({(v - SYNTH_BASE) // SYNTH_STEP for v in timestamps})
        assert ranks == list(range(len(ranks))), (
            f"{path.name}: timestamp grid is not contiguous from 0: {ranks[:5]}…"
        )


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
    assert data["next_matchups"] == []
    assert data["draft"] is not None
    assert data["draft_picks"], "the draft recap needs the picks"


def test_week01_fixture_has_no_prior_week_history() -> None:
    data = json.loads((FIXTURE_DIR / "week01-openers.json").read_text("utf-8"))
    assert data["meta"]["target_week"] == 1
    assert set(data["matchups"]) == {"1"}
    assert set(data["transactions"]) <= {"1"}
    assert data["winners_bracket"] == [] and data["losers_bracket"] == []


@pytest.mark.parametrize(
    "path", [p for p in FIXTURES if p.name != "rookie-draft.json"], ids=lambda p: p.name
)
def test_matchup_rows_carry_per_player_weekly_points(path: Path) -> None:
    """AC1: every matchup row now carries players_points/starters_points.

    ``rookie-draft.json`` is excluded — it's pre-week-1 and has no matchup rows
    at all (see test_rookie_draft_fixture_is_pre_week_one).
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = [row for rows in data["matchups"].values() for row in rows]
    assert rows, f"{path.name}: fixture has no matchup rows to check"
    for row in rows:
        assert "players_points" in row, f"{path.name}: {row} missing players_points"
        assert "starters_points" in row, f"{path.name}: {row} missing starters_points"


def test_playoff_fixture_has_brackets_and_an_eliminated_roster() -> None:
    data = json.loads((FIXTURE_DIR / "week17-playoffs.json").read_text("utf-8"))
    assert data["meta"]["target_week"] >= 15  # playoff_week_start in the raw league
    assert data["winners_bracket"], "playoff-week fixture needs a non-empty winners_bracket"
    assert data["losers_bracket"], "playoff-week fixture needs a non-empty losers_bracket"

    target_week = data["meta"]["target_week"]
    tw = str(target_week)
    playing = {row["roster_id"] for row in data["matchups"][tw]}
    all_rosters = {r["roster_id"] for r in data["rosters"]}
    missing = all_rosters - playing
    # The exact set the week17-playoffs Case documents (tools/assemble_bundle.py):
    # rosters eliminated in an earlier round, absent from the target week.
    assert missing == {3, 6, 7, 9}

    # Cross-check against the bracket: the missing roster(s) should also be
    # absent from *this round's* bracket matches (not merely absent from the
    # matchup rows) — a season-long bracket row from an earlier/later round
    # naturally still names them, so the check is scoped to the target round.
    playoff_week_start = data["league"]["settings"]["playoff_week_start"]
    round_no = target_week - playoff_week_start + 1
    round_participants: set[int] = set()
    for row in data["winners_bracket"] + data["losers_bracket"]:
        if row.get("r") == round_no:
            for key in ("t1", "t2"):
                if isinstance(row.get(key), int):
                    round_participants.add(row[key])
    assert missing.isdisjoint(round_participants), (
        "the dropped roster(s) should be absent from this round's bracket "
        "participants too, not merely absent from the matchup rows"
    )


def test_median_fixture_has_a_roster_exactly_at_the_league_median() -> None:
    data = json.loads((FIXTURE_DIR / "week08-median.json").read_text("utf-8"))
    tw = str(data["meta"]["target_week"])
    rows = data["matchups"][tw]
    assert rows

    def score(row: dict[str, Any]) -> float:
        return row["custom_points"] if row.get("custom_points") is not None else row["points"]

    scores = sorted(score(r) for r in rows)
    n = len(scores)
    median = (
        scores[n // 2]
        if n % 2
        else (scores[n // 2 - 1] + scores[n // 2]) / 2
    )
    at_median = {r["roster_id"] for r in rows if score(r) == median}
    # The exact roster ids the week08-median Case documents (tools/assemble_bundle.py):
    # rosters 6 and 7 tied at the curated custom_points median.
    assert at_median == {6, 7}
    above = sum(score(r) > median for r in rows)
    below = sum(score(r) < median for r in rows)
    assert above > 0 and below > 0, "scores must split on both sides of the median"


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_fixture_under_size_budget(path: Path) -> None:
    kb = path.stat().st_size / 1024
    assert kb < SIZE_BUDGET_KB, f"{path.name} is {kb:.0f} KB (budget {SIZE_BUDGET_KB})"


def test_cross_fixture_id_tokens_agree() -> None:
    """All eight slices of one league must tokenize a given team the same way."""
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
    # the timestamp remap is idempotent: an already-gridded fixture is unchanged
    src_ts = [v for k, v in _walk_keyed(src) if k in TIMESTAMP_KEYS]
    again_ts = [v for k, v in _walk_keyed(again) if k in TIMESTAMP_KEYS]
    assert again_ts == src_ts and src_ts


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


# --------------------------------------------------------------------------- #
# tools/assemble_bundle.py — the per-endpoint -> bundle pre-step.
# Assembly logic runs on CI against a synthetic mini raw export (built below);
# only the byte-for-byte reproduction of the real committed fixtures needs the
# private raw source and skips where it is absent (CI, a fresh contributor clone).
# --------------------------------------------------------------------------- #

ASSEMBLE_PATH = REPO_ROOT / "tools" / "assemble_bundle.py"
POINT_IN_TIME_PATH = REPO_ROOT / "tools" / "point_in_time_rosters.py"
#: Fixtures whose ``rosters`` totals are rewritten as of the target week (Story 5.13a).
POINT_IN_TIME_CASES = frozenset({"week10-blowout"})
RAW_DIR = REPO_ROOT.parent / "brief" / "phase-0" / "raw"
CASE_NAMES = sorted(p.stem for p in FIXTURES)

requires_raw = pytest.mark.skipif(
    not RAW_DIR.is_dir(),
    reason="private raw Sleeper export not present (expected on CI / fresh clones)",
)


def _load_assemble() -> Any:
    spec = importlib.util.spec_from_file_location("assemble_bundle", ASSEMBLE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_point_in_time() -> Any:
    spec = importlib.util.spec_from_file_location("point_in_time_rosters", POINT_IN_TIME_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mini_raw_files() -> dict[str, Any]:
    """A tiny 2-team, weeks-1..10 per-endpoint export — every section the tool
    reads, with planted values that must not survive (a real-looking id, a trade
    note) and a failed transaction that must be dropped."""
    base = {
        f"10{n:02d}": {
            "first_name": f"Player{n}", "last_name": "Example", "position": "RB",
            "team": "KC", "years_exp": n % 6, "number": str(n), "injury_status": "",
            "fantasy_positions": ["RB"], "status": "Active", "college": "State U",
            "age": 24 + (n % 5),
            "metadata": {"rookie_year": str(2018 + n % 5), "channel_id": "9" * 19},
        }
        for n in range(8)
    }
    league = {
        "league_id": "111111111111111111", "draft_id": "222222222222222222",
        "previous_league_id": None, "name": "My Secret League", "season": "2025",
        "season_type": "regular", "sport": "nfl", "status": "in_season",
        "total_rosters": 2,
        "roster_positions": ["QB", "QB", "RB", "WR", "FLEX", "BN", "BN"],
        "scoring_settings": {"rec": 0.5, "pass_td": 4},
        # 15 (matching the real raw league) keeps every existing mini-raw case
        # (target_week <= 10) out of the playoff period; tests that need the
        # playoff branch lower this on their own tmp copy of league.json.
        "settings": {"divisions": 1, "playoff_week_start": 15},
        "metadata": {"trophy_winner": "trophy1"},
    }
    users = [
        {"user_id": "333333333333333333", "league_id": "111111111111111111",
         "display_name": "realhandle_a", "metadata": {"team_name": "Real Team Name A"}},
        {"user_id": "444444444444444444", "league_id": "111111111111111111",
         "display_name": "realhandle_b", "metadata": {"team_name": "Real Team Name B"}},
    ]
    rosters = [
        {"roster_id": 1, "owner_id": "333333333333333333", "co_owners": None,
         "league_id": "111111111111111111", "players": ["1000", "1001", "1002"],
         "starters": ["1000", "1001"], "reserve": [], "taxi": [], "keepers": None,
         "settings": {"wins": 5}, "metadata": {"record": "WWWWW", "streak": "5W"}},
        {"roster_id": 2, "owner_id": "444444444444444444", "co_owners": None,
         "league_id": "111111111111111111", "players": ["1003", "1004", "1005"],
         "starters": ["1003", "1004"], "reserve": [], "taxi": [], "keepers": None,
         "settings": {"wins": 4}, "metadata": {"record": "WWWWL", "streak": "1L"}},
    ]
    draft = {
        "draft_id": "222222222222222222", "league_id": "111111111111111111",
        "type": "linear", "status": "complete", "season": "2025",
        "season_type": "regular", "sport": "nfl", "settings": {"rounds": 1},
        "slot_to_roster_id": {"1": 1, "2": 2},
        "draft_order": {"333333333333333333": 1, "444444444444444444": 2},
        "creators": ["333333333333333333"],
        "metadata": {"name": "My Secret League", "scoring_type": "dynasty",
                     "description": "Priya's home league"},
        "created": 1_600_000_000_000, "start_time": 1_600_000_100_000,
        "last_picked": 1_600_000_200_000,
    }
    draft_picks = [
        {"draft_id": "222222222222222222", "picked_by": "333333333333333333",
         "roster_id": 1, "round": 1, "draft_slot": 1, "pick_no": 1, "is_keeper": None,
         "player_id": "1006", "metadata": {"first_name": "Sixth", "last_name": "Pick",
                                           "position": "WR", "player_id": "1006"}},
        {"draft_id": "222222222222222222", "picked_by": "444444444444444444",
         "roster_id": 2, "round": 1, "draft_slot": 2, "pick_no": 2, "is_keeper": None,
         "player_id": "1007", "metadata": {"first_name": "Seventh", "last_name": "Pick",
                                           "position": "RB", "player_id": "1007"}},
    ]
    traded_picks = [{"season": "2026", "round": 1, "roster_id": 1, "owner_id": 2,
                     "previous_owner_id": 1}]
    matchups = {
        str(w): [
            {"roster_id": 1, "matchup_id": 1, "points": 100.0 + w,
             "players": ["1000", "1001", "1002"], "starters": ["1000", "1001"]},
            {"roster_id": 2, "matchup_id": 1, "points": 90.0 + w,
             "players": ["1003", "1004", "1005"], "starters": ["1003", "1004"]},
        ]
        for w in range(1, 11)
    }
    transactions = {str(w): [] for w in range(1, 11)}
    transactions["1"] = [
        {"type": "waiver", "status": "complete", "transaction_id": "555555555555555555",
         "created": 1_700_000_000_000, "status_updated": 1_700_000_005_000,
         "creator": "333333333333333333", "adds": {"1006": 1}, "drops": {"1002": 1},
         "roster_ids": [1], "consenter_ids": [1],
         "metadata": {"notes": "per-side deal with a real name"}},
        {"type": "waiver", "status": "failed", "transaction_id": "666666666666666666",
         "created": 1_700_000_010_000, "status_updated": 1_700_000_010_000,
         "creator": "444444444444444444", "adds": {"1007": 2}, "drops": None,
         "roster_ids": [2], "metadata": {}},
    ]
    players = base | {
        "1006": {"first_name": "Sixth", "last_name": "Pick", "position": "WR",
                 "team": "SF", "years_exp": 0, "college": "Bama", "age": 22,
                 "metadata": {"rookie_year": "2025"}},
        "1007": {"first_name": "Seventh", "last_name": "Pick", "position": "RB",
                 "team": "DAL", "years_exp": 9, "college": "Ohio State", "age": 31,
                 "metadata": {"rookie_year": "2016"}},
    }
    return {
        "league.json": league, "users.json": users, "rosters.json": rosters,
        "draft.json": draft, "draft_picks.json": draft_picks,
        "traded_picks.json": traded_picks, "players_filtered.json": players,
        "matchups_by_week.json": matchups, "transactions_by_week.json": transactions,
    }


@pytest.fixture
def mini_raw(tmp_path: Path) -> Path:
    d = tmp_path / "mini-raw"
    d.mkdir()
    for name, content in _mini_raw_files().items():
        (d / name).write_text(json.dumps(content), encoding="utf-8")
    return d


def test_assemble_module_lists_exactly_the_eight_cases() -> None:
    mod = _load_assemble()
    assert sorted(mod.CASES) == CASE_NAMES


def test_assemble_rejects_unknown_case() -> None:
    mod = _load_assemble()
    with pytest.raises(ValueError, match="unknown case"):
        mod.assemble(RAW_DIR, "week99-does-not-exist")


def test_assemble_rookie_draft_is_pre_week_one(mini_raw: Path) -> None:
    mod = _load_assemble()
    bundle = mod.assemble(mini_raw, "rookie-draft")
    anonymize.load_bundle(bundle)  # structurally valid
    assert bundle["meta"]["target_week"] is None
    assert bundle["matchups"] == {}
    assert bundle["transactions"] == {}
    assert bundle["next_matchups"] == []
    assert bundle["winners_bracket"] == [] and bundle["losers_bracket"] == []
    assert bundle["draft_picks"], "the draft recap needs the picks"


def test_assemble_carries_next_week_pairings(mini_raw: Path) -> None:
    """Story 5.7: a case whose ``target_week + 1`` is still regular season
    carries that week's pairings, projected to ``{roster_id, matchup_id}``."""
    mod = _load_assemble()
    bundle = mod.assemble(mini_raw, "week05-trade")
    assert bundle["next_matchups"]
    for row in bundle["next_matchups"]:
        assert set(row) == {"roster_id", "matchup_id"}
    assert {row["roster_id"] for row in bundle["next_matchups"]} == {1, 2}
    assert bundle["next_matchups"] == [
        row for row in bundle["next_matchups"] if row["matchup_id"] == 1
    ]


def test_assemble_truncates_window_and_drops_non_settled_transactions(
    mini_raw: Path,
) -> None:
    mod = _load_assemble()
    raw_wk1 = json.loads(
        (mini_raw / "transactions_by_week.json").read_text("utf-8")
    )["1"]
    assert sum(t["status"] != "complete" for t in raw_wk1) == 1  # a failed one exists

    bundle = mod.assemble(mini_raw, "week05-trade")
    assert sorted(int(w) for w in bundle["matchups"]) == [1, 2, 3, 4, 5]
    assert sorted(int(w) for w in bundle["transactions"]) == [1, 2, 3, 4, 5]
    assert len(bundle["transactions"]["1"]) == 1  # the failed row was dropped
    assert all(
        t["status"] == "complete"
        for rows in bundle["transactions"].values()
        for t in rows
    )
    assert "666666666666666666" not in json.dumps(bundle)  # failed tx id gone


def test_assemble_superflex_mutates_one_slot_only(mini_raw: Path) -> None:
    mod = _load_assemble()
    plain = mod.assemble(mini_raw, "week10-blowout")["league"]["roster_positions"]
    flex = mod.assemble(mini_raw, "week10-superflex")["league"]["roster_positions"]
    assert plain[:2] == ["QB", "QB"]
    assert flex == ["QB", "SUPER_FLEX"] + plain[2:]


def _lower_mini_playoff_week_start(mini_raw: Path, value: int) -> None:
    path = mini_raw / "league.json"
    league = json.loads(path.read_text("utf-8"))
    league["settings"]["playoff_week_start"] = value
    path.write_text(json.dumps(league), encoding="utf-8")


def test_assemble_leaves_brackets_empty_outside_the_playoff_period(
    mini_raw: Path,
) -> None:
    mod = _load_assemble()
    # mini league's playoff_week_start (15) is well past every mini case's
    # target_week, so brackets stay empty even though bracket files are absent.
    bundle = mod.assemble(mini_raw, "week10-blowout")
    assert bundle["winners_bracket"] == [] and bundle["losers_bracket"] == []


def test_assemble_raises_when_bracket_files_missing_for_a_playoff_case(
    mini_raw: Path,
) -> None:
    mod = _load_assemble()
    # Bring target_week 10 into the (lowered) playoff period; mini_raw has no
    # winners_bracket.json / losers_bracket.json, so this must raise cleanly.
    _lower_mini_playoff_week_start(mini_raw, 10)
    with pytest.raises(ValueError, match="winners_bracket|losers_bracket"):
        mod.assemble(mini_raw, "week10-blowout")


def test_assemble_populates_brackets_only_in_the_playoff_period(
    mini_raw: Path,
) -> None:
    mod = _load_assemble()
    _lower_mini_playoff_week_start(mini_raw, 10)
    winners = [{"m": 1, "r": 1, "t1": 1, "t2": 2, "w": 1, "l": 2}]
    (mini_raw / "winners_bracket.json").write_text(json.dumps(winners), encoding="utf-8")
    (mini_raw / "losers_bracket.json").write_text(json.dumps([]), encoding="utf-8")

    playoff_bundle = mod.assemble(mini_raw, "week10-blowout")  # target_week 10 >= 10
    assert playoff_bundle["winners_bracket"] == winners
    assert playoff_bundle["losers_bracket"] == []
    # the schedule is at/after the (lowered) cutoff, so no pairings are carried
    assert playoff_bundle["next_matchups"] == []

    pre_playoff_bundle = mod.assemble(mini_raw, "week02-nailbiter")  # target_week 2 < 10
    assert pre_playoff_bundle["winners_bracket"] == []
    assert pre_playoff_bundle["losers_bracket"] == []
    assert pre_playoff_bundle["next_matchups"]


def _give_mini_week_distinct_pairings(mini_raw: Path, week: int, matchup_id: int) -> None:
    """Rewrite one week of the mini raw export so its rows carry a *different*
    ``matchup_id`` from every other week -- the only way a test can tell which
    week ``assemble`` read the pairings from."""
    path = mini_raw / "matchups_by_week.json"
    data = json.loads(path.read_text("utf-8"))
    data[str(week)] = [{**row, "matchup_id": matchup_id} for row in data[str(week)]]
    path.write_text(json.dumps(data), encoding="utf-8")


def test_assemble_reads_the_pairings_from_the_week_after_the_target(mini_raw: Path) -> None:
    _give_mini_week_distinct_pairings(mini_raw, week=6, matchup_id=77)
    mod = _load_assemble()

    bundle = mod.assemble(mini_raw, "week05-trade")  # target_week 5 -> week 6

    assert {row["matchup_id"] for row in bundle["next_matchups"]} == {77}
    # the target week itself is untouched
    assert {row["matchup_id"] for row in bundle["matchups"]["5"]} == {1}


@pytest.mark.parametrize(
    ("playoff_week_start", "carries_pairings"),
    [
        pytest.param(6, False, id="next_week_is_the_first_playoff_week"),
        pytest.param(7, True, id="next_week_is_the_last_regular_season_week"),
    ],
)
def test_assemble_cutoff_is_next_week_strictly_before_playoff_week_start(
    mini_raw: Path, playoff_week_start: int, carries_pairings: bool
) -> None:
    _lower_mini_playoff_week_start(mini_raw, playoff_week_start)
    mod = _load_assemble()

    bundle = mod.assemble(mini_raw, "week05-trade")  # target_week 5

    assert bool(bundle["next_matchups"]) is carries_pairings


def test_assemble_skips_a_pre_week_one_case_with_no_schedule(mini_raw: Path) -> None:
    """``target_week is None`` has no week ``n + 1`` to project."""
    mod = _load_assemble()
    assert mod.assemble(mini_raw, "rookie-draft")["next_matchups"] == []


def test_assemble_drops_rostered_rows_at_the_target_week(
    mini_raw: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The week17-playoffs mechanism (drop_rosters_at_target), exercised against
    mini_raw's 2-team, weeks-1..10 export via a test-only Case injected into
    mod.CASES — real weeks only reach 10, so the named playoff case (target_week
    17) can't run here directly."""
    mod = _load_assemble()
    test_case = mod.Case(
        "test-roster-drop",
        (1, 2, 3, 4, 5),
        5,
        False,
        "test only",
        drop_rosters_at_target=frozenset({2}),
    )
    monkeypatch.setitem(mod.CASES, test_case.name, test_case)

    bundle = mod.assemble(mini_raw, "test-roster-drop")
    roster_ids = {row["roster_id"] for row in bundle["matchups"]["5"]}
    assert roster_ids == {1}, "roster 2's row should be dropped from week 5"
    # untouched weeks keep both rosters
    assert {row["roster_id"] for row in bundle["matchups"]["4"]} == {1, 2}


def test_assemble_applies_custom_points_override_at_the_target_week(
    mini_raw: Path,
) -> None:
    """The week08-median mechanism (custom_points_override). mini_raw's roster
    ids (1, 2) are both covered by week08-median's override map and its
    target_week (8) is within mini_raw's weeks-1..10 range, so the real named
    case can run directly against it."""
    mod = _load_assemble()
    case = mod.CASES["week08-median"]
    assert case.custom_points_override  # sanity: the case under test has one

    bundle = mod.assemble(mini_raw, "week08-median")
    rows_by_roster = {row["roster_id"]: row for row in bundle["matchups"][str(case.target_week)]}
    assert rows_by_roster[1]["custom_points"] == case.custom_points_override[1]
    assert rows_by_roster[2]["custom_points"] == case.custom_points_override[2]


def test_assemble_then_anonymize_round_trips_and_scrubs(mini_raw: Path) -> None:
    mod = _load_assemble()
    out = anonymize.anonymize_bundle(mod.assemble(mini_raw, "week02-nailbiter"), seed=0)
    anonymize.load_bundle(out)
    blob = json.dumps(out)
    assert "My Secret League" not in blob and "realhandle_a" not in blob
    assert "per-side deal with a real name" not in blob
    assert "333333333333333333" not in blob  # real-looking id tokenised
    ts = [v for k, v in _walk_keyed(out) if k in TIMESTAMP_KEYS]
    assert ts and all((v - SYNTH_BASE) % SYNTH_STEP == 0 and v >= SYNTH_BASE for v in ts)
    rec = next(r for r in out["players"].values() if r.get("rookie_year"))
    assert set(rec) <= set(anonymize._PLAYER_FIELDS)  # college/age/rookie_year ok, junk not
    # Story 5.7: the pairings survive the round trip, still two fields.
    assert out["next_matchups"]
    for row in out["next_matchups"]:
        assert set(row) == {"roster_id", "matchup_id"}


def test_assemble_raises_when_a_needed_week_is_absent(mini_raw: Path) -> None:
    mod = _load_assemble()
    txns = json.loads((mini_raw / "transactions_by_week.json").read_text("utf-8"))
    del txns["4"]
    (mini_raw / "transactions_by_week.json").write_text(json.dumps(txns), encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        mod.assemble(mini_raw, "week05-trade")


def test_assemble_omits_referenced_players_absent_from_the_filtered_table(
    mini_raw: Path, capsys
) -> None:
    mod = _load_assemble()
    rosters = json.loads((mini_raw / "rosters.json").read_text("utf-8"))
    rosters[0]["players"].append("999999")  # not in players_filtered.json
    (mini_raw / "rosters.json").write_text(json.dumps(rosters), encoding="utf-8")
    bundle = mod.assemble(mini_raw, "rookie-draft")
    assert "999999" not in bundle["players"]
    assert "not in" in capsys.readouterr().err  # the drop is reported on stderr


def test_assemble_reports_unreadable_raw_dir_cleanly() -> None:
    mod = _load_assemble()
    with pytest.raises(ValueError, match="cannot read raw file"):
        mod.assemble(REPO_ROOT / "tests" / "does-not-exist", "rookie-draft")


@requires_raw
@pytest.mark.parametrize("case", CASE_NAMES)
def test_committed_fixture_reproduces_byte_for_byte_from_raw(case: str) -> None:
    """The committed fixture is exactly assemble(raw) piped through anonymize
    at seed 0 (plus ``tools/point_in_time_rosters.py`` for the cases that pin
    a point-in-time record) — nothing hand-edited, nothing stale."""
    mod = _load_assemble()
    regenerated = anonymize.anonymize_bundle(mod.assemble(RAW_DIR, case), seed=0)
    if case in POINT_IN_TIME_CASES:
        regenerated = _load_point_in_time().apply(regenerated)
    committed = json.loads((FIXTURE_DIR / f"{case}.json").read_text("utf-8"))
    assert regenerated == committed


@pytest.mark.parametrize("case", sorted(POINT_IN_TIME_CASES))
def test_committed_point_in_time_fixture_is_already_point_in_time(case: str) -> None:
    """Runs without the private raw export: a hand edit or re-anonymize that skips
    the tool leaves season-final totals, and the fold would change them."""
    committed = json.loads((FIXTURE_DIR / f"{case}.json").read_text("utf-8"))
    assert _load_point_in_time().apply(json.loads(json.dumps(committed))) == committed


def test_point_in_time_rosters_fold_wins_points_and_streak_from_the_matchups() -> None:
    """A tie counts as a tie, points split into Sleeper's integer + hundredths,
    the record string is the folded results, and re-running changes nothing."""
    mod = _load_point_in_time()

    def row(roster_id: int, points: float) -> dict[str, Any]:
        return {"roster_id": roster_id, "matchup_id": 1, "points": points}

    bundle: dict[str, Any] = {
        "meta": {"target_week": 2},
        "matchups": {
            "1": [row(1, 100.10), row(2, 90.05)],
            "2": [row(1, 80.0), row(2, 80.0)],
            "3": [row(1, 500.0), row(2, 1.0)],  # after the target week: ignored
        },
        "rosters": [
            {"roster_id": 1, "settings": {"wins": 9}, "metadata": {}},
            {"roster_id": 2, "settings": {"wins": 9}, "metadata": {}},
        ],
    }
    once = mod.apply(bundle)
    first, second = once["rosters"]
    assert (first["settings"]["wins"], first["settings"]["ties"]) == (1, 1)
    assert (second["settings"]["losses"], second["settings"]["ties"]) == (1, 1)
    assert (first["settings"]["fpts"], first["settings"]["fpts_decimal"]) == (180, 10)
    assert (first["settings"]["fpts_against"], first["settings"]["fpts_against_decimal"]) == (170, 5)
    assert first["metadata"] == {"record": "WT", "streak": "1T"}
    assert mod.apply(json.loads(json.dumps(once))) == once
