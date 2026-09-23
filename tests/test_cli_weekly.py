"""Story 5.11a — ``commishdesk --league <id> --week <n>`` (the weekly CLI run).

No network, no secrets: a stub ``SleeperAdapter`` returns the committed
``week17-playoffs.json`` / ``week10-blowout.json`` bundles (plus a synthetic
``nfl_state`` making the target week final), and the Discord post is recorded by
a fake ``post_discord_text``. The weekly narrator is the zero-credential
template unless a provider key is set, so nothing here spends anything by
default.

Direct unit coverage of ``render/discord.py``'s weekly additions
(:func:`~commishdesk.render.discord.render_weekly_discord_summary` /
:func:`~commishdesk.render.discord._escape_discord_markdown`) lives in
``tests/test_render_discord.py`` beside the draft-recap summary's own tests,
not here — this file is CLI-level (the pipeline chained end to end).

Story 5.11c (the ``--reason`` reissue) is covered here too: it is CLI wiring on
this same path, not a new module. Story 5.12's weekly narrator gate (key +
budget, degrading to template; published ranks read from and written to the
Store) is covered here as well, for the same reason.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from commishdesk.cli import app
from commishdesk.facts.schema import WeeklyNarration
from commishdesk.ingest import build_week_model
from commishdesk.narrate.weekly_template import (
    render_weekly_issue,
    weekly_issue_to_text,
)
from commishdesk.store import FileStore
from tests.conftest import REPO_ROOT

runner = CliRunner()

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"

#: The seven sections every weekly Issue carries, in order (``weekly_template.py``).
WEEKLY_SECTION_HEADINGS = (
    "The Lead",
    "Around the League",
    "Standings and the Playoff Picture",
    "Power Rankings",
    "The Luck Index",
    "Next Week",
    "The Transaction Desk",
)

_FAKE_WEBHOOK_URL = "https://discord.com/api/webhooks/222222222222222222/faketoken"
_WEBHOOK_ID = "222222222222222222"

#: ``week17-playoffs.json`` is the one committed fixture whose standings
#: cross-check passes; ``week10-blowout.json`` trips ``CrossCheckError`` by
#: construction (its ``Roster`` totals are season-final). See spec-5-6's Design
#: Notes ("Hand-off to 5.11a").
_WEEK17 = "week17-playoffs.json"
_WEEK10 = "week10-blowout.json"

#: A final week, for the happy paths: Sleeper's own state is past the target
#: week (the ``post`` season phase is neither "pre" nor "regular").
_FINAL_POST = {"week": 18, "season_type": "post"}

#: Every environment variable that turns the LLM narrator on. Cleared by the
#: autouse fixture below so a developer's (or a CI image's) exported key can
#: never make a "template narrator" assertion silently wrong.
_LLM_KEY_VARS = ("ANTHROPIC_API_KEY", "LLM_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")


# --------------------------------------------------------------------------- #
# Fixtures / stubs
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _no_ambient_provider_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test hermetic: an exported ``COMMISHDESK_DISCORD_WEBHOOK_URL``
    or provider key must never leak in from the developer's shell or a CI image.
    Tests that need a webhook or a key set it themselves."""
    monkeypatch.delenv("COMMISHDESK_DISCORD_WEBHOOK_URL", raising=False)
    for name in _LLM_KEY_VARS:
        monkeypatch.delenv(name, raising=False)


def _load_fixture(name: str) -> dict[str, Any]:
    """The raw committed fixture bundle (mirrors
    ``tests/test_ingest_build.py::_load_fixture``)."""
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _stub_adapter(
    monkeypatch: pytest.MonkeyPatch,
    *,
    week_bundle: dict[str, Any] | None = None,
    nfl_state: dict[str, Any] | None = None,
    league_bundle: dict[str, Any] | None = None,
) -> dict[str, list]:
    """Replace ``SleeperAdapter`` with a recording stub (mirrors
    ``tests/test_cli_draft_recap.py``'s ``monkeypatch.setattr`` pattern).

    ``fetch`` returns *league_bundle* — the committed demo fixture by default,
    so a real :class:`~commishdesk.ingest.LeagueModel` can be built fully
    offline. ``fetch_week`` returns *week_bundle* (``week17-playoffs.json`` by
    default) stamped with a synthetic ``"nfl_state"``. Every call is recorded so
    a short-circuit test can assert "no fetch happened"."""
    from commishdesk import demo

    demo_bundle = demo.load_demo_bundle()
    base_league = league_bundle if league_bundle is not None else demo_bundle
    base_week = week_bundle if week_bundle is not None else _load_fixture(_WEEK17)
    state = nfl_state if nfl_state is not None else _FINAL_POST
    calls: dict[str, list] = {"fetch": [], "fetch_week": []}

    class _StubAdapter:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def fetch(self, league_id: str) -> dict[str, Any]:
            calls["fetch"].append(league_id)
            return base_league

        def fetch_week(self, league_id: str, week: int) -> dict[str, Any]:
            calls["fetch_week"].append((league_id, week))
            return {**base_week, "nfl_state": state}

        def close(self) -> None: ...

    monkeypatch.setattr("commishdesk.adapters.sleeper.SleeperAdapter", _StubAdapter)
    return calls


def _stub_post_discord_text(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record every ``post_discord_text`` call instead of hitting the network
    (the CLI's lazy import picks this up at call time). Returns the calls."""
    calls: list[tuple[str, str]] = []

    def _fake(url: str, content: str, **_kwargs: object) -> str:
        calls.append((url, content))
        return _WEBHOOK_ID

    monkeypatch.setattr("commishdesk.deliver.discord.post_discord_text", _fake)
    return calls


def _league_bundle_with(monkeypatch: pytest.MonkeyPatch, **league_updates: Any) -> dict[str, Any]:
    """The demo fixture's bundle with its ``league`` section overridden."""
    from commishdesk import demo

    bundle = demo.load_demo_bundle()
    return {**bundle, "league": {**bundle["league"], **league_updates}}


class _WeeklyVoiceClient:
    """A deterministic stand-in for the weekly Voice (Story 5.12).

    It takes the payload the CLI actually builds (the ``WeeklyNarration`` JSON),
    renders the template Issue for it, re-states each power line with a rank
    *nudge* positions off the model rank, and returns that. ``nudge=0`` is a
    Voice that agrees with the model; ``nudge=3`` is one that over-reaches
    ``POWER_NUDGE_CAP``.
    """

    def __init__(self, nudge: int = 0, *, invent_fact: bool = False) -> None:
        self.nudge = nudge
        self.invent_fact = invent_fact
        self.calls: list[str] = []

    def generate(self, payload: str, voice: object) -> str:
        self.calls.append(payload)
        narration = WeeklyNarration.model_validate_json(payload)
        issue = render_weekly_issue(narration)
        rows = sorted(narration.power, key=lambda row: (row.model_rank is None, row.model_rank or 0))
        lines = [
            f"{(row.model_rank or 0) + self.nudge}. {row.team} — {row.rec}, {row.avg_pf} points a week."
            for row in rows
        ]
        sections = [
            section.model_copy(update={"blocks": lines})
            if section.heading == "Power Rankings"
            else section
            for section in issue.sections
        ]
        if self.invent_fact:
            # The matrix's "nudge cites an absent fact" row: a number that
            # appears nowhere in the narration payload — the closed-world scan
            # in check_narration must flag this exactly like a hallucinated
            # draft-recap claim, with no code changes of its own (Story 5.12's
            # Design Notes).
            sections = [
                s.model_copy(update={"blocks": [*s.blocks, "A league source confirms 84719 total yards."]})
                if s.heading == "The Lead"
                else s
                for s in sections
            ]
        return weekly_issue_to_text(issue.model_copy(update={"sections": sections}))


def _stub_weekly_voice(
    monkeypatch: pytest.MonkeyPatch,
    *,
    nudge: int = 0,
    invent_fact: bool = False,
    ceiling: str = "100",
) -> _WeeklyVoiceClient:
    """Point the CLI at a fake weekly Voice, and set the key + ceiling that let it
    run at all. Returns the client so a test can count its calls."""
    from commishdesk.llmconfig import load_llm_config  # noqa: F401  (documents the gate)

    client = _WeeklyVoiceClient(nudge=nudge, invent_fact=invent_fact)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("COMMISHDESK_COST_CEILING_USD", ceiling)
    monkeypatch.setattr("commishdesk.narrate.llm.build_client", lambda cfg: client)
    return client


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #


def test_weekly_happy_path_prints_sections_and_writes_files(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1 / AC6 (no ``--post``): every weekly section, a real week-17 fact, and
    the local text Issue plus the generic HTML dump on disk."""
    calls = _stub_adapter(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(app, ["--league", "77", "--week", "17", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output

    for heading in WEEKLY_SECTION_HEADINGS:
        assert heading in result.stdout, heading

    # A sample factual claim taken from the fixture: one standings row per roster.
    expected_teams = len(build_week_model(_load_fixture(_WEEK17)).rosters)
    assert result.stdout.count("points for") == expected_teams
    assert "Week 17 Recap" in result.stdout  # the masthead title
    assert "Week 17:" in result.stdout  # the lead line

    assert calls["fetch_week"] == [("77", 17)]

    html_path = tmp_path / "commishdesk-77-weekly-week17.html"
    text_path = tmp_path / "commishdesk-77-weekly-week17.txt"
    assert html_path.is_file() and text_path.is_file()

    body = html_path.read_text(encoding="utf-8")
    assert body.startswith("<!doctype html>")
    assert "<h1>" in body and "<h2>" in body
    assert "UNVERIFIED" not in body
    assert text_path.read_text(encoding="utf-8").strip()

    # A passing cross-check does advance narrative memory (the flip side of
    # test_weekly_cross_check_without_post_writes_an_unverified_issue's guard).
    store = FileStore(tmp_path / "cache" / "commishdesk")
    assert store.read_storylines("77") != []
    # ...and the template narrator persisted no published rank at all (Story 5.12:
    # only a confirmed voiced publish leaves one).
    assert store.read_published_rank("77", 17) is None


def test_weekly_post_confirms_a_weekly_ledger_entry_and_posts_once(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: a ``--post`` run writes the local Issue and confirms exactly one
    ``kind="weekly"`` Send Ledger entry for this league-week."""
    _stub_adapter(monkeypatch)
    posted = _stub_post_discord_text(monkeypatch)
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(
        app, ["--league", "41", "--week", "17", "--post", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "posted to Discord" in result.stdout
    assert len(posted) == 1
    assert posted[0][0] == _FAKE_WEBHOOK_URL
    assert "Week 17 Recap" in posted[0][1]

    store = FileStore(tmp_path / "cache" / "commishdesk")
    ledger = store.read_ledger("41", 17)
    assert [(entry.kind, entry.channel, entry.recipient) for entry in ledger] == [
        ("weekly", "discord", _WEBHOOK_ID)
    ]


def test_weekly_post_without_a_webhook_url_fails_fast_no_fetch(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1's fail-fast guard (step-04 review, blind-hunter): ``--post`` with no
    ``COMMISHDESK_DISCORD_WEBHOOK_URL`` set (the autouse
    ``_no_ambient_provider_config`` fixture keeps this true here) raises before
    any fetch — mirrors the draft-recap path's identical guard."""
    calls = _stub_adapter(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(
        app, ["--league", "43", "--week", "17", "--post", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "COMMISHDESK_DISCORD_WEBHOOK_URL" in result.output
    assert calls["fetch_week"] == []
    assert not list(tmp_path.glob("commishdesk-*"))


def test_weekly_post_second_identical_run_skips_before_any_fetch(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: re-running the identical ``--post`` command short-circuits — no
    fetch, no render, no second post."""
    calls = _stub_adapter(monkeypatch)
    posted = _stub_post_discord_text(monkeypatch)
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    first = runner.invoke(
        app, ["--league", "42", "--week", "17", "--post", "--out-dir", str(tmp_path)]
    )
    assert first.exit_code == 0, first.output
    assert len(posted) == 1
    assert len(calls["fetch_week"]) == 1

    second = runner.invoke(
        app, ["--league", "42", "--week", "17", "--post", "--out-dir", str(tmp_path)]
    )
    assert second.exit_code == 0, second.output
    assert "already confirmed" in second.stdout
    assert len(posted) == 1
    assert len(calls["fetch_week"]) == 1  # the short-circuit precedes the fetch


def test_weekly_run_with_explicit_llm_flag_warns_it_is_ignored(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step-04 review (blind-hunter), kept for Story 5.12: the weekly narrator is
    chosen from the environment (a provider key + a budget), never from this flag,
    so an explicit ``--llm``/``--no-llm`` is still accepted and loudly ignored."""
    _stub_adapter(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(
        app, ["--league", "78", "--week", "17", "--llm", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "has no effect on a weekly run" in result.output


# --------------------------------------------------------------------------- #
# Story 5.12 — the weekly narrator gate (key + budget)
# --------------------------------------------------------------------------- #


def test_weekly_without_a_provider_key_uses_the_template_narrator(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The matrix's "no key configured" row: no key, no spend, and the power
    section shows the Model Rank only."""
    _stub_adapter(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(app, ["--league", "90", "--week", "17", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "model rank" in result.stdout


def test_weekly_with_a_key_and_a_budget_uses_the_voice_narrator(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: a key plus a passing estimate runs the Voice — exactly one
    ``generate()`` call — and the published rank it states survives into the
    Issue on every surface."""
    _stub_adapter(monkeypatch)
    voice = _stub_weekly_voice(monkeypatch, nudge=1)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(app, ["--league", "91", "--week", "17", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert len(voice.calls) == 1  # I3: one paid generation per league-week
    for heading in WEEKLY_SECTION_HEADINGS:
        assert heading in result.stdout, heading
    assert (tmp_path / "commishdesk-91-weekly-week17.txt").is_file()


def test_weekly_with_an_exceeded_budget_silently_uses_the_template_narrator(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The matrix's "key present, budget exceeded" row: the estimate is over the
    ceiling, so the run degrades to the template — no ``CostCeilingExceededError``,
    no exit code change, and no paid call at all."""
    _stub_adapter(monkeypatch)
    voice = _stub_weekly_voice(monkeypatch, nudge=1, ceiling="0.000001")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(app, ["--league", "92", "--week", "17", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "CostCeilingExceededError" not in result.output
    assert voice.calls == []
    assert "The Lead" in result.stdout


def test_weekly_a_nudge_past_the_cap_regenerates_once_then_falls_back(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The matrix's "nudge exceeds POWER_NUDGE_CAP" row: the first generation is
    flagged at the regenerate tier, the one permitted re-narration repeats it, and
    the run ships the deterministic template Issue — power section present — after
    exactly two calls."""
    _stub_adapter(monkeypatch)
    voice = _stub_weekly_voice(monkeypatch, nudge=3)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(app, ["--league", "93", "--week", "17", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert len(voice.calls) == 2  # initial attempt + one permitted regeneration
    assert "Power Rankings" in result.stdout  # the template's own section
    assert "content-safety alert" in result.output


def test_weekly_a_nudge_citing_an_absent_fact_regenerates_once_then_falls_back(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The matrix's "nudge cites an absent fact" row: a number invented outside
    the narration payload trips the *same* closed-world/regenerate/fallback path
    as a cap violation (no new safety.py code, per Story 5.12's Design Notes) —
    one permitted re-narration, then the deterministic template Issue ships."""
    _stub_adapter(monkeypatch)
    voice = _stub_weekly_voice(monkeypatch, invent_fact=True)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(app, ["--league", "95", "--week", "17", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert len(voice.calls) == 2  # initial attempt + one permitted regeneration
    assert "Power Rankings" in result.stdout  # the template's own section
    assert "content-safety alert" in result.output
    assert "84719" not in result.stdout  # the invented fact never ships


def test_weekly_a_confirmed_voiced_post_persists_the_published_rank(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4's write half: a confirmed voiced publish persists exactly one
    ``{roster_id: published_rank}`` map for the league-week, so the *next* week's
    ``prev_published_rank`` has something to resolve."""
    _stub_adapter(monkeypatch)
    voice = _stub_weekly_voice(monkeypatch, nudge=1)
    posted = _stub_post_discord_text(monkeypatch)
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(
        app, ["--league", "94", "--week", "17", "--post", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert len(posted) == 1
    assert len(voice.calls) == 1

    store = FileStore(tmp_path / "cache" / "commishdesk")
    ranks = store.read_published_rank("94", 17)
    assert ranks is not None
    assert len(ranks) == len(build_week_model(_load_fixture(_WEEK17)).rosters)


def test_weekly_a_voiced_run_without_post_persists_nothing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4's gate: the published rank is written only once ``--post`` confirms —
    a local-only voiced run leaves no "this week was published" evidence."""
    _stub_adapter(monkeypatch)
    _stub_weekly_voice(monkeypatch, nudge=1)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(app, ["--league", "95", "--week", "17", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    store = FileStore(tmp_path / "cache" / "commishdesk")
    assert store.read_published_rank("95", 17) is None


# --------------------------------------------------------------------------- #
# Week finality (AC3)
# --------------------------------------------------------------------------- #


def test_weekly_refuses_a_week_that_is_not_final_yet(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: Sleeper's own state has only reached the requested week — refuse with
    a clear message and nothing written."""
    _stub_adapter(monkeypatch, nfl_state={"week": 17, "season_type": "regular"})
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(app, ["--league", "52", "--week", "17", "--out-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "not final" in result.output
    assert not list(tmp_path.glob("commishdesk-*"))


def test_weekly_refuses_before_the_season_starts(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: preseason — the season has not started, so no week is final."""
    _stub_adapter(monkeypatch, nfl_state={"week": 0, "season_type": "pre"})
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(app, ["--league", "53", "--week", "1", "--out-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "not started" in result.output


def test_weekly_proceeds_when_nfl_state_is_unreadable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3's flip side (step-04 review, verification-gap): ``nfl_state`` degraded
    to ``{"week": None, "season_type": None}`` (a real ``SleeperAdapter``'s shape
    for an unreadable ``/state/nfl`` response) must fail *open* — never block a
    real league on a missing platform field. Asserted directly since nothing else
    in this suite ever exercises the "unreadable" shape (every other test
    supplies a fully-populated state)."""
    _stub_adapter(monkeypatch, nfl_state={"week": None, "season_type": None})
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(app, ["--league", "54", "--week", "17", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "The Lead" in result.stdout


# --------------------------------------------------------------------------- #
# Cross-check: hold with --post, UNVERIFIED without (AC4)
# --------------------------------------------------------------------------- #


def test_weekly_cross_check_holds_the_issue_with_post(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: under ``--post`` a mismatched cross-check holds the whole Issue —
    nothing rendered, written or posted; one-line alert, exit 1."""
    _stub_adapter(
        monkeypatch,
        week_bundle=_load_fixture(_WEEK10),
        nfl_state={"week": 13, "season_type": "regular"},
    )
    posted = _stub_post_discord_text(monkeypatch)
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(
        app, ["--league", "61", "--week", "10", "--post", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "disagree with Sleeper" in result.output  # the error's own message
    assert not posted
    assert not list(tmp_path.glob("commishdesk-61-*"))


def test_weekly_cross_check_without_post_writes_an_unverified_issue(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: without ``--post`` the mismatch is printed and the local Issue still
    ships, with ``UNVERIFIED —`` prefixed to the dateline (visible on stdout and
    in the HTML dump). Step-04 review (blind-hunter): the mismatched numbers
    must not durably taint next week's storyline continuity either — narrative
    memory stays untouched for this league-week, same as a held --post run."""
    _stub_adapter(
        monkeypatch,
        week_bundle=_load_fixture(_WEEK10),
        nfl_state={"week": 13, "season_type": "regular"},
    )
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(app, ["--league", "62", "--week", "10", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "disagree with Sleeper" in result.output  # both values are named
    assert "UNVERIFIED — " in result.stdout

    html_path = tmp_path / "commishdesk-62-weekly-week10.html"
    assert html_path.is_file()
    assert "UNVERIFIED — " in html_path.read_text(encoding="utf-8")

    store = FileStore(tmp_path / "cache" / "commishdesk")
    assert store.read_storylines("62") == []


# --------------------------------------------------------------------------- #
# Per-league faults (AC5) and markdown escaping (AC2)
# --------------------------------------------------------------------------- #


def test_weekly_lineup_error_is_reported_without_a_traceback(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5: an ``OptimalLineupError`` from an unsolvable ``roster_slots`` is a
    one-line message naming the league and the offending slot — no traceback,
    exit 1, the league skipped."""
    bad_league = _league_bundle_with(
        monkeypatch, roster_positions=["QB", "RB", "WR", "TE", "MYSTERY_FLEX"]
    )
    _stub_adapter(monkeypatch, league_bundle=bad_league)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(app, ["--league", "63", "--week", "17", "--out-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "MYSTERY_FLEX" in result.output
    assert not list(tmp_path.glob("commishdesk-63-*"))


def test_weekly_markdown_in_a_league_supplied_name_is_escaped_in_the_post(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2 / the matrix's markdown row: a league-supplied name carrying Discord
    markdown reaches the posted summary escaped, never reformatting it.

    The only league-supplied name reliably present in the composed summary is the
    league name (it is the masthead title; the first lead block is the fixed
    "Week N: … in the standings" line), so that is the name made hostile here."""
    hostile = "Trench *Warfare*_`Log`~|># Bulletin"
    _stub_adapter(monkeypatch, league_bundle=_league_bundle_with(monkeypatch, name=hostile))
    posted = _stub_post_discord_text(monkeypatch)
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(
        app, ["--league", "64", "--week", "17", "--post", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert len(posted) == 1
    _, content = posted[0]
    for char in "*_`~|>#":
        assert f"\\{char}" in content, char
    assert hostile not in content  # nothing raw survived


# --------------------------------------------------------------------------- #
# Reissue (Story 5.11c)
# --------------------------------------------------------------------------- #


def _first_weekly_post(
    tmp_path, monkeypatch: pytest.MonkeyPatch, league_id: str
) -> tuple[list[tuple[str, str]], FileStore]:
    """A plain, successful ``--week 17 --post`` run — one Discord post recorded
    and one ``kind="weekly"`` Send Ledger entry confirmed. Returns the recorded
    posts and the store the run wrote to, so a reissue test can assert against
    both."""
    _stub_adapter(monkeypatch)
    posted = _stub_post_discord_text(monkeypatch)
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    first = runner.invoke(
        app, ["--league", league_id, "--week", "17", "--post", "--out-dir", str(tmp_path)]
    )
    assert first.exit_code == 0, first.output
    assert len(posted) == 1
    store = FileStore(tmp_path / "cache" / "commishdesk")
    assert [entry.kind for entry in store.read_ledger(league_id, 17)] == ["weekly"]
    return posted, store


def test_weekly_reissue_posts_a_corrected_issue_and_records_the_reason(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The matrix's "normal reissue" row / AC1: with a weekly send already
    confirmed, ``--week 17 --post --reason …`` rebuilds the Issue, posts the
    correction, and appends a second ledger entry carrying the operator's
    reason — and the Correction label reaches stdout, the text Issue, the HTML
    dump and the posted message alike."""
    posted, store = _first_weekly_post(tmp_path, monkeypatch, "81")
    assert "Correction" not in posted[0][1]

    reissued = runner.invoke(
        app,
        [
            "--league", "81", "--week", "17", "--post",
            "--reason", "fixed the QB stat line",
            "--out-dir", str(tmp_path),
        ],
    )
    assert reissued.exit_code == 0, reissued.output
    assert len(posted) == 2
    assert "posted to Discord" in reissued.stdout

    _, content = posted[1]
    assert "Correction" in content
    assert "fixed the QB stat line" in content

    # Every local surface carries the correction too.
    assert "Correction" in reissued.stdout
    assert "Correction" in (tmp_path / "commishdesk-81-weekly-week17.txt").read_text(encoding="utf-8")
    assert "Correction" in (tmp_path / "commishdesk-81-weekly-week17.html").read_text(encoding="utf-8")

    assert [
        (entry.kind, entry.channel, entry.recipient, entry.reason)
        for entry in store.read_ledger("81", 17)
    ] == [
        ("weekly", "discord", _WEBHOOK_ID, None),
        ("weekly", "discord", _WEBHOOK_ID, "fixed the QB stat line"),
    ]


def test_weekly_reissue_with_unchanged_numbers_says_nothing_moved(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The matrix's "nothing actually changed" row: the reissue still posts (the
    operator's explicit call) and the correction's diff summary states exactly
    that no numeric value moved.

    Narrative memory is the one input the first run durably advanced, so it is
    reset here to reproduce the row's own premise — "new Facts JSON identical to
    prior snapshot" — before the second run rebuilds it."""
    posted, store = _first_weekly_post(tmp_path, monkeypatch, "82")
    store.write_storylines("82", [])

    reissued = runner.invoke(
        app,
        [
            "--league", "82", "--week", "17", "--post",
            "--reason", "repost after a bad publish",
            "--out-dir", str(tmp_path),
        ],
    )
    assert reissued.exit_code == 0, reissued.output
    assert len(posted) == 2
    assert "no numeric differences found" in reissued.stdout
    assert "no numeric differences found" in posted[1][1]


def test_weekly_reissue_without_a_confirmed_send_fails_fast(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The matrix's "nothing confirmed yet" row: ``--reason`` on a league-week
    with no confirmed weekly send has nothing to correct — one-line message, no
    fetch, no post, nothing written."""
    calls = _stub_adapter(monkeypatch)
    posted = _stub_post_discord_text(monkeypatch)
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(
        app,
        [
            "--league", "83", "--week", "17", "--post",
            "--reason", "wrong number",
            "--out-dir", str(tmp_path),
        ],
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "nothing to correct" in result.output
    assert "no confirmed send for this league-week" in result.output
    assert calls["fetch_week"] == []
    assert not posted
    assert not list(tmp_path.glob("commishdesk-*"))


def test_weekly_second_plain_run_still_skips_without_a_reason(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2 / the matrix's "accidental second plain run" row: with no ``--reason``
    the idempotency short-circuit is untouched — the second run posts nothing and
    appends no ledger entry."""
    posted, store = _first_weekly_post(tmp_path, monkeypatch, "84")

    again = runner.invoke(
        app, ["--league", "84", "--week", "17", "--post", "--out-dir", str(tmp_path)]
    )
    assert again.exit_code == 0, again.output
    assert "already confirmed" in again.stdout
    assert len(posted) == 1
    assert len(store.read_ledger("84", 17)) == 1


def test_weekly_reason_without_post_is_rejected_before_any_fetch(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The matrix's "``--reason`` without ``--post``" row: rejected up front,
    before any fetch."""
    calls = _stub_adapter(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(
        app,
        ["--league", "85", "--week", "17", "--reason", "x", "--out-dir", str(tmp_path)],
    )
    assert result.exit_code == 2
    assert "--reason requires --post" in result.output
    assert calls["fetch_week"] == []


def test_weekly_reason_without_week_is_rejected(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--reason`` on its own (no ``--week``) names nothing to correct — it is
    rejected as a usage error rather than falling through to the informational
    path."""
    calls = _stub_adapter(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(
        app, ["--league", "86", "--reason", "x", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 2
    assert "--reason requires --week" in result.output
    assert calls["fetch"] == [] and calls["fetch_week"] == []


def test_weekly_reason_is_rejected_on_the_draft_recap_path(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The matrix's "``--reason`` with ``--draft-recap``" row: a reissue is a
    weekly-only concern, so the draft path refuses the flag before any fetch."""
    calls = _stub_adapter(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(
        app,
        ["--league", "87", "--draft-recap", "--reason", "x", "--out-dir", str(tmp_path)],
    )
    assert result.exit_code == 2
    assert "cannot be combined with --draft-recap" in result.output
    assert calls["fetch"] == [] and calls["fetch_week"] == []


def test_weekly_blank_reason_is_rejected_before_any_fetch(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The matrix's "blank reason" row: whitespace is not a reason."""
    calls = _stub_adapter(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(
        app,
        ["--league", "88", "--week", "17", "--post", "--reason", "   ", "--out-dir", str(tmp_path)],
    )
    assert result.exit_code == 2
    assert "non-blank" in result.output
    assert calls["fetch_week"] == []


def test_weekly_reissue_reports_a_real_changed_number(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review-loop 1 (verification-gap / blind-hunter): every other reissue test
    reuses identical upstream data between the first post and the reissue, so
    the diff always lands in the empty-changes branch and the "changed
    numbers: <path>: <old> -> <new>" line this story exists to produce was
    never exercised. Here the reissue's underlying week bundle genuinely
    differs — one roster's ``points`` is bumped, mirroring a real stat
    correction — and the posted correction must show the actual old and new
    values, not just the word "Correction"."""
    posted, store = _first_weekly_post(tmp_path, monkeypatch, "89")

    corrected_week = _load_fixture(_WEEK17)
    corrected_week["matchups"]["17"][0]["points"] = 254.52  # was 204.52
    _stub_adapter(monkeypatch, week_bundle=corrected_week)

    reissued = runner.invoke(
        app,
        [
            "--league", "89", "--week", "17", "--post",
            "--reason", "fixed a scoring error",
            "--out-dir", str(tmp_path),
        ],
    )
    assert reissued.exit_code == 0, reissued.output
    assert len(posted) == 2

    _, content = posted[1]
    assert "changed numbers:" in content
    assert "204.52" in content and "254.52" in content
    assert store.read_cache("weekly-facts-snapshot", "89-17") is not None


def test_weekly_facts_snapshot_key_is_pinned_and_round_trips(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blind-hunter: the blob-cache key format (``<league_id>-<week>``, not the
    spec's own ``:``-separated example — ``:`` is not a legal Windows file
    name, and ``FileStore`` maps a cache key straight onto one) is a durable
    on-disk contract, not an implementation detail free to drift. Pins it
    directly, independent of any CLI run."""
    from commishdesk.cli import _weekly_facts_snapshot_key

    assert _weekly_facts_snapshot_key("12345", 7) == "12345-7"

    store = FileStore(tmp_path / "cache")
    store.write_cache("weekly-facts-snapshot", _weekly_facts_snapshot_key("12345", 7), {"pf": 1.0})
    assert store.read_cache("weekly-facts-snapshot", "12345-7") == {"pf": 1.0}
