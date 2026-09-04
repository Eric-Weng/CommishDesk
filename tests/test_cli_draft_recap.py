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
    from commishdesk import consensus, demo
    from commishdesk.consensus import ConsensusRank
    import commishdesk.facts as facts_pkg

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


def test_real_league_id_with_no_network_is_exit_1_no_traceback() -> None:
    """A bogus Sleeper id cannot be fetched; the CLI prints a one-line message
    and exits 1 (no traceback)."""
    result = _run("--league", "0", "--draft-recap")
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
