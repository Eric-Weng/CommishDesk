"""The ``commishdesk`` console script — chains ``ingest → stats → facts → narrate(template) → render``.

A draft recap for ``--league demo`` runs offline against the committed fixture; a
real Sleeper id fetches the board from Sleeper and the consensus rank from
FantasyCalc (``/values/current``), with the Sleeper players file as the offline
fallback. A weekly recap (``--week``) chains the weekly ingest → stats → facts →
narrator (template, or the voiced LLM narrator once a key and a budget are
available — Story 5.12) → deliver path to a local text Issue plus the Story 2.7
generic HTML dump, and optionally posts an idempotent Discord summary. ``cli.py``
is the only module that imports across every pipeline stage (AD-1).
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from commishdesk import __version__
from commishdesk.errors import (
    CommishDeskError,
    CostCeilingExceededError,
    DeliveryError,
    StoreError,
)
from commishdesk.logconfig import configure_logging, log_context

if TYPE_CHECKING:
    from commishdesk.facts.schema import DraftRecapFacts, WeeklyFacts, WeeklyNarration
    from commishdesk.llmconfig import LLMConfig
    from commishdesk.narrate import Recap, SafetyReport, TieredResponse
    from commishdesk.narrate.llm import CallUsage
    from commishdesk.narrate.weekly_template import WeeklyIssue
    from commishdesk.store import FileStore, IssueKind, Store
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
    ctx: typer.Context,
    league: str | None = typer.Option(None, "--league", help="Sleeper league id to build a recap for (or 'demo')."),
    week: int | None = typer.Option(
        None,
        "--week",
        min=1,
        max=18,
        help="NFL week (1-18) for a weekly recap; omit for a draft recap.",
    ),
    draft_recap: bool = typer.Option(False, "--draft-recap", help="Build the draft recap instead of a weekly recap."),
    post: bool = typer.Option(
        False,
        "--post",
        help=(
            "Deliver the rendered Issue to Discord after rendering (requires "
            "--draft-recap or --week, and COMMISHDESK_DISCORD_WEBHOOK_URL). "
            "Idempotent — a recipient already confirmed for this league-week is "
            "not re-sent."
        ),
    ),
    reason: str | None = typer.Option(
        None,
        "--reason",
        help=(
            "Reissue a weekly recap as a correction, recording this text as the "
            "reason in the Send Ledger. Requires --week and --post, and a weekly "
            "post already confirmed for that league-week."
        ),
    ),
    llm: bool | None = typer.Option(
        None,
        "--llm/--no-llm",
        help=(
            "Force the voiced LLM narrator on/off. Default: on when a provider API "
            "key is set (ANTHROPIC_API_KEY / LLM_API_KEY / GEMINI_API_KEY / "
            "GOOGLE_API_KEY). '--league demo' is always the template narrator, and "
            "the weekly recap is always the template narrator."
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
    verbose: bool = typer.Option(False, "--verbose", help="Increase output verbosity."),
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
    draft-recap or weekly-recap pipeline.

    Adding the first ``@app.command`` (``verify-webhook``) flips Click into group
    mode, so this callback also fires ahead of a subcommand — return immediately
    in that case and let the subcommand own the run. Every bare
    ``commishdesk --league …`` invocation still lands here unchanged.

    Exactly one of ``--draft-recap`` / ``--week`` selects the mode; ``--post`` is
    legal with either. ``--reason`` is the weekly path's reissue escape hatch
    (Story 5.11c): it only means anything alongside ``--week --post``, so it is
    rejected on the draft-recap path, without ``--week``, without ``--post``, and
    as blank text — all before any fetch. Neither flag keeps the unchanged
    informational path.
    """
    if ctx.invoked_subcommand is not None:
        return
    logger = configure_logging(verbose)
    with log_context(league_id=league, week=week):
        if draft_recap and week is not None:
            raise typer.BadParameter("--draft-recap builds the draft recap and cannot be combined with --week")
        if post and not (draft_recap or week is not None):
            raise typer.BadParameter("--post requires --draft-recap or --week")
        if reason is not None:
            if draft_recap:
                # Kept short on purpose: this Click version prints a
                # ``BadParameter`` in a boxed panel that word-wraps to the
                # console width, and a longer message wraps the
                # "--draft-recap" tail onto its own line.
                raise typer.BadParameter("--reason cannot be combined with --draft-recap")
            if week is None:
                raise typer.BadParameter("--reason requires --week")
            if not post:
                raise typer.BadParameter("--reason requires --post")
            if not reason.strip():
                raise typer.BadParameter("--reason must be a non-blank string")
            # Normalized once, here, so the text the Send Ledger persists
            # (review-loop 1) is byte-identical to the text the Correction
            # section displays -- both read this same stripped value from now
            # on, never the raw option string.
            reason = reason.strip()
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
                    post=post,
                )
            except (CommishDeskError, OSError) as exc:
                typer.echo(_one_line(exc), err=True)
                raise typer.Exit(code=1) from exc
            raise typer.Exit(code=exit_code)

        if week is not None:
            if not league:
                raise typer.BadParameter("--week requires --league")
            if llm is not None:
                # Story 5.11a/5.12: the weekly narrator is chosen from the
                # environment (a provider key + a budget), never from this flag,
                # so an explicit --llm/--no-llm here would otherwise be silently
                # ignored.
                logger.warning(
                    "--%sllm has no effect on a weekly run: the weekly narrator "
                    "is chosen from the environment (Story 5.12)",
                    "" if llm else "no-",
                )
            logger.debug("cli invoked: mode=week %s recap", week)
            try:
                exit_code = _run_weekly(league, week, out_dir, logger, post=post, reason=reason)
            except (CommishDeskError, OSError) as exc:
                typer.echo(_one_line(exc), err=True)
                raise typer.Exit(code=1) from exc
            raise typer.Exit(code=exit_code)

        mode = "recap"
        logger.debug("cli invoked: mode=%s", mode)
        league_label = league if league else "<none>"
        typer.echo(
            f"CommishDesk: {mode} for league {league_label} is not yet implemented (weekly recap lands in Epic 5)."
        )
        raise typer.Exit(code=0)


def _one_line(exc: Exception) -> str:
    """A single-line, traceback-free message for a caught fault."""
    return str(exc) or exc.__class__.__name__


def _fmt_usd(value: float) -> str:
    """A USD amount at enough precision that two values within a cent of each
    other still read as visibly different (review-loop 1 — a naive two-decimal
    rounding made an over-ceiling error message show the same two numbers as the
    ceiling it exceeded)."""
    return f"${value:.4f}"


#: The Discord channel webhook the MVP delivers to. A secret — read from the
#: process environment only, never a file or a committed ``leagues/*.toml``.
_DISCORD_WEBHOOK_VAR = "COMMISHDESK_DISCORD_WEBHOOK_URL"

#: Per league-week, at most two narration *attempts* can each yield one
#: successful, fully-billed completion: the initial attempt, and the single
#: ``regenerate``-tier re-narration ``_produce_issue`` permits (I3
#: reconciliation, epic-3-retro-item-44 / sprint-status.yaml: "at most one
#: successful generation per narration attempt, at most two narration attempts
#: per league-week"). ``RETRY_CAP``-driven transient retries never produce a
#: billed completion, so they need no multiplier here.
_MAX_BILLABLE_NARRATION_ATTEMPTS = 2

#: Per league-week, at most one claim-verification call (content-safety P1).
#: ``_produce_issue`` verifies only the prose it is about to ship, and both of its
#: LLM ship paths return through that one check — so a regeneration never buys a
#: second verification, and member count never enters into it.
_MAX_BILLABLE_VERIFICATION_CALLS = 1


@app.command("verify-webhook")
def verify_webhook(
    league: str = typer.Option(
        ...,
        "--league",
        help="Sleeper league id to name in the test post (or 'demo').",
    ),
    verbose: bool = typer.Option(False, "--verbose", help="Increase output verbosity."),
) -> None:
    """Post a visible test message to the configured Discord webhook and accept
    the destination only if Discord accepts the post (FR-25).

    Reads the webhook URL from the COMMISHDESK_DISCORD_WEBHOOK_URL environment
    variable (a secret — never a file or committed config) and names the league in
    the message so you can see it land in the right channel. A missing variable, a
    non-Discord URL, a webhook the API rejects, or an unreachable host each print
    one line to stderr and exit 1 with no traceback; the destination is not
    accepted.
    """
    # Lazy import: keeps ``httpx`` off the default CLI import path (I4 / test_I4).
    from commishdesk.deliver.discord import post_discord_text

    logger = configure_logging(verbose)
    with log_context(league_id=league):
        url = (os.environ.get(_DISCORD_WEBHOOK_VAR) or "").strip()
        if not url:
            typer.echo(
                f"set {_DISCORD_WEBHOOK_VAR} to the league's Discord channel webhook URL before running verify-webhook",
                err=True,
            )
            raise typer.Exit(code=1)

        try:
            name = _resolve_league_name(league, logger)
        except (CommishDeskError, OSError) as exc:
            typer.echo(_one_line(exc), err=True)
            raise typer.Exit(code=1) from exc

        try:
            accepted_id = post_discord_text(url, f"CommishDesk test post — {name}. Webhook is working.")
        except DeliveryError as exc:
            typer.echo(_one_line(exc), err=True)
            raise typer.Exit(code=1) from exc

        logger.info(
            "verify-webhook accepted Discord webhook id %s for league %s",
            accepted_id,
            league,
        )
        typer.echo(f"Discord webhook accepted — a test post naming {name!r} is in the channel.")


def _resolve_league_name(league: str, logger: logging.Logger) -> str:
    """The league's display name, for the verify-webhook test post. ``demo``
    resolves offline against the committed fixture; a real id is fetched from
    Sleeper (adapter lifecycle per :func:`_recap_one_league`)."""
    from commishdesk.demo import DEMO_LEAGUE_ID, load_demo_bundle
    from commishdesk.ingest import build_league_model

    if league == DEMO_LEAGUE_ID:
        logger.debug("resolving the demo league name offline")
        return build_league_model(load_demo_bundle()).name

    from commishdesk.adapters.sleeper import SleeperAdapter

    logger.debug("fetching the league name from Sleeper")
    adapter = SleeperAdapter()
    try:
        bundle = adapter.fetch(league)
    finally:
        adapter.close()
    return build_league_model(bundle).name


#: Any one of these, set and non-blank, turns the LLM narrator on by default.
_LLM_KEY_VARS = ("ANTHROPIC_API_KEY", "LLM_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")


def _llm_enabled(cli_flag: bool | None) -> bool:
    """Resolve the draft-recap narrator selection (FR-17): an explicit ``--llm``
    / ``--no-llm`` wins; otherwise the LLM narrator is on when a provider API key
    is present, and the template narrator otherwise. ``--league demo`` is always
    the template narrator regardless of this result (forced in
    :func:`_recap_one_league`).

    The weekly path (Story 5.12) does **not** consult an explicit flag: it calls
    this with ``cli_flag=None``, so its narrator is chosen purely from the
    environment, and a ``--llm``/``--no-llm`` on a weekly run is ignored with a
    warning. That is deliberate — the weekly run is scheduled (Story 5.11b) and
    has no interactive operator to pass a flag, so "a key is present" is the only
    signal that can decide it.

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
    post: bool,
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
                post=post,
            )
        except (CommishDeskError, OSError) as exc:
            logger.debug("league %s faulted: %s", resolved, _one_line(exc))
            typer.echo(_one_line(exc), err=True)
            exit_code = 1
    return exit_code


def _run_weekly(
    league: str,
    week: int,
    out_dir: Path,
    logger: logging.Logger,
    *,
    post: bool,
    reason: str | None = None,
) -> int:
    """Derive the run list (the one Generation Set constructor) and build a weekly
    Issue for each activated league. Returns the process exit code: ``0`` when
    every league in the set produced an Issue, ``1`` when one or more faulted
    (each fault — a not-yet-final week, a cross-check hold, an
    :class:`~commishdesk.errors.OptimalLineupError`, a reissue with nothing to
    correct, anything else — is isolated and printed as a one-line message, AD-9).
    Raises :class:`~commishdesk.errors.CommishDeskError` before any league runs
    when the league is not activated for a run.

    ``reason`` (Story 5.11c) is the operator's explicit reissue: it turns this run
    into a correction of an already-confirmed weekly send. It is threaded
    unchanged down to :func:`_recap_one_league_weekly`, which owns both the
    "something to correct" guard and the corrected Issue's label.

    Story 5.12 gives this path an optional voiced narrator, but it stays a
    *degrading* one: the narrator is chosen from the environment (a provider key
    present **and** a pre-call cost estimate inside ``cost_ceiling_usd``), and no
    condition on this path ever raises
    :class:`~commishdesk.errors.CostCeilingExceededError` — an over-budget
    scheduled run silently falls back to the deterministic template. The cost
    machinery itself lives in :func:`_weekly_estimate_within_ceiling`.
    """
    from commishdesk.generation import build_generation_set

    run_list = build_generation_set([league]).league_ids
    if not run_list:
        raise CommishDeskError(f"league {league!r} is not activated for a run")

    exit_code = 0
    for resolved in run_list:
        try:
            _recap_one_league_weekly(resolved, week, out_dir, logger, post=post, reason=reason)
        except (CommishDeskError, OSError) as exc:
            logger.debug("league %s faulted: %s", resolved, _one_line(exc))
            typer.echo(_one_line(exc), err=True)
            exit_code = 1
    return exit_code


def _week_not_final_reason(nfl_state: object, week: int) -> str | None:
    """Story 5.11a: why the requested ``week`` is not final yet, per Sleeper's own
    ``GET /state/nfl`` projection carried on the week bundle's ``"nfl_state"`` key
    — or ``None`` when it is final.

    Not final when the season has not started (``season_type == "pre"``), or when
    the regular season has not advanced past ``week`` yet (``season_type ==
    "regular"`` and Sleeper's own week counter is ``<= week``). An unreadable or
    absent state means "finality unknown" and fails **open** — a courtesy check
    that must never block a real league on a missing platform field."""
    if not isinstance(nfl_state, Mapping):
        return None
    season_type = nfl_state.get("season_type")
    if season_type == "pre":
        return "the NFL season has not started"
    current = nfl_state.get("week")
    if (
        season_type == "regular"
        and isinstance(current, int)
        and not isinstance(current, bool)
        and current <= week
    ):
        return f"week {week} is not final yet — Sleeper's NFL state is only through week {current}"
    return None


# --------------------------------------------------------------------------- #
# Story 5.11c — the weekly Facts JSON snapshot and its numeric diff
#
# The reissue needs to say *what changed*, which means it needs the previous
# confirmed run's Facts JSON to diff against. Persisted through the generic
# ``Store`` blob cache (no new port method, AD-5 shape), exactly as
# ``consensus.py`` wraps the same read/write pair for its own payloads.
# --------------------------------------------------------------------------- #

#: The blob-cache namespace a league-week's Facts JSON snapshot lives under.
_WEEKLY_FACTS_NAMESPACE = "weekly-facts-snapshot"

#: How many changed numeric leaves a reissue's diff summary names before it stops
#: listing them and just counts the rest.
_MAX_DIFF_ENTRIES = 8


def _weekly_facts_snapshot_key(league_id: str, week: int) -> str:
    """The blob-cache key one league-week's Facts JSON snapshot is stored under.

    A ``-`` joins the league id and the week rather than the ``:`` the design note
    names: ``FileStore`` maps a cache key straight onto a file name
    (``cache/<namespace>/<key>.json``), and ``:`` is not a legal file name
    character on Windows -- there ``mkstemp`` / ``os.replace`` raise ``OSError``,
    which ``write_cache`` reports as a ``StoreError`` (swallowed, since this cache
    is deliberately best-effort) and ``read_cache`` never finds the entry, so the
    snapshot could never round-trip and a reissue could never say what changed.
    Sleeper league ids are digits, so the two separators cannot collide."""
    return f"{league_id}-{week}"


def _read_weekly_facts_snapshot(
    store: Store, league_id: str, week: int
) -> dict[str, object] | None:
    """The Facts JSON snapshot an earlier confirmed post persisted for this
    league-week, or ``None`` when there is none. Best-effort, like
    ``consensus.py``'s own cache readers: a ``StoreError`` (a corrupt or
    unreadable entry) reads as "no snapshot" rather than failing the reissue."""
    try:
        return store.read_cache(
            _WEEKLY_FACTS_NAMESPACE, _weekly_facts_snapshot_key(league_id, week)
        )
    except StoreError:
        return None


def _write_weekly_facts_snapshot(
    store: Store, league_id: str, week: int, facts: Mapping[str, object]
) -> None:
    """Persist one league-week's Facts JSON snapshot for a later reissue to diff
    against. Best-effort, mirroring ``consensus.py``: a ``StoreError`` here must
    not discard an Issue that has already been rendered and posted."""
    try:
        store.write_cache(
            _WEEKLY_FACTS_NAMESPACE, _weekly_facts_snapshot_key(league_id, week), facts
        )
    except StoreError:
        pass


def _read_previous_published_ranks(
    store: Store, league_id: str, week: int
) -> dict[int, dict[str, int]]:
    """Every published-rank snapshot a *prior* week persisted for this league,
    keyed by week (Story 5.12).

    Walked backward from ``week - 1``: only a week the engine actually confirmed
    has a file (``write_published_rank`` is success-gated at its call site), so
    probing the strictly-prior weeks and keeping whatever answers **is** the
    backward-fallback search — a held or skipped week simply has nothing to
    return and the caller's lookback walks straight past it.

    Best-effort, like the snapshot readers above: a ``StoreError`` on one week
    reads as "that week has no snapshot" rather than failing the whole build.
    """
    found: dict[int, dict[str, int]] = {}
    for prior in range(1, week):
        try:
            ranks = store.read_published_rank(league_id, prior)
        except StoreError:
            continue
        if ranks:
            found[prior] = dict(ranks)
    return found


def _write_published_ranks(
    store: Store,
    league_id: str,
    week: int,
    ranks: Mapping[str, int],
    *,
    logger: logging.Logger,
) -> None:
    """Persist one league-week's published ranks, after a confirmed post
    (Story 5.12). Best-effort, like :func:`_write_weekly_facts_snapshot`: the
    Issue has already been rendered and delivered, and losing a snapshot is not
    worth failing a run that succeeded."""
    try:
        store.write_published_rank(league_id, week, ranks)
    except StoreError:
        logger.warning(
            "league %s: could not persist the published rank for week %s", league_id, week
        )


def _numeric_leaves(node: object, path: str, out: dict[str, int | float]) -> None:
    """Collect every numeric leaf of a decoded Facts JSON document into ``out``,
    keyed by its dotted / indexed path (``period.summary.high_score``,
    ``teams[0].pf``).

    Booleans are skipped on purpose — ``True`` is an ``int`` in Python, and a flag
    flipping is not a number that moved."""
    if isinstance(node, Mapping):
        for key, value in node.items():
            _numeric_leaves(value, f"{path}.{key}" if path else str(key), out)
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            _numeric_leaves(value, f"{path}[{index}]", out)
    elif isinstance(node, bool):
        return
    elif isinstance(node, (int, float)):
        out[path] = node


def _fmt_number(value: int | float) -> str:
    """A number as a compact display string (``12.0`` -> ``"12"``, ``1234.5`` ->
    ``"1234.5"``, ``12345678`` -> ``"12345678"``)."""
    if isinstance(value, float) and not value.is_integer():
        return f"{value:g}"
    return str(int(value))


def _diff_facts_numbers(
    previous: Mapping[str, object], current: Mapping[str, object]
) -> list[str]:
    """The numeric leaves that moved between two Facts JSON documents, one
    ``"<path>: <old> -> <new>"`` line each (a leaf present on only one side is
    tagged ``(new)`` / ``(removed)``).

    Text only, ASCII only: ``->`` rather than an arrow, because the summary is
    echoed to stdout and written into the Issue's text/HTML files, and a character
    outside the platform's ANSI code page would crash a redirected run on Windows.
    Non-numeric leaves are deliberately ignored: ``generated_at`` moves on every
    run and is not a correction, and a re-worded headline is not a number."""
    before: dict[str, int | float] = {}
    after: dict[str, int | float] = {}
    _numeric_leaves(previous, "", before)
    _numeric_leaves(current, "", after)
    changes: list[str] = []
    for path in sorted(set(before) | set(after)):
        if path in before and path in after:
            old, new = before[path], after[path]
            if old != new:
                changes.append(f"{path}: {_fmt_number(old)} -> {_fmt_number(new)}")
        elif path in after:
            changes.append(f"{path}: (new) {_fmt_number(after[path])}")
        else:
            changes.append(f"{path}: {_fmt_number(before[path])} (removed)")
    return changes


def _weekly_facts_diff_summary(
    store: Store, league_id: str, week: int, current: Mapping[str, object]
) -> str:
    """One line saying what changed (if anything) between the league-week's
    previous confirmed Facts JSON snapshot and *current* — the sentence a reissue
    carries as its "what changed"."""
    previous = _read_weekly_facts_snapshot(store, league_id, week)
    if previous is None:
        return "no previous snapshot is stored for this league-week"
    changes = _diff_facts_numbers(previous, current)
    if not changes:
        return "no numeric differences found"
    listed = changes[:_MAX_DIFF_ENTRIES]
    summary = "changed numbers: " + "; ".join(listed)
    if len(changes) > _MAX_DIFF_ENTRIES:
        summary += f"; and {len(changes) - _MAX_DIFF_ENTRIES} more"
    return summary


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
    post: bool,
) -> None:
    """Chain the five pipeline stages for one league and emit the recap to stdout
    plus a local HTML file; optionally deliver it to Discord (Story 4.6)."""
    from commishdesk.demo import demo_consensus_slots, load_demo_bundle
    from commishdesk.facts import build_draft_recap_facts
    from commishdesk.facts.schema import Storyline
    from commishdesk.facts.storylines import DRAFT_RECAP_WEEK, advance_storylines
    from commishdesk.ingest import build_league_model
    from commishdesk.stats import (
        compute_board_metrics,
        compute_consensus_metrics,
        compute_draft_grades,
    )
    from commishdesk.store import FileStore

    # The one Issue kind this function ever sends — named once so the early-check
    # filter and the real ``send_issue`` call below can't drift apart into two
    # independent string literals.
    draft_recap_kind: IssueKind = "draft_recap"

    # --post fails fast on a missing/blank/malformed webhook before any Sleeper /
    # consensus / LLM work for this league (mirrors verify-webhook's own check)
    # — zero wasted work, zero spend, on a misconfigured destination.
    # ``webhook_id`` validates the URL shape too (not just non-blank) and hands
    # back the recipient id the Discord block below reuses — computed once here,
    # never recomputed.
    webhook_url: str | None = None
    recipient_id: str | None = None
    if post:
        from commishdesk.deliver.discord import webhook_id

        webhook_url = (os.environ.get(_DISCORD_WEBHOOK_VAR) or "").strip()
        if not webhook_url:
            raise DeliveryError(
                f"set {_DISCORD_WEBHOOK_VAR} to the league's Discord channel webhook URL before running --post"
            )
        recipient_id = webhook_id(webhook_url)

        # Retro finding O1/H1: check the Send Ledger for an already-confirmed
        # Discord post *before* any Sleeper fetch, board/facts work, storyline
        # write, or paid narration — not after rendering. Mirrors the
        # dedupe-set pattern in ``deliver/ledger.py``'s ``send_issue`` (defense
        # in depth, kept below at the actual send site — this is a fast-path
        # short-circuit in front of it, not a replacement for it).
        already_confirmed = {
            entry.recipient
            for entry in FileStore(_cache_dir()).read_ledger(resolved, DRAFT_RECAP_WEEK)
            if entry.channel == "discord" and entry.kind == draft_recap_kind
        }
        if recipient_id in already_confirmed:
            typer.echo(f"Discord post already confirmed for webhook {recipient_id} — skipped")
            return

    generated_at = datetime.now(tz=UTC)

    # Storyline persistence bridge — mirrors the consensus bridge below: the Store
    # I/O lives here in the CLI, never in ``facts/`` (import fence). The demo path
    # stays side-effect-free and offline for storylines specifically, so
    # ``previous_storylines`` stays empty for it regardless of ``post`` — see
    # ``is_demo`` below, which gates the write, not ``store is not None`` (a
    # ``--post`` demo run still gets a ``store``, for the Send Ledger only).
    is_demo = resolved == demo_id
    store: FileStore | None = None
    previous_storylines: list[Storyline] = []

    if is_demo:
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
        if post:
            # --post needs a Store for the Send Ledger even on the demo path —
            # this must NOT re-enable storyline persistence for demo (see the
            # ``is_demo`` guard below, not ``store is not None``).
            store = FileStore(_cache_dir())
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

    # Epic-3-retro-item-35 / AC2: computing the next storyline set is pure (no
    # store I/O) and safe to do here; the actual ``store.write_storylines``
    # call is deferred until after ``_produce_issue`` returns successfully AND
    # the cost-ceiling check (below) has passed — see that call site for why.
    next_storylines: list[Storyline] | None = None
    if store is not None and not is_demo:
        next_storylines = [
            storyline.model_copy(update={"league_id": resolved})
            for storyline in advance_storylines(
                previous_storylines,
                kind=draft_recap_kind,
                week=DRAFT_RECAP_WEEK,
                board=board,
                consensus=consensus,
                grades=grades,
                draft_summary=doc.draft_summary,
                superlatives=doc.superlatives,
            )
        ]

    if llm_enabled:
        assert llm_config is not None  # loaded before the loop whenever llm_enabled holds
        assert voice is not None  # loaded alongside llm_config, same guard
        from commishdesk.narrate import (
            MAX_OUTPUT_TOKENS,
            build_narration_payload,
            estimate_cost_usd,
            is_pricing_stale,
        )
        from commishdesk.narrate import pricing as narrate_pricing

        if is_pricing_stale():
            logger.warning(
                "narrate/pricing.py's price table was last reviewed %s (more "
                "than %d days ago) — cost estimates may be stale; refresh "
                "MODEL_PRICES",
                narrate_pricing.PRICING_UPDATED,
                narrate_pricing.PRICING_REVIEW_INTERVAL_DAYS,
            )

        # Priced for length only — this concatenation is never sent anywhere as
        # a real payload. Both AnthropicClient.generate and GoogleClient.generate
        # (narrate/llm.py) send voice.system_prompt as the system message on
        # every real call, so the worst-case bound must count it too, or it is
        # not actually a worst-case bound.
        payload = build_narration_payload(doc.narration) + voice.system_prompt
        # Worst case: primary AND fallback both billed — a non-transient
        # failure on the primary (e.g. a truncated max_tokens completion) can
        # fall through to the fallback within the same narration attempt, so
        # the ceiling must sum the two per-call costs, not take whichever
        # model is pricier alone. Times the two narration attempts one
        # league-week can actually bill (the initial attempt plus the one
        # permitted regeneration — see _MAX_BILLABLE_NARRATION_ATTEMPTS).
        # max_output_tokens is imported from narrate.llm (via the narrate
        # package re-export), not duplicated, so the two can never silently
        # desync.
        per_call_estimate = estimate_cost_usd(
            payload, llm_config.primary, max_output_tokens=MAX_OUTPUT_TOKENS
        ) + estimate_cost_usd(payload, llm_config.fallback, max_output_tokens=MAX_OUTPUT_TOKENS)
        estimate = per_call_estimate * _MAX_BILLABLE_NARRATION_ATTEMPTS
        if llm_config.verifier is not None:
            from commishdesk.narrate.pricing import CHARS_PER_TOKEN
            from commishdesk.narrate.verify import EXTRACTOR_VOICE

            # The claim verifier reads the finished prose — at most one completion
            # long, so MAX_OUTPUT_TOKENS tokens at the estimator's CHARS_PER_TOKEN — plus its own
            # system prompt, and its reply is priced at the same output ceiling.
            # Priced for length only, like the narration payload above; an
            # unpriced verifier model fails closed here by name, as an unpriced
            # narrator does.
            verifier_input = "x" * int(MAX_OUTPUT_TOKENS * CHARS_PER_TOKEN) + EXTRACTOR_VOICE.system_prompt
            estimate += (
                estimate_cost_usd(verifier_input, llm_config.verifier, max_output_tokens=MAX_OUTPUT_TOKENS)
                * _MAX_BILLABLE_VERIFICATION_CALLS
            )
        typer.echo(f"estimated cost: {_fmt_usd(estimate)} (ceiling {_fmt_usd(llm_config.cost_ceiling_usd)})")
        if estimate > llm_config.cost_ceiling_usd:
            raise CostCeilingExceededError(
                f"estimated cost {_fmt_usd(estimate)} for league {resolved} "
                f"exceeds the ceiling {_fmt_usd(llm_config.cost_ceiling_usd)} — "
                "no paid call made"
            )

    logger.debug("narrating local HTML")

    # AD-12 Layer 3 + FR-17: select the narrator, validate its output, apply the
    # tiered response (suppress a section / one LLM regeneration / hold the
    # Issue), and degrade to the template narrator whenever LLM prose cannot be
    # cleanly repaired. A hold raises ``ContentSafetyError`` — caught per league
    # in ``_run_draft_recap`` → one-line stderr, exit 1, no HTML (AD-9).
    from commishdesk.narrate.llm import recording_usage

    # Every paid call _produce_issue makes — narration, the one permitted
    # regeneration, claim verification — records the provider's own token
    # counts. Reported even when the Issue is then held: that spend happened.
    with recording_usage() as usage:
        try:
            body = _produce_issue(
                doc,
                voice,
                llm_config,
                llm_enabled=llm_enabled,
                logger=logger,
                resolved=resolved,
                allow_content_hold=allow_content_hold,
            )
        finally:
            _report_actual_spend(usage, logger=logger, resolved=resolved)

    # Epic-3-retro-item-35 / AC2: this is the earliest point a hold
    # (``ContentSafetyError``, above) or a cost-ceiling abort (raised earlier,
    # before this function even reaches ``_produce_issue``) is guaranteed to
    # have NOT happened — so it is the earliest point narrative memory may be
    # durably updated. Writing here, rather than back when ``next_storylines``
    # was computed, is what makes a held or cost-aborted run leave
    # ``store.read_storylines`` for this league completely unchanged (AC2).
    # This ordering guarantee is scoped precisely to those two cases; it says
    # nothing about a later ``OSError`` writing render output or a
    # ``DeliveryError`` posting to Discord below, both of which run after this
    # point and are outside AC2.
    if store is not None and not is_demo:
        logger.debug("persisting storylines")
        assert next_storylines is not None  # computed above whenever store is not None and not is_demo
        store.write_storylines(resolved, next_storylines)

    _render_and_write_issue(
        doc,
        body,
        out_dir=out_dir,
        resolved=resolved,
        week=DRAFT_RECAP_WEEK,
        kind=draft_recap_kind,
        logger=logger,
    )

    # Story 4.6: --post chains Story 4.4's idempotent Send Ledger through
    # Story 4.3's Discord webhook delivery.
    if post:
        assert webhook_url is not None  # checked fail-fast at the top of this function
        assert recipient_id is not None  # computed alongside webhook_url, same guard
        assert store is not None  # post=True guarantees a store on every path, demo included
        from commishdesk.render import render_discord_summary

        _deliver_issue(
            summary=render_discord_summary(doc, recap=body.recap, llm_text=body.llm_text),
            store=store,
            resolved=resolved,
            week=DRAFT_RECAP_WEEK,
            kind=draft_recap_kind,
            webhook_url=webhook_url,
            recipient_id=recipient_id,
        )


# --------------------------------------------------------------------------- #
# The weekly narrator gate (Story 5.12)
# --------------------------------------------------------------------------- #


def _weekly_llm_selection(resolved: str, logger: logging.Logger) -> tuple[Voice, LLMConfig] | None:
    """The weekly path's narrator gate: the default Voice + LLM config, or
    ``None`` when this run must use the deterministic template.

    ``None`` when no provider key is present (``_llm_enabled(None)`` — the weekly
    run has no interactive operator to pass ``--llm``), and ``None`` again when
    the ``COMMISHDESK_LLM_*`` values are malformed: an unattended scheduled run
    degrades, it never dies on a config typo.
    """
    if not _llm_enabled(None):
        return None
    from commishdesk.llmconfig import load_llm_config
    from commishdesk.voices import load_default_voice

    try:
        config = load_llm_config()
    except CommishDeskError as exc:
        logger.warning(
            "league %s: no LLM narrator for the weekly run (%s); using the template narrator",
            resolved,
            _one_line(exc),
        )
        return None
    return load_default_voice(), config


def _weekly_estimate_within_ceiling(
    narration: WeeklyNarration,
    voice: Voice,
    config: LLMConfig,
    *,
    resolved: str,
    logger: logging.Logger,
) -> bool:
    """Whether one weekly league-week may spend (Story 5.12).

    Mirrors :func:`_recap_one_league`'s pre-call worst-case estimate — the same
    payload the narrator will actually send (via ``build_weekly_payload``, plus
    the voice's system prompt), both providers summed, times
    :data:`_MAX_BILLABLE_NARRATION_ATTEMPTS` — but a failing check **degrades**
    rather than raising: the scheduled weekly run falls back to the template
    narrator, and :class:`~commishdesk.errors.CostCeilingExceededError` never
    reaches it. An unpriced model fails the same way (``estimate_cost_usd`` raises
    that error by name), for the same reason: not worth a paid guess.
    """
    from commishdesk.narrate.llm import MAX_OUTPUT_TOKENS, build_weekly_payload
    from commishdesk.narrate.pricing import estimate_cost_usd

    try:
        payload = build_weekly_payload(narration) + voice.system_prompt
        per_call_estimate = estimate_cost_usd(
            payload, config.primary, max_output_tokens=MAX_OUTPUT_TOKENS
        ) + estimate_cost_usd(payload, config.fallback, max_output_tokens=MAX_OUTPUT_TOKENS)
    except CommishDeskError as exc:
        logger.warning(
            "league %s: no LLM narrator for the weekly run (%s); using the template narrator",
            resolved,
            _one_line(exc),
        )
        return False

    estimate = per_call_estimate * _MAX_BILLABLE_NARRATION_ATTEMPTS
    if estimate > config.cost_ceiling_usd:
        logger.warning(
            "league %s: estimated weekly LLM cost %s exceeds the ceiling %s — "
            "using the template narrator",
            resolved,
            _fmt_usd(estimate),
            _fmt_usd(config.cost_ceiling_usd),
        )
        return False
    return True


def _stamp_published_ranks(
    narration: WeeklyNarration, ranks: Mapping[str, int]
) -> WeeklyNarration:
    """A copy of *narration* whose power rows carry the narrator's published
    ranks (Story 5.12).

    Stamping is what makes the Issue's own published ranks **in-world**: the
    closed-world scan in :func:`commishdesk.narrate.safety.check_narration` reads
    the Issue text against this payload, and the rank the narrator chose is a
    number the Facts document could not have contained on its own. Everything
    else about the payload is untouched, so a nudge that cites a *genuinely*
    absent number is still refuted.
    """
    if not ranks:
        return narration
    power = [
        row.model_copy(update={"published_rank": ranks[row.roster_id]})
        if row.roster_id in ranks
        else row
        for row in narration.power
    ]
    return narration.model_copy(update={"power": power})


def _produce_weekly_issue(
    doc: WeeklyFacts, *, resolved: str, logger: logging.Logger
) -> tuple[WeeklyIssue, dict[str, int]]:
    """Select the weekly narrator and return ``(issue, published_ranks)``.

    The template narrator returns ``(render_weekly_issue(narration), {})``.
    The LLM narrator (Story 5.12) returns a parsed :class:`WeeklyIssue` plus the
    published ranks it stated — at most two ``generate()`` attempts, the second
    only when the first earned the ``regenerate`` tier. A hold, a
    non-seven-section completion, or an unrepairable finding degrades to the
    template Issue: the weekly path never withholds an Issue, and it never makes
    a third paid call.
    """
    from commishdesk.narrate.response import classify
    from commishdesk.narrate.safety import check_narration
    from commishdesk.narrate.weekly_template import (
        parse_published_ranks,
        render_weekly_issue,
        weekly_issue_from_text,
        weekly_issue_to_text,
    )

    narration = doc.narration

    def template() -> tuple[WeeklyIssue, dict[str, int]]:
        return render_weekly_issue(narration), {}

    selection = _weekly_llm_selection(resolved, logger)
    if selection is None:
        return template()
    voice, config = selection
    if not _weekly_estimate_within_ceiling(narration, voice, config, resolved=resolved, logger=logger):
        return template()

    # ``build_client`` is imported (not captured as a default argument) so a test
    # can swap the factory on ``commishdesk.narrate.llm`` and have it take effect.
    from commishdesk.narrate.llm import build_client, narrate_weekly_issue
    from commishdesk.narrate.published_rank import published_rank_findings

    def emit_alerts(alerts: tuple[str, ...]) -> None:
        for line in alerts:
            logger.error("league %s content-safety: %s", resolved, line)
            typer.echo(f"content-safety alert for league {resolved}: {line}", err=True)

    for attempt in (1, 2):
        result = narrate_weekly_issue(
            narration, voice, config, llm_enabled=True, client_factory=build_client
        )
        if result.narrator == "template":
            # every provider attempt failed inside narrate_weekly_issue
            return template()
        issue = weekly_issue_from_text(result.text, narration)
        if issue is None:
            logger.warning(
                "league %s: the weekly LLM narration is not the expected seven-section "
                "shape; using the template narrator",
                resolved,
            )
            return template()

        ranks = parse_published_ranks(result.text, narration)
        stamped = _stamp_published_ranks(narration, ranks)
        report = check_narration(weekly_issue_to_text(issue), stamped, voice=voice)
        deviation = published_rank_findings(stamped, ranks)
        if deviation:
            report = report.model_copy(update={"findings": report.findings + deviation})
        decision = classify(report, narrator_is_template=False)

        # A hold is never shipped as an *LLM* Issue: the deterministic template is
        # the floor, exactly as it is for a failed generation. (This is a stronger
        # response than the draft path's ``raise``, and deliberately so — the
        # weekly run is scheduled and must always produce an Issue.)
        if decision.hold:
            emit_alerts(decision.alerts)
            logger.warning(
                "league %s: weekly LLM narration held on content safety; using the template narrator",
                resolved,
            )
            return template()

        if decision.regenerate and attempt == 1:
            logger.warning(
                "league %s: content-safety regeneration of the weekly narration "
                "(one attempt permitted — AD-12 Layer 3)",
                resolved,
            )
            continue

        if decision.regenerate or decision.suppress:
            emit_alerts(decision.alerts)
            logger.warning(
                "league %s: weekly LLM narration still unclean after %d attempt(s); "
                "using the template narrator",
                resolved,
                attempt,
            )
            return template()

        return issue, ranks
    return template()


def _recap_one_league_weekly(
    resolved: str,
    week: int,
    out_dir: Path,
    logger: logging.Logger,
    *,
    post: bool,
    reason: str | None = None,
) -> None:
    """Story 5.11a: chain ingest → stats → facts → narrator → render for one
    league-week, writing the local text Issue plus the Story 2.7 generic HTML
    dump and (with ``--post``) delivering an idempotent Discord summary.

    Story 5.11c adds the reissue seam: a non-``None`` ``reason`` is the operator's
    explicit "post this again, it was wrong". That inverts the ledger gate at the
    top — instead of short-circuiting on an already-confirmed send, a reissue
    *requires* one (there must be something to correct) — and, once the Issue is
    rebuilt exactly as a plain ``--week --post`` run would rebuild it, prepends a
    "Correction" section carrying the reason and a numeric diff against the
    previous confirmed run's persisted Facts JSON. The reason rides through
    :func:`_deliver_issue` into Story 4.4's own re-send-and-append mechanism.

    Story 5.12 makes the narrator a choice: the deterministic template (always
    available, no key, no spend) or the voiced weekly LLM narrator, selected only
    when a provider key is present **and** the pre-call cost estimate fits
    ``cost_ceiling_usd``. Nothing on this path raises
    :class:`~commishdesk.errors.CostCeilingExceededError` — an over-budget run
    degrades to the template.

    Ordering (epic-3-retro-item-35, applied to the weekly kind from the start):

    1. the ``--post`` webhook fail-fast + already-confirmed ledger short-circuit
       (or, on a reissue, the store must already hold that confirmed entry),
       before any fetch at all;
    2. the fetch and Sleeper's own week-finality refusal;
    3. the standings cross-check — a ``CrossCheckError`` is a **hold** under
       ``--post`` (nothing rendered, written or posted) and an ``UNVERIFIED —``
       dateline otherwise (the mismatch is printed and the local Issue still
       ships);
    4. the durable writes (the persisted-or-built player snapshot, then the
       advanced storylines once the Issue exists and any hold has resolved);
    5. (Story 5.11c, reissue only) the Correction section is prepended to the
       rebuilt Issue only after step 4's writes, so its diff reads the same
       Facts JSON the storylines above were advanced from;
    6. (Story 5.11c) the Facts JSON snapshot this run persists for a future
       reissue to diff against — and (Story 5.12) the published ranks the
       narrator stated — are written only once ``--post`` has actually confirmed
       the send: never on a failed delivery, and never on a run that only writes
       the local Issue.
    """
    from commishdesk.deliver.discord import webhook_id
    from commishdesk.errors import CrossCheckError
    from commishdesk.facts.storylines import advance_storylines
    from commishdesk.facts.weekly import build_weekly_facts
    from commishdesk.ingest import (
        build_league_model,
        build_player_names,
        build_week_model,
        bye_teams,
        get_player_snapshot,
    )
    from commishdesk.narrate.weekly_template import WeeklySection
    from commishdesk.stats.standings import compute_standings, cross_check_standings
    from commishdesk.store import FileStore

    weekly_kind: IssueKind = "weekly"
    store = FileStore(_cache_dir())

    # --post fails fast on a missing/blank/malformed webhook before any fetch or
    # any work (mirrors _recap_one_league's draft fast-path). The Send Ledger then
    # gates the run: a plain run short-circuits on an already-confirmed (league,
    # week, kind="weekly") entry, while a reissue (Story 5.11c) requires one —
    # there is nothing to correct otherwise.
    webhook_url: str | None = None
    recipient_id: str | None = None
    if post:
        webhook_url = (os.environ.get(_DISCORD_WEBHOOK_VAR) or "").strip()
        if not webhook_url:
            raise DeliveryError(
                f"set {_DISCORD_WEBHOOK_VAR} to the league's Discord channel webhook URL before running --post"
            )
        recipient_id = webhook_id(webhook_url)
        already_confirmed = {
            entry.recipient
            for entry in store.read_ledger(resolved, week)
            if entry.channel == "discord" and entry.kind == weekly_kind
        }
        if reason is None:
            if recipient_id in already_confirmed:
                typer.echo(f"Discord post already confirmed for webhook {recipient_id} — skipped")
                return
        elif recipient_id not in already_confirmed:
            raise CommishDeskError(
                f"nothing to correct for league {resolved!r} week {week} — "
                "no confirmed send for this league-week"
            )

    from commishdesk.adapters.sleeper import SleeperAdapter

    logger.debug("fetching Sleeper week %s for league %s", week, resolved)
    adapter = SleeperAdapter()
    try:
        week_bundle = adapter.fetch_week(resolved, week)
        # Refuse before any stats work when Sleeper's own state says this week's
        # games are not final yet (or the season has not started).
        reason_not_final = _week_not_final_reason(week_bundle.get("nfl_state"), week)
        if reason_not_final is not None:
            raise CommishDeskError(
                f"league {resolved!r}: cannot build a Week {week} recap — {reason_not_final}"
            )
        league_bundle = adapter.fetch(resolved)
    finally:
        adapter.close()

    logger.debug("building the weekly models")
    model = build_league_model(league_bundle)
    week_model = build_week_model(week_bundle)
    player_names = build_player_names(week_bundle)

    season = model.season
    nfl_byes = bye_teams(season, week)
    nfl_byes_next_week = bye_teams(season, week + 1)

    # Cross-check before the Facts JSON is built (Story 5.11a). This is the one
    # stats call facts/weekly.py::_build does not make itself, so the CLI owns it.
    logger.debug("computing weekly standings + cross-check")
    standings = compute_standings(week_model, model)
    cross_check_passed = True
    try:
        cross_check_standings(standings, week_model)
    except CrossCheckError as exc:
        if post:
            # A --post run must not ship an Issue whose numbers disagree with
            # Sleeper's own season totals: hold (nothing rendered, written or
            # posted), one-line alert, exit 1 — the same shape as the
            # ContentSafetyError hold on the draft path.
            raise
        cross_check_passed = False
        logger.warning("league %s cross-check failed: %s", resolved, _one_line(exc))
        typer.echo(_one_line(exc), err=True)

    # Durable writes begin only now that the week is final and any cross-check
    # hold has resolved: the persisted-or-built player snapshot (FR-5), then the
    # league's narrative memory, then (Story 5.12) the published ranks a prior
    # confirmed week left behind — read here, never inside facts/ (AD-1).
    players = get_player_snapshot(store, resolved, week, week_bundle)
    previous_storylines = store.read_storylines(resolved)
    previous_published_ranks = _read_previous_published_ranks(store, resolved, week)

    logger.debug("building the weekly Facts JSON")
    doc = build_weekly_facts(
        week_model,
        model,
        players,
        player_names,
        generated_at=datetime.now(tz=UTC),
        nfl_byes=nfl_byes,
        nfl_byes_next_week=nfl_byes_next_week,
        previous_storylines=previous_storylines,
        previous_published_ranks=previous_published_ranks,
    )
    # The decoded Facts JSON, in two places below: the reissue's diff (against the
    # previous confirmed run's snapshot) and the snapshot this run persists. Read
    # ``generated_at`` here is a string, so the diff helper never sees it move.
    facts_json = doc.model_dump(mode="json")

    logger.debug("narrating the weekly Issue")
    issue, published_ranks = _produce_weekly_issue(doc, resolved=resolved, logger=logger)
    if not cross_check_passed:
        # Visible on every surface that reads the dateline (stdout, the text
        # file, the HTML dump) — the same technique _render_and_write_issue uses
        # to stamp the generation timestamp onto the draft-recap dateline.
        issue = issue.model_copy(update={"dateline": f"UNVERIFIED — {issue.dateline}"})

    # Story 5.11c: label the corrected Issue as a correction. The section is
    # prepended, so its first block is what ``render_weekly_discord_summary`` reads
    # as the Discord lead — the reason *and* the changed-numbers summary therefore
    # reach stdout, the text/HTML files and the posted message alike, with no
    # change to ``render/discord.py``.
    #
    # Gated on ``post`` too (review-loop 1, edge-case-hunter): ``run()`` already
    # requires ``--post`` alongside ``--reason``, so a CLI-driven call never
    # reaches this function with ``reason`` set and ``post`` false — but nothing
    # here re-derives that invariant, and this function has no other caller to
    # rely on it either. Without the guard, such a call would still label the
    # local-only Issue a "Correction" and read/diff the Facts snapshot despite
    # never checking the ledger for something to correct, and never posting or
    # ledgering anything. ``reason`` is already the stripped text (validated and
    # normalized in ``run()``).
    if post and reason is not None:
        correction = reason
        summary = _weekly_facts_diff_summary(store, resolved, week, facts_json)
        logger.info(
            "league %s week %s reissue (%s): %s",
            resolved,
            week,
            correction,
            summary,
        )
        issue = issue.model_copy(
            update={
                "sections": [
                    WeeklySection(
                        heading="Correction",
                        blocks=[f"Correction — {correction} — {summary}"],
                    ),
                    *issue.sections,
                ]
            }
        )

    # epic-3-retro-item-35: narrative memory is durably updated only once the
    # narratable Issue exists and any cross-check hold has resolved. Extended
    # (step-04 review, blind-hunter): "resolved" means *passed* -- a
    # cross-check failure without --post still ships an UNVERIFIED local
    # Issue built from the mismatched numbers, but must not let those same
    # numbers durably taint next week's storyline continuity. Computing
    # next_storylines is pure and harmless either way; only the write is
    # gated, mirroring how the draft-recap path already gates its write (not
    # the computation) on success.
    next_storylines = [
        storyline.model_copy(update={"league_id": resolved})
        for storyline in advance_storylines(
            previous_storylines,
            kind=weekly_kind,
            week=week,
            teams=doc.teams,
            period=doc.period,
        )
    ]
    if cross_check_passed:
        store.write_storylines(resolved, next_storylines)

    _render_and_write_weekly_issue(
        issue,
        out_dir=out_dir,
        resolved=resolved,
        week=week,
        logger=logger,
    )

    if post:
        assert webhook_url is not None  # checked fail-fast at the top of this function
        assert recipient_id is not None  # computed alongside webhook_url, same guard
        from commishdesk.render import render_weekly_discord_summary

        _deliver_issue(
            summary=render_weekly_discord_summary(issue, week=week),
            store=store,
            resolved=resolved,
            week=week,
            kind=weekly_kind,
            webhook_url=webhook_url,
            recipient_id=recipient_id,
            reason=reason,
        )
        # Story 5.11c: record this league-week's Facts JSON so a later reissue can
        # say what changed. Written only after the post is confirmed, so a failed
        # delivery never leaves a snapshot claiming the Issue went out.
        _write_weekly_facts_snapshot(store, resolved, week, facts_json)
        # Story 5.12: same ordering, same reason — the published rank is this
        # week's "confirmed publish" evidence for the *next* week's lookback, so
        # it is written only once the send is confirmed. A template run has no
        # published ranks, so it leaves no file and the lookback walks past it.
        if published_ranks:
            _write_published_ranks(store, resolved, week, published_ranks, logger=logger)


def _issue_filename_stem(week: int, kind: IssueKind) -> str:
    """Filename stem for one Issue's output files — stable across ``week`` /
    ``kind`` so the weekly path reuses :func:`_render_and_write_issue` /
    :func:`_deliver_issue` / :func:`_render_and_write_weekly_issue` unchanged.
    ``draft_recap`` reproduces the exact pre-existing ``"draft-recap"`` stem
    (``week`` is unused — a draft recap is always week 1); the ``weekly`` branch
    (``f"{kind}-week{week}"``) is what the Story 5.11a weekly path writes."""
    if kind == "draft_recap":
        return "draft-recap"
    return f"{kind}-week{week}"


def _render_and_write_issue(
    doc: DraftRecapFacts,
    body: IssueBody,
    *,
    out_dir: Path,
    resolved: str,
    week: int,
    kind: IssueKind,
    logger: logging.Logger,
) -> None:
    """Render one produced Issue's body to stdout plus local HTML/email files —
    Story 4.1's web page and Story 4.2's email pair. Filenames key off
    ``week`` / ``kind`` via :func:`_issue_filename_stem`, so a future weekly
    path can call this unchanged; ``--draft-recap`` always passes
    ``kind="draft_recap"`` / ``week=DRAFT_RECAP_WEEK``, reproducing today's
    exact ``-draft-recap`` filenames and behavior (AC4)."""
    from commishdesk.narrate import recap_to_text
    from commishdesk.render import render_email, render_web, write_html_file, write_text_file

    stem = _issue_filename_stem(week, kind)

    if body.narrator == "template":
        assert body.recap is not None
        recap = body.recap.model_copy(update={"dateline": f"{body.recap.dateline} · generated {doc.generated_at}"})
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
        Path(out_dir) / f"commishdesk-{resolved}-{stem}.html",
    )
    typer.echo(str(written))

    # Story 4.2: the same content model, rendered for email — client-safe HTML
    # plus a text/plain alternative. The CLI writes the two files next to the
    # web page; delivery (Discord) is separate from rendering.
    email_parts = render_email(
        doc,
        recap=body.recap,
        llm_text=body.llm_text,
        generated_at=str(doc.generated_at),
    )
    email_html = write_html_file(
        email_parts.html,
        Path(out_dir) / f"commishdesk-{resolved}-{stem}.email.html",
    )
    email_text = write_text_file(
        email_parts.text,
        Path(out_dir) / f"commishdesk-{resolved}-{stem}.txt",
    )
    typer.echo(str(email_html))
    typer.echo(str(email_text))


def _render_and_write_weekly_issue(
    issue: WeeklyIssue,
    *,
    out_dir: Path,
    resolved: str,
    week: int,
    logger: logging.Logger,
) -> None:
    """Story 5.11a: print the weekly Issue's plain text and write the local text
    Issue plus the Story 2.7 generic HTML dump next to it.

    Both weekly narrators funnel through here unchanged: the LLM narrator's prose
    is parsed back into the same :class:`WeeklyIssue` shape
    (:func:`~commishdesk.narrate.weekly_template.weekly_issue_from_text`), so the
    cached-then-flattened text, the HTML dump and the Discord summary are
    identical surfaces either way."""
    from commishdesk.narrate.weekly_template import weekly_issue_to_text
    from commishdesk.render import recap_to_html, write_html_file, write_text_file

    stem = _issue_filename_stem(week, "weekly")
    text = weekly_issue_to_text(issue)
    typer.echo(text)

    written_html = write_html_file(
        recap_to_html(issue),
        Path(out_dir) / f"commishdesk-{resolved}-{stem}.html",
    )
    typer.echo(str(written_html))
    written_text = write_text_file(
        text,
        Path(out_dir) / f"commishdesk-{resolved}-{stem}.txt",
    )
    typer.echo(str(written_text))
    logger.debug("wrote the weekly Issue for league %s week %s", resolved, week)


def _deliver_issue(
    *,
    summary: str,
    store: FileStore,
    resolved: str,
    week: int,
    kind: IssueKind,
    webhook_url: str,
    recipient_id: str,
    reason: str | None = None,
) -> None:
    """Story 4.6: deliver one pre-composed Issue summary to Discord through
    Story 4.4's idempotent Send Ledger and Story 4.3's webhook post.

    Both surfaces build their own ``summary`` first — the draft path via
    :func:`~commishdesk.render.render_discord_summary`, the weekly path via
    :func:`~commishdesk.render.render_weekly_discord_summary` — so this function
    owns only the send/ledger logic, shared unchanged. ``week`` / ``kind`` key
    the ledger entry, so a future weekly path uses the same delivery machinery.

    ``reason`` (Story 5.11c) is forwarded verbatim to ``send_issue``'s own
    ``reason=``: a non-``None`` value is a deliberate re-issue, so every recipient
    is re-sent regardless of the ledger and the new entry carries that reason. The
    draft-recap caller never passes one.

    Lazy imports (mirrors the verify-webhook comment above) keep httpx off the
    default --post-less import path."""
    from commishdesk.deliver import send_issue
    from commishdesk.deliver.discord import post_discord_text

    url = webhook_url  # a plain local narrows the closure below for mypy

    def _post_to_the_expected_recipient(recipient: str, content: str) -> object:
        # retro item 58: recipients is a one-entry dict today, so this
        # closure only ever sees recipient_id here -- but if a future edit
        # ever makes recipients multi-entry without updating this sender,
        # every recipient would silently post to this same single webhook
        # while the Send Ledger records each as confirmed-delivered to its
        # own address. Raise DeliveryError (not a bare assert, which
        # python -O strips) so send_issue's own except DeliveryError
        # catches it, records it in report.failed, and the code below
        # re-raises it -- which the per-league except (CommishDeskError,
        # OSError) in the run loop then correctly catches.
        if recipient != recipient_id:
            raise DeliveryError(
                f"sender built for recipient {recipient_id!r} was called with "
                f"mismatched recipient {recipient!r}"
            )
        return post_discord_text(url, content)

    report = send_issue(
        store,
        league_id=resolved,
        week=week,
        channel="discord",
        kind=kind,
        recipients={recipient_id: summary},
        sender=_post_to_the_expected_recipient,
        reason=reason,
    )
    if report.failed:
        _, message = report.failed[0]  # a single recipient — no partial-success case
        raise DeliveryError(message)
    if report.delivered:
        typer.echo(f"posted to Discord (webhook {recipient_id})")
    else:
        typer.echo(f"Discord post already confirmed for webhook {recipient_id} — skipped")


def _report_actual_spend(usage: list[CallUsage], *, logger: logging.Logger, resolved: str) -> None:
    """Echo what this league's LLM calls actually billed, summed from the
    providers' own token counts — the figure the pre-spend estimate is a
    worst-case bound on. Silent when no paid call came back. A call whose
    provider reported no counts, or whose model has no price entry, is shown as
    unpriced rather than as zero."""
    if not usage:
        return
    input_tokens = sum(call.input_tokens or 0 for call in usage)
    output_tokens = sum(call.output_tokens or 0 for call in usage)
    thinking_tokens = sum(call.thinking_tokens or 0 for call in usage)
    costs = [call.usd() for call in usage]
    priced = sum(cost for cost in costs if cost is not None)
    unpriced = sum(1 for cost in costs if cost is None)
    amount = _fmt_usd(priced) + (f" + {unpriced} unpriced call(s)" if unpriced else "")
    line = (
        f"actual LLM spend: {amount} ({len(usage)} call(s): {input_tokens:,} input, "
        f"{output_tokens:,} output, {thinking_tokens:,} thinking tokens)"
    )
    typer.echo(line)
    logger.info("league %s %s", resolved, line)


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
        excise_offending_sentences,
        recap_to_text,
        render_draft_recap,
        restore_lead_heading,
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
            return ContentSafetyError(f"content-safety hold for league {resolved}: {joined}")
        logger.error(
            "league %s content-safety hold OVERRIDDEN (--allow-content-hold): %s",
            resolved,
            joined,
        )
        typer.echo(
            f"content-safety hold OVERRIDDEN for league {resolved} (--allow-content-hold): {joined}",
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
                "a regenerate-tier finding on the template narrator, which has no regeneration to spend",
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
                held = _hold(("a suppress-tier content-safety finding could not be localized to a section",))
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

    def _verified_llm_text(candidate: str) -> str | None:
        """Content-safety P1 — the last gate before LLM prose ships.

        One claim-verification call over *candidate*, the prose exactly as it
        would ship. Both LLM ship paths below return through here, so it runs at
        most once per league-week. It fails open: a switched-off, unreachable or
        garbled verifier returns *candidate* unchanged, to ship on the
        deterministic checks alone. A refuted claim gets the same free repair as
        a deterministic finding — its sentence is excised and the result
        rechecked. Returns the text to ship, or ``None`` when that repair is
        refused and the Issue must degrade to the template; never a regeneration,
        which would ship prose that was never verified.
        """
        from commishdesk.narrate.verify import verify_narration

        assert llm_config is not None  # the LLM branch loaded it
        outcome = verify_narration(candidate, narration, llm_config.verifier)
        if not outcome.ran:
            if outcome.error is not None:
                logger.warning(
                    "league %s: claim verification did not run; shipping on the deterministic checks: %s",
                    resolved,
                    outcome.error,
                )
            return candidate

        share = outcome.unverifiable_share
        logger.info(
            "league %s claim verification: %d supported, %d refuted, %d unverifiable (%s unreachable)",
            resolved,
            len(outcome.supported),
            len(outcome.refuted),
            len(outcome.unverifiable),
            "n/a" if share is None else f"{share:.0%}",
        )
        refutations = outcome.report()
        if not refutations.findings:
            return candidate

        for finding in refutations.findings:
            logger.warning("league %s content-safety: %s", resolved, finding.message)
        repaired, excised = excise_offending_sentences(candidate, refutations)
        if repaired is not None and excised:
            recheck = classify(check_narration(repaired, narration, voice=voice), narrator_is_template=False)
            if not (recheck.hold or recheck.regenerate or recheck.suppress):
                logger.warning(
                    "league %s: claim verification excised %d sentence(s) carrying refuted claims",
                    resolved,
                    len(excised),
                )
                return repaired
        _emit_alerts(tuple(f"{f.category}/{f.severity}: {f.message}" for f in refutations.findings))
        logger.warning("league %s: refuted claims could not be repaired; using the template narrator", resolved)
        return None

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

        text = restore_lead_heading(sanitize_completion(result.text))
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

        # A free, localized repair is attempted BEFORE any paid regeneration.
        #
        # The P0.1 live validation measured what the old order cost: 7 findings
        # across 40 generations, 6 of them regenerate-tier, ~13 paid calls spent
        # and 0 real hallucinations caught. Regeneration is also provably weak
        # for the dominant class — the same derived-arithmetic span recurred in
        # three independent generations, so re-rolling the identical prompt
        # mostly re-earns the identical finding. Excision costs nothing and is
        # correct whether the finding is a true or a false positive, which is
        # the property that matters when the split is unknown at runtime.
        if decision.regenerate or decision.suppress:
            repaired, excised = excise_offending_sentences(text, report)
            if repaired is not None and excised:
                recheck = classify(
                    check_narration(repaired, narration, voice=voice),
                    narrator_is_template=False,
                )
                if not (recheck.hold or recheck.regenerate or recheck.suppress):
                    logger.warning(
                        "league %s: content-safety repaired the LLM narration by "
                        "excising %d sentence(s); shipped without a regeneration",
                        resolved,
                        len(excised),
                    )
                    for sentence in excised:
                        logger.info("league %s content-safety excised: %s", resolved, sentence)
                    shipped = _verified_llm_text(repaired)
                    if shipped is None:
                        return _template_issue()
                    return IssueBody(narrator=result.narrator, llm_text=shipped)

        if decision.regenerate and attempts == 1:
            logger.warning(
                "league %s: content-safety regeneration of the LLM narration (one attempt permitted — AD-12 Layer 3)",
                resolved,
            )
            continue

        if decision.regenerate or decision.suppress:
            _emit_alerts(decision.alerts)
            logger.warning(
                "league %s: LLM narration still unclean after %d attempt(s); using the template narrator",
                resolved,
                attempts,
            )
            return _template_issue()

        shipped = _verified_llm_text(text)
        if shipped is None:
            return _template_issue()
        return IssueBody(narrator=result.narrator, llm_text=shipped)


def main() -> None:
    app()
