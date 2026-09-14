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
    "MAX_REPAIR_SENTENCES",
    "MIN_REPAIR_RETENTION",
    "SECTION_HEADINGS",
    "TieredResponse",
    "classify",
    "excise_offending_sentences",
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


def restore_lead_heading(text: str) -> str:
    """Insert a missing ``## The Lead`` heading when it is the only one of the six
    absent, the completion opens with a title, and prose sits between that title
    and the first section heading. Measured: 2 of 10 live narrations wrote the
    whole opening but skipped this one heading, and the Issue fell back to the
    template. No model call, and not a word of the prose changes. A completion
    with no title is left alone: the renderer would take an inserted heading as
    the title."""
    lead = SECTION_HEADINGS[0]
    found = {_heading_key(m.group(1)) for m in _HEADING_LINE.finditer(text)}
    if _HEADING_KEYS - found != {_heading_key(lead)}:
        return text
    lines = text.split("\n")
    first_section = next(
        index
        for index, line in enumerate(lines)
        if (match := _HEADING_LINE.match(line)) is not None and _heading_key(match.group(1)) in _HEADING_KEYS
    )
    above = [index for index in range(first_section) if lines[index].strip()]
    if len(above) < 2:
        return text
    title = lines[above[0]].strip()
    if not (_HEADING_LINE.match(title) or (len(title) <= 90 and not title.endswith((".", "!", "?")))):
        return text
    return "\n".join([*lines[: above[1]], f"## {lead}", "", *lines[above[1] :]])


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


def suppress_sections(recap: Recap, report: SafetyReport) -> tuple[Recap | None, tuple[str, ...]]:
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


#: The most offending sentences one repair pass may excise. Past this, the
#: generation is not carrying a few bad spans, it is broadly wrong — and a
#: regeneration (or the template) is the honest answer. Three is deliberately
#: tight: the live P0.1 validation run never saw more than two in one Issue.
MAX_REPAIR_SENTENCES = 3

#: The repaired text must keep at least this share of the original characters.
#: A repair that guts an Issue is not a repair; the length band the voice prompt
#: sets (±15%) is what ships, so a 10% floor leaves room without letting an
#: excision quietly produce a stub.
MIN_REPAIR_RETENTION = 0.90

#: A list-bullet line whose sentence has been excised, leaving only the marker.
_EMPTY_BULLET = re.compile(r"^[ \t]*(?:[*+\-•]|\d+[.)])[ \t]*$")


def _tidy_after_excision(text: str) -> str:
    """Collapse the whitespace an excision leaves behind: doubled spaces inside a
    line, bullet lines whose content is gone, and runs of blank lines."""
    kept: list[str] = []
    for line in text.split("\n"):
        collapsed = re.sub(r"[ \t]{2,}", " ", line).rstrip()
        if _EMPTY_BULLET.match(collapsed):
            continue
        kept.append(collapsed)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip() + "\n"


def excise_offending_sentences(text: str, report: SafetyReport) -> tuple[str | None, tuple[str, ...]]:
    """Remove every sentence a *repairable* finding landed in, or refuse.

    Returns ``(repaired_text, excised_sentences)``, or ``(None, attempted)`` when
    the repair must not be taken and the caller should escalate (regenerate, then
    the template).

    **Why this exists.** Before it, a single ``regenerate``-tier finding bought a
    whole second generation, and a ``suppress_section`` finding discarded the
    narration outright. The P0.1 live validation measured what that costs: 7
    findings across 40 generations, 6 of them ``regenerate``-tier, **~13 paid
    calls spent, 0 real hallucinations caught**. Worse, regeneration is
    *provably* ineffective for the dominant class — the same derived-arithmetic
    span ("6.07") recurred in three independent generations, so re-rolling the
    identical prompt mostly re-earns the identical finding.

    Excision costs **nothing** and is correct in both directions, which is the
    property that matters when the true/false split cannot be known in advance:
    on a false positive it drops one good sentence (a small quality cost); on a
    true positive it removes the actual problem. That asymmetry is what lets the
    detection side be *broad* — see ``real_world_references`` in
    ``safety_lists.toml``, which is only affordable because a hit is repaired
    rather than regenerated.

    Refuses — returns ``None`` — when:

    * any finding is ``hold_issue``. A hold is a hold; repair never launders one.
    * a finding cannot be localized (empty ``sentence``, or one that is not a
      substring of the normalized text). The offending phrase is still in there
      somewhere, so "removed nothing" must not be mistaken for "fixed".
    * more than :data:`MAX_REPAIR_SENTENCES` distinct sentences are implicated.
    * the result drops below :data:`MIN_REPAIR_RETENTION` of the original length,
      or stops satisfying :func:`structural_ok`.

    The caller must re-run ``check_narration`` on the repaired text and require a
    clean report before shipping it — this function does not re-validate, and a
    ``slop``/``banned_topic`` pattern can span a sentence boundary.
    """
    normalized = _normalize(text)
    targets: list[str] = []
    for finding in report.findings:
        if finding.severity == "hold_issue":
            return None, ()
        if finding.severity not in ("regenerate", "suppress_section"):
            continue
        if not finding.sentence or finding.sentence not in normalized:
            return None, tuple(targets)
        if finding.sentence not in targets:
            targets.append(finding.sentence)

    if not targets:
        return normalized, ()
    if len(targets) > MAX_REPAIR_SENTENCES:
        return None, tuple(targets)

    repaired = normalized
    for sentence in targets:
        repaired = repaired.replace(sentence, "", 1)
    repaired = _tidy_after_excision(repaired)

    if len(repaired) < MIN_REPAIR_RETENTION * len(normalized):
        return None, tuple(targets)
    if not structural_ok(repaired):
        return None, tuple(targets)
    return repaired, tuple(targets)
