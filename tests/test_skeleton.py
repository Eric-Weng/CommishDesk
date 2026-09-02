"""Story 1.2 scaffold checks: the six stage packages import and the CLI surface behaves."""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from typer.testing import CliRunner

import commishdesk
from commishdesk.cli import app

runner = CliRunner()

STAGES = ("ingest", "stats", "facts", "narrate", "render", "deliver")

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_all_stage_packages_import() -> None:
    for stage in STAGES:
        module = importlib.import_module(f"commishdesk.{stage}")
        assert module.__doc__, f"commishdesk.{stage} is missing its pipeline-role docstring"


def test_help_lists_every_option() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for option in ("--league", "--week", "--draft-recap", "--verbose"):
        assert option in result.output
    # "with descriptions": a distinctive fragment of each option's help text is shown.
    for fragment in (
        "Sleeper league id",
        "NFL week",
        "draft recap instead",
        "output verbosity",
    ):
        assert fragment in result.output


def test_draft_recap_reports_not_yet_implemented() -> None:
    result = runner.invoke(app, ["--league", "123", "--draft-recap"])
    assert result.exit_code == 0
    assert "not yet implemented" in result.output
    assert "Traceback" not in result.output


def test_weekly_recap_reports_not_yet_implemented() -> None:
    result = runner.invoke(app, ["--league", "123", "--week", "5"])
    assert result.exit_code == 0
    assert "not yet implemented" in result.output
    assert "Traceback" not in result.output


def test_no_args_shows_usage_without_traceback() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code in (0, 2)
    assert "Usage" in result.output
    assert "Traceback" not in result.output


def test_unknown_option_is_a_usage_error() -> None:
    result = runner.invoke(app, ["--bogus"])
    assert result.exit_code == 2
    assert "Traceback" not in result.output


def test_week_out_of_range_is_rejected() -> None:
    for bad in ("0", "99"):
        result = runner.invoke(app, ["--league", "123", "--week", bad])
        assert result.exit_code == 2
        assert "Traceback" not in result.output


def test_version_is_consistent() -> None:
    assert commishdesk.__version__ == importlib.metadata.version("commishdesk")


def test_console_script_entry_point_runs_out_of_process() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.argv=['commishdesk','--help']; "
            "from commishdesk.cli import main; main()",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--league" in result.stdout


def test_console_script_entry_point_is_registered() -> None:
    scripts = importlib.metadata.entry_points(group="console_scripts")
    match = {ep.name: ep.value for ep in scripts}
    assert match.get("commishdesk") == "commishdesk.cli:main"


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; sys.argv={['commishdesk', *args]!r}; "
            "from commishdesk.cli import main; main()",
        ],
        capture_output=True,
        text=True,
    )


def _stderr_json(result: subprocess.CompletedProcess) -> list[dict]:
    return [
        json.loads(line)
        for line in result.stderr.splitlines()
        if line.strip().startswith("{")
    ]


def test_verbose_run_emits_context_stamped_debug_json_to_stderr() -> None:
    result = _run_cli("--league", "123", "--draft-recap", "--verbose")
    assert result.returncode == 0
    assert "not yet implemented" in result.stdout
    assert "Traceback" not in result.stdout
    records = _stderr_json(result)
    assert records, result.stderr
    debug = [r for r in records if r["level"] == "DEBUG"]
    assert debug, records
    assert all(r["league_id"] == "123" for r in debug)
    assert all("week" not in r for r in debug)  # --draft-recap has no week


def test_verbose_weekly_run_stamps_week_on_stderr_json() -> None:
    result = _run_cli("--league", "123", "--week", "5", "--verbose")
    assert result.returncode == 0
    records = _stderr_json(result)
    assert records, result.stderr
    assert any(r.get("league_id") == "123" and r.get("week") == 5 for r in records)


def test_plain_run_emits_no_log_lines_to_stderr() -> None:
    result = _run_cli("--league", "123", "--draft-recap")
    assert result.returncode == 0
    assert "not yet implemented" in result.stdout
    assert result.stderr.strip() == ""


def test_runtime_dependency_allowlist() -> None:
    deps = _pyproject()["project"]["dependencies"]
    names = {
        re.split(r"[<>=!~ \[]", spec, maxsplit=1)[0].strip().lower() for spec in deps
    }
    assert names == {"httpx", "pydantic", "typer"}
