"""The public weekly-Issue entry (Story 6.9): build one league-week's Issue from a ``Store``.

``cli.py`` and a hosted batch runner both drive the same pipeline through this module, so
there is exactly one copy of the weekly produce/render half. The entry is platform-neutral:
it takes a :class:`~commishdesk.store.Store` and a league id (opaque), and returns the Facts,
the narrated Issue, the self-contained web page and the email parts. It writes no Issue
file and sends nothing; the caller stores and delivers the result.

Ordering matches the CLI (see ``cli._recap_one_league_weekly``): fetch and the week-finality
refusal, the standings cross-check (a hold by default; a hosted run must not publish numbers
that disagree with the platform's own), durable reads, narration, then the narrative-memory
write. The writes that may only follow a *confirmed publish* (the Facts snapshot and the
published ranks) are a separate call, :func:`record_published_weekly`.

Suppressed sections (AD-30) are passed in by the caller (read once per run); ``None`` reads
them from the store.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from commishdesk.errors import CommishDeskError, StoreError
from commishdesk.logconfig import LOGGER_NAME
from commishdesk.narrate.pricing import MAX_BILLABLE_NARRATION_ATTEMPTS

if TYPE_CHECKING:
    from commishdesk.facts.schema import Storyline, WeeklyFacts, WeeklyNarration
    from commishdesk.llmconfig import LLMConfig
    from commishdesk.narrate.l4 import L4Scorer
    from commishdesk.narrate.weekly_template import WeeklyIssue
    from commishdesk.render import EmailParts, WebEnhancer
    from commishdesk.stats.standings import PlayoffSeeding
    from commishdesk.store import Store
    from commishdesk.voices import Voice

__all__ = [
    "WeeklyIssueBuild",
    "build_weekly_issue",
    "record_published_weekly",
]

#: Environment variables whose presence selects the LLM narrator on the weekly path.
_LLM_KEY_VARS = ("ANTHROPIC_API_KEY", "LLM_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")


#: The blob-cache namespace a league-week's Facts JSON snapshot lives under.
_WEEKLY_FACTS_NAMESPACE = "weekly-facts-snapshot"


def _warn(message: str) -> None:
    """One stderr line (the CLI's AD-9 style), without importing the CLI framework."""
    print(message, file=sys.stderr)


def one_line(exc: Exception) -> str:
    """A single-line, traceback-free message for a caught fault."""
    return str(exc) or exc.__class__.__name__


def fmt_usd(value: float) -> str:
    """A USD amount at enough precision that two values within a cent of each
    other still read as visibly different (review-loop 1 — a naive two-decimal
    rounding made an over-ceiling error message show the same two numbers as the
    ceiling it exceeded)."""
    return f"${value:.4f}"


def llm_enabled(cli_flag: bool | None) -> bool:
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


def week_not_final_reason(nfl_state: object, week: int) -> str | None:
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


def weekly_facts_snapshot_key(league_id: str, week: int) -> str:
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


def read_weekly_facts_snapshot(
    store: Store, league_id: str, week: int
) -> dict[str, object] | None:
    """The Facts JSON snapshot an earlier confirmed post persisted for this
    league-week, or ``None`` when there is none. Best-effort, like
    ``consensus.py``'s own cache readers: a ``StoreError`` (a corrupt or
    unreadable entry) reads as "no snapshot" rather than failing the reissue."""
    try:
        return store.read_cache(
            _WEEKLY_FACTS_NAMESPACE, weekly_facts_snapshot_key(league_id, week)
        )
    except StoreError:
        return None


def write_weekly_facts_snapshot(
    store: Store, league_id: str, week: int, facts: Mapping[str, object]
) -> None:
    """Persist one league-week's Facts JSON snapshot for a later reissue to diff
    against. Best-effort, mirroring ``consensus.py``: a ``StoreError`` here must
    not discard an Issue that has already been rendered and posted."""
    try:
        store.write_cache(
            _WEEKLY_FACTS_NAMESPACE, weekly_facts_snapshot_key(league_id, week), facts
        )
    except StoreError:
        pass


def read_previous_published_ranks(
    store: Store, league_id: str, week: int, logger: logging.Logger | None = None
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
            if logger is not None:
                logger.warning(
                    "league %s: published rank for week %s is unreadable; treating it as a gap",
                    league_id,
                    prior,
                )
            continue
        if ranks:
            found[prior] = dict(ranks)
    return found


def write_published_ranks(
    store: Store,
    league_id: str,
    week: int,
    ranks: Mapping[str, int],
    *,
    logger: logging.Logger,
) -> None:
    """Persist one league-week's published ranks, after a confirmed post
    (Story 5.12). Best-effort, like :func:`write_weekly_facts_snapshot`: the
    Issue has already been rendered and delivered, and losing a snapshot is not
    worth failing a run that succeeded."""
    try:
        store.write_published_rank(league_id, week, ranks)
    except StoreError:
        logger.warning(
            "league %s: could not persist the published rank for week %s", league_id, week
        )


def weekly_llm_selection(
    resolved: str,
    logger: logging.Logger,
    *,
    cold_start: bool = False,
    suppressed: frozenset[str] = frozenset(),
) -> tuple[Voice, LLMConfig] | None:
    """The weekly path's narrator gate: the default Voice + LLM config, or
    ``None`` when this run must use the deterministic template.

    ``None`` when no provider key is present (``llm_enabled(None)`` — the weekly
    run has no interactive operator to pass ``--llm``), and ``None`` again when
    the ``COMMISHDESK_LLM_*`` values are malformed: an unattended scheduled run
    degrades, it never dies on a config typo.

    ``cold_start`` (Story 5.16) selects the Week-1 voice directly from
    ``commishdesk.voices.beat_writer`` rather than through
    :func:`commishdesk.voices.load_default_voice`, whose selector is keyed by
    content type alone (``"weekly"``) and would otherwise return the seven-section
    prompt for a four-section Issue.

    ``suppressed`` (AD-30) are the section ids the model is not asked for; the
    Voice's prompt then lists only the remaining sections.
    """
    if not llm_enabled(None):
        return None
    from commishdesk.llmconfig import load_llm_config
    from commishdesk.voices import load_default_voice
    from commishdesk.voices.beat_writer import BEAT_WRITER_WEEKLY_COLD_START

    try:
        config = load_llm_config()
    except CommishDeskError as exc:
        logger.warning(
            "league %s: weekly LLM config unusable (%s); using the template narrator",
            resolved,
            one_line(exc),
        )
        return None
    if suppressed:
        from commishdesk.voices.beat_writer import weekly_voice_for

        return weekly_voice_for(cold_start=cold_start, suppressed=suppressed), config
    voice = BEAT_WRITER_WEEKLY_COLD_START if cold_start else load_default_voice("weekly")
    return voice, config


def weekly_estimate_within_ceiling(
    narration: WeeklyNarration,
    voice: Voice,
    config: LLMConfig,
    *,
    resolved: str,
    logger: logging.Logger,
    cold_start: bool = False,
    suppressed: frozenset[str] = frozenset(),
) -> bool:
    """Whether one weekly league-week may spend (Story 5.12).

    Mirrors :func:`_recap_one_league`'s pre-call worst-case estimate — the same
    payload the narrator will actually send (via ``build_weekly_payload``, plus
    the voice's system prompt), both providers summed, times
    :data:`MAX_BILLABLE_NARRATION_ATTEMPTS` — but a failing check **degrades**
    rather than raising: the scheduled weekly run falls back to the template
    narrator, and :class:`~commishdesk.errors.CostCeilingExceededError` never
    reaches it. An unpriced model fails the same way (``estimate_cost_usd`` raises
    that error by name), for the same reason: not worth a paid guess.
    """
    from commishdesk.narrate.llm import MAX_OUTPUT_TOKENS, build_weekly_payload
    from commishdesk.narrate.pricing import estimate_cost_usd

    try:
        payload = (
            build_weekly_payload(narration, cold_start=cold_start, suppressed=suppressed)
            + voice.system_prompt
        )
        per_call_estimate = estimate_cost_usd(
            payload, config.primary, max_output_tokens=MAX_OUTPUT_TOKENS
        ) + estimate_cost_usd(payload, config.fallback, max_output_tokens=MAX_OUTPUT_TOKENS)
    except CommishDeskError as exc:
        logger.warning(
            "league %s: weekly LLM cost cannot be estimated (%s); using the template narrator",
            resolved,
            one_line(exc),
        )
        return False

    estimate = per_call_estimate * MAX_BILLABLE_NARRATION_ATTEMPTS
    if estimate > config.cost_ceiling_usd:
        logger.warning(
            "league %s: estimated weekly LLM cost %s exceeds the ceiling %s — "
            "using the template narrator",
            resolved,
            fmt_usd(estimate),
            fmt_usd(config.cost_ceiling_usd),
        )
        return False
    return True


def stamp_published_ranks(
    narration: WeeklyNarration,
    ranks: Mapping[str, int],
    justifications: Mapping[str, str] | None = None,
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
        row.model_copy(
            update={
                "published_rank": ranks[row.roster_id],
                "nudge_justification": (justifications or {}).get(row.roster_id),
            }
        )
        if row.roster_id in ranks
        else row
        for row in narration.power
    ]
    return narration.model_copy(update={"power": power})


def stamp_nudge_justifications(
    facts: Mapping[str, object], justifications: Mapping[str, str]
) -> dict[str, object]:
    """A copy of the decoded Facts JSON whose ``narration.power`` rows carry the
    narrator's cited nudge reasons (Story 5.13a).

    Called only **after** :func:`produce_weekly_issue` has run ``check_narration``:
    stamping a justification into the payload the closed-world scan reads would make
    every number in it in-world by construction (Story 5.12, G2). Only the reason is
    stamped — never ``published_rank`` — because the persisted ranks live in their
    own store and a rank written here would show up as a moved number in a later
    reissue's diff. The render reads a reason from this narration, never from the
    Facts ``teams`` (Facts is unchanged); a roster with no reason keeps ``None``.
    """
    stamped = dict(facts)
    narration = dict(stamped["narration"])  # type: ignore[call-overload]
    narration["power"] = [
        {**row, "nudge_justification": justifications.get(str(row["roster_id"]))}
        for row in narration["power"]
    ]
    stamped["narration"] = narration
    return stamped


def produce_weekly_issue(
    doc: WeeklyFacts,
    *,
    resolved: str,
    logger: logging.Logger,
    reissue: bool = False,
    allow_content_hold: bool = False,
    suppressed: frozenset[str] = frozenset(),
    l4: L4Scorer | None = None,
) -> tuple[WeeklyIssue, dict[str, int], dict[str, str], tuple[str, ...]]:
    """Select the weekly narrator and return ``(issue, published_ranks,
    nudge_justifications, l4_reverted)``.

    The template narrator's rendered Issue is itself gated on content safety
    (Story 5.11's retro finding S11): ``template()`` runs ``check_narration`` +
    ``classify(narrator_is_template=True)`` on every call, like
    ``_produce_issue``'s draft-recap ``_template_issue``. An unoverridden hold
    (or a suppress/regenerate-tier finding — the template has no repair path to
    spend one on) raises :class:`~commishdesk.errors.ContentSafetyError`;
    ``allow_content_hold`` downgrades that to a logged, echoed ``OVERRIDDEN``
    and ships the template anyway, exactly as the draft path's
    ``--allow-content-hold`` does.

    The LLM narrator (Story 5.12) returns a parsed :class:`WeeklyIssue` plus the
    published ranks it stated and their cited reasons — at most two ``generate()`` attempts, the second
    only when the first earned the ``regenerate`` tier. A hold, a
    non-seven-section completion, or an unrepairable finding degrades to the
    template Issue — which now runs through the same gate above, so a degrade
    whose template is *also* unshippable raises or overrides exactly like the
    template-only path. A *reissue* (Story 5.11c) always takes the template: the
    correction must not re-spend or drift the prose and the persisted ranks.

    ``doc.period.has_prior_week`` (Story 5.16) selects which Voice the LLM path
    may use — the cold-start weekly prompt on a Week-1 run.

    ``suppressed`` (AD-30) are section ids the operator toned down to template
    prose: the LLM is never asked for them (payload, prompt and expected headings
    all omit them), and the parsed Issue gets their template sections spliced in.
    An id this Issue does not carry is ignored; when every section is suppressed
    no LLM call is made.

    ``l4`` (AD-12 L4, AD-47) is an optional classifier handed in by the caller. When
    given, an LLM-narrated Issue that passed the L2/L3 gate is screened section by
    section: a section at or above the threshold is replaced by its template section,
    its id is logged at warning and returned as ``l4_reverted`` (a Power hit also
    empties the published ranks and nudge justifications, like a suppressed Power
    section). A classifier that cannot score raises
    :class:`~commishdesk.narrate.l4.L4UnavailableError`; unscreened LLM prose is never
    returned. Template and degrade results are never screened and report ``()``.
    """
    from commishdesk.errors import ContentSafetyError
    from commishdesk.narrate.response import classify
    from commishdesk.narrate.safety import check_narration
    from commishdesk.narrate.weekly_template import (
        parse_nudge_justifications,
        parse_published_ranks,
        render_weekly_issue,
        section_headings_for_has_prior_week,
        weekly_issue_from_text,
        weekly_issue_to_text,
    )
    from commishdesk.sections import effective_suppressions

    narration = doc.narration
    cold_start = not doc.period.has_prior_week
    suppressed = effective_suppressions(suppressed, doc.period.has_prior_week)

    def emit_alerts(alerts: tuple[str, ...]) -> None:
        for line in alerts:
            logger.error("league %s content-safety: %s", resolved, line)
            _warn(f"content-safety alert for league {resolved}: {line}")

    def _hold(reasons: tuple[str, ...]) -> ContentSafetyError | None:
        """The hold, unless the operator overrode it — mirrors ``_produce_issue``'s
        own ``_hold`` helper below. Returns the fault for the caller to
        ``raise``, or ``None`` when ``allow_content_hold`` is set, having already
        logged the override at ``error`` (with ``OVERRIDDEN`` and every reason)
        and echoed a stderr line distinct from both the AD-9 fault line and the
        per-finding ``emit_alerts`` lines."""
        joined = "; ".join(reasons)
        if not allow_content_hold:
            return ContentSafetyError(f"content-safety hold for league {resolved}: {joined}")
        logger.error(
            "league %s weekly content-safety hold OVERRIDDEN (--allow-content-hold): %s",
            resolved,
            joined,
        )
        _warn(
            f"content-safety hold OVERRIDDEN for league {resolved} (--allow-content-hold): {joined}",
        )
        return None

    def template(voice: Voice | None = None) -> tuple[WeeklyIssue, dict[str, int], dict[str, str], tuple[str, ...]]:
        """The deterministic weekly narrator, gated like the draft path's
        ``_template_issue`` (except that a suppress-tier finding holds rather
        than trimming sections — see below): ``check_narration`` runs on the rendered
        Issue text before it ships, on *every* call site — the no-selection /
        reissue path, the over-ceiling degrade, a malformed completion, and an
        LLM-hold degrade all land here. ``voice`` is the already-selected Voice
        on a degrade path (``None`` when no Voice was ever selected), so the
        check reads the same voice-derived keyword list the LLM attempt did.

        ``classify(..., narrator_is_template=True)`` filters every
        ``hallucination`` finding (the template only ever states the
        ``narration`` projection, so a closed-world miss against it would be a
        false positive by construction) and structurally never yields
        ``regenerate`` — checked defensively here anyway, since the template has
        no regeneration to spend. A ``suppress_section`` finding also holds: a
        real ``WeeklySection``-suppression path is a separable feature (Design
        Notes), so the conservative default is never to ship a flagged
        template section.
        """
        issue = render_weekly_issue(narration, has_prior_week=doc.period.has_prior_week)
        report = check_narration(weekly_issue_to_text(issue), narration, voice=voice)
        decision = classify(report, narrator_is_template=True)
        if decision.hold or decision.regenerate or decision.suppress:
            emit_alerts(decision.alerts)
            reasons = decision.hold_reasons or (
                "a suppress/regenerate-tier finding on the weekly template "
                "narrator, which has no repair path to spend",
            )
            held = _hold(reasons)
            if held is not None:
                raise held
        return issue, {}, {}, ()

    all_suppressed = not section_headings_for_has_prior_week(
        doc.period.has_prior_week, suppressed
    )
    selection = (
        None
        if reissue or all_suppressed
        else weekly_llm_selection(
            resolved, logger, cold_start=cold_start, suppressed=suppressed
        )
    )
    if selection is None:
        return template()
    voice, config = selection
    if not weekly_estimate_within_ceiling(
        narration,
        voice,
        config,
        resolved=resolved,
        logger=logger,
        cold_start=cold_start,
        suppressed=suppressed,
    ):
        return template(voice)

    # ``build_client`` is imported (not captured as a default argument) so a test
    # can swap the factory on ``commishdesk.narrate.llm`` and have it take effect.
    from commishdesk.narrate.llm import build_client, narrate_weekly_issue
    from commishdesk.narrate.published_rank import published_rank_findings

    for attempt in (1, 2):
        result = narrate_weekly_issue(
            narration,
            voice,
            config,
            llm_enabled=True,
            client_factory=build_client,
            cold_start=cold_start,
            suppressed=suppressed,
        )
        if result.narrator == "template":
            # every provider attempt failed inside narrate_weekly_issue
            return template(voice)
        issue = weekly_issue_from_text(
            result.text,
            narration,
            has_prior_week=doc.period.has_prior_week,
            suppressed=suppressed,
        )
        if issue is None:
            expected = section_headings_for_has_prior_week(doc.period.has_prior_week, suppressed)
            logger.warning(
                "league %s: the weekly LLM narration is not the expected %s-section "
                "shape; using the template narrator",
                resolved,
                len(expected),
            )
            return template(voice)

        # A suppressed Power section is template prose (AD-30): any list the model
        # wrote anyway publishes nothing, exactly as on the template path.
        power_suppressed = "power" in suppressed
        ranks = {} if power_suppressed else parse_published_ranks(result.text, narration)
        # Ranks only: the closed-world scan reads the whole stamped payload, so a
        # stamped justification would make every number in it in-world by construction.
        stamped = stamp_published_ranks(narration, ranks)
        report = check_narration(weekly_issue_to_text(issue), stamped, voice=voice)
        deviation = published_rank_findings(stamped, ranks)
        if deviation:
            report = report.model_copy(update={"findings": report.findings + deviation})
        decision = classify(report, narrator_is_template=False)

        # A hold is never shipped as an *LLM* Issue: degrade to the deterministic
        # template, exactly as for a failed generation, rather than the draft
        # path's immediate ``raise``. The template is itself gated (S11), so a
        # template that also holds still raises ``ContentSafetyError`` there.
        if decision.hold:
            emit_alerts(decision.alerts)
            logger.warning(
                "league %s: weekly LLM narration held on content safety; using the template narrator",
                resolved,
            )
            return template(voice)

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
            return template(voice)

        # Parsed from the raw completion, like the ranks: the parsed Issue joins a
        # tight list's lines into one block, which would hide every item after the first.
        justifications = {} if power_suppressed else parse_nudge_justifications(result.text, narration)
        reverted: tuple[str, ...] = ()
        if l4 is not None:
            from commishdesk.narrate.l4 import screen_weekly_issue

            issue, reverted = screen_weekly_issue(
                issue,
                narration,
                l4,
                has_prior_week=doc.period.has_prior_week,
                suppressed=suppressed,
            )
            for section_id in reverted:
                logger.warning(
                    "league %s: weekly section %s over the L4 threshold; replaced with template text",
                    resolved,
                    section_id,
                )
            if "power" in reverted:
                ranks, justifications = {}, {}
        return issue, ranks, justifications, reverted
    return template(voice)


def weekly_render_doc(
    doc: WeeklyFacts,
    published_ranks: Mapping[str, int],
    nudge_justifications: Mapping[str, str],
) -> WeeklyFacts:
    """The Facts every weekly surface renders from: *doc* itself, or (when the
    narrator published ranks) a copy whose narration carries those ranks and
    their cited reasons. Shared by the page, the email and the Discord post so
    all three pick the same power order."""
    if not published_ranks:
        return doc
    return doc.model_copy(
        update={"narration": stamp_published_ranks(doc.narration, published_ranks, nudge_justifications)}
    )


# --------------------------------------------------------------------------- #
# Story 6.9 — the public entry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WeeklyFactsBuild:
    """The Facts stage's result: the document plus the state later stages reuse."""

    doc: WeeklyFacts
    facts_json: dict[str, Any]
    previous_storylines: list[Storyline]
    cross_check_passed: bool


@dataclass(frozen=True)
class WeeklyIssueBuild:
    """Everything one league-week's publish and delivery need, all recipient-independent.

    ``web_html`` and ``email`` hold no signed link, slug or address. ``league_bundle`` is the
    opaque platform bundle the Facts were built from (for the caller's roster contract).
    """

    league_id: str
    week: int
    doc: WeeklyFacts
    render_doc: WeeklyFacts
    issue: WeeklyIssue
    published_ranks: dict[str, int]
    nudge_justifications: dict[str, str]
    web_html: str
    email: EmailParts
    facts_json: dict[str, Any]
    league_bundle: Mapping[str, Any]
    generated_at: str
    cross_check_passed: bool
    l4_reverted: tuple[str, ...] = ()


def fetch_weekly_bundles(adapter: Any, league_id: str, week: int) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Pull the week and league bundles. Refuses before any stats work when the platform says
    the week is not final yet (or the season has not started)."""
    week_bundle = adapter.fetch_week(league_id, week)
    reason_not_final = week_not_final_reason(week_bundle.get("nfl_state"), week)
    if reason_not_final is not None:
        raise CommishDeskError(f"league {league_id!r}: cannot build a Week {week} recap — {reason_not_final}")
    return week_bundle, adapter.fetch(league_id)


def compute_weekly_facts(
    store: Store,
    league_id: str,
    week: int,
    week_bundle: Mapping[str, Any],
    league_bundle: Mapping[str, Any],
    *,
    seeding: PlayoffSeeding | None,
    hold_on_cross_check: bool,
    logger: logging.Logger,
) -> WeeklyFactsBuild:
    """Raw bundles to the weekly Facts JSON. A standings cross-check failure raises
    ``CrossCheckError`` when ``hold_on_cross_check`` (a posting or hosted run), and otherwise
    is logged and flagged so the Issue ships with an ``UNVERIFIED`` dateline."""
    from commishdesk.errors import CrossCheckError
    from commishdesk.facts.weekly import build_weekly_facts
    from commishdesk.ingest import (
        build_league_model,
        build_player_names,
        build_week_model,
        bye_teams,
        get_player_snapshot,
    )
    from commishdesk.stats.standings import (
        compute_standings,
        cross_check_standings,
        validate_playoff_seeding,
    )

    logger.debug("building the weekly models")
    model = build_league_model(league_bundle)
    week_model = build_week_model(week_bundle)
    player_names = build_player_names(week_bundle)

    if seeding is not None:
        bracket_teams = model.format.playoff.bracket_teams if model.format.playoff is not None else None
        seeding = validate_playoff_seeding(
            seeding,
            roster_ids=[roster.roster_id for roster in week_model.rosters],
            bracket_teams=bracket_teams,
            week=week,
            playoff_week_start=week_model.playoff_week_start,
        )

    season = model.season
    nfl_byes = bye_teams(season, week)
    nfl_byes_next_week = bye_teams(season, week + 1)

    # Cross-check before the Facts JSON is built (Story 5.11a).
    logger.debug("computing weekly standings + cross-check")
    standings = compute_standings(week_model, model, seeding=seeding)
    cross_check_passed = True
    try:
        cross_check_standings(standings, week_model)
    except CrossCheckError as exc:
        if hold_on_cross_check:
            raise
        cross_check_passed = False
        logger.warning("league %s cross-check failed: %s", league_id, one_line(exc))
        _warn(one_line(exc))

    # Durable reads begin only now that the week is final and any hold has resolved.
    players = get_player_snapshot(store, league_id, week, week_bundle)
    previous_storylines = store.read_storylines(league_id)
    previous_published_ranks = read_previous_published_ranks(store, league_id, week, logger)

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
        playoff_seeding=seeding,
    )
    return WeeklyFactsBuild(
        doc=doc,
        facts_json=doc.model_dump(mode="json"),
        previous_storylines=previous_storylines,
        cross_check_passed=cross_check_passed,
    )


def persist_weekly_storylines(store: Store, league_id: str, week: int, built: WeeklyFactsBuild) -> None:
    """Advance narrative memory (epic-3-retro-item-35): only once the Issue exists and the
    cross-check passed, so mismatched numbers never taint next week's continuity."""
    from commishdesk.facts.storylines import advance_storylines

    next_storylines = [
        storyline.model_copy(update={"league_id": league_id})
        for storyline in advance_storylines(
            built.previous_storylines,
            kind="weekly",
            week=week,
            teams=built.doc.teams,
            period=built.doc.period,
        )
    ]
    if built.cross_check_passed:
        store.write_storylines(league_id, next_storylines)


def build_weekly_issue(
    store: Store,
    league_id: str,
    week: int,
    *,
    adapter: Any | None = None,
    suppressions: Collection[str] | None = None,
    seeding: PlayoffSeeding | None = None,
    allow_content_hold: bool = False,
    hold_on_cross_check: bool = True,
    enhancer: WebEnhancer | None = None,
    l4: L4Scorer | None = None,
    logger: logging.Logger | None = None,
) -> WeeklyIssueBuild:
    """Build one league-week's weekly Issue from *store* and return it, writing no Issue file.

    ``league_id`` is opaque. ``adapter`` is an open platform adapter (``fetch`` / ``fetch_week``
    / ``close``); when ``None`` the default adapter is opened and closed here. ``suppressions``
    are the section ids toned down to template prose (AD-30), read once by the caller per run;
    ``None`` reads them from the store. The narrator is chosen from the environment exactly as
    on the CLI (template unless a provider key and a budget are present). ``l4`` is an optional
    classifier for LLM-narrated sections (see :func:`produce_weekly_issue`); the section ids it
    reverted to template text come back as ``WeeklyIssueBuild.l4_reverted``, and a scorer failure
    raises ``L4UnavailableError`` (fail closed). A cross-check failure
    raises ``CrossCheckError`` unless ``hold_on_cross_check`` is false. Narrative memory
    (storylines) is advanced before returning; call :func:`record_published_weekly` once the
    Issue is durably published.

    Raises :class:`~commishdesk.errors.CommishDeskError` subtypes for every expected fault,
    including a week that is not final (nothing is written then).
    """
    from commishdesk.render import render_weekly_email, render_weekly_web

    log = logger or logging.getLogger(f"{LOGGER_NAME}.weekly")
    if not 1 <= week <= 18:
        raise CommishDeskError(f"week {week} is outside 1-18")
    suppressed = frozenset(suppressions if suppressions is not None else store.read_suppressions(league_id))

    owns_adapter = adapter is None
    if adapter is None:
        from commishdesk.adapters.sleeper import SleeperAdapter

        adapter = SleeperAdapter()
    try:
        week_bundle, league_bundle = fetch_weekly_bundles(adapter, league_id, week)
    finally:
        if owns_adapter:
            adapter.close()

    built = compute_weekly_facts(
        store,
        league_id,
        week,
        week_bundle,
        league_bundle,
        seeding=seeding,
        hold_on_cross_check=hold_on_cross_check,
        logger=log,
    )
    issue, ranks, justifications, l4_reverted = produce_weekly_issue(
        built.doc,
        resolved=league_id,
        logger=log,
        allow_content_hold=allow_content_hold,
        suppressed=suppressed,
        l4=l4,
    )
    if not built.cross_check_passed:
        issue = issue.model_copy(update={"dateline": f"UNVERIFIED — {issue.dateline}"})
    persist_weekly_storylines(store, league_id, week, built)

    render_doc = weekly_render_doc(built.doc, ranks, justifications)
    generated_at = str(built.doc.generated_at)
    web_html = render_weekly_web(
        render_doc,
        issue,
        output_id=league_id,
        generated_at=generated_at,
        enhancer=enhancer,
    )
    email = render_weekly_email(render_doc, issue, generated_at=generated_at)
    return WeeklyIssueBuild(
        league_id=league_id,
        week=week,
        doc=built.doc,
        render_doc=render_doc,
        issue=issue,
        published_ranks=dict(ranks),
        nudge_justifications=dict(justifications),
        web_html=web_html,
        email=email,
        facts_json=built.facts_json,
        league_bundle=league_bundle,
        generated_at=generated_at,
        cross_check_passed=built.cross_check_passed,
        l4_reverted=tuple(l4_reverted),
    )


def record_published_weekly(
    store: Store,
    league_id: str,
    week: int,
    facts_json: Mapping[str, object],
    published_ranks: Mapping[str, int],
    nudge_justifications: Mapping[str, str],
    *,
    logger: logging.Logger | None = None,
) -> None:
    """The writes that may only follow a confirmed publish (Stories 5.11c, 5.12): the Facts
    snapshot a later reissue diffs against, and the published ranks next week's lookback reads.
    Best-effort, like the CLI: losing a snapshot never fails a run that already published."""
    log = logger or logging.getLogger(f"{LOGGER_NAME}.weekly")
    snapshot: Mapping[str, object] = facts_json
    if published_ranks:
        snapshot = stamp_nudge_justifications(facts_json, nudge_justifications)
    write_weekly_facts_snapshot(store, league_id, week, snapshot)
    if published_ranks:
        write_published_ranks(store, league_id, week, published_ranks, logger=log)
