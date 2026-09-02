"""Story 1.4: the Store port and FileStore — one test per I/O & Edge-Case row,
plus the cloud-neutrality source scans."""

from __future__ import annotations

import ast
import inspect
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from commishdesk.errors import CommishDeskError, StoreError
from commishdesk.store import Claim, FileStore, LedgerEntry, Store, Storyline

UTC = timezone.utc
ENGINE_ROOT = Path(__file__).resolve().parent.parent / "commishdesk"

# Substrings that must never appear in a public Store name or in engine imports.
CLOUD_NAME_TOKENS = ("cloud", "cloudflare", "d1", "r2", "s3", "bucket", "worker", "kv")
CLOUD_SDK_DENYLIST = ("boto3", "botocore", "cloudflare", "wrangler", "s3fs", "google.cloud")


def _store(tmp_path: Path) -> FileStore:
    return FileStore(tmp_path)


# --- read config -----------------------------------------------------------


def test_read_config_parses_present_toml(tmp_path: Path) -> None:
    (tmp_path / "leagues").mkdir()
    (tmp_path / "leagues" / "1.toml").write_text(
        'name = "Test League"\nweek_zero = 1\n', encoding="utf-8"
    )
    assert _store(tmp_path).read_config("1") == {"name": "Test League", "week_zero": 1}


def test_read_config_missing_raises_store_error(tmp_path: Path) -> None:
    with pytest.raises(StoreError) as excinfo:
        _store(tmp_path).read_config("1")
    assert isinstance(excinfo.value, CommishDeskError)
    assert excinfo.value.__cause__ is not None


def test_read_config_unparseable_raises_store_error(tmp_path: Path) -> None:
    (tmp_path / "leagues").mkdir()
    (tmp_path / "leagues" / "1.toml").write_text("name = = broken", encoding="utf-8")
    with pytest.raises(StoreError) as excinfo:
        _store(tmp_path).read_config("1")
    assert excinfo.value.__cause__ is not None


# --- ledger --------------------------------------------------------------


def _entry(week: int, recipient: str = "webhook-1") -> LedgerEntry:
    return LedgerEntry(
        league_id="1",
        week=week,
        channel="discord",
        recipient=recipient,
        sent_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


def test_append_then_read_ledger_returns_entry_and_excludes_other_weeks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_ledger_entry(_entry(3))
    store.append_ledger_entry(_entry(4, recipient="webhook-2"))
    week3 = store.read_ledger("1", 3)
    assert week3 == [_entry(3)]
    assert [e.week for e in store.read_ledger("1", 4)] == [4]


def test_read_ledger_no_file_returns_empty(tmp_path: Path) -> None:
    assert _store(tmp_path).read_ledger("1", 1) == []


def test_ledger_entry_status_is_confirmed_only() -> None:
    assert _entry(1).status == "confirmed"
    with pytest.raises(ValueError):
        LedgerEntry(
            league_id="1",
            week=1,
            channel="discord",
            recipient="webhook-1",
            status="pending",
            sent_at=datetime(2026, 9, 1, tzinfo=UTC),
        )


def test_ledger_line_serializes_datetime_as_iso_z(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_ledger_entry(_entry(3))
    line = (tmp_path / "ledger" / "1.jsonl").read_text(encoding="utf-8").strip()
    assert '"sent_at":"2026-09-01T00:00:00Z"' in line
    assert '"status":"confirmed"' in line


def test_read_ledger_malformed_line_raises_store_error(tmp_path: Path) -> None:
    (tmp_path / "ledger").mkdir()
    (tmp_path / "ledger" / "1.jsonl").write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(StoreError):
        _store(tmp_path).read_ledger("1", 1)


# --- storylines --------------------------------------------------------


def _storyline(sid: str) -> Storyline:
    return Storyline(
        id=sid,
        league_id="1",
        headline=f"Storyline {sid}",
        status="active",
        first_week=1,
        last_week=3,
    )


def test_write_then_read_storylines_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    original = [_storyline("a")]
    store.write_storylines("1", original)
    assert store.read_storylines("1") == original


def test_overwrite_storylines_replaces_whole_set(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write_storylines("1", [_storyline("a"), _storyline("b")])
    store.write_storylines("1", [_storyline("a")])
    assert store.read_storylines("1") == [_storyline("a")]


def test_read_storylines_no_file_returns_empty(tmp_path: Path) -> None:
    assert _store(tmp_path).read_storylines("1") == []


def test_read_storylines_malformed_raises_store_error(tmp_path: Path) -> None:
    (tmp_path / "storylines").mkdir()
    (tmp_path / "storylines" / "1.json").write_text("{ broken", encoding="utf-8")
    with pytest.raises(StoreError):
        _store(tmp_path).read_storylines("1")


# --- claims ----------------------------------------------------------


def test_read_claims_seeded_on_disk(tmp_path: Path) -> None:
    (tmp_path / "claims").mkdir()
    (tmp_path / "claims" / "1.json").write_text(
        '[{"league_id":"1","roster_id":"2","email":"user@example.com",'
        '"confirmed":true,"claimed_at":"2026-09-01T00:00:00Z"}]',
        encoding="utf-8",
    )
    claims = _store(tmp_path).read_claims("1")
    assert claims == [
        Claim(
            league_id="1",
            roster_id="2",
            email="user@example.com",
            confirmed=True,
            claimed_at=datetime(2026, 9, 1, tzinfo=UTC),
        )
    ]
    assert claims[0].confirmed is True


def test_read_claims_no_file_returns_empty(tmp_path: Path) -> None:
    assert _store(tmp_path).read_claims("1") == []


def test_read_claims_malformed_raises_store_error(tmp_path: Path) -> None:
    (tmp_path / "claims").mkdir()
    (tmp_path / "claims" / "1.json").write_text("not json at all", encoding="utf-8")
    with pytest.raises(StoreError) as excinfo:
        _store(tmp_path).read_claims("1")
    assert excinfo.value.__cause__ is not None


# --- read-after-write + abstractness ---------------------------------


def test_read_after_write_reflects_write_same_process(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_ledger_entry(_entry(7))
    assert store.read_ledger("1", 7) == [_entry(7)]
    store.write_storylines("1", [_storyline("x")])
    assert store.read_storylines("1") == [_storyline("x")]


def test_store_is_abstract() -> None:
    with pytest.raises(TypeError):
        Store()  # type: ignore[abstract]


def test_datetime_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError):
        LedgerEntry(
            league_id="1",
            week=1,
            channel="discord",
            recipient="webhook-1",
            sent_at=datetime(2026, 9, 1),  # naive
        )


def test_non_utc_datetime_is_normalized(tmp_path: Path) -> None:
    aware = datetime(2026, 9, 1, 5, tzinfo=timezone(timedelta(hours=5)))
    entry = LedgerEntry(
        league_id="1", week=1, channel="discord", recipient="webhook-1", sent_at=aware
    )
    assert entry.sent_at == datetime(2026, 9, 1, tzinfo=UTC)


# --- cloud-neutrality source scans --------------------------------


def _engine_py_files() -> list[Path]:
    return sorted(ENGINE_ROOT.rglob("*.py"))


def test_no_cloud_sdk_imports() -> None:
    offenders: list[str] = []
    for path in _engine_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            else:
                continue
            for mod in mods:
                if any(mod == d or mod.startswith(d + ".") for d in CLOUD_SDK_DENYLIST):
                    offenders.append(f"{path.name}: {mod}")
    assert not offenders, offenders


def test_store_api_names_are_cloud_neutral() -> None:
    public = [name for name in vars(Store) if not name.startswith("_")]
    assert set(public) == {
        "read_config",
        "read_ledger",
        "append_ledger_entry",
        "read_storylines",
        "write_storylines",
        "read_claims",
    }
    for name in public:
        member = getattr(Store, name)
        checkables = [name]
        checkables += list(inspect.signature(member).parameters)
        for token in checkables:
            lowered = token.lower()
            assert not any(bad in lowered for bad in CLOUD_NAME_TOKENS), (name, token)


def test_store_module_source_has_no_cloud_tokens() -> None:
    src = (ENGINE_ROOT / "store.py").read_text(encoding="utf-8").lower()
    # word-boundary check so "worker" etc. is caught but incidental substrings
    # inside longer identifiers are not falsely flagged.
    for token in CLOUD_NAME_TOKENS:
        assert not re.search(rf"\b{re.escape(token)}\b", src), token
