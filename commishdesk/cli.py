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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from commishdesk import __version__
from commishdesk.errors import CommishDeskError
from commishdesk.logconfig import configure_logging, log_context

if TYPE_CHECKING:
    from commishdesk.facts.schema import DraftRecapFacts
    from commishdesk.llmconfig import LLMConfig
    from commishdesk.narrate import Recap, SafetyReport, TieredResponse
    from commishdesk.voices import Voice


@dataclass(frozen=True)
class IssueBody:
    """The narrated body of one Issue, ready to render.

    Exactly one of ``recap`` (the template narrator's structured
    :class:`~commishdesk.narrate.Recap`, possibly with sections suppressed) or
    ``llm_text`` (the validated LLM prose) is set, per ``narrator``.
    """

    narrator: str
    recap: Recap | None = None
    llm_text: str | None = None

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
    allow_content_hold: bool | None = typer.Option(
        None,
        "--allow-content-hold/--no-allow-content-hold",
        help=(
            "Ship the Issue anyway when a content-safety check would hold it. The "
            "hold is logged at error and echoed to stderr instead of failing the "
            "league. Operator override for a false positive. Default: off, unless "
            "COMMISHDESK_ALLOW_CONTENT_HOLD is set; --no-allow-content-hold forces "
            "the hold back on for one run regardless of the environment."
        ),
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
                    league,
                    out_dir,
                    logger,
                    llm_enabled=_llm_enabled(llm),
                    allow_content_hold=_allow_content_hold(allow_content_hold),
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
    """Resolve the narrator selection (FR-17): an explicit ``--llm`` / ``--no-llm``
    wins; otherwise the LLM narrator is on when a provider API key is present, and
    the template narrator otherwise. ``--league demo`` is always the template
    narrator regardless of this result (forced in :func:`_recap_one_league`).

    This is *selection*, not a budget gate: whatever it returns, every league in
    the run still yields a complete Issue — a stubbed-failing or unusable LLM
    endpoint degrades to the template narrator, never to "no Issue". A run-cost
    ceiling / spend gate (AD-7 / AD-21) is a v1 concern and is not applied here.
    """
    if cli_flag is not None:
        return cli_flag
    return any((os.environ.get(name) or "").strip() for name in _LLM_KEY_VARS)


#: The environment equivalent of ``--allow-content-hold``, for an unattended run
#: that cannot pass a flag (a cron / CI job).
_ALLOW_CONTENT_HOLD_VAR = "COMMISHDESK_ALLOW_CONTENT_HOLD"

#: Values of :data:`_ALLOW_CONTENT_HOLD_VAR` that mean "off". Unlike
#: :func:`_llm_enabled` — where the variables are *keys*, so mere presence is the
#: signal — this one is a switch, and ``=0`` must not read as "on".
_FALSY = frozenset({"0", "false", "no", "off"})


def _allow_content_hold(cli_flag: bool | None) -> bool:
    """Resolve the AD-12 Layer-3 operator override, exactly as
    :func:`_llm_enabled` resolves narrator selection: an explicit
    ``--allow-content-hold`` / ``--no-allow-content-hold`` wins either way;
    otherwise :data:`_ALLOW_CONTENT_HOLD_VAR` set to anything non-blank outside
    :data:`_FALSY`.

    The negative form is not decoration: once the variable is exported in a shell
    profile or baked into a CI image there must still be a way to force the
    fail-closed behaviour back on for a single run.

    When on, a content-safety **hold** is downgraded to a loud ``logger.error`` +
    a stderr line and the best available body ships (the untrimmed template
    ``Recap``, or the validated LLM text). It is the operator's escape hatch from
    a deterministic false positive — the Epic 3 retro found several — and it
    bypasses **nothing else**: a provider fault, a structural failure, and the
    AD-9 per-league catch are all untouched.
    """
    if cli_flag is not None:
        return cli_flag
    raw = (os.environ.get(_ALLOW_CONTENT_HOLD_VAR) or "").strip()
    return bool(raw) and raw.lower() not in _FALSY


def _run_draft_recap(
    league: str,
    out_dir: Path,
    logger: logging.Logger,
    *,
    llm_enabled: bool,
    allow_content_hold: bool,
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

    # The zero-credential demo path never *depends* on LLM config: the voice +
    # model config are loaded and validated only when the run list holds a real
    # league, where a malformed COMMISHDESK_LLM_* value raises NarratorError here
    # — before any league runs — as an exit-1 one-liner, never mid-batch. On a
    # demo-only run list a malformed value is *tolerated*: it is parsed once, only
    # to emit a single warning that it is being ignored (never to fail the run).
    voice: Voice | None = None
    llm_config: LLMConfig | None = None
    if any(resolved != DEMO_LEAGUE_ID for resolved in run_list):
        from commishdesk.llmconfig import load_llm_config
        from commishdesk.voices import load_default_voice

        voice = load_default_voice()
        llm_config = load_llm_config()
    elif any(name.startswith("COMMISHDESK_LLM_") for name in os.environ):
        from commishdesk.llmconfig import load_llm_config

        try:
            load_llm_config()
        except CommishDeskError:
            logger.warning(
                "ignoring a malformed COMMISHDESK_LLM_* value: --league demo always "
                "uses the template narrator and never parses LLM config"
            )

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
                allow_content_hold=allow_content_hold,
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
    allow_content_hold: bool,
) -> None:
    """Chain the five pipeline stages for one league and emit the recap to stdout
    plus a local HTML file."""
    from commishdesk.demo import demo_consensus_slots, load_demo_bundle
    from commishdesk.facts import build_draft_recap_facts
    from commishdesk.facts.schema import Storyline
    from commishdesk.facts.storylines import DRAFT_RECAP_WEEK, advance_storylines
    from commishdesk.ingest import build_league_model
    from commishdesk.narrate import recap_to_text
    from commishdesk.render import (
        render_email,
        render_web,
        write_html_file,
        write_text_file,
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

    # AD-12 Layer 3 + FR-17: select the narrator, validate its output, apply the
    # tiered response (suppress a section / one LLM regeneration / hold the
    # Issue), and degrade to the template narrator whenever LLM prose cannot be
    # cleanly repaired. A hold raises ``ContentSafetyError`` — caught per league
    # in ``_run_draft_recap`` → one-line stderr, exit 1, no HTML (AD-9).
    body = _produce_issue(
        doc,
        voice,
        llm_config,
        llm_enabled=llm_enabled,
        logger=logger,
        resolved=resolved,
        allow_content_hold=allow_content_hold,
    )

    if body.narrator == "template":
        assert body.recap is not None
        recap = body.recap.model_copy(
            update={"dateline": f"{body.recap.dateline} · generated {doc.generated_at}"}
        )
        typer.echo(recap_to_text(recap))
    else:
        assert body.llm_text is not None
        logger.debug("llm narrator produced prose (%s)", body.narrator)
        typer.echo(f"generated {doc.generated_at}\n\n{body.llm_text}")

    # Story 4.1: both narrator paths write the one designed, self-contained page.
    written = write_html_file(
        render_web(
            doc,
            recap=body.recap,
            llm_text=body.llm_text,
            output_id=resolved,
            generated_at=str(doc.generated_at),
        ),
        dest,
    )
    typer.echo(str(written))

    # Story 4.2: the same content model, rendered for email — client-safe HTML
    # plus a text/plain alternative. No delivery here (Stories 4.3-4.6); the CLI
    # just writes the two files next to the web page.
    email_parts = render_email(
        doc,
        recap=body.recap,
        llm_text=body.llm_text,
        generated_at=str(doc.generated_at),
    )
    email_html = write_html_file(
        email_parts.html,
        Path(out_dir) / f"commishdesk-{resolved}-draft-recap.email.html",
    )
    email_text = write_text_file(
        email_parts.text,
        Path(out_dir) / f"commishdesk-{resolved}-draft-recap.txt",
    )
    typer.echo(str(email_html))
    typer.echo(str(email_text))


def _produce_issue(
    doc: DraftRecapFacts,
    voice: Voice | None,
    llm_config: LLMConfig | None,
    *,
    llm_enabled: bool,
    logger: logging.Logger,
    resolved: str,
    allow_content_hold: bool,
) -> IssueBody:
    """Narrator selection + AD-12 Layer 3 tiered response for one league.

    ``llm_enabled=False`` → the template narrator, checked and (if a league-
    supplied string tripped a pattern) section-suppressed or held. ``True`` → the
    ``primary → fallback → template`` selection, then: validate the completion's
    shape (:func:`~commishdesk.narrate.structural_ok` on
    :func:`~commishdesk.narrate.sanitize_completion` output — a miss is a failed
    generation → template); run the safety check; :func:`~commishdesk.narrate.classify`
    the report; on ``hold`` raise :class:`~commishdesk.errors.ContentSafetyError`,
    on ``regenerate`` re-narrate once then degrade, on ``regenerate`` / ``suppress``
    otherwise degrade straight to the template. A clean pass ships the LLM prose.

    The one permitted regeneration is spent only on a ``regenerate`` (hallucination)
    tier; a ``suppress`` tier or a ``structural_ok`` failure degrades to the
    template with no retry. ``narrate_draft_recap`` is invoked at most twice for
    one league-week (the CLI-level ceiling above I3's single paid call).

    ``allow_content_hold=True`` (``--allow-content-hold`` /
    ``COMMISHDESK_ALLOW_CONTENT_HOLD``) downgrades every **hold** here to a loud
    ``logger.error`` carrying ``OVERRIDDEN`` plus every reason, a distinct stderr
    echo, and the best available body — the untrimmed template ``Recap``, or the
    validated LLM text. It changes nothing else: the alerts still fire, the
    checks still run, and a provider fault / structural failure / the AD-9
    per-league catch behave exactly as before.
    """
    from commishdesk.errors import ContentSafetyError
    from commishdesk.narrate import (
        check_narration,
        classify,
        recap_to_text,
        render_draft_recap,
        sanitize_completion,
        structural_ok,
        suppress_sections,
    )

    narration = doc.narration

    def _emit_alerts(alerts: tuple[str, ...]) -> None:
        """One structured ``logger.error`` per driving finding, plus a one-line
        stderr message distinct from the AD-9 per-league fault line."""
        for line in alerts:
            logger.error("league %s content-safety: %s", resolved, line)
            typer.echo(f"content-safety alert for league {resolved}: {line}", err=True)

    def _hold(reasons: tuple[str, ...]) -> ContentSafetyError | None:
        """The hold, unless the operator overrode it.

        Returns the :class:`~commishdesk.errors.ContentSafetyError` for the caller
        to ``raise``, or ``None`` when ``allow_content_hold`` is set — in which
        case it has already logged the override at ``error`` (with ``OVERRIDDEN``
        and every reason) and echoed a stderr line distinct from both the AD-9
        fault line and the per-finding ``_emit_alerts`` lines. The caller then
        ships the best available body."""
        joined = "; ".join(reasons)
        if not allow_content_hold:
            return ContentSafetyError(
                f"content-safety hold for league {resolved}: {joined}"
            )
        logger.error(
            "league %s content-safety hold OVERRIDDEN (--allow-content-hold): %s",
            resolved,
            joined,
        )
        typer.echo(
            f"content-safety hold OVERRIDDEN for league {resolved} "
            f"(--allow-content-hold): {joined}",
            err=True,
        )
        return None

    def _log_filtered_findings(report: SafetyReport, decision: TieredResponse) -> None:
        """Findings ``classify`` dropped (today only template-narrator
        hallucinations) are still worth seeing at DEBUG for a maintainer chasing
        a false positive — they never reach :func:`_emit_alerts`."""
        actioned = set(decision.alerts)
        for finding in report.findings:
            line = f"{finding.category}/{finding.severity}: {finding.message}"
            if line not in actioned:
                logger.debug(
                    "league %s content-safety finding not actioned (%s): %s",
                    resolved,
                    finding.severity,
                    finding.message,
                )

    def _template_issue() -> IssueBody:
        recap = render_draft_recap(narration)
        report = check_narration(recap_to_text(recap), narration, voice=voice)
        decision = classify(report, narrator_is_template=True)
        _log_filtered_findings(report, decision)
        # classify() filters every hallucination finding for the template
        # narrator, and only hallucinations carry the ``regenerate`` tier, so
        # decision.regenerate is structurally always False here. If that ever
        # changes, the template narrator has no retry to spend — treat it as
        # un-repairable and hold rather than silently dropping the flag.
        if decision.hold or decision.regenerate:
            _emit_alerts(decision.alerts)
            reasons = decision.hold_reasons or (
                "a regenerate-tier finding on the template narrator, which has no "
                "regeneration to spend",
            )
            held = _hold(reasons)
            if held is not None:
                raise held
            return IssueBody(narrator="template", recap=recap)
        if decision.suppress:
            _emit_alerts(decision.alerts)
            trimmed, removed = suppress_sections(recap, report)
            if not removed:
                # a suppress-tier finding that maps to no section: the flagged
                # phrase is still in the recap (title / dateline / a split
                # artefact). Un-localizable → never ship it (unless overridden).
                held = _hold(
                    (
                        "a suppress-tier content-safety finding could not be "
                        "localized to a section",
                    )
                )
                if held is not None:
                    raise held
                return IssueBody(narrator="template", recap=recap)
            if trimmed is None:
                held = _hold(
                    (
                        "section suppression would remove The Lead or leave fewer "
                        f"than two sections (offending: {', '.join(removed)})",
                    )
                )
                if held is not None:
                    raise held
                # the override ships the *untrimmed* recap: the trimmed one is
                # unshippable by construction (no Lead / fewer than two sections).
                return IssueBody(narrator="template", recap=recap)
            logger.warning(
                "league %s content-safety: suppressed section(s) %s",
                resolved,
                ", ".join(removed),
            )
            recap = trimmed
        return IssueBody(narrator="template", recap=recap)

    if not llm_enabled:
        return _template_issue()

    # When llm_enabled is True the run list held a real league, so the voice and
    # model config were loaded and validated before the loop.
    assert voice is not None and llm_config is not None
    from commishdesk.narrate import narrate_draft_recap

    attempts = 0
    while True:
        attempts += 1
        result = narrate_draft_recap(narration, voice, llm_config, llm_enabled=True)
        if result.narrator == "template":
            # every provider attempt failed inside narrate_draft_recap
            return _template_issue()

        text = sanitize_completion(result.text)
        if not structural_ok(text):
            logger.warning(
                "league %s: LLM narration is not the expected six-section shape "
                "(attempt %d); using the template narrator",
                resolved,
                attempts,
            )
            return _template_issue()

        report = check_narration(text, narration, voice=voice)
        decision = classify(report, narrator_is_template=False)
        _log_filtered_findings(report, decision)

        if decision.hold:
            _emit_alerts(decision.alerts)
            held = _hold(decision.hold_reasons)
            if held is not None:
                raise held
            return IssueBody(narrator=result.narrator, llm_text=text)

        if decision.regenerate and attempts == 1:
            logger.warning(
                "league %s: content-safety regeneration of the LLM narration "
                "(one attempt permitted — AD-12 Layer 3)",
                resolved,
            )
            continue

        if decision.regenerate or decision.suppress:
            _emit_alerts(decision.alerts)
            logger.warning(
                "league %s: LLM narration still unclean after %d attempt(s); "
                "using the template narrator",
                resolved,
                attempts,
            )
            return _template_issue()

        return IssueBody(narrator=result.narrator, llm_text=text)


def main() -> None:
    app()
