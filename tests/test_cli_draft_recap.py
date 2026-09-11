"""Story 2.7 — ``commishdesk --league demo --draft-recap`` end to end.

Exit 0, every section heading + sample claims on stdout, an HTML file written to
``--out-dir``, two-run byte-identity modulo ``generated_at``, the
``--draft-recap`` + ``--week`` usage error, ``--version``, and a clean typed
error when the demo fixture is absent.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

import commishdesk
from commishdesk.cli import app
from commishdesk.errors import CommishDeskError, DeliveryError
from commishdesk.narrate import safety

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parent.parent

_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T[0-9:.]+Z")

SECTION_HEADINGS = (
    "The Lead",
    "The Board — Round 1",
    "Superlatives",
    "Team Grades",
    "Positional Read",
    "The Picks We'll Be Arguing About in December",
)


@pytest.fixture(autouse=True)
def _no_ambient_content_hold_override(monkeypatch) -> None:
    """``COMMISHDESK_ALLOW_CONTENT_HOLD`` must never leak in from the developer's
    shell or a CI image — ``.env.example`` now tells operators to export it, and
    every fail-closed assertion in this module (the hold tests) would silently
    invert if it were set. Each test opts in explicitly instead.

    ``monkeypatch.delenv`` edits ``os.environ``, so the ``_run`` subprocess helper
    inherits the cleaned environment too."""
    monkeypatch.delenv("COMMISHDESK_ALLOW_CONTENT_HOLD", raising=False)


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; sys.argv={['commishdesk', *args]!r}; "
            "from commishdesk.cli import main; main()",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        cwd=str(cwd or REPO_ROOT),
    )


def test_demo_draft_recap_runs_end_to_end(tmp_path: Path) -> None:
    result = _run("--league", "demo", "--draft-recap", "--out-dir", str(tmp_path))
    assert result.returncode == 0, result.stderr

    for heading in SECTION_HEADINGS:
        assert heading in result.stdout
    assert "Trench Warfare" in result.stdout  # the league name
    assert "Five of the first eleven picks" in result.stdout
    assert "Ashton Jeanty went 1.01." in result.stdout  # the 1.01 player
    for letter in ("A+", "A-", "B+"):  # a spread of grade letters
        assert letter in result.stdout

    html_file = tmp_path / "commishdesk-demo-draft-recap.html"
    assert html_file.is_file()
    assert str(html_file) in result.stdout
    body = html_file.read_text(encoding="utf-8")
    assert body.startswith("<!doctype html>")
    assert "<h1" in body and "<h2>" in body
    assert body.count("<style>") == 1 and "<svg" in body  # the Story 4.1 designed page

    # Story 4.2: the email-safe HTML + the text/plain alternative, both echoed
    email_html = tmp_path / "commishdesk-demo-draft-recap.email.html"
    email_text = tmp_path / "commishdesk-demo-draft-recap.txt"
    assert email_html.is_file() and str(email_html) in result.stdout
    assert email_text.is_file() and str(email_text) in result.stdout
    email_body = email_html.read_text(encoding="utf-8")
    assert email_body.startswith("<!DOCTYPE html>")
    assert '<table role="presentation"' in email_body
    for token in ("<svg", "<img", "<script", "var(--", "url("):
        assert token not in email_body, token
    text_body = email_text.read_text(encoding="utf-8")
    assert "THE BOARD" in text_body and "PICKS PER TEAM" in text_body
    assert text_body.strip()


def test_two_demo_runs_are_byte_identical_modulo_generated_at(tmp_path: Path) -> None:
    out_a, out_b = tmp_path / "a", tmp_path / "b"
    r1 = _run("--league", "demo", "--draft-recap", "--out-dir", str(out_a))
    r2 = _run("--league", "demo", "--draft-recap", "--out-dir", str(out_b))
    assert r1.returncode == 0 and r2.returncode == 0

    def _mask(text: str) -> str:
        text = _STAMP.sub("<STAMP>", text)
        return text.replace(str(out_a), "<OUT>").replace(str(out_b), "<OUT>")

    assert _mask(r1.stdout) == _mask(r2.stdout)

    html1 = (out_a / "commishdesk-demo-draft-recap.html").read_text(encoding="utf-8")
    html2 = (out_b / "commishdesk-demo-draft-recap.html").read_text(encoding="utf-8")
    assert _STAMP.sub("<STAMP>", html1) == _STAMP.sub("<STAMP>", html2)
    # and the timestamp really is present (so the mask is not masking nothing)
    assert _STAMP.search(html1)

    # Story 4.2: the email HTML and the text/plain part both carry the generation
    # stamp and are byte-deterministic modulo it
    for name in ("commishdesk-demo-draft-recap.email.html", "commishdesk-demo-draft-recap.txt"):
        a = (out_a / name).read_text(encoding="utf-8")
        b = (out_b / name).read_text(encoding="utf-8")
        assert _STAMP.sub("<STAMP>", a) == _STAMP.sub("<STAMP>", b), name
        assert _STAMP.search(a), name


def test_demo_writes_the_designed_self_contained_page(tmp_path: Path) -> None:
    """Story 4.1 — the written HTML is the designed page: one ``<!doctype html>``
    document, one inline ``<style>``, three inline ``<svg>`` charts, LF-only on
    disk, and no external sub-resource construct anywhere."""
    result = _run("--league", "demo", "--draft-recap", "--out-dir", str(tmp_path))
    assert result.returncode == 0, result.stderr
    path = tmp_path / "commishdesk-demo-draft-recap.html"
    body = path.read_text(encoding="utf-8")
    assert body.startswith("<!doctype html>")
    assert body.count("<style>") == 1 and "<script" not in body
    assert body.count("<svg") == 3  # grid + pick-count bar + positional timeline
    # no actual sub-resource construct (not a blunt "no http anywhere")
    assert "<link " not in body and " src=" not in body and " srcset=" not in body
    assert "@import" not in body
    style = body.split("<style>", 1)[1].split("</style>", 1)[0]
    assert "url(" not in style and "http://" not in style and "https://" not in style
    assert not re.search(r'=\s*"[^"]*//[^"/]', body)  # no protocol-relative host in an attr
    for target in re.findall(r'href="([^"]*)"', body):
        assert target.startswith("#"), target
    assert b"\r\n" not in path.read_bytes()  # LF-only
    # Story 4.2: the two email surfaces are LF-only on disk too
    assert b"\r\n" not in (tmp_path / "commishdesk-demo-draft-recap.email.html").read_bytes()
    assert b"\r\n" not in (tmp_path / "commishdesk-demo-draft-recap.txt").read_bytes()


def test_draft_recap_with_week_is_a_usage_error() -> None:
    result = runner.invoke(app, ["--league", "demo", "--draft-recap", "--week", "5"])
    assert result.exit_code == 2
    assert "Traceback" not in result.output
    assert "--week" in result.output


def test_version_flag_exits_zero() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"commishdesk {commishdesk.__version__}"


def test_version_short_form_matches_long_form() -> None:
    long_form = runner.invoke(app, ["--version"])
    short_form = runner.invoke(app, ["-V"])
    assert short_form.exit_code == 0
    assert short_form.output == long_form.output


def test_draft_recap_without_league_is_a_usage_error() -> None:
    result = runner.invoke(app, ["--draft-recap"])
    assert result.exit_code == 2
    assert "Traceback" not in result.output
    assert "--league" in result.output


def test_demo_fixture_absent_raises_a_clean_typed_error(monkeypatch) -> None:
    """Matrix row: ``--league demo`` from a tree with no ``tests/fixtures/``
    (an installed wheel) — a typed ``CommishDeskError``, not a traceback."""
    from commishdesk import demo

    monkeypatch.setattr(demo, "_DRAFT_FIXTURE", "tests/fixtures/not-a-real-file.json")
    for loader in (demo.load_demo_bundle, demo.demo_consensus_slots):
        with pytest.raises(CommishDeskError) as excinfo:
            loader()
        assert "source checkout" in str(excinfo.value)

    result = runner.invoke(app, ["--league", "demo", "--draft-recap"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output


def test_corrupt_demo_fixture_is_a_clean_typed_error(monkeypatch, tmp_path) -> None:
    from commishdesk import demo

    bad = tmp_path / "corrupt.json"
    bad.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(demo, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(demo, "_DRAFT_FIXTURE", "corrupt.json")
    with pytest.raises(CommishDeskError) as excinfo:
        demo.load_demo_bundle()
    assert "corrupt" in str(excinfo.value)


def test_cli_honours_an_empty_generation_set(tmp_path: Path, monkeypatch) -> None:
    """A bypass like ``for resolved in [league]`` would pass every other test;
    this proves the CLI actually routes through the Generation Set."""
    from commishdesk import generation

    monkeypatch.setattr(
        generation,
        "build_generation_set",
        lambda *a, **k: generation.GenerationSet(()),
    )
    result = runner.invoke(
        app, ["--league", "demo", "--draft-recap", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "not activated" in result.output
    assert "Traceback" not in result.output
    for heading in SECTION_HEADINGS:
        assert heading not in result.output
    assert not list(tmp_path.iterdir())


def test_real_league_branch_threads_the_mocked_consensus_source(
    tmp_path: Path, monkeypatch
) -> None:
    """Exercise the network branch past ``adapter.fetch``: a fake adapter returns
    the demo bundle, a fake ``build_consensus_rank`` supplies known
    ``source`` / ``as_of``, and the CLI must thread those into the Facts JSON."""
    import commishdesk.facts as facts_pkg
    from commishdesk import consensus, demo
    from commishdesk.consensus import ConsensusRank

    # the real-league branch now reads/writes the storyline store; keep it in tmp
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    bundle = demo.load_demo_bundle()

    class _FakeAdapter:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def fetch(self, league_id: str) -> dict:
            return bundle

        def close(self) -> None: ...

    fake_rank = ConsensusRank(
        source="fantasycalc", as_of="2099-07", slots=demo.demo_consensus_slots()
    )
    monkeypatch.setattr("commishdesk.adapters.sleeper.SleeperAdapter", _FakeAdapter)
    monkeypatch.setattr(consensus, "build_consensus_rank", lambda *a, **k: fake_rank)

    seen: dict[str, object] = {}
    real_build = facts_pkg.build_draft_recap_facts

    def _spy(*args: object, **kwargs: object):
        seen.update(kwargs)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(facts_pkg, "build_draft_recap_facts", _spy)

    result = runner.invoke(
        app, ["--league", "999", "--draft-recap", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert seen["consensus_source_name"] == "fantasycalc"
    assert seen["consensus_as_of"] == "2099-07"
    assert (tmp_path / "commishdesk-999-draft-recap.html").is_file()


def _fake_real_league(monkeypatch) -> None:
    """Wire the real-league branch to run fully offline off the demo bundle."""
    from commishdesk import consensus, demo
    from commishdesk.consensus import ConsensusRank

    bundle = demo.load_demo_bundle()

    class _FakeAdapter:
        def __init__(self, *a: object, **k: object) -> None: ...

        def fetch(self, league_id: str) -> dict:
            return bundle

        def close(self) -> None: ...

    fake_rank = ConsensusRank(
        source="fantasycalc", as_of="2099-07", slots=demo.demo_consensus_slots()
    )
    monkeypatch.setattr("commishdesk.adapters.sleeper.SleeperAdapter", _FakeAdapter)
    monkeypatch.setattr(consensus, "build_consensus_rank", lambda *a, **k: fake_rank)


def test_storylines_round_trip_across_two_real_league_runs(tmp_path: Path, monkeypatch) -> None:
    """Two non-demo runs for one league: the second reads the storylines the
    first persisted, and continuity fields (``first_week``) carry over — proving
    the read -> advance -> write wiring is not a no-op."""
    import json as _json

    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    store_file = tmp_path / "commishdesk" / "storylines" / "42.json"

    r1 = runner.invoke(app, ["--league", "42", "--draft-recap", "--out-dir", str(tmp_path)])
    assert r1.exit_code == 0, r1.output
    assert store_file.is_file(), "first run must persist the league's storylines"
    persisted = _json.loads(store_file.read_text(encoding="utf-8"))
    assert persisted and all(s["league_id"] == "42" for s in persisted)

    # tamper the continuity span (first_week <= last_week must hold); a working
    # read path must carry it through untouched
    for entry in persisted:
        entry["first_week"] = 3
        entry["last_week"] = 9
    store_file.write_text(_json.dumps(persisted), encoding="utf-8")

    r2 = runner.invoke(app, ["--league", "42", "--draft-recap", "--out-dir", str(tmp_path)])
    assert r2.exit_code == 0, r2.output
    after = _json.loads(store_file.read_text(encoding="utf-8"))
    assert {s["id"] for s in after} == {s["id"] for s in persisted}
    assert all(s["first_week"] == 3 for s in after), "second run reset first_week — wiring is a no-op"


def test_demo_run_touches_no_storyline_store(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    result = _run("--league", "demo", "--draft-recap", "--out-dir", str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "commishdesk" / "storylines").exists()


def test_real_league_id_with_no_network_is_exit_1_no_traceback() -> None:
    """A bogus Sleeper id cannot be fetched; the CLI prints a one-line message
    and exits 1 (no traceback)."""
    result = _run("--league", "0", "--draft-recap")
    assert result.returncode == 1
    assert "Traceback" not in result.stderr


# --------------------------------------------------------------------------- #
# Story 3.3 — the --llm / --no-llm narrator selection
# --------------------------------------------------------------------------- #

_KEY_VARS = ("ANTHROPIC_API_KEY", "LLM_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")


def test_no_llm_flag_suppresses_generation_even_with_a_working_provider(
    tmp_path: Path, monkeypatch
) -> None:
    """``--no-llm`` with a key set AND a working (fake) provider: the recap is the
    structured template render, and the fake model's reply appears nowhere — a
    regression that ignored ``--no-llm`` would emit it and fail here."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-a-real-key")
    _install_fake_anthropic(monkeypatch, "FAKE-MODEL-REPLY-SENTINEL should never appear")
    result = runner.invoke(
        app, ["--league", "88", "--draft-recap", "--no-llm", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    for heading in SECTION_HEADINGS:
        assert heading in result.output
    assert "FAKE-MODEL-REPLY-SENTINEL" not in result.output
    body = (tmp_path / "commishdesk-88-draft-recap.html").read_text(encoding="utf-8")
    assert "<h2>Superlatives</h2>" in body  # the structured template render
    assert "FAKE-MODEL-REPLY-SENTINEL" not in body


@pytest.mark.parametrize(
    "flag, env, expected",
    [
        (True, {}, True),
        (False, {"ANTHROPIC_API_KEY": "sk"}, False),
        (None, {}, False),
        (None, {"ANTHROPIC_API_KEY": "sk"}, True),
        (None, {"GEMINI_API_KEY": "  "}, False),  # blank is not "set"
        (None, {"LLM_API_KEY": "x"}, True),
    ],
)
def test_llm_enabled_helper(flag, env, expected, monkeypatch) -> None:
    from commishdesk.cli import _llm_enabled

    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert _llm_enabled(flag) is expected


def test_malformed_llm_config_is_exit_1_before_any_league_runs(
    tmp_path: Path, monkeypatch
) -> None:
    """A bad ``COMMISHDESK_LLM_*`` value raises ``NarratorError`` (a
    ``CommishDeskError``) from ``load_llm_config`` before the league loop — one
    line, exit 1, no recap and no HTML. Tested on a real (faked) league, since
    the demo path never loads LLM config at all."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("COMMISHDESK_LLM_PRIMARY", "bogus-no-colon")
    result = runner.invoke(
        app, ["--league", "99", "--draft-recap", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    for heading in SECTION_HEADINGS:
        assert heading not in result.output
    assert not list(tmp_path.iterdir())  # nothing written — the fault is pre-loop


def test_demo_run_tolerates_a_malformed_llm_config_and_warns_once(
    tmp_path: Path, monkeypatch
) -> None:
    """The zero-credential onboarding/smoke path never *depends* on LLM config: a
    broken ``COMMISHDESK_LLM_*`` on ``--league demo`` is not fatal — the run
    still exits 0 and prints the template recap. It is parsed exactly once, only
    to emit a single warning that it is being ignored (Story 3.5)."""
    monkeypatch.setenv("COMMISHDESK_LLM_PRIMARY", "bogus-no-colon")
    result = runner.invoke(
        app, ["--league", "demo", "--draft-recap", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    for heading in SECTION_HEADINGS:
        assert heading in result.output
    assert (tmp_path / "commishdesk-demo-draft-recap.html").is_file()
    assert result.output.count("ignoring a malformed COMMISHDESK_LLM_") == 1


def test_demo_is_always_template_even_with_a_provider_key(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-a-real-key")
    result = _run("--league", "demo", "--draft-recap", "--out-dir", str(tmp_path))
    assert result.returncode == 0, result.stderr
    for heading in SECTION_HEADINGS:
        assert heading in result.stdout
    body = (tmp_path / "commishdesk-demo-draft-recap.html").read_text(encoding="utf-8")
    assert "<h2>Superlatives</h2>" in body
    # item 9: the override of an implied LLM request is announced
    assert "--league demo always uses the template narrator" in result.stderr


def test_demo_without_a_key_does_not_announce_a_narrator_override(
    tmp_path: Path, monkeypatch
) -> None:
    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    result = _run("--league", "demo", "--draft-recap", "--out-dir", str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "always uses the template narrator" not in result.stderr


def test_demo_with_a_key_imports_no_provider_sdk(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-a-real-key")
    prog = (
        "import sys; sys.argv = ['commishdesk', '--league', 'demo', '--draft-recap',"
        f" '--out-dir', {str(tmp_path)!r}];"
        "from commishdesk.cli import main\n"
        "try:\n    main()\nexcept SystemExit as exc:\n    assert not exc.code, exc.code\n"
        "bad = {'anthropic', 'google.genai'} & sys.modules.keys()\n"
        "assert not bad, sorted(bad)\n"
        "print('ok')\n"
    )
    probe = subprocess.run(
        [sys.executable, "-c", prog],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        cwd=str(REPO_ROOT),
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip().splitlines()[-1] == "ok"


def test_llm_flag_without_a_key_falls_back_to_the_template(
    tmp_path: Path, monkeypatch
) -> None:
    """``--llm`` with no key and no SDK: the adapters raise, ``narrate_draft_recap``
    swallows it, and the run finishes on the structured template path — exit 0,
    no bare text dump."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    result = runner.invoke(
        app, ["--league", "55", "--draft-recap", "--llm", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    for heading in SECTION_HEADINGS:
        assert heading in result.output
    body = (tmp_path / "commishdesk-55-draft-recap.html").read_text(encoding="utf-8")
    assert "<h2>Superlatives</h2>" in body


def _install_fake_anthropic(monkeypatch, reply: str) -> dict:
    """A minimal ``anthropic`` stand-in: ``Anthropic().messages.create(...)``
    returns one text block with *reply* and a clean stop reason. Returns a dict
    whose ``["create"]`` counts generation calls (one paid call each — I3)."""
    module = types.ModuleType("anthropic")
    calls = {"create": 0}

    class _Messages:
        def create(self, **_kw: object) -> object:
            calls["create"] += 1
            return types.SimpleNamespace(
                stop_reason="end_turn",
                content=[types.SimpleNamespace(type="text", text=reply)],
            )

    class _Anthropic:
        def __init__(self, **_kw: object) -> None:
            self.messages = _Messages()

    module.Anthropic = _Anthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return calls


def _six_section_llm_text(*extra_blocks: str) -> str:
    """A structurally-valid LLM completion — all six canonical ``## `` headings,
    plus any *extra_blocks* appended as their own paragraphs."""
    lines = ["Trench Warfare Draft Recap", ""]
    for heading in SECTION_HEADINGS:
        # lowercase filler so no token trips the closed-world hallucination check
        lines += [f"## {heading}", "", "nothing here is worth flagging at all.", ""]
    for block in extra_blocks:
        lines += [block, ""]
    return "\n".join(lines)


def _stub_narrator(monkeypatch, text: str, narrator: str = "llm-primary") -> dict:
    """Replace ``narrate_draft_recap`` with a call-counting stub returning a fixed
    ``NarrationResult``. Returns a dict whose ``["n"]`` is the invocation count."""
    from commishdesk.narrate import NarrationResult

    calls = {"n": 0}

    def _fake(*_a: object, **_k: object) -> NarrationResult:
        calls["n"] += 1
        return NarrationResult(text=text, narrator=narrator)

    monkeypatch.setattr("commishdesk.narrate.llm.narrate_draft_recap", _fake)
    return calls


def test_unsafe_narrator_output_holds_the_league_exit_1_no_html(
    tmp_path: Path, monkeypatch
) -> None:
    """AD-12 L3 — a structurally-valid LLM narration with a manager's name beside
    a banned-category term: ``ContentSafetyError`` from ``_produce_issue``, one
    stderr line naming "content-safety hold", exit 1, and no HTML."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _stub_narrator(
        monkeypatch,
        _six_section_llm_text("Pull-Guard Pumas clearly drafted hungover this year."),
    )
    result = runner.invoke(
        app, ["--league", "70", "--draft-recap", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "content-safety hold" in result.output
    assert not (tmp_path / "commishdesk-70-draft-recap.html").is_file()
    for heading in SECTION_HEADINGS:
        assert heading not in result.output


def test_slop_on_llm_output_degrades_to_template_with_an_alert(
    tmp_path: Path, monkeypatch
) -> None:
    """AD-12 L3 — a slop phrase in a structurally-valid LLM narration is a
    ``suppress_section`` tier: degrade to the template narrator, emit a
    ``logger.error`` + a distinct stderr alert naming the league, exit 0 + HTML."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    calls = _stub_narrator(
        monkeypatch,
        _six_section_llm_text("Honestly, make no mistake, this draft had chaos."),
    )
    result = runner.invoke(
        app, ["--league", "71", "--draft-recap", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert calls["n"] == 1  # a suppress tier must NOT consume the regeneration budget
    body = (tmp_path / "commishdesk-71-draft-recap.html").read_text(encoding="utf-8")
    assert "<h2>Superlatives</h2>" in body  # the structured template render shipped
    assert "content-safety alert for league 71" in result.output
    assert "slop" in result.output


def test_safety_hold_lists_every_hold_finding(tmp_path: Path, monkeypatch) -> None:
    """AD-12 L3 — two holds in one narration: the exit-1 message names both."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _stub_narrator(
        monkeypatch,
        _six_section_llm_text(
            "Pull-Guard Pumas clearly drafted hungover. "
            "Blitz Alpacas is an idiot, frankly."
        ),
    )
    result = runner.invoke(
        app, ["--league", "72", "--draft-recap", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "hungover" in result.output and "idiot" in result.output
    assert not (tmp_path / "commishdesk-72-draft-recap.html").is_file()


def test_demo_draft_recap_passes_the_safety_gate(tmp_path: Path) -> None:
    """Story 3.4 — the gate runs on the demo path, does not hold, exit 0 + HTML,
    and emits no content-safety warning."""
    result = _run("--league", "demo", "--draft-recap", "--out-dir", str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "content-safety" not in result.stderr
    assert (tmp_path / "commishdesk-demo-draft-recap.html").is_file()


def test_llm_narrator_path_emits_text_and_the_designed_html(
    tmp_path: Path, monkeypatch
) -> None:
    """Key present + a working provider: stdout is the LLM prose behind the
    ``generated <ts>`` stamp, and the HTML file is the Story 4.1 designed page
    (masthead from ``facts.league``, inline ``<style>`` + inline ``<svg>``) with
    the LLM prose as the body — not the structured template render, and the LLM
    ``## `` headings survive as ``<h2>`` section headers."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    reply = _six_section_llm_text("Jeanty went 1.01, and it only got weirder.")
    fake = _install_fake_anthropic(monkeypatch, reply)

    result = runner.invoke(
        app, ["--league", "77", "--draft-recap", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert fake["create"] == 1  # a clean first pass makes exactly one paid call (I3)
    assert re.search(r"generated \d{4}-\d{2}-\d{2}T", result.output)
    assert "Jeanty went 1.01, and it only got weirder." in result.output
    # the LLM prose shipped, not the structured template render
    assert "content-safety alert" not in result.output

    html_path = tmp_path / "commishdesk-77-draft-recap.html"
    body = html_path.read_text(encoding="utf-8")
    assert body.startswith("<!doctype html>")
    # masthead title from facts.league, never parsed out of llm_text
    assert '<h1 class="nameplate">Trench Warfare</h1>' in body
    assert "<title>Trench Warfare — 2025 Draft Recap</title>" in body
    assert "<h2>The Lead</h2>" in body  # the LLM heading survives as a section header
    assert "Jeanty went 1.01, and it only got weirder." in body  # LLM prose is the body
    assert "generated " in body  # provenance stamp in the footer
    # the designed page: exactly one inline <style>, inline <svg>, no script
    assert body.count("<style>") == 1 and "<script" not in body
    assert "<svg" in body
    assert "http://" not in body and "https://" not in body
    # the LLM prose shipped, never the template narrator's fixed method sentence
    assert "Grades weigh each pick against the consensus board" not in body

    raw = html_path.read_bytes()
    assert b"\r\n" not in raw  # LF-only on disk
    raw.decode("utf-8")  # valid UTF-8, no surrogate escapes

    # Story 4.2: the LLM narrator path also writes the email HTML + text part,
    # with the LLM prose as the body and the title still from facts.league
    email_body = (tmp_path / "commishdesk-77-draft-recap.email.html").read_text(
        encoding="utf-8"
    )
    assert email_body.startswith("<!DOCTYPE html>")
    assert "<title>Trench Warfare — 2025 Draft Recap</title>" in email_body
    assert "Jeanty went 1.01, and it only got weirder." in email_body
    assert "<svg" not in email_body and "var(--" not in email_body
    text_body = (tmp_path / "commishdesk-77-draft-recap.txt").read_text(encoding="utf-8")
    assert "Jeanty went 1.01, and it only got weirder." in text_body
    assert "THE BOARD" in text_body


# --------------------------------------------------------------------------- #
# Story 3.5 — tiered failure response + narrator selection (AD-12 L3 / FR-17)
# --------------------------------------------------------------------------- #


def test_hallucination_on_llm_output_regenerates_once_then_degrades(
    tmp_path: Path, monkeypatch
) -> None:
    """A ``regenerate`` tier (a proper noun absent from the payload) triggers one
    regeneration; a second unclean result degrades to the template. Even though
    every attempt trips the same tier, ``narrate_draft_recap`` is invoked
    **exactly twice** — the CLI-level ceiling above I3's single paid call."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    calls = _stub_narrator(
        monkeypatch,
        _six_section_llm_text("Bratwurst Lindqvist had the draft of his life."),
    )
    result = runner.invoke(
        app, ["--league", "73", "--draft-recap", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert calls["n"] == 2, calls  # never a 3rd narrate_draft_recap call
    body = (tmp_path / "commishdesk-73-draft-recap.html").read_text(encoding="utf-8")
    assert "<h2>Superlatives</h2>" in body  # degraded to the template render
    assert "Bratwurst" not in body


def test_hallucination_regeneration_ships_a_clean_retry(
    tmp_path: Path, monkeypatch
) -> None:
    """When the one permitted regeneration comes back clean, that regenerated LLM
    prose ships (not the template)."""
    from commishdesk.narrate import NarrationResult

    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    replies = iter(
        [
            _six_section_llm_text("Bratwurst Lindqvist had the draft of his life."),
            _six_section_llm_text("the clean retry reads just fine."),
        ]
    )
    calls = {"n": 0}

    def _fake(*_a: object, **_k: object) -> NarrationResult:
        calls["n"] += 1
        return NarrationResult(text=next(replies), narrator="llm-primary")

    monkeypatch.setattr("commishdesk.narrate.llm.narrate_draft_recap", _fake)

    result = runner.invoke(
        app, ["--league", "74", "--draft-recap", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert calls["n"] == 2
    assert "the clean retry reads just fine." in result.output
    body = (tmp_path / "commishdesk-74-draft-recap.html").read_text(encoding="utf-8")
    assert "the clean retry reads just fine." in body
    # the bare LLM dump, not the structured template render
    assert "Grades weigh each pick against the consensus board" not in body


def test_structurally_broken_llm_output_falls_back_to_the_template(
    tmp_path: Path, monkeypatch
) -> None:
    """An LLM completion missing the six ``## `` headings counts as a failed
    generation — the league is narrated by the template, exit 0."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _stub_narrator(monkeypatch, "just one bare paragraph, no headings at all.")
    result = runner.invoke(
        app, ["--league", "75", "--draft-recap", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    for heading in SECTION_HEADINGS:
        assert heading in result.output
    body = (tmp_path / "commishdesk-75-draft-recap.html").read_text(encoding="utf-8")
    assert "<h2>Superlatives</h2>" in body


def test_empty_llm_output_falls_back_to_the_template(
    tmp_path: Path, monkeypatch
) -> None:
    """A whitespace-only completion is a failed generation → template, exit 0."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    # narrate_draft_recap itself rejects an empty completion, so simulate a stub
    # that slipped one through to _produce_issue's own validation.
    _stub_narrator(monkeypatch, "   \n  \t \n ")
    result = runner.invoke(
        app, ["--league", "76", "--draft-recap", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    body = (tmp_path / "commishdesk-76-draft-recap.html").read_text(encoding="utf-8")
    assert "<h2>Team Grades</h2>" in body


def _stub_template_recap(
    monkeypatch,
    *,
    offending_heading: str | None = None,
    phrase: str = "nothing offending here.",
    title: str = "Trench Warfare — 2025 Draft Recap",
    dateline: str = "Trench Warfare · 2025 season · Half PPR",
) -> None:
    """Replace ``render_draft_recap`` with one returning a six-section recap. When
    *offending_heading* is given that section carries *phrase*; otherwise every
    section is clean and *phrase* lives only where *title* / *dateline* put it."""
    from commishdesk.narrate import Recap, Section

    def _fake(*_a: object, **_k: object) -> Recap:
        return Recap(
            title=title,
            dateline=dateline,
            sections=[
                Section(
                    heading=h,
                    blocks=[phrase if h == offending_heading else "nothing to see here."],
                )
                for h in SECTION_HEADINGS
            ],
        )

    monkeypatch.setattr("commishdesk.narrate.render_draft_recap", _fake)


def test_banned_pattern_in_a_template_section_drops_that_section(
    tmp_path: Path, monkeypatch
) -> None:
    """A banned-topic pattern (no manager name) in a non-lead template section:
    that ``Section`` is removed, an alert is emitted, the rest ships, exit 0."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    _stub_template_recap(
        monkeypatch,
        offending_heading="Superlatives",
        phrase="The betting line on this pick was absurd.",
    )
    result = runner.invoke(
        app, ["--league", "80", "--draft-recap", "--no-llm", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "content-safety alert for league 80" in result.output
    body = (tmp_path / "commishdesk-80-draft-recap.html").read_text(encoding="utf-8")
    assert "<h2>The Lead</h2>" in body
    assert "<h2>Superlatives</h2>" not in body  # the offending section is gone


def test_suppression_hitting_the_lead_holds_the_issue(
    tmp_path: Path, monkeypatch
) -> None:
    """When suppression would remove **The Lead**, the whole Issue is held:
    ``ContentSafetyError``, exit 1, no HTML."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    _stub_template_recap(
        monkeypatch,
        offending_heading="The Lead",
        phrase="The betting line on this whole draft was absurd.",
    )
    result = runner.invoke(
        app, ["--league", "81", "--draft-recap", "--no-llm", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "content-safety hold" in result.output
    assert not (tmp_path / "commishdesk-81-draft-recap.html").is_file()


def test_total_llm_outage_still_produces_every_league_an_issue(
    tmp_path: Path, monkeypatch
) -> None:
    """Every provider attempt fails across a multi-league run — each league still
    writes a template-narrated HTML Issue and the run exits 0 (FR-17)."""
    from commishdesk import generation

    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        generation,
        "build_generation_set",
        lambda *a, **k: generation.GenerationSet(("201", "202", "203")),
    )
    # --llm with no key and no SDK: every adapter call raises, narrate_draft_recap
    # swallows it to narrator="template".
    result = runner.invoke(
        app, ["--league", "200", "--draft-recap", "--llm", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    for league_id in ("201", "202", "203"):
        assert (tmp_path / f"commishdesk-{league_id}-draft-recap.html").is_file()


def test_template_section_manager_plus_banned_term_holds_directly(
    tmp_path: Path, monkeypatch
) -> None:
    """A manager's name beside a banned-category term inside a template section is
    a ``named_person_proximity`` hold reached directly (not via
    ``suppress_sections`` returning ``None``): ``ContentSafetyError``, exit 1, no
    HTML."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    _stub_template_recap(
        monkeypatch,
        offending_heading="Team Grades",
        phrase="Pull-Guard Pumas clearly drafted hungover this year.",
    )
    result = runner.invoke(
        app, ["--league", "83", "--draft-recap", "--no-llm", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "content-safety hold" in result.output
    assert "hungover" in result.output
    assert not (tmp_path / "commishdesk-83-draft-recap.html").is_file()


def test_suppress_finding_mapping_to_no_section_holds_the_issue(
    tmp_path: Path, monkeypatch
) -> None:
    """A ``suppress`` tier whose sentence is only in ``recap.title`` (no section)
    cannot be localized — fail closed: ``ContentSafetyError``, exit 1, no HTML,
    and no empty "suppressed section(s)" line."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    _stub_template_recap(
        monkeypatch,
        title="The betting line on this whole draft was a joke. Trench Warfare Recap",
    )
    result = runner.invoke(
        app, ["--league", "84", "--draft-recap", "--no-llm", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "content-safety hold" in result.output
    assert "suppressed section(s) " not in result.output
    assert not (tmp_path / "commishdesk-84-draft-recap.html").is_file()


def test_regenerated_llm_attempt_that_holds_exits_1_after_exactly_two_calls(
    tmp_path: Path, monkeypatch
) -> None:
    """First attempt trips ``regenerate``; the regenerated attempt trips a
    ``hold`` — ``ContentSafetyError``, exit 1, no HTML, exactly two
    ``narrate_draft_recap`` calls."""
    from commishdesk.narrate import NarrationResult

    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    replies = iter(
        [
            _six_section_llm_text("Bratwurst Lindqvist had a monster draft."),
            _six_section_llm_text("Pull-Guard Pumas clearly drafted hungover."),
        ]
    )
    calls = {"n": 0}

    def _fake(*_a: object, **_k: object) -> NarrationResult:
        calls["n"] += 1
        return NarrationResult(text=next(replies), narrator="llm-primary")

    monkeypatch.setattr("commishdesk.narrate.llm.narrate_draft_recap", _fake)

    result = runner.invoke(
        app, ["--league", "85", "--draft-recap", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert calls["n"] == 2
    assert "content-safety hold" in result.output
    assert not (tmp_path / "commishdesk-85-draft-recap.html").is_file()


def test_content_safety_alert_is_on_stderr_not_stdout(
    tmp_path: Path, monkeypatch
) -> None:
    """The operator alert (`_emit_alerts`) goes to stderr; the recap goes to
    stdout; the alert string never appears in stdout."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _stub_narrator(
        monkeypatch,
        _six_section_llm_text("Honestly, make no mistake, this draft had chaos."),
    )
    result = runner.invoke(
        app, ["--league", "86", "--draft-recap", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "content-safety alert for league 86" in result.stderr
    assert "## The Lead" in result.stdout  # the template recap went to stdout
    assert "content-safety alert" not in result.stdout


def test_llm_completion_is_sanitized_on_the_ship_path(
    tmp_path: Path, monkeypatch
) -> None:
    """A clean six-section completion carrying an ANSI/SGR escape and a control
    char ships as LLM prose — but the escape and control bytes are stripped from
    both stdout and the written HTML (`sanitize_completion`)."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _stub_narrator(
        monkeypatch,
        _six_section_llm_text("the \x1b[1mbold bit\x1b[0m and the \x07 bell are cleaned."),
    )
    result = runner.invoke(
        app, ["--league", "87", "--draft-recap", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    body = (tmp_path / "commishdesk-87-draft-recap.html").read_text(encoding="utf-8")
    for blob in (result.stdout, body):
        assert "bold bit" in blob and "bell are cleaned." in blob
        assert "\x1b" not in blob and "\x07" not in blob and "[1m" not in blob


# --------------------------------------------------------------------------- #
# Pre-4.1 calibration gate — the --allow-content-hold operator override (D1)
# --------------------------------------------------------------------------- #

_HOLD_PHRASE = "Pull-Guard Pumas clearly drafted hungover this year."


def test_a_suppressible_section_that_names_the_league_still_localizes(
    tmp_path: Path, monkeypatch
) -> None:
    """P1 — the offending sentence also carries the league name, so the check
    matched it masked. The finding must still report the *unmasked* sentence, or
    ``suppress_sections`` finds no section, ``removed`` comes back empty, and the
    Issue is held instead of being trimmed."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    _stub_template_recap(
        monkeypatch,
        offending_heading="Superlatives",
        phrase="Trench Warfare saw the betting line on this pick go absurd.",
    )
    result = runner.invoke(
        app, ["--league", "89", "--draft-recap", "--no-llm", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "content-safety hold" not in result.output
    assert "suppressed section(s) Superlatives" in result.stderr
    body = (tmp_path / "commishdesk-89-draft-recap.html").read_text(encoding="utf-8")
    assert "<h2>The Lead</h2>" in body
    assert "<h2>Superlatives</h2>" not in body


@pytest.mark.parametrize(
    "flag, env",
    [
        (["--allow-content-hold"], {}),
        ([], {"COMMISHDESK_ALLOW_CONTENT_HOLD": "1"}),
    ],
)
def test_allow_content_hold_ships_a_held_issue(
    tmp_path: Path, monkeypatch, flag: list[str], env: dict[str, str]
) -> None:
    """A real ``named_person_proximity`` hold on the template path ships anyway:
    exit 0, HTML written, the hold logged + echoed with ``OVERRIDDEN`` and its
    reason. Both the flag and the env var reach the same resolver."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    _stub_template_recap(
        monkeypatch, offending_heading="Team Grades", phrase=_HOLD_PHRASE
    )
    result = runner.invoke(
        app,
        ["--league", "90", "--draft-recap", "--no-llm", "--out-dir", str(tmp_path), *flag],
    )
    assert result.exit_code == 0, result.output
    assert "OVERRIDDEN" in result.stderr
    assert "hungover" in result.stderr  # every hold reason is named
    # the per-finding operator alerts still fire alongside the override line
    assert "content-safety alert for league 90" in result.stderr
    assert "named_person_proximity/hold_issue" in result.stderr
    assert "Traceback" not in result.output
    body = (tmp_path / "commishdesk-90-draft-recap.html").read_text(encoding="utf-8")
    # the untrimmed recap shipped — every section, offending one included
    assert "<h2>Team Grades</h2>" in body
    assert "<h2>The Lead</h2>" in body


@pytest.mark.parametrize(
    "flag, env",
    [
        ([], {}),
        # the explicit negative form beats an ambient environment variable
        (["--no-allow-content-hold"], {"COMMISHDESK_ALLOW_CONTENT_HOLD": "1"}),
    ],
)
def test_the_same_input_without_the_override_still_holds(
    tmp_path: Path, monkeypatch, flag: list[str], env: dict[str, str]
) -> None:
    """The regression anchor for the row above: no override in force → unchanged
    ``ContentSafetyError``, exit 1, no HTML."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    _stub_template_recap(
        monkeypatch, offending_heading="Team Grades", phrase=_HOLD_PHRASE
    )
    result = runner.invoke(
        app,
        ["--league", "91", "--draft-recap", "--no-llm", "--out-dir", str(tmp_path), *flag],
    )
    assert result.exit_code == 1
    assert "content-safety hold" in result.output
    assert "OVERRIDDEN" not in result.output
    assert not (tmp_path / "commishdesk-91-draft-recap.html").is_file()


def test_allow_content_hold_ships_the_llm_text_on_the_llm_path(
    tmp_path: Path, monkeypatch
) -> None:
    """On the LLM path the override ships the validated completion itself, not a
    degraded template render."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _stub_narrator(monkeypatch, _six_section_llm_text(_HOLD_PHRASE))
    result = runner.invoke(
        app,
        [
            "--league", "92", "--draft-recap",
            "--allow-content-hold", "--out-dir", str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "OVERRIDDEN" in result.stderr
    body = (tmp_path / "commishdesk-92-draft-recap.html").read_text(encoding="utf-8")
    assert _HOLD_PHRASE in body
    # the bare LLM dump, not the structured template render
    assert "Grades weigh each pick against the consensus board" not in body


def test_allow_content_hold_ships_an_unlocalizable_suppress_finding(
    tmp_path: Path, monkeypatch
) -> None:
    """The other hold path: a suppress-tier finding that maps to no section.
    Overridden, the untrimmed recap ships."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    _stub_template_recap(
        monkeypatch,
        title="The betting line on this whole draft was a joke. Trench Warfare Recap",
    )
    result = runner.invoke(
        app,
        [
            "--league", "93", "--draft-recap", "--no-llm",
            "--allow-content-hold", "--out-dir", str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "OVERRIDDEN" in result.stderr
    assert (tmp_path / "commishdesk-93-draft-recap.html").is_file()


def test_allow_content_hold_ships_a_lead_removing_suppression(
    tmp_path: Path, monkeypatch
) -> None:
    """The third hold path, and the one with teeth: suppression that would drop
    **The Lead**. Overridden, ``_produce_issue`` must return the *untrimmed*
    recap — if it ever returned the ``None`` trimmed one instead,
    ``_recap_one_league``'s ``assert body.recap is not None`` fires an
    ``AssertionError``, which the AD-9 ``except (CommishDeskError, OSError)``
    does **not** catch: a traceback in place of an Issue."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    _stub_template_recap(
        monkeypatch,
        offending_heading="The Lead",
        phrase="The betting line on this whole draft was absurd.",
    )
    result = runner.invoke(
        app,
        [
            "--league", "96", "--draft-recap", "--no-llm",
            "--allow-content-hold", "--out-dir", str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert "OVERRIDDEN" in result.stderr
    body = (tmp_path / "commishdesk-96-draft-recap.html").read_text(encoding="utf-8")
    assert "<h2>The Lead</h2>" in body  # the untrimmed recap, Lead included


def test_allow_content_hold_does_not_bypass_a_per_league_fault(
    tmp_path: Path, monkeypatch
) -> None:
    """The override touches ``ContentSafetyError`` and nothing else. A typed
    fault raised **inside** the league loop still lands in the AD-9 per-league
    catch: one line on stderr, exit 1, no HTML — with the flag on."""
    from commishdesk.errors import AdapterError

    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)

    class _BrokenAdapter:
        def __init__(self, *a: object, **k: object) -> None: ...

        def fetch(self, league_id: str) -> dict:
            raise AdapterError("sleeper is unreachable")

        def close(self) -> None: ...

    monkeypatch.setattr("commishdesk.adapters.sleeper.SleeperAdapter", _BrokenAdapter)
    result = runner.invoke(
        app,
        [
            "--league", "94", "--draft-recap", "--no-llm",
            "--allow-content-hold", "--out-dir", str(tmp_path),
        ],
    )
    assert result.exit_code == 1
    assert "sleeper is unreachable" in result.output
    assert "Traceback" not in result.output
    assert "OVERRIDDEN" not in result.output
    assert not list(tmp_path.glob("*.html"))


def test_allow_content_hold_does_not_bypass_a_pre_loop_fault(
    tmp_path: Path, monkeypatch
) -> None:
    """And a malformed ``COMMISHDESK_LLM_*`` still aborts before the league loop
    even with the flag on."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("COMMISHDESK_LLM_PRIMARY", "bogus-no-colon")
    result = runner.invoke(
        app,
        [
            "--league", "97", "--draft-recap",
            "--allow-content-hold", "--out-dir", str(tmp_path),
        ],
    )
    assert result.exit_code == 1
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "flag, env, expected",
    [
        # an explicit flag wins either way, exactly like --llm / --no-llm
        (True, {}, True),
        (True, {"COMMISHDESK_ALLOW_CONTENT_HOLD": "0"}, True),
        (False, {}, False),
        (False, {"COMMISHDESK_ALLOW_CONTENT_HOLD": "1"}, False),
        (False, {"COMMISHDESK_ALLOW_CONTENT_HOLD": "yes"}, False),
        # unset flag: the environment decides
        (None, {}, False),
        (None, {"COMMISHDESK_ALLOW_CONTENT_HOLD": "1"}, True),
        (None, {"COMMISHDESK_ALLOW_CONTENT_HOLD": "yes"}, True),
        (None, {"COMMISHDESK_ALLOW_CONTENT_HOLD": "  "}, False),
        (None, {"COMMISHDESK_ALLOW_CONTENT_HOLD": "0"}, False),
        (None, {"COMMISHDESK_ALLOW_CONTENT_HOLD": "false"}, False),
        (None, {"COMMISHDESK_ALLOW_CONTENT_HOLD": "OFF"}, False),
    ],
)
def test_allow_content_hold_helper(flag, env, expected, monkeypatch) -> None:
    from commishdesk.cli import _allow_content_hold

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert _allow_content_hold(flag) is expected


def test_a_league_named_after_a_banned_term_still_produces_an_issue(
    tmp_path: Path, monkeypatch
) -> None:
    """D1 end to end — "The Sportsbook League" on the zero-credential template
    path needs no flag at all: exit 0, HTML written, and the real league name
    still in the title and the dateline."""
    from commishdesk import demo

    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)

    bundle = demo.load_demo_bundle()
    # AD-24: ingest/sanitize.py passes this through untouched, so it reaches the
    # Facts JSON — and, before this gate, every safety check — verbatim.
    league_name = "The Sportsbook League"

    def _renamed(league_id: str) -> dict:
        renamed = dict(bundle)
        renamed["league"] = {**bundle["league"], "name": league_name}
        return renamed

    class _FakeAdapter:
        def __init__(self, *a: object, **k: object) -> None: ...

        def fetch(self, league_id: str) -> dict:
            return _renamed(league_id)

        def close(self) -> None: ...

    monkeypatch.setattr("commishdesk.adapters.sleeper.SleeperAdapter", _FakeAdapter)

    result = runner.invoke(
        app, ["--league", "95", "--draft-recap", "--no-llm", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "content-safety" not in result.stderr
    body = (tmp_path / "commishdesk-95-draft-recap.html").read_text(encoding="utf-8")
    assert f'<h1 class="nameplate">{league_name}</h1>' in body
    assert body.count(league_name) >= 2  # <title> + masthead <h1>
    # the mask lives in the check's working copy only: the two places the league
    # name is rendered carry the real name, never the placeholder. (render_web's
    # own footer prose deliberately avoids the words "the league".)
    title_line = next(line for line in body.splitlines() if "<title>" in line)
    h1_line = next(line for line in body.splitlines() if "<h1" in line)
    for line in (title_line, h1_line):
        assert league_name in line, line
        assert safety._LEAGUE_PLACEHOLDER not in line, line


# --------------------------------------------------------------------------- #
# Story 4.6 — --post (idempotent Discord delivery) + the pre-call cost estimate
# --------------------------------------------------------------------------- #

_FAKE_WEBHOOK_URL = "https://discord.com/api/webhooks/111111111111111111/faketoken"


def _stub_post_discord_text(monkeypatch, *, fail: Exception | None = None) -> list:
    """Replace ``commishdesk.deliver.discord.post_discord_text`` (the CLI's lazy
    import picks this up at call time) with a recording fake — no real network
    call. Returns the list of ``(url, content)`` calls made."""
    calls: list[tuple[str, str]] = []

    def _fake(url: str, content: str, **_kw: object) -> str:
        calls.append((url, content))
        if fail is not None:
            raise fail
        return "111111111111111111"

    monkeypatch.setattr("commishdesk.deliver.discord.post_discord_text", _fake)
    return calls


def test_fmt_usd_shows_enough_precision_to_distinguish_near_ceiling_values() -> None:
    """review-loop 1: a naive two-decimal rounding made an over-ceiling error
    message show the exact same two numbers as the ceiling it exceeded."""
    from commishdesk.cli import _fmt_usd

    assert _fmt_usd(1.001) != _fmt_usd(1.000)


def test_post_without_draft_recap_is_a_usage_error() -> None:
    result = runner.invoke(app, ["--league", "demo", "--post"])
    assert result.exit_code == 2
    assert "Traceback" not in result.output
    assert "--post" in result.output


@pytest.mark.parametrize("webhook_value", [None, "", "   "])
def test_post_without_a_webhook_fails_before_any_sleeper_fetch(
    tmp_path: Path, monkeypatch, webhook_value: str | None
) -> None:
    """The --post-specific webhook check runs before any Sleeper / consensus /
    LLM work for the league — proven by an adapter that raises if it is ever
    reached at all."""
    if webhook_value is None:
        monkeypatch.delenv("COMMISHDESK_DISCORD_WEBHOOK_URL", raising=False)
    else:
        monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", webhook_value)

    class _MustNotFetchAdapter:
        def __init__(self, *a: object, **k: object) -> None: ...

        def fetch(self, league_id: str) -> dict:
            raise AssertionError("Sleeper must not be fetched before the --post webhook check")

        def close(self) -> None: ...

    monkeypatch.setattr("commishdesk.adapters.sleeper.SleeperAdapter", _MustNotFetchAdapter)
    result = runner.invoke(
        app, ["--league", "170", "--draft-recap", "--post", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "COMMISHDESK_DISCORD_WEBHOOK_URL" in result.output
    assert not list(tmp_path.iterdir())


def test_post_without_a_webhook_on_the_demo_path_also_fails(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("COMMISHDESK_DISCORD_WEBHOOK_URL", raising=False)
    result = runner.invoke(
        app, ["--league", "demo", "--draft-recap", "--post", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert not list(tmp_path.iterdir())


def test_post_with_a_malformed_webhook_url_fails_before_any_paid_call(
    tmp_path: Path, monkeypatch
) -> None:
    """review-loop 2, finding 1: a non-blank but non-Discord-shaped webhook URL
    must fail the same fail-fast way a blank one does — before any Sleeper
    fetch or paid LLM call, not after burning the full narration cost."""
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", "https://example.com/not-a-webhook")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    class _MustNotFetchAdapter:
        def __init__(self, *a: object, **k: object) -> None: ...

        def fetch(self, league_id: str) -> dict:
            raise AssertionError(
                "Sleeper must not be fetched before the --post webhook check"
            )

        def close(self) -> None: ...

    monkeypatch.setattr("commishdesk.adapters.sleeper.SleeperAdapter", _MustNotFetchAdapter)
    result = runner.invoke(
        app, ["--league", "171", "--draft-recap", "--post", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "not a Discord webhook" in result.output
    assert "estimated cost" not in result.output  # never reached the cost-estimate block
    assert not list(tmp_path.iterdir())


def test_post_first_run_posts_once_and_writes_every_file(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    calls = _stub_post_discord_text(monkeypatch)
    result = runner.invoke(
        app, ["--league", "demo", "--draft-recap", "--post", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][0] == _FAKE_WEBHOOK_URL
    assert "posted to Discord" in result.output
    assert (tmp_path / "commishdesk-demo-draft-recap.html").is_file()
    assert (tmp_path / "commishdesk-demo-draft-recap.email.html").is_file()
    assert (tmp_path / "commishdesk-demo-draft-recap.txt").is_file()

    from commishdesk.store import FileStore

    store = FileStore(tmp_path / "cache" / "commishdesk")
    ledger = store.read_ledger("demo", 1)  # DRAFT_RECAP_WEEK
    assert len(ledger) == 1 and ledger[0].channel == "discord"


def test_post_second_identical_run_skips_discord_but_rewrites_files(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    calls = _stub_post_discord_text(monkeypatch)

    r1 = runner.invoke(
        app, ["--league", "demo", "--draft-recap", "--post", "--out-dir", str(tmp_path)]
    )
    assert r1.exit_code == 0, r1.output
    assert len(calls) == 1

    r2 = runner.invoke(
        app, ["--league", "demo", "--draft-recap", "--post", "--out-dir", str(tmp_path)]
    )
    assert r2.exit_code == 0, r2.output
    assert len(calls) == 1  # no second Discord post
    assert "already confirmed" in r2.output
    assert (tmp_path / "commishdesk-demo-draft-recap.html").is_file()  # rewritten regardless


def test_post_on_demo_creates_a_store_but_never_persists_storylines(
    tmp_path: Path, monkeypatch
) -> None:
    """review-loop 1 regression: --post gives the demo path a Store (for the
    Send Ledger), which must NOT re-enable storyline persistence for demo."""
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    _stub_post_discord_text(monkeypatch)
    result = runner.invoke(
        app, ["--league", "demo", "--draft-recap", "--post", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output

    from commishdesk.store import FileStore

    store = FileStore(tmp_path / "cache" / "commishdesk")
    assert store.read_storylines("demo") == []  # never written
    assert store.read_ledger("demo", 1) != []  # but the ledger WAS written


def test_post_discord_failure_is_exit_1_with_no_ledger_entry(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    _stub_post_discord_text(monkeypatch, fail=DeliveryError("discord rejected the webhook"))
    result = runner.invoke(
        app, ["--league", "demo", "--draft-recap", "--post", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "discord rejected the webhook" in result.output
    # rendering already happened before delivery was attempted
    assert (tmp_path / "commishdesk-demo-draft-recap.html").is_file()

    from commishdesk.store import FileStore

    store = FileStore(tmp_path / "cache" / "commishdesk")
    assert store.read_ledger("demo", 1) == []


def test_post_real_league_full_flow_with_llm_narration(
    tmp_path: Path, monkeypatch
) -> None:
    """The full acceptance criterion: a working webhook, an estimate under
    ceiling, all three surfaces rendered, and one Discord post."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    calls = _stub_post_discord_text(monkeypatch)
    _stub_narrator(monkeypatch, _six_section_llm_text("posted with the LLM narrator."))

    result = runner.invoke(
        app, ["--league", "169", "--draft-recap", "--post", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "estimated cost: $" in result.output
    assert len(calls) == 1
    assert "Trench Warfare" in calls[0][1]  # the league name, in the Discord summary
    assert (tmp_path / "commishdesk-169-draft-recap.html").is_file()
    assert (tmp_path / "commishdesk-169-draft-recap.email.html").is_file()
    assert (tmp_path / "commishdesk-169-draft-recap.txt").is_file()


def test_content_safety_hold_with_post_makes_no_discord_post_or_ledger_entry(
    tmp_path: Path, monkeypatch
) -> None:
    """Defensive regression (review-loop 1): a held Issue must never reach the
    --post block — structurally guaranteed by ``_produce_issue`` raising before
    ``if post:``, but untested at review-loop 0."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    calls = _stub_post_discord_text(monkeypatch)
    _stub_narrator(
        monkeypatch,
        _six_section_llm_text("Pull-Guard Pumas clearly drafted hungover this year."),
    )
    result = runner.invoke(
        app, ["--league", "168", "--draft-recap", "--post", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "content-safety hold" in result.output
    assert not calls
    assert not (tmp_path / "commishdesk-168-draft-recap.html").is_file()

    from commishdesk.store import FileStore

    store = FileStore(tmp_path / "commishdesk")
    assert store.read_ledger("168", 1) == []


def test_post_run_every_json_log_line_carries_league_id(
    tmp_path: Path, monkeypatch
) -> None:
    """The league_id log_context binding (bound once in run()) must cover the
    whole --post path, Discord call included — one shared binding site, not a
    second one scoped narrower (pattern: tests/test_logging.py:53)."""
    monkeypatch.setenv("COMMISHDESK_DISCORD_WEBHOOK_URL", _FAKE_WEBHOOK_URL)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    _stub_post_discord_text(monkeypatch)
    result = runner.invoke(
        app,
        [
            "--league", "demo", "--draft-recap", "--post",
            "--out-dir", str(tmp_path), "--verbose",
        ],
    )
    assert result.exit_code == 0, result.output
    records = [
        json.loads(line) for line in result.stderr.splitlines() if line.strip().startswith("{")
    ]
    assert records, result.stderr
    assert all(r.get("league_id") == "demo" for r in records)
    # proves the Discord/ledger area itself is in scope, not just the bookends
    assert any("confirmed delivery" in r.get("msg", "") for r in records)


def test_cost_estimate_under_ceiling_prints_before_produce_issue_and_proceeds(
    tmp_path: Path, monkeypatch
) -> None:
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _stub_narrator(monkeypatch, _six_section_llm_text("cost estimate happy path."))
    result = runner.invoke(
        app, ["--league", "160", "--draft-recap", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "estimated cost: $" in result.output
    assert "(ceiling $1.0000)" in result.output
    assert result.output.index("estimated cost") < result.output.index(
        "cost estimate happy path."
    )
    body = (tmp_path / "commishdesk-160-draft-recap.html").read_text(encoding="utf-8")
    assert "cost estimate happy path." in body


def test_cost_estimate_over_ceiling_aborts_before_any_paid_call(
    tmp_path: Path, monkeypatch
) -> None:
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("COMMISHDESK_COST_CEILING_USD", "0.000001")
    calls = _stub_narrator(monkeypatch, _six_section_llm_text("must never appear."))
    result = runner.invoke(
        app, ["--league", "161", "--draft-recap", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "estimated cost: $" in result.output  # printed either way
    assert "exceeds the ceiling" in result.output
    assert calls["n"] == 0  # narrate_draft_recap never ran — no paid call
    assert not (tmp_path / "commishdesk-161-draft-recap.html").is_file()
    assert "must never appear." not in result.output


def test_cost_estimate_unknown_model_id_raises_naming_the_model(
    tmp_path: Path, monkeypatch
) -> None:
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("COMMISHDESK_LLM_PRIMARY", "anthropic:claude-totally-unpriced")
    calls = _stub_narrator(monkeypatch, _six_section_llm_text("must never appear."))
    result = runner.invoke(
        app, ["--league", "162", "--draft-recap", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "anthropic:claude-totally-unpriced" in result.output
    assert calls["n"] == 0
    assert not (tmp_path / "commishdesk-162-draft-recap.html").is_file()


def test_cost_estimate_uses_the_pricier_of_primary_or_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    """review-loop 1: swap the two priced defaults so the *fallback* is the
    pricier model — the ceiling check must still use the higher figure. A
    regression that priced ``llm_config.primary`` alone would pass this
    scenario's cheap primary and proceed; the fix must not."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("COMMISHDESK_LLM_PRIMARY", "google:gemini-3.5-flash")
    monkeypatch.setenv("COMMISHDESK_LLM_FALLBACK", "anthropic:claude-sonnet-5")
    monkeypatch.setenv("COMMISHDESK_COST_CEILING_USD", "0.05")
    calls = _stub_narrator(monkeypatch, _six_section_llm_text("must never appear."))
    result = runner.invoke(
        app, ["--league", "163", "--draft-recap", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "exceeds the ceiling" in result.output
    assert calls["n"] == 0
    assert not (tmp_path / "commishdesk-163-draft-recap.html").is_file()


def test_cost_estimate_includes_the_voice_system_prompt(
    tmp_path: Path, monkeypatch
) -> None:
    """review-loop 2, finding 2: every real provider call also sends
    ``voice.system_prompt`` as the system message (``AnthropicClient.generate`` /
    ``GoogleClient.generate``, ``narrate/llm.py``) — a worst-case bound that only
    prices the narration payload silently under-counts every real call. The
    printed estimate must be measurably larger than pricing the narration
    payload alone would produce."""
    from datetime import UTC, datetime

    from commishdesk.demo import demo_consensus_slots, load_demo_bundle
    from commishdesk.facts import build_draft_recap_facts
    from commishdesk.ingest import build_league_model
    from commishdesk.llmconfig import load_llm_config
    from commishdesk.narrate import (
        MAX_OUTPUT_TOKENS,
        build_narration_payload,
        estimate_cost_usd,
    )
    from commishdesk.stats import (
        compute_board_metrics,
        compute_consensus_metrics,
        compute_draft_grades,
    )
    from commishdesk.voices import load_default_voice

    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _stub_narrator(monkeypatch, _six_section_llm_text("nothing special here."))

    result = runner.invoke(
        app, ["--league", "175", "--draft-recap", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    printed_line = next(
        line for line in result.output.splitlines() if line.startswith("estimated cost: ")
    )
    printed_amount = float(re.search(r"\$([\d.]+)", printed_line).group(1))

    # Reconstruct the same Facts doc the run actually built (mirroring
    # _fake_real_league's fixed source/as_of) to compute, independently, what
    # the estimate would be if it priced ONLY the narration payload — no
    # voice.system_prompt.
    bundle = load_demo_bundle()
    model = build_league_model(bundle)
    slots = demo_consensus_slots()
    board = compute_board_metrics(model)
    consensus = compute_consensus_metrics(model, slots)
    grades = compute_draft_grades(model, consensus)
    doc = build_draft_recap_facts(
        model, board, consensus, grades,
        generated_at=datetime.now(tz=UTC), draft_id=model.draft.id,
        consensus_source_name="fantasycalc", consensus_as_of="2099-07",
        previous_storylines=[],
    )
    narration_only_payload = build_narration_payload(doc.narration)
    llm_config = load_llm_config({})
    per_call_narration_only = max(
        estimate_cost_usd(
            narration_only_payload, llm_config.primary, max_output_tokens=MAX_OUTPUT_TOKENS
        ),
        estimate_cost_usd(
            narration_only_payload, llm_config.fallback, max_output_tokens=MAX_OUTPUT_TOKENS
        ),
    )
    narration_only_total = per_call_narration_only * 2  # _MAX_BILLABLE_NARRATION_ATTEMPTS

    voice = load_default_voice()
    assert voice.system_prompt  # sanity: there is something to add
    assert printed_amount > narration_only_total


def test_cost_estimate_reflects_the_two_attempt_multiplier(
    tmp_path: Path, monkeypatch
) -> None:
    """review-loop 1: the printed number is per_call * _MAX_BILLABLE_NARRATION_ATTEMPTS
    (2), not per_call alone — proven by comparing the real run against the same
    run with the multiplier patched to 1."""
    import commishdesk.cli as cli_mod

    def _estimate_line(league_id: str) -> str:
        result = runner.invoke(
            app, ["--league", league_id, "--draft-recap", "--out-dir", str(tmp_path)]
        )
        assert result.exit_code == 0, result.output
        return next(
            line for line in result.output.splitlines() if line.startswith("estimated cost: ")
        )

    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _stub_narrator(monkeypatch, _six_section_llm_text("nothing special here."))

    assert cli_mod._MAX_BILLABLE_NARRATION_ATTEMPTS == 2
    line_at_2x = _estimate_line("165")

    monkeypatch.setattr(cli_mod, "_MAX_BILLABLE_NARRATION_ATTEMPTS", 1)
    line_at_1x = _estimate_line("166")

    assert line_at_2x != line_at_1x
    amount_2x = float(re.search(r"\$([\d.]+)", line_at_2x).group(1))
    amount_1x = float(re.search(r"\$([\d.]+)", line_at_1x).group(1))
    # each figure is independently rounded to 4 decimals for display, so an
    # absolute (not relative) tolerance absorbs up to 0.0001 of rounding on
    # each side without masking a real multiplier regression
    assert amount_2x == pytest.approx(amount_1x * 2, abs=2e-4)


def test_stale_pricing_table_warns_once_through_the_cli(
    tmp_path: Path, monkeypatch
) -> None:
    """review-loop 1: review-loop 0 only unit-tested is_pricing_stale() in
    isolation, never through cli.py — this proves the call site (args, logic,
    and that it is actually reached) all still work."""
    from datetime import date, timedelta

    from commishdesk.narrate import pricing as narrate_pricing

    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _stub_narrator(monkeypatch, _six_section_llm_text("nothing special here."))

    updated = date.fromisoformat(narrate_pricing.PRICING_UPDATED)
    far_future = updated + timedelta(days=narrate_pricing.PRICING_REVIEW_INTERVAL_DAYS + 1)
    monkeypatch.setattr(narrate_pricing, "_today", lambda: far_future)

    result = runner.invoke(
        app, ["--league", "167", "--draft-recap", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "price table was last reviewed" in result.stderr
    assert (tmp_path / "commishdesk-167-draft-recap.html").is_file()  # staleness never blocks
