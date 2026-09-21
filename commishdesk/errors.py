"""Typed exception hierarchy for the engine.

Every error the engine raises for an expected condition derives from
``CommishDeskError`` so the orchestrator can catch faults per league and skip
one league without taking down the batch (CLAUDE.md §5, AD-9). No bare
``except``; no engine code raises a bare ``Exception`` for a domain fault.

``StoreError`` is the first concrete member: any failure reading or parsing a
backing store (missing/unparseable config, malformed JSON) surfaces as a
``StoreError`` chained from the original OS or decode error.

``AdapterError`` is the second: any failure fetching or parsing a platform
adapter's raw data (a non-2xx response, a transport failure, a malformed body,
or a malformed/missing id in the platform's own response shape) surfaces as an
``AdapterError`` chained from the underlying exception. This module still
imports only stdlib + pydantic + commishdesk (``tests/test_store.py``); wrapping
``httpx`` and shape exceptions happens at the call site in
``commishdesk/adapters/sleeper.py``, not here.

``IngestError`` is the third: any structural failure building the stage-1
``LeagueModel`` from a raw platform bundle — a missing or mistyped ``league`` /
``rosters`` / ``users`` / ``draft`` section, a non-object list item, a field the
Pydantic model rejects — surfaces as an ``IngestError`` chained (where an
underlying exception exists) from the original ``KeyError`` / ``TypeError`` /
``ValueError`` / ``OverflowError`` / ``ValidationError``. The wrapping happens in
``commishdesk/ingest/build.py``, not here; no partial model is returned.

``ConsensusError`` is the fourth: any failure fetching, parsing, or ranking an
external pre-draft consensus board — a non-2xx response, a transport failure, a
malformed body, an already-closed client, or a payload that ranks none of the
drafted players — surfaces as a ``ConsensusError`` chained (where an underlying
exception exists) from ``httpx`` / ``RuntimeError`` / ``ValueError``. It is raised
only when *every* source fails against a cold cache; a single source failing is
swallowed and the fallback taken. The wrapping happens at the call site in
``commishdesk/consensus.py``, not here.

``SchemaValidationError`` is the fifth: the stage-3 Facts JSON builder
(``commishdesk/facts/build.py``) validates its own output and fails loud — any
``pydantic.ValidationError`` raised while constructing the ``DraftRecapFacts``
model, or while round-tripping ``model_validate(doc.model_dump())``, surfaces as a
``SchemaValidationError`` chained from that ``ValidationError``. No partial
document is returned; no consumer downstream of ``facts/`` re-validates (AD-2).
The wrapping happens in ``commishdesk/facts/build.py``, not here.

``NarratorError`` is the sixth. Two failure modes raise it. (1) The opt-in LLM
narrator (``commishdesk/narrate/llm.py``) raises it when a provider adapter is
unusable — the SDK is not installed, its API key is absent, the completion is
truncated or empty — chained from the underlying exception where one exists, and
again as the internal all-attempts-failed signal ``narrate_with_llm`` raises
after the primary and the fallback have each been tried (a transient provider
fault is retried on the same provider up to ``RETRY_CAP`` times first — FR-40).
These are caught by
the selection function (``narrate_draft_recap``), which degrades to the
zero-credential template narrator's text — a provider/generation fault never
reaches the caller (AD-8 / I3). (2) A malformed ``COMMISHDESK_LLM_*`` value (an
unknown provider token, a spec that is not ``<provider>:<model_id>``) raises it
from ``commishdesk/llmconfig.py::load_llm_config`` at startup, by design —
misconfiguration fails loud rather than silently narrating with the wrong model.

``ContentSafetyError`` is the seventh: the CLI raises it when the AD-12 Layer 3
tiered response (``commishdesk/narrate/response.py``) decides a narration cannot
ship — a manager's name in the same sentence as a banned-category term or a
personal-insult-lexicon hit, a section suppression so broad it would drop **The
Lead** or leave fewer than two sections, or a ``suppress`` finding that maps to no
section at all. It is a ``CommishDeskError`` caught by the per-league ``except``
(one-line stderr, exit 1, no HTML — AD-9); it exists as its own class so an
operator can tell a content-review hold from an infra retry. The raising happens
in ``commishdesk/cli.py``, not here.

``DeliveryError`` is the eighth: any failure posting an Issue to a delivery
channel — a webhook URL that is not a Discord webhook, a ``content`` payload over
Discord's 2000-character limit, a non-2xx response from the webhook, or a
transport failure reaching it — surfaces as a ``DeliveryError`` chained (where an
underlying exception exists) from ``httpx.HTTPError`` / ``RuntimeError``. It is a
``CommishDeskError`` caught by the ``_run_draft_recap`` per-league
``except (CommishDeskError, OSError)`` and by the ``verify-webhook`` subcommand
(one-line stderr, exit 1, no traceback). This module still imports only stdlib +
pydantic + commishdesk; the wrapping happens at the call site in
``commishdesk/deliver/discord.py``, not here — and the webhook token is never put
in the message.

``CostCeilingExceededError``: ``commishdesk/cli.py`` raises it
before any paid LLM call when the pre-call worst-case cost estimate
(``commishdesk/narrate/pricing.py::estimate_cost_usd``) exceeds
``LLMConfig.cost_ceiling_usd`` (``COMMISHDESK_COST_CEILING_USD``), and
``narrate/pricing.py`` itself raises it when a model id has no
``MODEL_PRICES`` entry — an unpriced model fails closed rather than silently
skipping pricing. A ``CommishDeskError`` caught by the same per-league
``except (CommishDeskError, OSError)`` as every other engine fault (AD-9); no new
catch site. The estimate is a worst-case bound, never an average — see
``narrate/pricing.py`` for the char-proxy token approximation.

``OptimalLineupError``: the stage-2 lineup solver
(``commishdesk/stats/lineup.py::compute_weekly_lineups``) raises it when
``league.format.roster_slots`` cannot be solved at all — an empty slot list, a
slot with an empty name, or a flex slot that declares no eligible position
(``flex_eligibility[slot] == []``, the shape a league whose real slot name is
outside ``ingest/build.py``'s known flex table produces). This is a broken
*league format*, not a broken league-week, so it is deliberately a distinct class
rather than an ``IngestError``: the message names the league id, the offending
slot list, and the two documents (``tools/anonymize.py``, ``CONTRIBUTING.md``)
that explain how to regenerate or contribute the league that exposed it. A
``CommishDeskError``, so the CLI's existing per-league
``except (CommishDeskError, OSError)`` already isolates it -- one-line stderr,
exit 1, and the next league in the run list still builds. The raising happens in
``commishdesk/stats/lineup.py``, not here.

``CrossCheckError``: the stage-2 standings module
(``commishdesk/stats/standings.py::cross_check_standings``) raises it when the
standings it folded from ``WeekModel.matchups`` disagree with the season-cumulative
totals Sleeper reports on ``Roster`` -- a win/loss/tie count that differs, or a
points-for total outside the half-cent-per-folded-week rounding tolerance. This is
the one place the engine's own numbers are checked against the platform's, so a
divergence means the fixture is a different slice of the season (or the fold is
wrong), not that the league is unusual: the message names every mismatched roster /
field / computed value / Sleeper value in one line and points to
``tools/anonymize.py`` and ``CONTRIBUTING.md``. ``mismatches`` carries the same
data typed, as a tuple of :class:`CrossCheckMismatch` in ``(roster, field)``
order. A ``CommishDeskError``, so the CLI's existing per-league
``except (CommishDeskError, OSError)`` already isolates it (one-line stderr,
exit 1, next league still builds). The raising happens in
``commishdesk/stats/standings.py``, not here.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "AdapterError",
    "CommishDeskError",
    "ConsensusError",
    "ContentSafetyError",
    "CostCeilingExceededError",
    "CrossCheckError",
    "CrossCheckMismatch",
    "DeliveryError",
    "IngestError",
    "NarratorError",
    "OptimalLineupError",
    "SchemaValidationError",
    "StoreError",
]


class CommishDeskError(Exception):
    """Base class for every expected, typed engine fault."""


class AdapterError(CommishDeskError):
    """A platform adapter could not fetch or parse a league's raw data."""


class ConsensusError(CommishDeskError):
    """No external consensus ranking could be fetched or parsed from any source."""


class ContentSafetyError(CommishDeskError):
    """The AD-12 Layer 3 tiered response held the whole Issue for content review.

    Raised by ``commishdesk/cli.py`` when ``narrate/response.py`` classifies a
    narration as un-shippable — a manager's name in the same sentence as a
    banned-category term or a personal-insult-lexicon hit, a section suppression
    that would drop **The Lead** or leave fewer than two sections, or a
    ``suppress`` finding that maps to no section at all. A distinct class (not
    ``NarratorError``) so an operator can tell a content review from an infra
    retry. Caught per league like any other fault (AD-9).
    """


class CostCeilingExceededError(CommishDeskError):
    """A pre-call cost estimate exceeded ``LLMConfig.cost_ceiling_usd``, or a
    model id has no ``narrate/pricing.py::MODEL_PRICES`` entry to price against.
    Raised before any paid call — fail closed, zero spend."""


@dataclass(frozen=True, slots=True)
class CrossCheckMismatch:
    """One roster's computed season total disagreeing with Sleeper's own.

    ``field`` is one of ``"wins"`` / ``"losses"`` / ``"ties"`` / ``"points_for"``;
    ``computed`` is what the matchup fold in ``stats/standings.py`` produced and
    ``sleeper`` is the value the fixture's ``Roster`` carries. ``computed`` and
    ``sleeper`` are ``int`` for the three counting fields and ``float`` for
    ``points_for``. Pure stdlib, so ``errors.py`` keeps its stdlib-only import
    contract."""

    roster_id: str
    field: str
    computed: int | float
    sleeper: int | float


class CrossCheckError(CommishDeskError):
    """The computed standings disagree with Sleeper's own season totals.

    Raised by ``commishdesk/stats/standings.py::cross_check_standings``. This is
    the one place the engine checks its own numbers against the platform's, so a
    divergence is a hold, never a wrong number shipped. ``mismatches`` is the
    typed list, in ``(roster, field)`` order; the message names each one and
    points to ``tools/anonymize.py`` and ``CONTRIBUTING.md``. Caught per league
    like any other ``CommishDeskError`` (AD-9).
    """

    def __init__(self, mismatches: tuple[CrossCheckMismatch, ...]) -> None:
        self.mismatches = mismatches
        details = "; ".join(
            f"roster {mismatch.roster_id} {mismatch.field}: "
            f"computed {mismatch.computed}, Sleeper {mismatch.sleeper}"
            for mismatch in mismatches
        )
        super().__init__(
            f"computed standings disagree with Sleeper's season totals ({details}). "
            "Regenerate the fixture's rosters with tools/anonymize.py, or see "
            "CONTRIBUTING.md for how to contribute the league that exposed it."
        )


class DeliveryError(CommishDeskError):
    """An Issue could not be posted to a delivery channel (wrapped at the call
    site in ``commishdesk/deliver/discord.py``; the webhook token is never in the
    message)."""


class IngestError(CommishDeskError):
    """A raw platform bundle could not be turned into a valid ``LeagueModel``."""


class NarratorError(CommishDeskError):
    """An LLM narrator provider adapter is unusable, or both providers failed."""


class OptimalLineupError(CommishDeskError):
    """A league's ``roster_slots`` cannot be solved into a legal lineup.

    Raised by ``commishdesk/stats/lineup.py::compute_weekly_lineups`` for a
    structurally unsolvable league format — an empty slot list, an empty slot
    name, or a flex slot with no eligible positions. The message names the league
    id, the slot list, ``tools/anonymize.py``, and ``CONTRIBUTING.md``. Caught
    per league like any other ``CommishDeskError`` (AD-9); never a bare
    ``KeyError``.
    """


class SchemaValidationError(CommishDeskError):
    """The Facts JSON builder produced a document the schema rejects."""


class StoreError(CommishDeskError):
    """A backing store could not be read or its contents could not be parsed."""
