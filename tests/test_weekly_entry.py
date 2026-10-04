"""Story 6.9 -- the public ``build_weekly_issue`` entry: platform-neutral, store-driven, no file writes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from commishdesk import demo
from commishdesk.errors import CommishDeskError
from commishdesk.store import FileStore
from commishdesk.weekly import build_weekly_issue, record_published_weekly
from tests.conftest import REPO_ROOT

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
_FINAL = {"week": 18, "season_type": "post"}


class _Adapter:
    def __init__(self, nfl_state: dict[str, Any] = _FINAL) -> None:
        self._state = nfl_state
        self.closed = False
        self.calls: list[str] = []

    def fetch(self, league_id: str) -> dict[str, Any]:
        self.calls.append("fetch")
        return demo.load_demo_bundle()

    def fetch_week(self, league_id: str, week: int) -> dict[str, Any]:
        self.calls.append("fetch_week")
        bundle = json.loads((FIXTURE_DIR / "week17-playoffs.json").read_text(encoding="utf-8"))
        return {**bundle, "nfl_state": self._state}

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ANTHROPIC_API_KEY", "LLM_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_builds_issue_web_and_email_from_a_store(tmp_path: Path) -> None:
    store = FileStore(tmp_path)
    adapter = _Adapter()
    built = build_weekly_issue(store, "L1", 17, adapter=adapter, suppressions=set())
    assert built.week == 17 and built.league_id == "L1"
    assert built.web_html.lstrip().lower().startswith("<!doctype html>")
    assert "<script" not in built.web_html  # enhancer=None -> static page
    assert built.email.html and built.email.text
    assert built.issue.sections
    assert built.league_bundle["league"]
    # An injected adapter is the caller's to close.
    assert adapter.closed is False
    # The player snapshot is persisted; no Issue file is written anywhere.
    assert store.read_player_snapshot("L1", 17) is not None
    assert not list(tmp_path.rglob("*.html"))


def test_suppressions_are_taken_from_the_caller_not_re_read(tmp_path: Path) -> None:
    class _Spy(FileStore):
        def read_suppressions(self, league_id: str) -> set[str]:
            raise AssertionError("suppressions must be passed in, not re-read")

    built = build_weekly_issue(_Spy(tmp_path), "L1", 17, adapter=_Adapter(), suppressions={"luck"})
    assert built.issue.sections


def test_not_final_week_is_a_clean_fault_and_writes_nothing(tmp_path: Path) -> None:
    store = FileStore(tmp_path)
    with pytest.raises(CommishDeskError, match="not final|not started"):
        build_weekly_issue(
            store, "L1", 17, adapter=_Adapter({"week": 3, "season_type": "regular"}), suppressions=set()
        )
    assert not list(tmp_path.rglob("*.json"))


def test_week_out_of_range_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(CommishDeskError):
        build_weekly_issue(FileStore(tmp_path), "L1", 19, adapter=_Adapter(), suppressions=set())


def test_render_is_free_of_the_league_id_and_signed_links(tmp_path: Path) -> None:
    built = build_weekly_issue(FileStore(tmp_path), "SECRETLEAGUE9", 17, adapter=_Adapter(), suppressions=set())
    for body in (built.web_html, built.email.html, built.email.text):
        assert "SECRETLEAGUE9" not in body
        assert "/u?c=" not in body and "&s=" not in body


def test_record_published_weekly_writes_the_snapshot(tmp_path: Path) -> None:
    store = FileStore(tmp_path)
    built = build_weekly_issue(store, "L1", 17, adapter=_Adapter(), suppressions=set())
    record_published_weekly(store, "L1", 17, built.facts_json, built.published_ranks, built.nudge_justifications)
    assert store.read_cache("weekly-facts-snapshot", "L1-17") is not None


class _MismatchedAdapter(_Adapter):
    """Week 10 with roster 1's wins disagreeing with the fold: the always-on cross-check mismatch."""

    def fetch_week(self, league_id: str, week: int) -> dict[str, Any]:
        bundle = json.loads((FIXTURE_DIR / "week10-blowout.json").read_text(encoding="utf-8"))
        next(r for r in bundle["rosters"] if r["roster_id"] == 1)["settings"]["wins"] = 10
        return {**bundle, "nfl_state": {"week": 13, "season_type": "regular"}}


def test_cross_check_mismatch_holds_by_default_and_writes_no_storylines(tmp_path: Path) -> None:
    from commishdesk.errors import CrossCheckError

    store = FileStore(tmp_path)
    with pytest.raises(CrossCheckError):
        build_weekly_issue(store, "L1", 10, adapter=_MismatchedAdapter(), suppressions=set())
    assert store.read_storylines("L1") == []


def test_cross_check_mismatch_unheld_is_unverified_and_writes_no_storylines(tmp_path: Path) -> None:
    store = FileStore(tmp_path)
    built = build_weekly_issue(
        store, "L1", 10, adapter=_MismatchedAdapter(), suppressions=set(), hold_on_cross_check=False
    )
    assert built.issue.dateline.startswith("UNVERIFIED — ")
    assert store.read_storylines("L1") == []
