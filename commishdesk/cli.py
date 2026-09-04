"""The ``commishdesk`` console script — chains ``ingest → stats → facts → narrate(template) → render``.

A draft recap for ``--league demo`` runs offline against the committed fixture; a
real Sleeper id fetches the board from Sleeper and the consensus rank from
FantasyCalc (``/values/current``), with the Sleeper players file as the offline
fallback. The weekly recap is still Epic 5 (``--week`` alone prints a
not-yet-implemented notice). ``cli.py`` is the only module that imports across
every pipeline stage (AD-1).
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from commishdesk import __version__
from commishdesk.errors import CommishDeskError
from commishdesk.logconfig import configure_logging, log_context

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Build a weekly or draft recap for a Sleeper fantasy football league.",
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"commishdesk {__version__}")
        raise typer.Exit(code=0)


def _cache_dir() -> Path:
    """Where the consensus-rank fetch is cached between real-league runs —
    ``$XDG_CACHE_HOME/commishdesk`` when set, else ``~/.cache/commishdesk``, and a
    temp dir when the home directory cannot be resolved."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / "commishdesk"
    try:
        return Path.home() / ".cache" / "commishdesk"
    except RuntimeError:
        return Path(tempfile.gettempdir()) / "commishdesk-cache"


@app.callback(invoke_without_command=True)
def run(
    league: Optional[str] = typer.Option(
        None, "--league", help="Sleeper league id to build a recap for (or 'demo')."
    ),
    week: Optional[int] = typer.Option(
        None,
        "--week",
        min=1,
        max=18,
        help="NFL week (1-18) for a weekly recap; omit for a draft recap.",
    ),
    draft_recap: bool = typer.Option(
        False, "--draft-recap", help="Build the draft recap instead of a weekly recap."
    ),
    out_dir: Path = typer.Option(
        Path("."), "--out-dir", help="Directory to write the recap HTML file into."
    ),
    verbose: bool = typer.Option(
        False, "--verbose", help="Increase output verbosity."
    ),
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Print the commishdesk version and exit.",
    ),
) -> None:
    """Configure logging, bind the league/week log context, and dispatch to the
    draft-recap pipeline (or the not-yet-implemented weekly recap)."""
    logger = configure_logging(verbose)
    with log_context(league_id=league, week=week):
        if draft_recap and week is not None:
            raise typer.BadParameter(
                "--draft-recap builds the draft recap and cannot be combined with --week"
            )
        if draft_recap:
            if not league:
                raise typer.BadParameter("--draft-recap requires --league")
            logger.debug("cli invoked: mode=draft recap")
            try:
                exit_code = _run_draft_recap(league, out_dir, logger)
            except (CommishDeskError, OSError) as exc:
                typer.echo(_one_line(exc), err=True)
                raise typer.Exit(code=1) from exc
            raise typer.Exit(code=exit_code)

        mode = f"week {week} recap" if week is not None else "recap"
        logger.debug("cli invoked: mode=%s", mode)
        league_label = league if league else "<none>"
        typer.echo(
            f"CommishDesk: {mode} for league {league_label} is not yet implemented "
            "(weekly recap lands in Epic 5)."
        )
        raise typer.Exit(code=0)


def _one_line(exc: Exception) -> str:
    """A single-line, traceback-free message for a caught fault."""
    return str(exc) or exc.__class__.__name__


def _run_draft_recap(league: str, out_dir: Path, logger: logging.Logger) -> int:
    """Derive the run list (the one Generation Set constructor) and build a recap
    for each activated league. Returns the process exit code: ``0`` when every
    league in the set produced a recap, ``1`` when one or more faulted (each
    fault is isolated and printed as a one-line message — AD-9). Raises
    :class:`~commishdesk.errors.CommishDeskError` before any league runs when the
    league is not activated for a run."""
    from commishdesk.demo import (
        DEMO_CONSENSUS_AS_OF,
        DEMO_CONSENSUS_SOURCE_NAME,
        DEMO_LEAGUE_ID,
    )
    from commishdesk.generation import build_generation_set

    # I1 / AD-6: the run list is derived by the one Generation Set constructor —
    # no other code path adds a league to a run.
    run_list = build_generation_set([league]).league_ids
    if not run_list:
        raise CommishDeskError(f"league {league!r} is not activated for a run")

    exit_code = 0
    for resolved in run_list:
        try:
            _recap_one_league(
                resolved,
                out_dir,
                logger,
                demo_id=DEMO_LEAGUE_ID,
                demo_source_name=DEMO_CONSENSUS_SOURCE_NAME,
                demo_as_of=DEMO_CONSENSUS_AS_OF,
            )
        except (CommishDeskError, OSError) as exc:
            logger.debug("league %s faulted: %s", resolved, _one_line(exc))
            typer.echo(_one_line(exc), err=True)
            exit_code = 1
    return exit_code


def _recap_one_league(
    resolved: str,
    out_dir: Path,
    logger: logging.Logger,
    *,
    demo_id: str,
    demo_source_name: str,
    demo_as_of: str,
) -> None:
    """Chain the five pipeline stages for one league and emit the recap to stdout
    plus a local HTML file."""
    from commishdesk.demo import demo_consensus_slots, load_demo_bundle
    from commishdesk.facts import build_draft_recap_facts
    from commishdesk.ingest import build_league_model
    from commishdesk.narrate import recap_to_text, render_draft_recap
    from commishdesk.render import write_draft_recap
    from commishdesk.stats import (
        compute_board_metrics,
        compute_consensus_metrics,
        compute_draft_grades,
    )

    generated_at = datetime.now(tz=timezone.utc)

    if resolved == demo_id:
        logger.debug("loading committed demo fixture")
        model = build_league_model(load_demo_bundle())
        slots = demo_consensus_slots()
        consensus_source_name: str | None = demo_source_name
        consensus_as_of: str | None = demo_as_of
    else:
        from commishdesk.adapters.sleeper import SleeperAdapter
        from commishdesk.consensus import build_consensus_rank
        from commishdesk.store import FileStore

        logger.debug("fetching Sleeper board")
        adapter = SleeperAdapter()
        try:
            bundle = adapter.fetch(resolved)
        finally:
            adapter.close()
        model = build_league_model(bundle)
        logger.debug("fetching consensus rank")
        rank = build_consensus_rank(model, FileStore(_cache_dir()))
        slots = rank.slots
        consensus_source_name = rank.source
        consensus_as_of = rank.as_of

    logger.debug("computing board / consensus / grades")
    board = compute_board_metrics(model)
    consensus = compute_consensus_metrics(model, slots)
    grades = compute_draft_grades(model, consensus)

    logger.debug("building Facts JSON")
    doc = build_draft_recap_facts(
        model,
        board,
        consensus,
        grades,
        generated_at=generated_at,
        draft_id=model.draft.id,
        consensus_source_name=consensus_source_name,
        consensus_as_of=consensus_as_of,
    )

    logger.debug("narrating (template) and rendering local HTML")
    recap = render_draft_recap(doc.narration)
    recap = recap.model_copy(
        update={"dateline": f"{recap.dateline} · generated {doc.generated_at}"}
    )
    typer.echo(recap_to_text(recap))

    dest = Path(out_dir) / f"commishdesk-{resolved}-draft-recap.html"
    written = write_draft_recap(recap, dest)
    typer.echo(str(written))


def main() -> None:
    app()
