"""Story 5.11a — ``commishdesk --league <id> --week <n>`` (the weekly CLI run).

No network, no secrets: a stub ``SleeperAdapter`` returns the committed
``week17-playoffs.json`` / ``week10-blowout.json`` bundles (plus a synthetic
``nfl_state`` making the target week final), and the Discord post is recorded by
a fake ``post_discord_text``. The weekly narrator is the zero-credential
template unless a provider key is set, so nothing here spends anything by
default.

Direct unit coverage of ``render/discord.py``'s weekly additions
(:func:`~commishdesk.render.discord.render_weekly_discord_post` /
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
import re
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

#: Both committed fixtures' standings cross-checks pass since Story 5.13a made
#: ``week10-blowout.json``'s ``Roster`` totals point-in-time. The mismatch-path
#: tests below use ``_mismatched_week10`` (season-final wins planted on one
#: roster). See spec-5-6's Design Notes ("Hand-off to 5.11a").
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


def _mismatched_week10() -> dict[str, Any]:
    """``week10-blowout.json`` with roster 1's wins disagreeing with the fold —
    the always-on cross-check mismatch."""
    bundle = _load_fixture(_WEEK10)
    next(r for r in bundle["rosters"] if r["roster_id"] == 1)["settings"]["wins"] = 10
    return bundle


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

    def __init__(self, nudge: int = 0, *, invent_fact: bool = False, hold: bool = False, tight: bool = False) -> None:
        self.nudge = nudge
        self.hold = hold
        self.tight = tight
        self.invent_fact = invent_fact
        self.calls: list[str] = []
        self.voices: list[object] = []

    def generate(self, payload: str, voice: object) -> str:
        self.calls.append(payload)
        self.voices.append(voice)
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
        if self.hold:
            # A personal insult beside a weekly team label: a hold-tier finding.
            label = rows[0].team
            sections = [
                s.model_copy(update={"blocks": [*s.blocks, f"{label} is run by an idiot."]})
                if s.heading == "The Lead"
                else s
                for s in sections
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
        text = weekly_issue_to_text(issue.model_copy(update={"sections": sections}))
        if self.tight:
            # A real model writes a numbered list with no blank lines between items.
            blank = chr(10) * 2
            text = text.replace(blank.join(lines), chr(10).join(lines))
        return text


def _stub_weekly_voice(
    monkeypatch: pytest.MonkeyPatch,
    *,
    nudge: int = 0,
    invent_fact: bool = False,
    hold: bool = False,
    tight: bool = False,
    ceiling: str = "100",
) -> _WeeklyVoiceClient:
    """Point the CLI at a fake weekly Voice, and set the key + ceiling that let it
    run at all. Returns the client so a test can count its calls."""
    client = _WeeklyVoiceClient(nudge=nudge, invent_fact=invent_fact, hold=hold, tight=tight)
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
    # Story 5.14b: the weekly email pair, next to the page.
    assert (tmp_path / "commishdesk-77-weekly-week17.email.html").is_file()
    assert (tmp_path / "commishdesk-77-weekly-week17.email.txt").read_text(encoding="utf-8").strip()

    body = html_path.read_text(encoding="utf-8")
    assert body.startswith("<!doctype html>")
    # Story 5.14a: the designed weekly page, not the Story 2.7 generic dump.
    assert 'class="paper weekly_issue"' in body
    assert "<h1>" not in body and "<h2>" not in body  # the dump's bare, unstyled headings
    assert body.count("<style>") == 1 and "<script" not in body
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
    # Story 5.14b: the designed post, not the 5.11a title-plus-lead summary.
    assert posted[0][1].startswith("## 🏈 ") and posted[0][1].split("\n", 1)[0].endswith(" · Week 17")
    assert "📊 **Standings**" in posted[0][1] and "Recap" not in posted[0][1]

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
    from commishdesk.voices.beat_writer import BEAT_WRITER_WEEKLY, BEAT_WRITER_WEEKLY_COLD_START

    # a warm week (17) uses the warm voice, never the Week-1 cold-start prompt
    assert voice.voices == [BEAT_WRITER_WEEKLY]
    assert BEAT_WRITER_WEEKLY_COLD_START not in voice.voices
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
        week_bundle=_mismatched_week10(),
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
        week_bundle=_mismatched_week10(),
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
    assert "fixed the QB stat line" in (tmp_path / "commishdesk-81-weekly-week17.email.txt").read_text(encoding="utf-8")
    # Story 5.14b: the correction is the line right under the post's title
    assert content.split("\n")[1].startswith("Correction — fixed the QB stat line — ")

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


def test_weekly_with_a_malformed_llm_config_degrades_to_the_template(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unattended run must not die on a config typo: a key is present but
    ``COMMISHDESK_LLM_PRIMARY`` is malformed, so the weekly path uses the template
    (the draft path, by contrast, exits 1 on the same input)."""
    _stub_adapter(monkeypatch)
    voice = _stub_weekly_voice(monkeypatch)
    monkeypatch.setenv("COMMISHDESK_LLM_PRIMARY", "bogus-no-colon")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(app, ["--league", "96", "--week", "17", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert voice.calls == []
    assert "model rank" in result.stdout
    assert (tmp_path / "commishdesk-96-weekly-week17.txt").is_file()


def _values_under_key(node: object, key: str) -> list[object]:
    """Every value stored under *key*, anywhere in a decoded JSON document."""
    found: list[object] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                found.append(v)
            found.extend(_values_under_key(v, key))
    elif isinstance(node, list):
        for v in node:
            found.extend(_values_under_key(v, key))
    return found


def test_weekly_reads_the_previous_weeks_persisted_published_rank(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4 through the CLI call site: ranks persisted for week 16 must reach week
    17's Facts as ``prev_published_rank`` (the pure helper is tested in
    ``test_facts_weekly.py``; this pins the wiring)."""
    _stub_adapter(monkeypatch)
    _stub_weekly_voice(monkeypatch)
    _stub_post_discord_text(monkeypatch)
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    store = FileStore(tmp_path / "cache" / "commishdesk")
    roster_ids = [str(r.roster_id) for r in build_week_model(_load_fixture(_WEEK17)).rosters]
    store.write_published_rank("97", 16, {rid: i + 1 for i, rid in enumerate(roster_ids)})

    result = runner.invoke(
        app, ["--league", "97", "--week", "17", "--post", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    snapshot = store.read_cache("weekly-facts-snapshot", "97-17")
    assert snapshot is not None
    prev = _values_under_key(snapshot, "prev_published_rank")
    assert any(isinstance(v, int) for v in prev), prev
    assert 1 in prev  # the roster ranked first in week 16 resolves to exactly 1


def test_weekly_a_held_llm_narration_ships_the_template_and_persists_nothing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hold-tier finding is never shipped as an LLM Issue: the template ships,
    alerts are emitted, one paid call was made, and no rank is persisted."""
    _stub_adapter(monkeypatch)
    voice = _stub_weekly_voice(monkeypatch, hold=True)
    _stub_post_discord_text(monkeypatch)
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(
        app, ["--league", "98", "--week", "17", "--post", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert len(voice.calls) == 1
    assert "content-safety alert" in result.output
    assert "idiot" not in result.stdout
    assert FileStore(tmp_path / "cache" / "commishdesk").read_published_rank("98", 17) is None


def test_weekly_a_tight_numbered_list_is_fully_parsed_and_persisted(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Power Rankings list with no blank lines between items still yields one
    published rank per team (the cap check and persistence cover every row)."""
    _stub_adapter(monkeypatch)
    _stub_weekly_voice(monkeypatch, nudge=1, tight=True)
    _stub_post_discord_text(monkeypatch)
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(
        app, ["--league", "99", "--week", "17", "--post", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    ranks = FileStore(tmp_path / "cache" / "commishdesk").read_published_rank("99", 17)
    assert ranks is not None
    assert len(ranks) == len(build_week_model(_load_fixture(_WEEK17)).rosters)


def test_weekly_reissue_stays_on_the_template_even_with_a_key(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Story 5.11c's reissue rebuilds the free template Issue: it must not re-spend
    on the Voice or overwrite the week's persisted published rank."""
    _stub_adapter(monkeypatch)
    voice = _stub_weekly_voice(monkeypatch, nudge=1)
    _stub_post_discord_text(monkeypatch)
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    out = ["--league", "100", "--week", "17", "--post", "--out-dir", str(tmp_path)]

    first = runner.invoke(app, out)
    assert first.exit_code == 0, first.output
    store = FileStore(tmp_path / "cache" / "commishdesk")
    original = store.read_published_rank("100", 17)
    assert original and len(voice.calls) == 1

    again = runner.invoke(app, [*out, "--reason", "fix a typo"])
    assert again.exit_code == 0, again.output
    assert len(voice.calls) == 1  # no second paid call
    assert store.read_published_rank("100", 17) == original


# --------------------------------------------------------------------------- #
# Story 5.13a: the nudge justification is persisted in the snapshot narration
# --------------------------------------------------------------------------- #


def _snapshot_power_rows(tmp_path, league: str) -> list[dict[str, Any]]:
    store = FileStore(tmp_path / "cache" / "commishdesk")
    snapshot = store.read_cache("weekly-facts-snapshot", f"{league}-17")
    assert snapshot is not None
    return snapshot["narration"]["power"]


def test_weekly_a_voiced_post_persists_each_deviations_justification_in_the_snapshot(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A confirmed voiced publish stamps the cited reason onto the persisted
    narration for exactly the rosters whose published rank left the model rank,
    and never stamps ``published_rank`` there (a reissue diff would read it as a
    moved number)."""
    _stub_adapter(monkeypatch)
    _stub_weekly_voice(monkeypatch, nudge=1)
    _stub_post_discord_text(monkeypatch)
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(
        app, ["--league", "101", "--week", "17", "--post", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    rows = _snapshot_power_rows(tmp_path, "101")
    ranks = FileStore(tmp_path / "cache" / "commishdesk").read_published_rank("101", 17)
    assert ranks is not None
    deviating = {r["roster_id"] for r in rows if ranks[r["roster_id"]] != r["model_rank"]}
    assert deviating, "the stub's nudge=1 must move at least one roster"
    stamped = {r["roster_id"] for r in rows if r["nudge_justification"]}
    assert stamped == deviating
    assert all(r["published_rank"] is None for r in rows)


def test_weekly_the_justification_is_stamped_only_after_check_narration(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The closed-world scan must never see a stamped reason (5.12 residual G2):
    every narration payload handed to ``check_narration`` has null justifications."""
    import commishdesk.narrate.safety as safety

    seen: list[list[str | None]] = []
    real = safety.check_narration

    def spy(text, narration, *args, **kwargs):
        seen.append([row.nudge_justification for row in narration.power])
        return real(text, narration, *args, **kwargs)

    monkeypatch.setattr(safety, "check_narration", spy)
    _stub_adapter(monkeypatch)
    _stub_weekly_voice(monkeypatch, nudge=1)
    _stub_post_discord_text(monkeypatch)
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(
        app, ["--league", "102", "--week", "17", "--post", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert seen and all(j is None for call in seen for j in call)
    assert any(r["nudge_justification"] for r in _snapshot_power_rows(tmp_path, "102"))


def test_weekly_a_template_post_leaves_every_justification_null(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No LLM, no deviation, no reason — and none is invented."""
    _stub_adapter(monkeypatch)
    _stub_post_discord_text(monkeypatch)
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(
        app, ["--league", "103", "--week", "17", "--post", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert all(r["nudge_justification"] is None for r in _snapshot_power_rows(tmp_path, "103"))


# --------------------------------------------------------------------------- #
# Story 5.14a — the designed weekly page
# --------------------------------------------------------------------------- #


def test_weekly_week10_run_writes_the_designed_page(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The weekly run writes the designed, self-contained page (every section of
    a regular-season week) in place of the generic dump; the text Issue is kept."""
    _stub_adapter(monkeypatch, week_bundle=_load_fixture(_WEEK10), nfl_state={"week": 13, "season_type": "regular"})
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(app, ["--league", "110", "--week", "10", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    body = (tmp_path / "commishdesk-110-weekly-week10.html").read_text(encoding="utf-8")
    assert 'class="paper weekly_issue"' in body
    for label in ("The lead", "Around the league", "Standings", "Power rankings", "The luck index", "Next week"):
        assert f'aria-label="{label}"' in body, label
    assert "Nudged" not in body  # the template narrator publishes no rank
    assert (tmp_path / "commishdesk-110-weekly-week10.txt").is_file()
    # Story 5.14b: the email pair sits next to the page; the .txt Issue is unchanged.
    email_html = (tmp_path / "commishdesk-110-weekly-week10.email.html").read_text(encoding="utf-8")
    email_text = (tmp_path / "commishdesk-110-weekly-week10.email.txt").read_text(encoding="utf-8")
    assert email_html.startswith("<!DOCTYPE html>") and "<svg" not in email_html and "<script" not in email_html
    assert "THE LUCK INDEX" in email_text and "NEXT WEEK" in email_text
    text_issue = (tmp_path / "commishdesk-110-weekly-week10.txt").read_text(encoding="utf-8")
    assert text_issue.startswith("Trench Warfare — Week 10 Recap\n") and "## The Lead" in text_issue


def test_weekly_week10_post_is_the_designed_post(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--week --post`` sends ``render_weekly_discord_post`` built from the same
    render doc as the page; the ledger and the snapshot are written as before."""
    _stub_adapter(monkeypatch, week_bundle=_load_fixture(_WEEK10), nfl_state={"week": 13, "season_type": "regular"})
    posted = _stub_post_discord_text(monkeypatch)
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(app, ["--league", "113", "--week", "10", "--post", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert len(posted) == 1
    content = posted[0][1]
    lines = content.split("\n")
    assert lines[0] == "## 🏈 Trench Warfare · Week 10"
    assert lines[1].endswith("** 🪑")
    for marker in ("📊 **Standings**", "── playoff line", "🏈 **Results**", "📈 **Power top 5**", "🍀 **Luck**",
                   "👀 **Next week**", "🔄 **Wire**", "🏆 Coach of the week"):
        assert marker in content, marker
    assert "Full Issue" not in content  # no hosted URL yet
    assert len(content.encode("utf-16-le")) // 2 <= 1940
    store = FileStore(tmp_path / "cache" / "commishdesk")
    assert [entry.kind for entry in store.read_ledger("113", 10)] == ["weekly"]
    assert store.read_cache("weekly-facts-snapshot", "113-10") is not None


def test_weekly_a_voiced_run_renders_its_published_ranks_and_cited_reasons(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With published ranks, the page renders from the narration stamped with
    them and with each deviation's reason parsed from the Issue text. Story
    5.14b: the email and the Discord post follow the same published order."""
    _stub_adapter(monkeypatch, week_bundle=_load_fixture(_WEEK10), nfl_state={"week": 13, "season_type": "regular"})
    _stub_weekly_voice(monkeypatch, nudge=1)
    posted = _stub_post_discord_text(monkeypatch)
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(app, ["--league", "111", "--week", "10", "--post", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    body = (tmp_path / "commishdesk-111-weekly-week10.html").read_text(encoding="utf-8")
    power = body.split('aria-label="Power rankings"', 1)[1].split("</section>", 1)[0]
    assert "Nudged down · model #1" in power
    # the cited reason is the rest of the narrated rank line, never invented
    assert '<span class="why">9-1-0, 202.87 points a week.</span>' in power
    email_text = (tmp_path / "commishdesk-111-weekly-week10.email.txt").read_text(encoding="utf-8")
    assert "Nudged down · model #1" in email_text and "9-1-0, 202.87 points a week." in email_text
    page_rows = re.findall(r'<span class="rk">#?(\d+)</span><span class="tn">(.*?)</span>', power)
    top = next(line for line in posted[0][1].split("\n") if line.startswith("📈"))
    # the page's published ranks and order (the stub shifts every rank, so the numbers matter)
    assert re.findall(r"(\d+)\. (.+?)(?= · |$)", top) == page_rows[:5]
    assert top.split("  ", 1)[1].startswith("2. ")


def test_weekly_a_tight_list_gives_every_nudged_row_its_own_reason(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reasons come from the raw completion: a tight list (no blank lines) is
    joined into one paragraph in the parsed Issue, which must not leak into the
    page (one reason swallowing the list) or into the snapshot."""
    _stub_adapter(monkeypatch, week_bundle=_load_fixture(_WEEK10), nfl_state={"week": 13, "season_type": "regular"})
    _stub_weekly_voice(monkeypatch, nudge=1, tight=True)
    _stub_post_discord_text(monkeypatch)
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(app, ["--league", "112", "--week", "10", "--post", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    body = (tmp_path / "commishdesk-112-weekly-week10.html").read_text(encoding="utf-8")
    power = body.split('aria-label="Power rankings"', 1)[1].split("</section>", 1)[0]
    reasons = re.findall(r'<span class="why">(.*?)</span>', power)
    assert len(reasons) == power.count('<p class="nudge">') > 1
    assert all(reason.count("points a week") == 1 for reason in reasons)
    snapshot = FileStore(tmp_path / "cache" / "commishdesk").read_cache("weekly-facts-snapshot", "112-10")
    assert snapshot is not None
    stamped = [r["nudge_justification"] for r in snapshot["narration"]["power"] if r["nudge_justification"]]
    assert len(stamped) == len(reasons)
    assert all(reason.count("points a week") == 1 for reason in stamped)


def test_the_draft_recap_page_is_not_the_weekly_design(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Story 5.14a leaves the draft-recap surfaces alone: its page is still Story
    4.1's ``render_web`` output (no weekly marker, no embedded fonts)."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    result = runner.invoke(app, ["--league", "demo", "--draft-recap", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    body = (tmp_path / "commishdesk-demo-draft-recap.html").read_text(encoding="utf-8")
    assert "weekly_issue" not in body and "@font-face" not in body
    assert '<header class="masthead">' in body and "Draft Recap" in body


# --------------------------------------------------------------------------- #
# Playoff seeding (Story 5.15): --seeding / COMMISHDESK_PLAYOFF_SEEDING
# --------------------------------------------------------------------------- #

_SEEDING_NOTE = "Seeding unconfirmed"


def _seeded_run(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    seeding: str | None = None,
    env: str | None = None,
    week: int = 17,
    league: str = "151",
    week_bundle: dict[str, Any] | None = None,
    league_bundle: dict[str, Any] | None = None,
):
    """A ``--post`` weekly run with the seeding channel under test. Returns the
    result, the store, the recorded posts and the adapter calls."""
    kwargs: dict[str, Any] = {}
    if week_bundle is not None:
        kwargs["week_bundle"] = week_bundle
        kwargs["nfl_state"] = {"week": 13, "season_type": "regular"}
    if league_bundle is not None:
        kwargs["league_bundle"] = league_bundle
    calls = _stub_adapter(monkeypatch, **kwargs)
    posted = _stub_post_discord_text(monkeypatch)
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    if env is not None:
        monkeypatch.setenv("COMMISHDESK_PLAYOFF_SEEDING", env)
    else:
        monkeypatch.delenv("COMMISHDESK_PLAYOFF_SEEDING", raising=False)
    args = ["--league", league, "--week", str(week), "--post", "--out-dir", str(tmp_path)]
    if seeding is not None:
        args += ["--seeding", seeding]
    result = runner.invoke(app, args)
    store = FileStore(tmp_path / "cache" / "commishdesk")
    return result, store, posted, calls


def _snapshot_picture(store: FileStore, league: str, week: int) -> dict[str, Any]:
    snapshot = store.read_cache("weekly-facts-snapshot", f"{league}-{week}")
    assert snapshot is not None
    picture = snapshot["standings"]["playoff_picture"]
    assert picture is not None
    return picture


def _assert_refused_without_side_effects(
    result: Any, store: FileStore, posted: list, tmp_path, league: str, week: int, *fragments: str
) -> None:
    assert result.exit_code == 1, result.output
    for fragment in fragments:
        assert fragment in result.output, result.output
    assert "Traceback" not in result.output
    assert posted == []
    assert store.read_ledger(league, week) == []
    assert store.read_storylines(league) == []
    assert store.read_cache("weekly-facts-snapshot", f"{league}-{week}") is None
    assert not list(tmp_path.glob("commishdesk-*"))


def test_weekly_playoff_week_without_seeding_notes_it_is_unconfirmed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, store, posted, _ = _seeded_run(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    picture = _snapshot_picture(store, "151", 17)
    assert picture["source"] == "derived" and picture["seeding_unconfirmed"] is True
    assert _SEEDING_NOTE in result.stdout
    assert _SEEDING_NOTE in (tmp_path / "commishdesk-151-weekly-week17.html").read_text(encoding="utf-8")
    assert _SEEDING_NOTE in posted[0][1]


def test_weekly_regular_season_without_seeding_carries_no_note(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, store, posted, _ = _seeded_run(
        tmp_path, monkeypatch, week=10, league="152", week_bundle=_load_fixture(_WEEK10)
    )
    assert result.exit_code == 0, result.output
    picture = _snapshot_picture(store, "152", 10)
    assert picture["source"] == "derived" and picture["seeding_unconfirmed"] is False
    assert _SEEDING_NOTE not in result.stdout and _SEEDING_NOTE not in posted[0][1]


def test_weekly_confirm_marks_the_picture_confirmed_and_drops_the_note(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, store, posted, _ = _seeded_run(tmp_path, monkeypatch, seeding="confirm")
    assert result.exit_code == 0, result.output
    picture = _snapshot_picture(store, "151", 17)
    assert picture["source"] == "confirmed" and picture["seeding_unconfirmed"] is False
    assert _SEEDING_NOTE not in result.stdout and _SEEDING_NOTE not in posted[0][1]


def test_weekly_a_valid_list_becomes_the_seeds_and_survives_regeneration(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeds = "12, 7,3 ,1,9,5"
    result, store, posted, _ = _seeded_run(tmp_path, monkeypatch, seeding=seeds)
    assert result.exit_code == 0, result.output
    picture = _snapshot_picture(store, "151", 17)
    assert picture["source"] == "commissioner" and picture["seeding_unconfirmed"] is False
    assert picture["in_bracket"] == ["12", "7", "3", "1", "9", "5"]
    assert picture["byes"] == ["12", "7"]
    assert _SEEDING_NOTE not in result.stdout

    # The reissue reads the same option and keeps the override.
    reissued = runner.invoke(
        app,
        [
            "--league", "151", "--week", "17", "--post", "--seeding", seeds,
            "--reason", "seeding fix", "--out-dir", str(tmp_path),
        ],
    )
    assert reissued.exit_code == 0, reissued.output
    assert len(posted) == 2
    picture = _snapshot_picture(store, "151", 17)
    assert picture["in_bracket"] == ["12", "7", "3", "1", "9", "5"]
    assert picture["source"] == "commissioner"
    assert _SEEDING_NOTE not in reissued.stdout and _SEEDING_NOTE not in posted[1][1]


def test_weekly_the_seeding_env_var_is_the_same_channel(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, store, _, _ = _seeded_run(tmp_path, monkeypatch, env="12,7,3,1,9,5")
    assert result.exit_code == 0, result.output
    assert _snapshot_picture(store, "151", 17)["in_bracket"] == ["12", "7", "3", "1", "9", "5"]


@pytest.mark.parametrize("channel", ["seeding", "env"])
@pytest.mark.parametrize("value", ["", "   "])
def test_weekly_an_empty_or_blank_value_is_no_override(
    tmp_path, monkeypatch: pytest.MonkeyPatch, value: str, channel: str
) -> None:
    result, store, _, _ = _seeded_run(tmp_path, monkeypatch, league="153", **{channel: value})
    assert result.exit_code == 0, result.output
    picture = _snapshot_picture(store, "153", 17)
    assert picture["source"] == "derived" and picture["seeding_unconfirmed"] is True
    assert _SEEDING_NOTE in result.stdout


@pytest.mark.parametrize("channel", ["seeding", "env"])
@pytest.mark.parametrize(
    ("value", "fragments"),
    [
        ("12,7,99,1,9,5", ("99", "not a roster")),
        ("12,7,12,1,9,5", ("duplicate", "12")),
        ("12,7,3,1,9", ("6", "5")),
        ("12,7,3,1,9,5,2", ("6", "7")),
        ("12,,3,1,9,5", ("empty entry",)),
        ("confirm,4", ("confirm",)),
    ],
)
def test_weekly_an_invalid_seeding_exits_1_naming_the_problem_with_no_side_effects(
    tmp_path, monkeypatch: pytest.MonkeyPatch, value: str, fragments: tuple[str, ...], channel: str
) -> None:
    client = _stub_weekly_voice(monkeypatch)
    result, store, posted, _ = _seeded_run(tmp_path, monkeypatch, league="154", **{channel: value})
    _assert_refused_without_side_effects(result, store, posted, tmp_path, "154", 17, *fragments)
    assert client.calls == []


def test_weekly_a_malformed_seeding_is_refused_before_any_fetch(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _, posted, calls = _seeded_run(tmp_path, monkeypatch, seeding="12,,3")
    assert result.exit_code == 1, result.output
    assert "empty entry" in result.output
    assert calls["fetch"] == [] and calls["fetch_week"] == []
    assert posted == []


def test_weekly_a_league_with_no_playoff_format_rejects_a_supplied_seeding(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _league_bundle_with(monkeypatch)
    no_playoffs = {
        **base,
        "league": {**base["league"], "settings": {**base["league"]["settings"], "playoff_teams": 0}},
    }
    result, store, posted, _ = _seeded_run(
        tmp_path, monkeypatch, seeding="confirm", league="155", league_bundle=no_playoffs
    )
    _assert_refused_without_side_effects(result, store, posted, tmp_path, "155", 17, "no playoff format")

    # ...while none supplied is a no-op.
    sub = tmp_path / "none"
    sub.mkdir()
    ok, _, _, _ = _seeded_run(sub, monkeypatch, league="156", league_bundle=no_playoffs)
    assert ok.exit_code == 0, ok.output


# --------------------------------------------------------------------------- #
# Story 5.16 — the Week-1 cold-start Issue, end to end
# --------------------------------------------------------------------------- #

_WEEK01 = "week01-openers.json"
_COLD_START_HEADINGS = ("The Lead", "Around the League", "Standings", "Next Week")
_STOOD_DOWN_HEADINGS = (
    "Power Rankings",
    "The Luck Index",
    "The Transaction Desk",
    "Playoff Picture",
    "Standings and the Playoff Picture",
)


def _point_in_time_week01() -> dict[str, Any]:
    """``week01-openers.json`` with each roster's Sleeper totals made
    point-in-time (the week-1 record and points, as ``week10-blowout.json`` does
    for week 10), so the standings cross-check passes and ``--post`` is allowed."""
    bundle = _load_fixture(_WEEK01)
    games = bundle["matchups"]["1"]
    by_id: dict[int, list[dict[str, Any]]] = {}
    for game in games:
        by_id.setdefault(game["matchup_id"], []).append(game)
    for roster in bundle["rosters"]:
        mine = next(g for g in games if g["roster_id"] == roster["roster_id"])
        other = next(g for g in by_id[mine["matchup_id"]] if g is not mine)
        points = round(mine["points"], 2)
        whole, hundredths = divmod(round(points * 100), 100)
        settings = roster["settings"]
        settings["wins"] = int(mine["points"] > other["points"])
        settings["losses"] = int(mine["points"] < other["points"])
        settings["ties"] = int(mine["points"] == other["points"])
        settings["fpts"], settings["fpts_decimal"] = whole, hundredths
    return bundle


def _stub_week01(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _stub_adapter(
        monkeypatch,
        week_bundle=_point_in_time_week01(),
        nfl_state={"week": 3, "season_type": "regular"},
    )
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


class _ColdStartVoiceClient:
    """A fake client that records the Voice it was handed and returns *reply*
    (or, when ``reply`` is ``None``, the template Issue for the payload it got)."""

    def __init__(self, reply: str | None = None) -> None:
        self.reply = reply
        self.voices: list[object] = []
        self.calls: list[str] = []

    def generate(self, payload: str, voice: object) -> str:
        self.calls.append(payload)
        self.voices.append(voice)
        if self.reply is not None:
            return self.reply
        narration = WeeklyNarration.model_validate_json(payload)
        return weekly_issue_to_text(render_weekly_issue(narration))


def _stub_cold_start_client(
    monkeypatch: pytest.MonkeyPatch, reply: str | None = None
) -> _ColdStartVoiceClient:
    client = _ColdStartVoiceClient(reply)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("COMMISHDESK_COST_CEILING_USD", "100")
    monkeypatch.setattr("commishdesk.narrate.llm.build_client", lambda cfg: client)
    return client


def _issue_headings(text: str) -> list[str]:
    return re.findall(r"^## (.+)$", text, flags=re.MULTILINE)


def test_weekly_week01_template_run_yields_only_the_four_cold_start_sections(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix: Week 1, template + every surface. The text Issue, page, email pair
    and Discord post carry no Power/Luck/Transaction/playoff heading."""
    _stub_week01(monkeypatch, tmp_path)
    posted = _stub_post_discord_text(monkeypatch)
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)

    result = runner.invoke(app, ["--league", "901", "--week", "1", "--post", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output

    text_issue = (tmp_path / "commishdesk-901-weekly-week1.txt").read_text(encoding="utf-8")
    assert _issue_headings(text_issue) == list(_COLD_START_HEADINGS)
    assert _issue_headings(result.stdout) == list(_COLD_START_HEADINGS)

    page = (tmp_path / "commishdesk-901-weekly-week1.html").read_text(encoding="utf-8")
    email_html = (tmp_path / "commishdesk-901-weekly-week1.email.html").read_text(encoding="utf-8")
    email_text = (tmp_path / "commishdesk-901-weekly-week1.email.txt").read_text(encoding="utf-8")
    assert 'aria-label="The lead"' in page and 'aria-label="Standings"' in page
    for label in ("Power rankings", "The luck index", "Transaction", "Playoff"):
        assert f'aria-label="{label}' not in page, label
    assert len(posted) == 1
    discord = posted[0][1]
    for surface in (page, email_html, email_text, discord, text_issue):
        lowered = surface.lower()
        for stood_down in ("power rankings", "the luck index", "transaction desk", "playoff picture"):
            assert stood_down not in lowered, stood_down
    for marker in ("📈", "🍀", "🔄"):
        assert marker not in discord, marker
    assert "📊 **Standings**" in discord and "🏈 **Results**" in discord
    assert "STANDINGS" in email_text


def test_weekly_week01_llm_run_selects_the_cold_start_voice(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from commishdesk.voices.beat_writer import BEAT_WRITER_WEEKLY_COLD_START

    _stub_week01(monkeypatch, tmp_path)
    client = _stub_cold_start_client(monkeypatch)

    result = runner.invoke(app, ["--league", "902", "--week", "1", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert client.voices and all(v is BEAT_WRITER_WEEKLY_COLD_START for v in client.voices)
    text_issue = (tmp_path / "commishdesk-902-weekly-week1.txt").read_text(encoding="utf-8")
    assert _issue_headings(text_issue) == list(_COLD_START_HEADINGS)


def test_weekly_week01_malformed_llm_completion_degrades_to_the_template(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seven-section (warm) completion at Week 1 is rejected by the parser and
    the run delivers the template Issue instead of failing."""
    warm = "\n\n".join(f"## {h}\n\nSomething about the league." for h in WEEKLY_SECTION_HEADINGS)
    _stub_week01(monkeypatch, tmp_path)
    client = _stub_cold_start_client(monkeypatch, reply=warm)

    result = runner.invoke(app, ["--league", "903", "--week", "1", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert client.calls  # the LLM path was tried
    text_issue = (tmp_path / "commishdesk-903-weekly-week1.txt").read_text(encoding="utf-8")
    assert _issue_headings(text_issue) == list(_COLD_START_HEADINGS)
    assert "Something about the league" not in text_issue


def test_weekly_week01_post_after_a_draft_recap_is_not_blocked_and_is_idempotent(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from commishdesk.store import LedgerEntry

    _stub_week01(monkeypatch, tmp_path)
    posted = _stub_post_discord_text(monkeypatch)
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    store = FileStore(tmp_path / "cache" / "commishdesk")
    store.append_ledger_entry(
        LedgerEntry(
            league_id="904",
            week=1,
            channel="discord",
            recipient=_WEBHOOK_ID,
            kind="draft_recap",
            sent_at=datetime(2026, 9, 1, tzinfo=UTC),
        )
    )

    args = ["--league", "904", "--week", "1", "--post", "--out-dir", str(tmp_path)]
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.output
    assert len(posted) == 1
    assert [(e.kind, e.status) for e in store.read_ledger("904", 1)] == [
        ("draft_recap", "confirmed"),
        ("weekly", "confirmed"),
    ]

    second = runner.invoke(app, args)
    assert second.exit_code == 0, second.output
    assert len(posted) == 1  # idempotent: the confirmed weekly entry blocks a double post
    assert [e.kind for e in store.read_ledger("904", 1)] == ["draft_recap", "weekly"]
