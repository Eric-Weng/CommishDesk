"""The `commishdesk` console script — argument surface only; no pipeline is wired yet."""

from __future__ import annotations

from typing import Optional

import typer

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Build a weekly or draft recap for a Sleeper fantasy football league.",
)


@app.callback(invoke_without_command=True)
def run(
    league: Optional[str] = typer.Option(
        None, "--league", help="Sleeper league id to build a recap for."
    ),
    week: Optional[int] = typer.Option(
        None, "--week", help="NFL week (1-18) for a weekly recap; omit for a draft recap."
    ),
    draft_recap: bool = typer.Option(
        False, "--draft-recap", help="Build the draft recap instead of a weekly recap."
    ),
    verbose: bool = typer.Option(
        False, "--verbose", help="Emit DEBUG-level logs."
    ),
) -> None:
    """Print a not-yet-implemented notice and exit 0 (Story 1.2 scaffold)."""
    if draft_recap:
        mode = "draft recap"
    elif week is not None:
        mode = f"week {week} recap"
    else:
        mode = "recap"
    league_label = league if league else "<none>"
    typer.echo(
        f"CommishDesk: {mode} for league {league_label} is not yet implemented "
        "(Story 1.2 scaffold)."
    )
    raise typer.Exit(code=0)


def main() -> None:
    app()
