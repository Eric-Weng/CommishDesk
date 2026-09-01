"""Story 1.2 scaffold checks: the six stage packages import and the CLI surface behaves."""

from __future__ import annotations

import importlib

from typer.testing import CliRunner

from commishdesk.cli import app

runner = CliRunner()

STAGES = ("ingest", "stats", "facts", "narrate", "render", "deliver")


def test_all_stage_packages_import() -> None:
    for stage in STAGES:
        module = importlib.import_module(f"commishdesk.{stage}")
        assert module.__doc__, f"commishdesk.{stage} is missing its pipeline-role docstring"


def test_help_lists_every_option() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for option in ("--league", "--week", "--draft-recap", "--verbose"):
        assert option in result.output


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
