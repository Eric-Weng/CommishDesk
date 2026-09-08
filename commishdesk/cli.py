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
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from commishdesk import __version__
from commishdesk.errors import CommishDeskError
from commishdesk.logconfig import configure_logging, log_context

if TYPE_CHECKING:
    from commishdesk.llmconfig import LLMConfig
    from commishdesk.voices import Voice

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
    league: str | None = typer.Option(
        None, "--league", help="Sleeper league id to build a recap for (or 'demo')."
    ),
    week: int | None = typer.Option(
        None,
        "--week",
        min=1,
        max=18,
        help="NFL week (1-18) for a weekly recap; omit for a draft recap.",
    ),
    draft_recap: bool = typer.Option(
        False, "--draft-recap", help="Build the draft recap instead of a weekly recap."
    ),
    llm: bool | None = typer.Option(
        None,
        "--llm/--no-llm",
        help=(
            "Force the voiced LLM narrator on/off. Default: on when a provider API "
            "key is set (ANTHROPIC_API_KEY / LLM_API_KEY / GEMINI_API_KEY / "
            "GOOGLE_API_KEY). '--league demo' is always the template narrator."
        ),
    ),
    out_dir: Path = typer.Option(  # noqa: B008 -- canonical typer idiom
        Path("."), "--out-dir", help="Directory to write the recap HTML file into."
    ),
    verbose: bool = typer.Option(
        False, "--verbose", help="Increase output verbosity."
    ),
    version: bool | None = typer.Option(
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
                exit_code = _run_draft_recap(
                    league, out_dir, logger, llm_enabled=_llm_enabled(llm)
                )
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


#: Any one of these, set and non-blank, turns the LLM narrator on by default.
_LLM_KEY_VARS = ("ANTHROPIC_API_KEY", "LLM_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")


def _llm_enabled(cli_flag: bool | None) -> bool:
    """Resolve the narrator selection: an explicit ``--llm`` / ``--no-llm`` wins;
    otherwise the LLM narrator is on when a provider API key is present."""
    if cli_flag is not None:
        return cli_flag
    return any((os.environ.get(name) or "").strip() for name in _LLM_KEY_VARS)


def _run_draft_recap(
    league: str, out_dir: Path, logger: logging.Logger, *, llm_enabled: bool
) -> int:
    """Derive the run list (the one Generation Set constructor) and build a recap
    for each activated league. Returns the process exit code: ``0`` when every
    league in the set produced a recap, ``1`` when one or more faulted (each
    fault is isolated and printed as a one-line message — AD-9). Raises
    :class:`~commishdesk.errors.CommishDeskError` before any league runs when the
    league is not activated for a run or a ``COMMISHDESK_LLM_*`` value is
    malformed."""
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

    # The zero-credential demo path must not depend on LLM config parsing at all:
    # only load (and validate) the voice + model config when the run list holds a
    # real league. A malformed COMMISHDESK_LLM_* value then raises NarratorError
    # here — before any league runs — as an exit-1 one-liner, never mid-batch.
    voice: Voice | None = None
    llm_config: LLMConfig | None = None
    if any(resolved != DEMO_LEAGUE_ID for resolved in run_list):
        from commishdesk.llmconfig import load_llm_config
        from commishdesk.voices import load_default_voice

        voice = load_default_voice()
        llm_config = load_llm_config()

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
                voice=voice,
                llm_config=llm_config,
                llm_enabled=llm_enabled,
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
    voice: Voice | None,
    llm_config: LLMConfig | None,
    llm_enabled: bool,
) -> None:
    """Chain the five pipeline stages for one league and emit the recap to stdout
    plus a local HTML file."""
    from commishdesk.demo import demo_consensus_slots, load_demo_bundle
    from commishdesk.errors import NarratorError
    from commishdesk.facts import build_draft_recap_facts
    from commishdesk.facts.schema import Storyline
    from commishdesk.facts.storylines import DRAFT_RECAP_WEEK, advance_storylines
    from commishdesk.ingest import build_league_model
    from commishdesk.narrate import check_narration, recap_to_text, render_draft_recap
    from commishdesk.render import (
        narrated_text_to_html,
        write_draft_recap,
        write_html_file,
    )
    from commishdesk.stats import (
        compute_board_metrics,
        compute_consensus_metrics,
        compute_draft_grades,
    )
    from commishdesk.store import FileStore

    generated_at = datetime.now(tz=UTC)

    # Storyline persistence bridge — mirrors the consensus bridge below: the Store
    # I/O lives here in the CLI, never in ``facts/`` (import fence). The demo path
    # stays side-effect-free and offline, so it never touches the store and
    # ``previous_storylines`` stays empty for it.
    store: FileStore | None = None
    previous_storylines: list[Storyline] = []

    if resolved == demo_id:
        logger.debug("loading committed demo fixture")
        model = build_league_model(load_demo_bundle())
        slots = demo_consensus_slots()
        consensus_source_name: str | None = demo_source_name
        consensus_as_of: str | None = demo_as_of
        # I4: the onboarding sample is stats + templated prose only — no model call,
        # no SDK import, byte-deterministic — regardless of any provider key.
        if llm_enabled:
            logger.info("--league demo always uses the template narrator")
        llm_enabled = False
    else:
        from commishdesk.adapters.sleeper import SleeperAdapter
        from commishdesk.consensus import build_consensus_rank

        logger.debug("fetching Sleeper board")
        adapter = SleeperAdapter()
        try:
            bundle = adapter.fetch(resolved)
        finally:
            adapter.close()
        model = build_league_model(bundle)
        logger.debug("fetching consensus rank")
        store = FileStore(_cache_dir())
        rank = build_consensus_rank(model, store)
        slots = rank.slots
        consensus_source_name = rank.source
        consensus_as_of = rank.as_of
        previous_storylines = store.read_storylines(resolved)

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
        previous_storylines=previous_storylines,
    )

    if store is not None:
        logger.debug("persisting storylines")
        next_storylines = [
            storyline.model_copy(update={"league_id": resolved})
            for storyline in advance_storylines(
                previous_storylines,
                week=DRAFT_RECAP_WEEK,
                board=board,
                consensus=consensus,
                grades=grades,
                draft_summary=doc.draft_summary,
                superlatives=doc.superlatives,
            )
        ]
        store.write_storylines(resolved, next_storylines)

    logger.debug("narrating and rendering local HTML")
    dest = Path(out_dir) / f"commishdesk-{resolved}-draft-recap.html"

    narrator = "template"
    narrated_text = ""
    if llm_enabled:
        from commishdesk.narrate import narrate_draft_recap

        # When llm_enabled is True the run list held a real league, so the voice
        # and model config were loaded and validated before the loop.
        assert voice is not None and llm_config is not None
        # A provider/generation fault is swallowed inside narrate_draft_recap to
        # narrator="template" — it never reaches here. The HTML path keys off the
        # result's narrator, not llm_enabled: only a real llm-primary /
        # llm-fallback result takes the bare text→HTML dump (Design Notes).
        result = narrate_draft_recap(
            doc.narration, voice, llm_config, llm_enabled=True
        )
        narrator, narrated_text = result.narrator, result.text

    # AD-12 Layer 2 minimal gate: check the narrator's own prose (not the CLI's
    # ``generated <ts>`` provenance line) before anything is emitted. A
    # ``hold_issue`` finding raises ``NarratorError`` — caught per league in
    # ``_run_draft_recap`` → one-line stderr, exit 1, no HTML (AD-9). Everything
    # else warns and proceeds; Story 3.5 owns the graded tiered response.
    template_recap = render_draft_recap(doc.narration) if narrator == "template" else None
    safety_body = recap_to_text(template_recap) if template_recap is not None else narrated_text
    report = check_narration(safety_body, doc.narration, voice=voice)
    for finding in report.findings:
        logger.warning(
            "league %s content-safety (%s/%s): %s",
            resolved,
            finding.category,
            finding.severity,
            finding.message,
        )
    if report.held:
        holds = [f.message for f in report.findings if f.severity == "hold_issue"]
        raise NarratorError(
            f"content-safety hold for league {resolved}: " + "; ".join(holds)
        )

    if narrator == "template":
        assert template_recap is not None
        recap = template_recap.model_copy(
            update={"dateline": f"{template_recap.dateline} · generated {doc.generated_at}"}
        )
        typer.echo(recap_to_text(recap))
        written = write_draft_recap(recap, dest)
    else:
        logger.debug("llm narrator produced prose (%s)", narrator)
        typer.echo(f"generated {doc.generated_at}\n\n{narrated_text}")
        written = write_html_file(
            narrated_text_to_html(narrated_text, generated_at=str(doc.generated_at)),
            dest,
        )

    typer.echo(str(written))


def main() -> None:
    app()
