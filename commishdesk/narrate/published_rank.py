"""Stage 4 — the published-rank deviation check (Story 5.12).

AD-13 assigns the *published* power rank to ``narrate/``: the model rank
(``stats/power.py::compute_power_ranks``) is the deterministic number, and a
narrator may publish its own editorial rank — but only within
:data:`~commishdesk.stats.power.POWER_NUDGE_CAP` positions of it, and only with
a cited justification for every deviation.

:func:`published_rank_findings` turns a narrator's published ranks into
``hallucination``-tier :class:`~commishdesk.narrate.safety.SafetyFinding`
records — the same tier ``narrate/response.py::classify`` already routes to one
regeneration and then the template narrator. Nothing here decides *what to do*;
the CLI owns the response, exactly as it does for the deterministic checks.

The *cited fact* half of the rule needs no code here at all: a nudge's
justification is prose in the Issue, and
:func:`~commishdesk.narrate.safety.check_narration`'s closed-world scan already
refutes any number in it the Facts payload does not contain. Only the *cap* is
new, because no token-level scan can tell that a published rank sits too far
from the model's.

This lives beside ``narrate/response.py`` rather than inside it so that module's
documented import fence (stdlib + ``safety`` + ``template`` +
``facts.schema``) stays exactly as it is; this one module additionally reads
``commishdesk.stats.power``, which the CLI imports lazily.

Import fence: stdlib + ``commishdesk.facts.schema`` +
``commishdesk.narrate.safety`` + ``commishdesk.stats.power`` — no provider SDK,
no ``httpx``, no network, no clock.

Pure and deterministic: the same ``(narration, published_ranks)`` yields a
byte-identical tuple, in ``narration.power`` order.
"""

from __future__ import annotations

from collections.abc import Mapping

from commishdesk.facts.schema import WeeklyNarration
from commishdesk.narrate.safety import CATEGORY_SEVERITY, SafetyFinding
from commishdesk.stats.power import POWER_NUDGE_CAP

__all__ = ["published_rank_findings"]


def published_rank_findings(
    narration: WeeklyNarration, published_ranks: Mapping[str, int]
) -> tuple[SafetyFinding, ...]:
    """One ``hallucination``-tier finding per roster whose published rank sits
    more than :data:`~commishdesk.stats.power.POWER_NUDGE_CAP` positions from
    its model rank.

    ``published_ranks`` maps ``roster_id`` -> the rank the narrator published.
    A roster the narrator did not rank, or one the model itself cannot rank
    (a cold start), is skipped — there is nothing to compare, and "unranked" is
    not a deviation.

    The finding carries an empty ``sentence`` on purpose: nothing in the Issue
    text is the *location* of the fault, so the caller must not try to localize
    and excise it. ``classify`` reads ``category``/``severity``/``message`` only,
    so the deviation still routes to the regenerate tier and, failing that, the
    template narrator.
    """
    findings: list[SafetyFinding] = []
    for row in narration.power:
        published = published_ranks.get(row.roster_id)
        if published is None or row.model_rank is None:
            continue
        deviation = abs(published - row.model_rank)
        if deviation <= POWER_NUDGE_CAP:
            continue
        findings.append(
            SafetyFinding(
                category="hallucination",
                severity=CATEGORY_SEVERITY["hallucination"],
                message=(
                    f"published rank {published} for {row.team!r} is {deviation} "
                    f"positions from the model rank {row.model_rank} "
                    f"(cap {POWER_NUDGE_CAP})"
                ),
                sentence="",
                matched=str(published),
            )
        )
    return tuple(findings)
