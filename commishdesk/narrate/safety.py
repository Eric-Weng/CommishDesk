"""Stage 4 — deterministic content-safety checks over a narrator's output (AD-12 L2).

``check_narration(text, narration, *, voice=None)`` runs a fixed, credential-free
pipeline over the prose *any* narrator produced — the template narrator, the LLM
narrator, or the demo path alike — and returns a :class:`SafetyReport` of
:class:`SafetyFinding`\\ s. It **returns findings; it does not act** —
``narrate/response.py`` (AD-12 Layer 3) turns the report into a graded decision
(suppress the offending section / one LLM regeneration / hold the whole Issue)
and ``cli.py`` applies it alongside narrator *selection*.

Import fence (AD-4 / I4): this module imports **stdlib +
``commishdesk.facts.schema`` + ``commishdesk.voices`` + ``commishdesk.errors``
only** — no provider SDK, no ``httpx``, no network, no clock, no
``commishdesk.ingest``. It is re-exported *eagerly* from ``narrate/__init__.py``
(like ``template.py``), so ``import commishdesk.narrate`` still pulls in no SDK.

Pure and deterministic: the same ``(text, narration, voice)`` yields a
byte-identical report with a stable ``findings`` order — findings are emitted in
the fixed check order below, and within a check in sentence order then match
order, de-duplicated.

Fixed order (AD-12 Layer 2):

1. NFKC-normalize the text and strip ``Cc`` / ``Cf`` code points (keep newlines).
2. **Named-person proximity** — a sentence carrying a manager's name (from the
   ``narration`` projection) *and* a banned-category term *or* a personal-insult
   hit → ``named_person_proximity`` / ``hold_issue``.
3. **Banned-topic patterns** — the same term with no manager name in that
   sentence → the lesser ``banned_topic`` tier.
4. **Closed-world** — every proper-noun / numeric token in the output must appear
   casefolded as a substring of ``narration.model_dump_json()`` (minus a curated
   stop-set). A miss → ``hallucination`` / ``regenerate``. This is the
   ``tests/test_voices.py::_score_recap`` heuristic ported into the package — a
   gate against gross hallucination, not a proof of accuracy.
5. **Slop / tone** — a cliché from the ``slop`` list → ``slop``.

Pattern lists live in ``commishdesk/narrate/safety_lists.toml`` (shipped as
package data, loaded via :mod:`importlib.resources` + :mod:`tomllib`) so they are
editable without a code change; a load/compile failure raises
:class:`~commishdesk.errors.NarratorError`. A ``Voice``'s ``banned_topics`` prose
merges in on top as extracted keyword patterns under a ``voice:<voice_id>``
category.
"""

from __future__ import annotations

import functools
import re
import tomllib
import unicodedata
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from commishdesk.errors import NarratorError

if TYPE_CHECKING:
    from commishdesk.facts.schema import Narration
    from commishdesk.voices import Voice

__all__ = [
    "CATEGORY_SEVERITY",
    "SafetyCategory",
    "SafetyFinding",
    "SafetyReport",
    "SafetySeverity",
    "check_narration",
]

SafetyCategory = Literal[
    "named_person_proximity", "banned_topic", "hallucination", "slop"
]
SafetySeverity = Literal["hold_issue", "regenerate", "suppress_section"]

#: The AD-12 tier map: each category's severity, stamped onto every
#: :class:`SafetyFinding` at construction. ``narrate/response.py`` reads
#: ``finding.severity`` to grade its response — ``hold_issue`` holds the whole
#: Issue, ``regenerate`` earns one LLM regeneration, ``suppress_section`` drops
#: the offending section (or degrades LLM prose to the template).
CATEGORY_SEVERITY: dict[SafetyCategory, SafetySeverity] = {
    "named_person_proximity": "hold_issue",
    "banned_topic": "suppress_section",
    "hallucination": "regenerate",
    "slop": "suppress_section",
}

_LISTS_FILENAME = "safety_lists.toml"


# --------------------------------------------------------------------------- #
# Result models
# --------------------------------------------------------------------------- #


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SafetyFinding(_Frozen):
    """One issue the check raised, with the sentence and the matched text."""

    category: SafetyCategory
    severity: SafetySeverity
    message: str
    sentence: str
    matched: str


class SafetyReport(_Frozen):
    """The verdict on one narration: an ordered, de-duplicated finding tuple."""

    findings: tuple[SafetyFinding, ...] = ()

    @property
    def ok(self) -> bool:
        """No finding of any kind."""
        return not self.findings

    @property
    def held(self) -> bool:
        """At least one ``hold_issue`` finding. Convenience predicate only —
        ``narrate/response.py``'s :func:`classify` is the authority on what a
        report means (it can, for one, ignore a template narrator's
        false-positive ``hallucination``)."""
        return any(f.severity == "hold_issue" for f in self.findings)


# --------------------------------------------------------------------------- #
# (1) normalize
# --------------------------------------------------------------------------- #


def _normalize(text: str) -> str:
    """NFKC-normalize, then drop every ``Cc`` / ``Cf`` code point except ``\\n``.

    This runs first so a zero-width / soft-hyphen evasion ("M\\u200ba\\u200brcus")
    collapses to plain text before any name or pattern match.

    Deletion, not substitution: a zero-width character *between* two separate
    words therefore joins them ("drafted\\u200bhungover" -> "draftedhungover"),
    and homoglyph / confusable substitution ("М" U+041C for "M") is not handled
    at all — NFKC does not fold those. This check is a deterministic floor; the
    Epic-7 Layer-4 classifier is the answer to adversarial evasion, not this
    normalizer.
    """
    nfkc = unicodedata.normalize("NFKC", text)
    return "".join(
        ch
        for ch in nfkc
        if ch == "\n" or unicodedata.category(ch) not in ("Cc", "Cf")
    )


#: Split on sentence punctuation followed by whitespace *or* an uppercase letter
#: (so a missing space, "hungover.He", still splits), plus any newline run. Biased
#: toward over-splitting: a false split only downgrades a would-be proximity hold
#: to two lesser findings, while a false merge fabricates a whole-league hold.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])(?=\s|[A-Z])|\n+")


def _sentences(text: str) -> tuple[str, ...]:
    """Split normalized text into non-empty, stripped sentence-ish spans."""
    return tuple(p.strip() for p in _SENTENCE_SPLIT.split(text) if p and p.strip())


# --------------------------------------------------------------------------- #
# (2) manager names from the narration projection
# --------------------------------------------------------------------------- #


def _manager_names(narration: Narration) -> frozenset[str]:
    """Every manager name reachable in the ``narration`` projection — the
    ``manager`` field wherever it appears, plus the ``left_waiting`` name lists.
    Single-character names are dropped; two-character handles ("AJ", "TJ", "Bo")
    are kept — word-boundary matching (see :func:`_compile_name_patterns`) makes
    them safe."""
    names: set[str] = set()

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key == "manager" and isinstance(value, str) and value.strip():
                    names.add(value.strip())
                elif key == "left_waiting" and isinstance(value, list):
                    names.update(
                        item.strip()
                        for item in value
                        if isinstance(item, str) and item.strip()
                    )
                else:
                    walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(narration.model_dump())
    return frozenset(name for name in names if len(name) >= 2)


def _compile_name_patterns(names: frozenset[str]) -> tuple[re.Pattern[str], ...]:
    """One ``\\b``-anchored, case-insensitive pattern per manager name — compiled
    once per :func:`check_narration` call. Word-boundary, never bare substring:
    "Sam" must not fire inside "same", "Ben" inside "benched", "Ross" inside
    "across"."""
    return tuple(
        re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE) for name in sorted(names)
    )


def _sentence_has_name(sentence: str, name_patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(sentence) for pattern in name_patterns)


# --------------------------------------------------------------------------- #
# lists — TOML package data, loaded + compiled once
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Lists:
    banned_topics: dict[str, tuple[re.Pattern[str], ...]]
    personal_insults: tuple[re.Pattern[str], ...]
    slop: tuple[re.Pattern[str], ...]


def _compile_all(patterns: object, where: str) -> tuple[re.Pattern[str], ...]:
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        raise NarratorError(f"{_LISTS_FILENAME}: {where} must be a list of strings")
    try:
        return tuple(re.compile(p, re.IGNORECASE) for p in patterns)
    except re.error as exc:
        raise NarratorError(
            f"{_LISTS_FILENAME}: {where} has an uncompilable pattern ({exc})"
        ) from exc


def _parse_lists(text: str) -> _Lists:
    """Parse + compile the raw TOML. Any failure → :class:`NarratorError`."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise NarratorError(f"{_LISTS_FILENAME} is not valid TOML ({exc})") from exc

    raw_banned = data.get("banned_topics", {})
    if not isinstance(raw_banned, dict):
        raise NarratorError(f"{_LISTS_FILENAME}: [banned_topics] must be a table")
    banned = {
        str(category): _compile_all(patterns, f"banned_topics.{category}")
        for category, patterns in raw_banned.items()
    }
    return _Lists(
        banned_topics=banned,
        personal_insults=_compile_all(
            data.get("personal_insults", []), "personal_insults"
        ),
        slop=_compile_all(data.get("slop", []), "slop"),
    )


def _lists_toml_text() -> str:
    return (
        resources.files("commishdesk.narrate")
        .joinpath(_LISTS_FILENAME)
        .read_text(encoding="utf-8")
    )


@functools.lru_cache(maxsize=1)
def _load_lists() -> _Lists:
    """Load + compile the packaged list file. A missing / non-UTF-8 file surfaces
    as :class:`NarratorError` (chained) rather than an ``OSError`` /
    ``UnicodeDecodeError`` that would escape the CLI's per-league catch and abort
    the whole batch (AD-9)."""
    try:
        text = _lists_toml_text()
    except (OSError, UnicodeDecodeError, ModuleNotFoundError) as exc:
        raise NarratorError(
            f"{_LISTS_FILENAME} could not be read ({type(exc).__name__})"
        ) from exc
    return _parse_lists(text)


# --------------------------------------------------------------------------- #
# voice keyword merge
# --------------------------------------------------------------------------- #

_VOICE_WORD = re.compile(r"[a-z]+")

#: Words never worth turning into a `\bword\b` pattern from a Voice's prose —
#: generic fantasy-football / sentence scaffolding that would fire on ordinary
#: recap copy. A voice-keyword hit can *only* ever produce a ``banned_topic``
#: (warn) finding — never a ``hold_issue``, even beside a manager name (see
#: :func:`check_narration` and the spec's Design Notes) — so this set only needs
#: to keep warn-tier noise (and the demo recap) down, not to be exhaustive.
_VOICE_STOPWORDS: frozenset[str] = frozenset(
    {
        "your", "their", "them", "they", "this", "that", "these", "those",
        "with", "from", "about", "over", "into", "than", "then", "when",
        "where", "have", "been", "being", "other", "around", "during",
        "real", "life",
        "player", "players", "manager", "managers", "roster", "rosters",
        "team", "teams", "draft", "pick", "picks", "league", "season",
        "field", "line", "lines", "history", "status", "advice",
    }
)


def _voice_patterns(voice: Voice | None) -> dict[str, tuple[re.Pattern[str], ...]]:
    """Extract keyword patterns from ``voice.banned_topics`` prose: lowercase each
    phrase, keep word tokens of length ≥ 4 that are not in the stop-set, and
    compile each as ``\\bword\\b`` under a synthetic ``voice:<voice_id>`` category.
    Empty ``frozenset`` / ``voice=None`` → ``{}`` (base lists only).

    :func:`check_narration` treats a ``voice:`` category as warn-tier only — it
    never contributes a ``hold_issue``, even beside a manager name."""
    if voice is None:
        return {}
    topics: frozenset[str] = voice.banned_topics
    if not topics:
        return {}

    words: list[str] = []
    seen: set[str] = set()
    for phrase in sorted(topics):
        for token in _VOICE_WORD.findall(phrase.lower()):
            if len(token) >= 4 and token not in _VOICE_STOPWORDS and token not in seen:
                seen.add(token)
                words.append(token)
    if not words:
        return {}

    category = f"voice:{voice.voice_id or 'voice'}"
    return {
        category: tuple(
            re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE) for word in words
        )
    }


# --------------------------------------------------------------------------- #
# (4) closed-world — ported from tests/test_voices.py::_score_recap
# --------------------------------------------------------------------------- #

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.’'%$+/-]*")

#: A letter-grade token (``A`` / ``B+`` / ``D-``). Checked as a substring of the
#: payload *before* the generic strip runs — otherwise ``A+`` / ``B+`` collapse
#: to ``a`` / ``b`` and a hallucinated grade sails through.
_GRADE = re.compile(r"^[A-F][+-]?$")

#: A trailing possessive ``'s`` / ``’s`` — dropped before the closed-world
#: lookup so the template narrator's own "Blitz Alpacas's swing" is not read as
#: an out-of-world token (Story 3.4 calibration).
_POSSESSIVE = re.compile(r"['’]s$")

#: A trailing ordinal suffix on a number ("11th" -> "11", "3rd" -> "3") — dropped
#: before the payload lookup so an ordinal phrasing of a real number is not read
#: as a hallucinated one.
_ORDINAL = re.compile(r"(?<=\d)(st|nd|rd|th)$", re.IGNORECASE)

#: Scaffolding / section-heading words that are capitalised or numeric in prose
#: but are not proper nouns or facts to trace. Ported verbatim from
#: ``tests/test_voices.py::_score_recap`` and then extended (clearly marked) with
#: the capitalised scaffolding the *template* narrator emits in its own prose.
_STOP: frozenset[str] = frozenset(
    {
        # articles / conjunctions / prepositions / pronouns / demonstratives
        "a", "an", "and", "or", "but", "nor", "so", "yet", "for", "of", "in", "on",
        "at", "to", "by", "as", "with", "from", "into", "than", "then", "that",
        "this", "these", "those", "there", "their", "them", "they", "it", "its",
        "he", "his", "him", "she", "her", "we", "we'll", "us", "our", "you",
        "your", "i", "if", "is", "was", "were", "be", "been", "being",
        "not", "no",
        # common sentence-openers / adverbs
        "the", "now", "here", "how", "when", "where", "what", "who", "whom",
        "which", "while", "after", "before", "once", "still", "also", "even",
        "just", "only", "both", "each", "every", "some", "any", "all", "most",
        "more", "less", "nobody", "everyone", "someone", "nothing", "everything",
        "because", "since", "until", "about", "over", "under", "between", "whether",
        # capitalised sentence-opener adverbs / conjuncts (Story 3.4)
        "meanwhile", "however", "granted", "remarkably", "elsewhere", "instead",
        "overall", "ultimately", "regardless", "admittedly", "notably",
        "curiously", "predictably", "otherwise", "besides", "conversely",
        "importantly", "frankly", "honestly", "arguably", "presumably",
        # section-heading / scaffolding words
        "round", "board", "lead", "superlatives", "grades", "grade", "team",
        "teams", "positional", "read", "picks", "pick", "december", "arguing",
        "best", "reach", "value", "swing", "swings", "boldest", "biggest",
        "runner-up", "consensus", "draft", "recap", "season", "waiting",
        "early", "window", "later", "run", "runs",
        # position nouns not spelled out in the narration projection
        "quarterback", "quarterbacks", "receiver", "receivers", "wideout",
        "wideouts", "tight", "flex", "kicker", "defense",
        # spelled integers used as plain scaffolding
        "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
        "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
        "seventeen", "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty",
        "hundred",
        # --- Story 3.4: capitalised scaffolding in the template narrator's prose
        "running", "back", "backs", "end", "ends",
    }
)


#: The bare single letters a grade-scale description names ("the scale runs A to
#: F") — always in-world regardless of who was graded. A *suffixed* grade
#: ("F-", "D+") must still be one actually awarded.
_GRADE_SCALE_LETTERS = frozenset("ABCDF")


def _closed_world(text: str, narration: Narration) -> list[str]:
    """Every capitalised or numeric token in *text* (minus the stop-set) that
    does not appear casefolded in ``narration.model_dump_json()``, order-stable
    and de-duplicated.

    A ``_GRADE``-shaped token is checked against the grades actually awarded
    (plus the bare scale letters), not as a JSON substring. A token is split on
    internal ``-`` / ``/`` and stripped of an ordinal suffix before the lookup so
    "12-team", "4.5/PPR" and "11th" do not flag."""
    haystack = narration.model_dump_json().lower()
    awarded = {team.grade for team in narration.teams}
    unknown: list[str] = []
    for raw in _TOKEN.findall(text):
        trimmed = raw.strip(".,;:!?()[]\"'’")
        if _GRADE.match(trimmed):
            if trimmed not in awarded and not (
                len(trimmed) == 1 and trimmed in _GRADE_SCALE_LETTERS
            ):
                unknown.append(trimmed)
            continue
        base = _POSSESSIVE.sub("", trimmed.strip("%$+/-"))
        for part in re.split(r"[-/]", base):
            token = _ORDINAL.sub("", part).strip("%$+/-")
            if not token:
                continue
            if not (token[:1].isupper() or any(ch.isdigit() for ch in token)):
                continue
            folded = token.lower()
            if folded in _STOP or folded in haystack:
                continue
            unknown.append(token)
    return list(dict.fromkeys(unknown))


# --------------------------------------------------------------------------- #
# the check
# --------------------------------------------------------------------------- #


def _containing_sentence(sentences: tuple[str, ...], needle: str) -> str:
    for sentence in sentences:
        if needle in sentence:
            return sentence
    return ""


def check_narration(
    text: str, narration: Narration, *, voice: Voice | None = None
) -> SafetyReport:
    """Run the five checks in fixed order and return an ordered
    :class:`SafetyReport`. Pure, deterministic, credential-free.

    Raises :class:`~commishdesk.errors.NarratorError` if
    ``safety_lists.toml`` cannot be loaded or a pattern will not compile.
    """
    lists = _load_lists()
    normalized = _normalize(text)
    sentences = _sentences(normalized)
    name_patterns = _compile_name_patterns(_manager_names(narration))

    # base categories first (TOML order), then the synthetic voice category
    categories: dict[str, tuple[re.Pattern[str], ...]] = {
        **lists.banned_topics,
        **_voice_patterns(voice),
    }

    # per-sentence hit data, gathered once
    rows: list[tuple[str, bool, list[tuple[str, str]], list[str]]] = []
    for sentence in sentences:
        cat_hits: list[tuple[str, str]] = []
        for category, patterns in categories.items():
            for pattern in patterns:
                match = pattern.search(sentence)
                if match:
                    cat_hits.append((category, match.group(0)))
        insult_hits = [
            match.group(0)
            for pattern in lists.personal_insults
            if (match := pattern.search(sentence))
        ]
        rows.append(
            (sentence, _sentence_has_name(sentence, name_patterns), cat_hits, insult_hits)
        )

    findings: list[SafetyFinding] = []
    seen: set[tuple[str, str, str, str]] = set()

    def add(category: SafetyCategory, message: str, sentence: str, matched: str) -> None:
        key = (category, sentence, matched, message)
        if key in seen:
            return
        seen.add(key)
        findings.append(
            SafetyFinding(
                category=category,
                severity=CATEGORY_SEVERITY[category],
                message=message,
                sentence=sentence,
                matched=matched,
            )
        )

    # (2) named-person proximity — every hold, in sentence order. A voice-derived
    # keyword category (``voice:<id>``) is skipped here: crude keyword extraction
    # over free-text prose is too false-positive-prone to silently hold a whole
    # league, so it only ever reaches the warn tier in step (3).
    for sentence, has_name, cat_hits, insult_hits in rows:
        if not has_name:
            continue
        for category, matched in cat_hits:
            if category.startswith("voice:"):
                continue
            add(
                "named_person_proximity",
                f"a manager's name and {category} content in one sentence: {matched!r}",
                sentence,
                matched,
            )
        for matched in insult_hits:
            add(
                "named_person_proximity",
                f"a manager's name and a personal insult in one sentence: {matched!r}",
                sentence,
                matched,
            )

    # (3) banned-topic patterns — the lesser (warn) tier. Every hit in a sentence
    # with no manager name lands here; in a *named* sentence, only the
    # voice-derived categories do (the curated categories already produced a hold
    # in step 2 and must not double-report).
    for sentence, has_name, cat_hits, insult_hits in rows:
        for category, matched in cat_hits:
            if has_name and not category.startswith("voice:"):
                continue
            add("banned_topic", f"{category} content: {matched!r}", sentence, matched)
        if not has_name:
            for matched in insult_hits:
                add(
                    "banned_topic",
                    f"possible personal insult: {matched!r}",
                    sentence,
                    matched,
                )

    # (4) closed-world
    for token in _closed_world(normalized, narration):
        add(
            "hallucination",
            f"token not found in the facts payload: {token!r}",
            _containing_sentence(sentences, token),
            token,
        )

    # (5) slop / tone
    for sentence in sentences:
        for pattern in lists.slop:
            match = pattern.search(sentence)
            if match:
                add("slop", f"slop phrase: {match.group(0)!r}", sentence, match.group(0))

    return SafetyReport(findings=tuple(findings))
