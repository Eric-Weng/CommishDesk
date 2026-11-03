"""Story 9.3: stat modules plug into the weekly facts builder."""

from __future__ import annotations

import logging
import os
import textwrap
from pathlib import Path
from typing import Any

import pytest

from commishdesk import demo_season
from commishdesk.errors import CommishDeskError, StatModuleError
from commishdesk.statmods._loader import default_statmod_dir, load_stat_modules, run_stat_modules
from commishdesk.store import FileStore
from commishdesk.weekly import compute_weekly_facts

LOG = logging.getLogger("test.statmods")
EVAL_DIR = Path(__file__).parent / "eval" / "statmods"
FACTS: dict[str, Any] = {"week": 3, "teams": [{"name": "a"}, {"name": "b"}]}


def _write(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _module(directory: Path, file: str, module_id: str, compute_body: str) -> Path:
    body = textwrap.indent(textwrap.dedent(compute_body), " " * 8)
    source = (
        "class M:\n"
        f"    module_id = {module_id!r}\n"
        '    module_version = "1"\n\n'
        "    def compute(self, facts):\n"
        f"{body}\n\n"
        "STAT_MODULE = M()\n"
    )
    return _write(directory, file, source)


def _run(dirs: list[Path], facts: dict[str, Any] | None = None) -> dict[str, Any]:
    return run_stat_modules(facts or FACTS, load_stat_modules(dirs, LOG), LOG)


def test_happy_path_block_lands_under_module_id() -> None:
    assert _run([EVAL_DIR]) == {"echo": {"week": 3, "team_count": 2}}


def test_engine_default_dir_holds_only_the_playoff_odds_reference_module() -> None:
    loaded = load_stat_modules([default_statmod_dir()], LOG)
    assert [m.module.module_id for m in loaded] == ["playoff_odds"]


def test_duplicate_id_in_one_dir_names_both_files(tmp_path: Path) -> None:
    a = _module(tmp_path, "a", "dup", 'return {"dup": {}}')
    b = _module(tmp_path, "b", "dup", 'return {"dup": {}}')
    with pytest.raises(StatModuleError) as err:
        load_stat_modules([tmp_path], LOG)
    assert str(a) in str(err.value) and str(b) in str(err.value)
    assert isinstance(err.value, CommishDeskError)


def test_duplicate_id_across_dirs(tmp_path: Path) -> None:
    _module(tmp_path / "one", "a", "dup", 'return {"dup": {}}')
    _module(tmp_path / "two", "b", "dup", 'return {"dup": {}}')
    with pytest.raises(StatModuleError, match="dup"):
        load_stat_modules([tmp_path / "one", tmp_path / "two"], LOG)


def test_core_key_clash(tmp_path: Path) -> None:
    _module(tmp_path, "t", "teams", 'return {"teams": {}}')
    with pytest.raises(StatModuleError, match="teams") as err:
        _run([tmp_path])
    assert "core Facts key" in str(err.value)


def test_outside_namespace_key(tmp_path: Path) -> None:
    _module(tmp_path, "s", "mine", 'return {"mine": {}, "stray": 1}')
    with pytest.raises(StatModuleError) as err:
        _run([tmp_path])
    assert "mine" in str(err.value) and "stray" in str(err.value)


def test_raising_module_costs_only_its_block(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _module(tmp_path, "boom", "boom", 'raise RuntimeError("nope")')
    _module(tmp_path, "ok", "ok", 'return {"ok": {"x": 1}}')
    with caplog.at_level(logging.WARNING, logger=LOG.name):
        assert _run([tmp_path]) == {"ok": {"x": 1}}
    assert any("boom" in r.getMessage() for r in caplog.records)


def test_non_json_block_is_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _module(tmp_path, "bad", "bad", 'return {"bad": {"x": object()}}')
    with caplog.at_level(logging.WARNING, logger=LOG.name):
        assert _run([tmp_path]) == {}
    assert any("bad" in r.getMessage() for r in caplog.records)


def test_import_failure_is_logged_with_the_file_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    broken = _write(tmp_path, "broken", "raise ImportError('x')\n")
    _module(tmp_path, "ok", "ok", 'return {"ok": {}}')
    with caplog.at_level(logging.WARNING, logger=LOG.name):
        assert _run([tmp_path]) == {"ok": {}}
    assert any(str(broken) in r.getMessage() for r in caplog.records)


def test_module_mutation_does_not_touch_core_facts(tmp_path: Path) -> None:
    _module(tmp_path, "mut", "mut", 'facts["teams"].clear()\nfacts["week"] = 99\nreturn {"mut": {}}')
    facts: dict[str, Any] = {"week": 3, "teams": [{"name": "a"}]}
    assert _run([tmp_path], facts) == {"mut": {}}
    assert facts == {"week": 3, "teams": [{"name": "a"}]}


def test_subpackage_is_discovered(tmp_path: Path) -> None:
    pkg = tmp_path / "pkgmod"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        'class M:\n    module_id = "pkg"\n    module_version = "1"\n'
        '    def compute(self, facts):\n        return {"pkg": {"ok": True}}\n'
        "STAT_MODULE = M()\n",
        encoding="utf-8",
    )
    assert _run([tmp_path]) == {"pkg": {"ok": True}}


def _demo_build(tmp_path: Path) -> Any:
    week = demo_season.WEEKS[0]
    return compute_weekly_facts(
        FileStore(tmp_path),
        demo_season.DEMO_LEAGUE_ID,
        week,
        demo_season.load_week_bundle(week),
        demo_season.load_league_bundle(),
        seeding=None,
        hold_on_cross_check=True,
        logger=LOG,
    )


def test_compute_weekly_facts_merges_blocks_and_default_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("COMMISHDESK_STATMOD_DIRS", raising=False)
    base = _demo_build(tmp_path / "a")
    # the engine's own reference module (Story 9.4) is the only thing the default adds
    assert {k: v for k, v in base.facts_json.items() if k != "playoff_odds"} == base.doc.model_dump(mode="json")

    monkeypatch.setenv("COMMISHDESK_STATMOD_DIRS", str(EVAL_DIR))
    built = _demo_build(tmp_path / "b")
    assert built.facts_json["echo"]["week"] == demo_season.WEEKS[0]
    assert built.facts_json["echo"]["team_count"] == len(built.doc.teams)
    # the core document is untouched (generated_at is a build timestamp, so compare the rest)
    core = {k: v for k, v in built.facts_json.items() if k != "echo"}
    base_core = {k: v for k, v in base.facts_json.items() if k != "playoff_odds"}
    assert core.keys() == base_core.keys()
    assert {k: v for k, v in core.items() if k != "generated_at"} == {
        k: v for k, v in base_core.items() if k != "generated_at"
    }


def test_env_var_takes_several_dirs_and_blank_means_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from commishdesk.weekly import _run_stat_modules

    other = tmp_path / "other"
    _module(other, "extra", "extra", 'return {"extra": {"x": 1}}')
    monkeypatch.setenv("COMMISHDESK_STATMOD_DIRS", f"{EVAL_DIR}{os.pathsep}{other}")
    assert set(_run_stat_modules(FACTS, LOG)) == {"echo", "extra"}
    monkeypatch.setenv("COMMISHDESK_STATMOD_DIRS", "  ")
    assert _run_stat_modules(FACTS, LOG) == {}  # the default module skips Facts it cannot read


def test_replay_keeps_stored_module_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from commishdesk.weekly import _replayed_facts, write_weekly_facts_snapshot

    monkeypatch.setenv("COMMISHDESK_STATMOD_DIRS", str(EVAL_DIR))
    built = _demo_build(tmp_path / "a")
    store = FileStore(tmp_path / "s")
    week = demo_season.WEEKS[0]
    write_weekly_facts_snapshot(store, demo_season.DEMO_LEAGUE_ID, week, built.facts_json)
    replay = _replayed_facts(store, demo_season.DEMO_LEAGUE_ID, week, True, LOG)
    assert replay is not None
    assert replay.facts_json["echo"] == built.facts_json["echo"]
