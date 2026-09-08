"""Story 2.7 — ``commishdesk --league demo --draft-recap`` end to end.

Exit 0, every section heading + sample claims on stdout, an HTML file written to
``--out-dir``, two-run byte-identity modulo ``generated_at``, the
``--draft-recap`` + ``--week`` usage error, ``--version``, and a clean typed
error when the demo fixture is absent.
"""

from __future__ import annotations

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
from commishdesk.errors import CommishDeskError

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
    assert "<h1>" in body and "<h2>" in body


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


def test_demo_run_ignores_a_malformed_llm_config(tmp_path: Path, monkeypatch) -> None:
    """The zero-credential onboarding/smoke path must not depend on LLM config
    parsing: ``--league demo`` with a broken ``COMMISHDESK_LLM_*`` still exits 0
    and prints the template recap."""
    monkeypatch.setenv("COMMISHDESK_LLM_PRIMARY", "bogus-no-colon")
    result = runner.invoke(
        app, ["--league", "demo", "--draft-recap", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    for heading in SECTION_HEADINGS:
        assert heading in result.output
    assert (tmp_path / "commishdesk-demo-draft-recap.html").is_file()


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


def _install_fake_anthropic(monkeypatch, reply: str) -> None:
    """A minimal ``anthropic`` stand-in: ``Anthropic().messages.create(...)``
    returns one text block with *reply* and a clean stop reason."""
    module = types.ModuleType("anthropic")

    class _Messages:
        def create(self, **_kw: object) -> object:
            return types.SimpleNamespace(
                stop_reason="end_turn",
                content=[types.SimpleNamespace(type="text", text=reply)],
            )

    class _Anthropic:
        def __init__(self, **_kw: object) -> None:
            self.messages = _Messages()

    module.Anthropic = _Anthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", module)


def test_unsafe_narrator_output_holds_the_league_exit_1_no_html(
    tmp_path: Path, monkeypatch
) -> None:
    """Story 3.4 — a narrator emitting a ``hold_issue`` line (a manager's name
    beside a banned-category term): that league raises ``NarratorError`` from the
    content-safety gate, prints one stderr line, exits 1, and writes no HTML."""
    from commishdesk.narrate import NarrationResult

    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    unsafe = NarrationResult(
        text="Pull-Guard Pumas clearly drafted hungover this year.",
        narrator="llm-primary",
    )
    monkeypatch.setattr(
        "commishdesk.narrate.llm.narrate_draft_recap", lambda *a, **k: unsafe
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


def test_safety_warn_tier_proceeds_and_logs_the_league_id(
    tmp_path: Path, monkeypatch
) -> None:
    """Story 3.4 P5/P6 — a warn-tier finding (slop, no manager name) does not
    hold: exit 0, HTML written, and a content-safety line naming the category and
    the league id reaches the operator."""
    from commishdesk.narrate import NarrationResult

    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    warn = NarrationResult(
        text="Trench Warfare Recap\n\nMake no mistake, this draft had chaos.",
        narrator="llm-primary",
    )
    monkeypatch.setattr(
        "commishdesk.narrate.llm.narrate_draft_recap", lambda *a, **k: warn
    )
    result = runner.invoke(
        app, ["--league", "71", "--draft-recap", "--out-dir", str(tmp_path), "--verbose"]
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "commishdesk-71-draft-recap.html").is_file()
    assert "content-safety" in result.output
    assert "slop" in result.output
    assert "league 71" in result.output


def test_safety_hold_lists_every_hold_finding(tmp_path: Path, monkeypatch) -> None:
    """Story 3.4 P6 — two holds in one narration: the exit-1 message names both."""
    from commishdesk.narrate import NarrationResult

    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    unsafe = NarrationResult(
        text=(
            "Pull-Guard Pumas clearly drafted hungover. "
            "Blitz Alpacas is an idiot, frankly."
        ),
        narrator="llm-primary",
    )
    monkeypatch.setattr(
        "commishdesk.narrate.llm.narrate_draft_recap", lambda *a, **k: unsafe
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


def test_llm_narrator_path_emits_text_and_a_bare_html_dump(
    tmp_path: Path, monkeypatch
) -> None:
    """Key present + a working provider: stdout is the LLM prose behind the
    ``generated <ts>`` stamp, and the HTML file is the bare ``narrated_text_to_html``
    dump (``<h1>``/``<h2>``, no ``<style>``) — not the structured template render."""
    _fake_real_league(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    reply = "Trench Warfare Rookie Recap\n\n## The Lead\n\nJeanty went 1.01, and it only got weirder.\n"
    _install_fake_anthropic(monkeypatch, reply)

    result = runner.invoke(
        app, ["--league", "77", "--draft-recap", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert re.search(r"generated \d{4}-\d{2}-\d{2}T", result.output)
    assert "Jeanty went 1.01, and it only got weirder." in result.output
    # the structured template sections must NOT be what got rendered
    assert "The Board — Round 1" not in result.output

    html_path = tmp_path / "commishdesk-77-draft-recap.html"
    body = html_path.read_text(encoding="utf-8")
    assert body.startswith("<!doctype html>")
    assert "<h1>Trench Warfare Rookie Recap</h1>" in body
    assert "<h2>The Lead</h2>" in body
    assert "<p>generated " in body  # provenance stamp on the bare dump too
    assert "<style>" not in body and "<script" not in body

    raw = html_path.read_bytes()
    assert b"\r\n" not in raw  # LF-only on disk
    raw.decode("utf-8")  # valid UTF-8, no surrogate escapes
