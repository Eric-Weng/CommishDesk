"""Stage 4 — the AD-12 Layer 3 tiered failure response.

``narrate/safety.py`` (Layer 2) *finds* problems; this module *decides what to do*
about them. :func:`classify` turns a :class:`~commishdesk.narrate.safety.SafetyReport`
into a :class:`TieredResponse` — hold the whole Issue, regenerate the LLM
narration once, or suppress the offending section — and :func:`suppress_sections`
excises the offending sections from a template :class:`~commishdesk.narrate.template.Recap`.
It **decides; it does not act** — the CLI (``cli.py``) owns the orchestration: it
runs the selected narrator, applies the decision, permits exactly one
regeneration, and degrades to the template narrator whenever LLM prose cannot be
cleanly repaired.

Two helpers guard the LLM surface *before* the safety check runs:
:func:`sanitize_completion` strips ANSI/SGR escapes then NFKC-normalizes and drops
control characters (keeping newlines), and :func:`structural_ok` checks the
completion still carries all six canonical ``## `` section headings. A miss counts
as a failed generation → the template narrator.

Import fence (AD-4 / I4): this module imports **stdlib +
``commishdesk.narrate.safety`` + ``commishdesk.narrate.template`` +
``commishdesk.facts.schema`` only** — no provider SDK, no ``httpx``, no network,
no clock, no ``commishdesk.ingest``. It is re-exported *eagerly* from
``narrate/__init__.py`` (like ``safety.py``), so ``import commishdesk.narrate``
still pulls in no SDK.

Pure and deterministic: :func:`classify`, :func:`suppress_sections`,
:func:`structural_ok` and :func:`sanitize_completion` are functions of their
inputs with stable output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from commishdesk.narrate.safety import _normalize

if TYPE_CHECKING:
    from commishdesk.narrate.safety import SafetyReport
    from commishdesk.narrate.template import Recap

__all__ = [
    "SECTION_HEADINGS",
    "TieredResponse",
    "classify",
    "sanitize_completion",
    "structural_ok",
    "suppress_sections",
]

#: The six section headings the template narrator emits, in order — mirrors the
#: literals in ``narrate/template.py`` (``_lead_section`` … ``_december_section``).
#: :data:`SECTION_HEADINGS[0]` ("The Lead") is load-bearing: a recap must never
#: ship without it (:func:`suppress_sections`). ``tests/test_narrate_response.py``
#: pins this tuple to ``render_draft_recap``'s actual output so an edit to
#: ``template.py`` cannot silently drift it out of sync.
SECTION_HEADINGS: tuple[str, ...] = (
    "The Lead",
    "The Board — Round 1",
    "Superlatives",
    "Team Grades",
    "Positional Read",
    "The Picks We'll Be Arguing About in December",
)

#: A CSI / SGR escape ("\x1b[1;31m"), a two-char escape ("\x1bM"), or a bare ESC.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]|\x1b")

#: An ATX heading line with real text after the marker (``## Superlatives``).
_HEADING_LINE = re.compile(r"^[ \t]*#{1,6}[ \t]+(\S.*?)[ \t]*$", re.MULTILINE)


def sanitize_completion(text: str) -> str:
    """Strip ANSI/SGR escape sequences, then NFKC-normalize and drop ``Cc`` /
    ``Cf`` code points (keeping ``\\n``).

    The same normalization :func:`commishdesk.narrate.safety.check_narration`
    runs internally, applied here first so LLM output is validated and rendered
    in its cleaned form. Idempotent.
    """
    return _normalize(_ANSI_ESCAPE.sub("", text))


def _heading_key(heading: str) -> str:
    """Fold a heading for comparison: lowercase, ``—``/``–``/``-`` unified,
    whitespace collapsed."""
    folded = re.sub(r"[—–-]", "-", heading.lower())
    return re.sub(r"\s+", " ", folded).strip()


_HEADING_KEYS = frozenset(_heading_key(h) for h in SECTION_HEADINGS)


def structural_ok(text: str) -> bool:
    """True when *text* is non-blank and every one of the six canonical section
    headings appears on some ``## `` line (case-insensitive, ``—``/``-``
    tolerated).

    This is a **presence** check only — deliberately narrow (the frozen spec
    scoped it here). It does **not** check heading order, does **not** reject
    extra ``##`` sections the model may have added, and does **not** require any
    section to have a non-empty body. A per-section-emptiness gate is filed as
    deferred work. A ``False`` means the completion is not recap-shaped at all —
    the caller treats it as a failed generation and falls back to the template
    narrator rather than shipping a malformed dump.
    """
    if not text or not text.strip():
        return False
    found = {_heading_key(m.group(1)) for m in _HEADING_LINE.finditer(text)}
    return _HEADING_KEYS <= found


@dataclass(frozen=True)
class TieredResponse:
    """What :func:`classify` decided for one narration.

    ``hold`` wins over everything: when it is set, ``regenerate`` and ``suppress``
    are both ``False`` and ``hold_reasons`` names why. ``alerts`` is one
    operator-facing line per finding that drove the decision (the CLI logs each
    as a structured ``logger.error`` and echoes a one-line stderr message).
    """

    hold: bool
    hold_reasons: tuple[str, ...]
    regenerate: bool
    suppress: bool
    alerts: tuple[str, ...]


def _alert_line(category: str, severity: str, message: str) -> str:
    return f"{category}/{severity}: {message}"


def classify(report: SafetyReport, *, narrator_is_template: bool) -> TieredResponse:
    """Map a :class:`SafetyReport` to a :class:`TieredResponse` (AD-12 Layer 3).

    Keyed off each finding's ``severity`` (the ``safety.CATEGORY_SEVERITY`` tier
    map, stamped onto every finding at construction): ``hold_issue`` → hold the
    whole Issue; ``regenerate`` → one LLM regeneration then, if still unclean,
    degrade to the template; ``suppress_section`` → drop that section (or, on LLM
    output, degrade to the template).

    ``narrator_is_template=True`` ignores every ``hallucination`` finding: the
    template narrator emits only from the ``narration`` projection, so a
    closed-world miss against that same projection is a false positive by
    construction. ``safety.check_narration`` is not modified — the filtering
    lives here.
    """
    hold_reasons: list[str] = []
    alerts: list[str] = []
    regenerate = False
    suppress = False

    for finding in report.findings:
        if narrator_is_template and finding.category == "hallucination":
            continue
        alerts.append(_alert_line(finding.category, finding.severity, finding.message))
        if finding.severity == "hold_issue":
            hold_reasons.append(finding.message)
        elif finding.severity == "regenerate":
            regenerate = True
        elif finding.severity == "suppress_section":
            suppress = True

    hold = bool(hold_reasons)
    return TieredResponse(
        hold=hold,
        hold_reasons=tuple(hold_reasons),
        regenerate=regenerate and not hold,
        suppress=suppress and not hold,
        alerts=tuple(alerts),
    )


def suppress_sections(
    recap: Recap, report: SafetyReport
) -> tuple[Recap | None, tuple[str, ...]]:
    """Remove every template :class:`~commishdesk.narrate.template.Section` a
    ``suppress_section`` finding lands in.

    For each such finding, mark **every** section whose normalized blocks contain
    the finding's ``sentence`` (an identical offending sentence can appear in more
    than one section). Returns ``(trimmed_recap, removed_headings)``, or
    ``(None, removed_headings)`` when the surviving set would drop **The Lead** or
    leave fewer than two sections.

    ``removed_headings`` is **empty** when no section contained any offending
    sentence — the finding could not be localized. The caller must treat that as
    un-shippable (hold), not as "nothing to do": the flagged phrase is still
    somewhere in the recap (title / dateline / a sentence-split artefact).
    """
    remove: set[str] = set()
    for finding in report.findings:
        if finding.severity != "suppress_section":
            continue
        if not finding.sentence:
            continue
        for section in recap.sections:
            haystack = _normalize("\n".join(section.blocks))
            if finding.sentence in haystack:
                remove.add(section.heading)

    removed = tuple(s.heading for s in recap.sections if s.heading in remove)
    if not removed:
        return recap, ()

    kept = [s for s in recap.sections if s.heading not in remove]
    lead = SECTION_HEADINGS[0]
    if len(kept) < 2 or all(s.heading != lead for s in kept):
        return None, removed
    return recap.model_copy(update={"sections": kept}), removed
